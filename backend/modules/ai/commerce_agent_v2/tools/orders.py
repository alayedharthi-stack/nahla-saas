"""Trusted customer-scoped, read-only order and shipment tools."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlparse

from agents import RunContextWrapper, function_tool

from core.local_order_resolver import (
    _order_matches_phone,
    _phone_lookup_keys,
    _snapshot_from_order,
    local_order_to_track_payload,
    resolve_customer_order_context,
)
from core.order_shipment_service import (
    get_order_shipment as get_persisted_order_shipment,
)
from core.order_status_label import order_status_label_ar
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    EvidenceRecord,
    OrderDetailsResult,
    OrderDetailsSnapshot,
    OrderLineItemSnapshot,
    OrderResolveResult,
    OrderShipmentResult,
    OrderShipmentSnapshot,
    OrderSummarySnapshot,
)
from modules.ai.security.tenant_isolation import (
    TenantIsolationLayer,
    TenantIsolationViolation,
)
from modules.ai.commerce_agent_v2.tools.catalog import _catalog_search_enabled


_SHIPMENT_ORDER_STATUSES = frozenset(
    {
        "shipment_created",
        "label_generated",
        "shipped",
        "in_transit",
        "out_for_delivery",
        "delivering",
        "delivered",
    }
)
_PLACEHOLDER_CARRIERS = frozenset({"", "internal", "placeholder", "unknown"})


def _canonical_money(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if "," in raw and "." not in raw:
        whole, fraction = raw.rsplit(",", 1)
        raw = f"{whole}.{fraction}" if len(fraction) <= 2 else raw.replace(",", "")
    else:
        raw = raw.replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _canonical_quantity(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return None
    return quantity if quantity >= 0 else None


def _absolute_http_url(value: Any) -> str | None:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _order_metadata(order: Any) -> dict[str, Any]:
    raw = getattr(order, "extra_metadata", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _metadata_shipment_facts(order: Any) -> dict[str, str]:
    """Conservative fallback for adapter-synced shipment facts on ``orders``."""
    meta = _order_metadata(order)
    shipping = meta.get("shipping") if isinstance(meta.get("shipping"), dict) else {}
    company = shipping.get("company") if isinstance(shipping.get("company"), dict) else {}
    carrier = str(
        meta.get("shipping_company")
        or meta.get("shipping_method")
        or company.get("name")
        or shipping.get("company_name")
        or ""
    ).strip()
    tracking_url = _absolute_http_url(
        meta.get("tracking_url")
        or meta.get("tracking_link")
        or shipping.get("tracking_url")
        or shipping.get("tracking_link")
    )
    return {
        "carrier": carrier,
        "tracking_number": str(
            meta.get("tracking_number") or shipping.get("tracking_number") or ""
        ).strip(),
        "tracking_url": tracking_url or "",
    }


def _load_tenant_order(context: CommerceAgentContext, order_id: int) -> Any:
    from models import Order

    row = (
        context.db.query(Order)
        .filter(
            Order.id == int(order_id),
            Order.tenant_id == context.tenant_id,
        )
        .one_or_none()
    )
    if row is not None:
        TenantIsolationLayer.assert_belongs(row, context.tenant_context)
    return row


def _is_current_conversation_draft(context: CommerceAgentContext, order: Any) -> bool:
    from services.nahla_order_bridge import nahla_wa_external_id

    if str(getattr(order, "source", "") or "").strip().lower() != "whatsapp":
        return False
    prefix = nahla_wa_external_id(context.tenant_id, context.conversation_id)
    return str(getattr(order, "external_id", "") or "").startswith(prefix)


def _assert_discovered_order_is_customer_scoped(
    context: CommerceAgentContext,
    order: Any,
) -> None:
    """Strengthen the legacy resolver's OR identity match before authorization."""
    TenantIsolationLayer.assert_belongs(order, context.tenant_context)
    linked_customer_id = getattr(order, "customer_id", None)
    if context.customer_id is not None and linked_customer_id is not None:
        if int(linked_customer_id) != int(context.customer_id):
            raise TenantIsolationViolation("order_not_in_trusted_customer_scope")
        return
    if _order_matches_phone(
        order,
        _phone_lookup_keys(context.normalized_customer_phone),
    ):
        return
    if linked_customer_id is None and _is_current_conversation_draft(context, order):
        return
    raise TenantIsolationViolation("order_not_in_trusted_customer_scope")


def _load_authorized_order(context: CommerceAgentContext, order_id: int) -> Any:
    context.assert_scope()
    context.require_authorized_order(order_id)
    row = _load_tenant_order(context, order_id)
    if row is None:
        return None
    _assert_discovered_order_is_customer_scoped(context, row)
    return row


def _summary_evidence(order: Any) -> tuple[OrderSummarySnapshot, EvidenceRecord]:
    snapshot = _snapshot_from_order(order)
    order_id = snapshot.order_id
    reference = snapshot.display_reference or None
    status = str(snapshot.status or "").strip()
    status_label = order_status_label_ar(status, source=snapshot.source)
    evidence_ref = f"order:summary:{order_id}"
    facts: list[CanonicalEvidenceFact] = []
    if reference:
        facts.append(
            CanonicalEvidenceFact(
                kind="order_reference",
                value=reference,
                subject_order_id=order_id,
            )
        )
    if status:
        facts.append(
            CanonicalEvidenceFact(
                kind="order_status",
                value=status,
                subject_order_id=order_id,
            )
        )
    facts.append(
        CanonicalEvidenceFact(
            kind="order_status_label",
            value=status_label,
            subject_order_id=order_id,
        )
    )
    evidence = EvidenceRecord(
        ref=evidence_ref,
        source="order_summary",
        source_id=str(order_id),
        facts=facts,
        fields={
            "order_id": order_id,
            "order_reference": reference,
            "status": status,
            "status_label": status_label,
        },
        provenance={
            "service": "core.local_order_resolver.resolve_customer_order_context",
            "record": "orders",
            "source": str(snapshot.source or "local"),
        },
    )
    return (
        OrderSummarySnapshot(
            order_id=order_id,
            order_reference=reference,
            status=status,
            status_label=status_label,
            evidence_ref=evidence_ref,
        ),
        evidence,
    )


@function_tool(timeout=8.0, is_enabled=_catalog_search_enabled)
async def resolve_customer_order(
    run_context: RunContextWrapper[CommerceAgentContext],
    order_number: str = "",
    purpose: Literal["status", "shipment"] = "status",
) -> OrderResolveResult:
    """Resolve one order inside the trusted current tenant and customer scope.

    ``order_number`` is only an optional lookup key; customer and tenant
    identity always come from trusted context. Use purpose ``shipment`` for a
    shipment/tracking question and ``status`` for other order questions.
    """
    context = run_context.context
    context.assert_scope()
    if not context.capabilities.read_orders:
        return OrderResolveResult(status="denied", failure_reason="order_reads_disabled")
    requested_number = str(order_number or "").strip()
    resolved = resolve_customer_order_context(
        context.db,
        tenant_id=context.tenant_id,
        conversation_id=context.conversation_id,
        customer_id=context.customer_id,
        phone=context.normalized_customer_phone,
        intent="track_order" if purpose == "shipment" else None,
        order_number=requested_number or None,
    )
    selected = resolved.selected_order
    if selected is None:
        explicit_missing = bool(requested_number)
        return OrderResolveResult(
            status="not_found",
            selection_reason=resolved.selected_reason,
            failure_reason=(
                "explicit_order_not_found_for_customer"
                if explicit_missing
                else "no_orders_in_trusted_customer_record"
            ),
        )
    order = _load_tenant_order(context, selected.order_id)
    if order is None:
        return OrderResolveResult(
            status="not_found",
            selection_reason=resolved.selected_reason,
            failure_reason="resolved_order_no_longer_exists",
        )
    _assert_discovered_order_is_customer_scoped(context, order)
    context.authorize_orders([selected.order_id])
    summary, evidence = _summary_evidence(order)
    context.register_evidence([evidence])
    return OrderResolveResult(
        status="ok",
        order=summary,
        selection_reason=resolved.selected_reason,
        evidence=[evidence],
    )


def _line_item_snapshots(order: Any) -> list[OrderLineItemSnapshot]:
    snapshot = _snapshot_from_order(order)
    payload_items = list(local_order_to_track_payload(snapshot).get("items") or [])
    results: list[OrderLineItemSnapshot] = []
    for index, raw in enumerate(snapshot.line_items):
        if not isinstance(raw, dict):
            continue
        name = str(
            (payload_items[index].get("name") if index < len(payload_items) else "") or ""
        ).strip()
        if not name:
            continue
        quantity = _canonical_quantity(
            raw.get("quantity") if raw.get("quantity") not in (None, "") else raw.get("qty")
        )
        results.append(OrderLineItemSnapshot(name=name, quantity=quantity))
    return results


@function_tool(timeout=8.0, is_enabled=_catalog_search_enabled)
async def get_order_details(
    run_context: RunContextWrapper[CommerceAgentContext],
    order_id: int,
) -> OrderDetailsResult:
    """Get total and line items for an order authorized by resolve_customer_order."""
    context = run_context.context
    if not context.capabilities.read_orders:
        return OrderDetailsResult(status="denied", failure_reason="order_reads_disabled")
    order = _load_authorized_order(context, order_id)
    if order is None:
        return OrderDetailsResult(status="not_found", failure_reason="authorized_order_missing")

    snapshot = _snapshot_from_order(order)
    reference = snapshot.display_reference or None
    total = _canonical_money(snapshot.total)
    currency = "SAR" if total is not None else None
    items = _line_item_snapshots(order)
    evidence_ref = f"order:details:{snapshot.order_id}"
    facts: list[CanonicalEvidenceFact] = []
    if reference:
        facts.append(
            CanonicalEvidenceFact(
                kind="order_reference",
                value=reference,
                subject_order_id=snapshot.order_id,
            )
        )
    if total is not None:
        facts.extend(
            [
                CanonicalEvidenceFact(
                    kind="order_total",
                    value=total,
                    subject_order_id=snapshot.order_id,
                ),
                CanonicalEvidenceFact(
                    kind="order_currency",
                    value=currency,
                    subject_order_id=snapshot.order_id,
                ),
            ]
        )
    for item in items:
        facts.append(
            CanonicalEvidenceFact(
                kind="order_item_name",
                value=item.name,
                subject_order_id=snapshot.order_id,
            )
        )
        if item.quantity is not None:
            facts.append(
                CanonicalEvidenceFact(
                    kind="order_item_quantity",
                    value=item.quantity,
                    subject_order_id=snapshot.order_id,
                )
            )
    fields = {
        "order_id": snapshot.order_id,
        "order_reference": reference,
        "total": total,
        "currency": currency,
        "line_items": [item.model_dump(mode="json") for item in items],
    }
    evidence = EvidenceRecord(
        ref=evidence_ref,
        source="order_details",
        source_id=str(snapshot.order_id),
        facts=facts,
        fields=fields,
        provenance={
            "service": "core.local_order_resolver.LocalOrderSnapshot",
            "record": "orders",
            "source": str(snapshot.source or "local"),
        },
    )
    context.register_evidence([evidence])
    return OrderDetailsResult(
        status="ok",
        order=OrderDetailsSnapshot(
            order_id=snapshot.order_id,
            order_reference=reference,
            total=total,
            currency=currency,
            line_items=items,
            evidence_ref=evidence_ref,
        ),
        evidence=[evidence],
    )


def _shipment_snapshot(
    order: Any,
    shipment: Any,
) -> tuple[OrderShipmentSnapshot, EvidenceRecord] | None:
    snapshot = _snapshot_from_order(order)
    order_meta_facts = _metadata_shipment_facts(order)
    shipment_meta = (
        dict(getattr(shipment, "extra_metadata", None) or {}) if shipment is not None else {}
    )
    raw_status = str(
        (getattr(shipment, "status", "") if shipment is not None else "")
        or (
            snapshot.status
            if str(snapshot.status or "").strip().lower() in _SHIPMENT_ORDER_STATUSES
            else ""
        )
        or ""
    ).strip()
    raw_carrier = str(
        (getattr(shipment, "provider", "") if shipment is not None else "")
        or order_meta_facts["carrier"]
        or ""
    ).strip()
    placeholder = bool(shipment_meta.get("placeholder_carrier"))
    carrier = (
        ""
        if placeholder or raw_carrier.lower() in _PLACEHOLDER_CARRIERS
        else raw_carrier
    )
    tracking_number = str(
        (getattr(shipment, "tracking_number", "") if shipment is not None else "")
        or order_meta_facts["tracking_number"]
        or ""
    ).strip()
    tracking_url = _absolute_http_url(shipment_meta.get("tracking_url"))
    if tracking_url is None and shipment is not None:
        # Existing imports historically store an external tracking URL in
        # label_url. Relative internal label routes are deliberately rejected.
        tracking_url = _absolute_http_url(getattr(shipment, "label_url", ""))
    tracking_url = tracking_url or order_meta_facts["tracking_url"] or ""
    reference = snapshot.display_reference or None
    if not any((raw_status, carrier, tracking_number, tracking_url)):
        return None

    status_label = order_status_label_ar(raw_status, source=snapshot.source) if raw_status else ""
    evidence_ref = f"order:shipment:{snapshot.order_id}"
    facts: list[CanonicalEvidenceFact] = []
    if reference:
        facts.append(
            CanonicalEvidenceFact(
                kind="order_reference",
                value=reference,
                subject_order_id=snapshot.order_id,
            )
        )
    for kind, value in (
        ("shipment_status", raw_status),
        ("shipment_status_label", status_label),
        ("carrier", carrier),
        ("tracking_number", tracking_number),
        ("tracking_url", tracking_url),
    ):
        if value:
            facts.append(
                CanonicalEvidenceFact(
                    kind=kind,
                    value=value,
                    subject_order_id=snapshot.order_id,
                )
            )
    evidence = EvidenceRecord(
        ref=evidence_ref,
        source="order_shipment",
        source_id=str(snapshot.order_id),
        facts=facts,
        fields={
            "order_id": snapshot.order_id,
            "order_reference": reference,
            "shipment_status": raw_status or None,
            "shipment_status_label": status_label or None,
            "carrier": carrier or None,
            "tracking_number": tracking_number or None,
            "tracking_url": tracking_url or None,
        },
        provenance={
            "service": (
                "core.order_shipment_service.get_order_shipment"
                if shipment is not None
                else "core.local_order_resolver.LocalOrderSnapshot"
            ),
            "record": "order_shipments" if shipment is not None else "orders",
            "source": str(snapshot.source or "local"),
        },
    )
    return (
        OrderShipmentSnapshot(
            order_id=snapshot.order_id,
            order_reference=reference,
            shipment_status=raw_status or None,
            shipment_status_label=status_label or None,
            carrier=carrier or None,
            tracking_number=tracking_number or None,
            tracking_url=tracking_url or None,
            evidence_ref=evidence_ref,
        ),
        evidence,
    )


@function_tool(timeout=8.0, is_enabled=_catalog_search_enabled)
async def get_order_shipment(
    run_context: RunContextWrapper[CommerceAgentContext],
    order_id: int,
) -> OrderShipmentResult:
    """Get shipment/tracking facts for an order authorized in this trusted run."""
    context = run_context.context
    if not context.capabilities.read_shipments:
        return OrderShipmentResult(status="denied", failure_reason="shipment_reads_disabled")
    order = _load_authorized_order(context, order_id)
    if order is None:
        return OrderShipmentResult(status="not_found", failure_reason="authorized_order_missing")
    shipment = get_persisted_order_shipment(context.db, context.tenant_id, int(order_id))
    projected = _shipment_snapshot(order, shipment)
    if projected is None:
        return OrderShipmentResult(
            status="no_evidence",
            failure_reason="shipment_not_available_for_authorized_order",
        )
    snapshot, evidence = projected
    context.register_evidence([evidence])
    return OrderShipmentResult(status="ok", shipment=snapshot, evidence=[evidence])


__all__ = [
    "get_order_details",
    "get_order_shipment",
    "resolve_customer_order",
]
