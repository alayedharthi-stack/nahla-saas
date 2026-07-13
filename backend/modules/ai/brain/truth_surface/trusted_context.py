"""
truth_surface/trusted_context.py
────────────────────────────────
Trusted Context & Facts Owner — single pre-decide snapshot builder.

Builds one ``TrustedContextSnapshot`` from official platform sources.
Shadow-only in Phase 1: does not replace legacy loaders yet.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from .contract import TrustedContextSnapshot, TrustedDomain, TrustedFact, TruthSource
from .flags import is_trusted_context_shadow_enabled

logger = logging.getLogger("nahla.brain.trusted_context")

_current: ContextVar[Optional[TrustedContextSnapshot]] = ContextVar(
    "trusted_context_snapshot",
    default=None,
)


def _fact(
    *,
    domain: TrustedDomain,
    key: str,
    value: Any,
    source: TruthSource,
    path: str = "",
    confidence: float = 1.0,
) -> Optional[TrustedFact]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, (list, dict)) and not value:
        return None
    return TrustedFact(
        domain=domain,
        key=key,
        value=value,
        source=source,
        path=path,
        confidence=confidence,
    )


def _append(facts: List[TrustedFact], item: Optional[TrustedFact]) -> None:
    if item is not None:
        facts.append(item)


def _prep_dict(brain_state: Any) -> Dict[str, Any]:
    prep = getattr(brain_state, "order_prep", None)
    if prep is None:
        return {}
    if isinstance(prep, dict):
        return dict(prep)
    out: Dict[str, Any] = {}
    for key in (
        "customer_first_name",
        "customer_last_name",
        "city",
        "short_address_code",
        "google_maps_url",
        "delivery_address_url",
        "line_items",
        "product_id",
        "order_status",
        "payment_receipt_received",
        "awaiting_payment_receipt",
        "catalog_line_items_authoritative",
        "catalog_checkout_total",
    ):
        if hasattr(prep, key):
            val = getattr(prep, key, None)
            if val not in (None, ""):
                out[key] = val
    return out


def _load_customer_order_facts(
    *,
    db: Any,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    brain_state: Any,
    inbound_metadata: Optional[Dict[str, Any]],
    message: str,
) -> List[TrustedFact]:
    facts: List[TrustedFact] = []
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415

        order_ctx = build_order_context(
            db,
            tenant_id=tenant_id,
            conversation=conversation,
            phone=customer_phone,
            brain_state=_prep_dict(brain_state) or None,
            inbound_metadata=dict(inbound_metadata or {}),
            build_source="trusted_context_owner",
            message=message or "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[TRUSTED_CONTEXT] order_context failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return facts

    identity = order_ctx.identity
    shipping = order_ctx.shipping
    draft = order_ctx.active_draft
    catalog = order_ctx.catalog_order

    _append(facts, _fact(
        domain=TrustedDomain.CUSTOMER,
        key="customer_id",
        value=identity.customer_id,
        source=TruthSource.STORE_SNAPSHOT,
        path="order_context.identity.customer_id",
    ))
    _append(facts, _fact(
        domain=TrustedDomain.CUSTOMER,
        key="operational_name",
        value=identity.operational_name,
        source=TruthSource.STORE_SNAPSHOT,
        path="order_context.identity.operational_name",
    ))
    _append(facts, _fact(
        domain=TrustedDomain.CUSTOMER,
        key="name_status",
        value=identity.name_status,
        source=TruthSource.STORE_SNAPSHOT,
        path="order_context.identity.name_status",
    ))
    _append(facts, _fact(
        domain=TrustedDomain.CUSTOMER,
        key="city",
        value=shipping.city,
        source=TruthSource.STORE_SNAPSHOT,
        path="order_context.shipping.city",
    ))
    _append(facts, _fact(
        domain=TrustedDomain.CUSTOMER,
        key="short_address",
        value=shipping.short_address,
        source=TruthSource.STORE_SNAPSHOT,
        path="order_context.shipping.short_address",
    ))
    _append(facts, _fact(
        domain=TrustedDomain.CUSTOMER,
        key="maps_url",
        value=shipping.maps_url,
        source=TruthSource.STORE_SNAPSHOT,
        path="order_context.shipping.maps_url",
    ))
    _append(facts, _fact(
        domain=TrustedDomain.CUSTOMER,
        key="accepted_delivery_address",
        value=shipping.accepted_delivery_address,
        source=TruthSource.STORE_SNAPSHOT,
        path="order_context.shipping.accepted_delivery_address",
    ))

    if draft is not None:
        _append(facts, _fact(
            domain=TrustedDomain.ORDER,
            key="order_id",
            value=draft.order_id,
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="order_context.active_draft.order_id",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.ORDER,
            key="external_id",
            value=draft.external_id,
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="order_context.active_draft.external_id",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.ORDER,
            key="status",
            value=draft.status,
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="order_context.active_draft.status",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.ORDER,
            key="missing_fields",
            value=list(draft.missing_fields or []),
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="order_context.active_draft.missing_fields",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.ORDER,
            key="line_items_count",
            value=len(draft.line_items or []),
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="order_context.active_draft.line_items",
        ))

    if catalog is not None and catalog.has_catalog_order:
        _append(facts, _fact(
            domain=TrustedDomain.CATALOG,
            key="catalog_order_submitted",
            value=True,
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="order_context.catalog_order.has_catalog_order",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.CATALOG,
            key="item_count",
            value=catalog.item_count,
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="order_context.catalog_order.item_count",
        ))

    return facts


def _load_state_order_facts(brain_state: Any) -> List[TrustedFact]:
    facts: List[TrustedFact] = []
    prep = _prep_dict(brain_state)
    if not prep:
        return facts

    for key in (
        "order_status",
        "payment_receipt_received",
        "awaiting_payment_receipt",
        "catalog_line_items_authoritative",
        "catalog_checkout_total",
        "product_id",
    ):
        _append(facts, _fact(
            domain=TrustedDomain.ORDER,
            key=key,
            value=prep.get(key),
            source=TruthSource.ORDER_PREPARATION_STATE,
            path=f"brain_state.order_prep.{key}",
        ))

    line_items = prep.get("line_items") or []
    if isinstance(line_items, list) and line_items:
        _append(facts, _fact(
            domain=TrustedDomain.ORDER,
            key="line_items_count",
            value=len(line_items),
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="brain_state.order_prep.line_items",
        ))

    focus = getattr(brain_state, "current_product_focus", None)
    if focus is not None:
        if isinstance(focus, dict):
            for fk, fv in (
                ("product_id", focus.get("id") or focus.get("product_id")),
                ("title", focus.get("title") or focus.get("name")),
                ("price", focus.get("price")),
            ):
                _append(facts, _fact(
                    domain=TrustedDomain.CATALOG,
                    key=fk,
                    value=fv,
                    source=TruthSource.PRODUCTS_TABLE,
                    path=f"brain_state.current_product_focus.{fk}",
                ))
        else:
            _append(facts, _fact(
                domain=TrustedDomain.CATALOG,
                key="product_focus",
                value=str(focus),
                source=TruthSource.PRODUCTS_TABLE,
                path="brain_state.current_product_focus",
            ))

    return facts


def _load_payment_shipment_facts(
    *,
    db: Any,
    tenant_id: int,
    customer_phone: str,
    inbound_metadata: Optional[Dict[str, Any]],
    commerce_bundle: Optional[Dict[str, Any]],
) -> List[TrustedFact]:
    facts: List[TrustedFact] = []
    meta = dict(inbound_metadata or {})
    bundle = dict(commerce_bundle or {})

    pe = str(meta.get("payment_evidence_status") or "").strip()
    if pe:
        _append(facts, _fact(
            domain=TrustedDomain.PAYMENT,
            key="payment_evidence_status",
            value=pe,
            source=TruthSource.MEDIA_REGISTRY,
            path="inbound_metadata.payment_evidence_status",
        ))
    if meta.get("payment_receipt_received") is not None:
        _append(facts, _fact(
            domain=TrustedDomain.PAYMENT,
            key="payment_receipt_received",
            value=bool(meta.get("payment_receipt_received")),
            source=TruthSource.MEDIA_REGISTRY,
            path="inbound_metadata.payment_receipt_received",
        ))
    if meta.get("awaiting_payment_receipt") is not None:
        _append(facts, _fact(
            domain=TrustedDomain.PAYMENT,
            key="awaiting_payment_receipt",
            value=bool(meta.get("awaiting_payment_receipt")),
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="inbound_metadata.awaiting_payment_receipt",
        ))

    order_status = str(bundle.get("order_status") or "").strip()
    if order_status:
        _append(facts, _fact(
            domain=TrustedDomain.SHIPMENT,
            key="order_status",
            value=order_status,
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="commerce_bundle.order_status",
        ))
    tracking = str(bundle.get("tracking_number") or bundle.get("tracking") or "").strip()
    if tracking:
        _append(facts, _fact(
            domain=TrustedDomain.SHIPMENT,
            key="tracking_number",
            value=tracking,
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="commerce_bundle.tracking_number",
        ))

    try:
        from modules.ai.brain.postprocess.shipment_evidence import (  # noqa: PLC0415
            evaluate_shipment_evidence,
        )

        evidence = evaluate_shipment_evidence(
            commerce_bundle=bundle,
            inbound_metadata=meta,
            payment_receipt_received=bool(meta.get("payment_receipt_received")),
        )
        _append(facts, _fact(
            domain=TrustedDomain.SHIPMENT,
            key="shipment_evidence_source",
            value=getattr(evidence, "evidence_source", ""),
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="shipment_evidence.evidence_source",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.SHIPMENT,
            key="tracking_present",
            value=bool(getattr(evidence, "tracking_present", False)),
            source=TruthSource.ORDER_PREPARATION_STATE,
            path="shipment_evidence.tracking_present",
        ))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — shipment evidence is optional in shadow
        pass

    return facts


def _load_capability_facts(db: Any, tenant_id: int) -> List[TrustedFact]:
    facts: List[TrustedFact] = []
    try:
        from modules.ai.commerce_agent.capability_resolver import (  # noqa: PLC0415
            resolve_tenant_capabilities,
        )

        caps = resolve_tenant_capabilities(db, tenant_id)
        for key, value in (
            ("whatsapp_order", caps.whatsapp_order),
            ("online_store", caps.online_store),
            ("pickup", caps.pickup),
            ("native_catalog", caps.native_catalog),
            ("showroom_enabled", caps.showroom_enabled),
            ("cod_enabled", caps.cod_enabled),
            ("store_url", caps.store_url),
            ("available_tools", list(caps.available_tools)),
        ):
            _append(facts, _fact(
                domain=TrustedDomain.CAPABILITIES,
                key=key,
                value=value,
                source=TruthSource.TENANT_SETTINGS,
                path=f"capabilities.{key}",
            ))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[TRUSTED_CONTEXT] capabilities failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return facts


def _load_merchant_policy_facts(db: Any, tenant_id: int) -> List[TrustedFact]:
    facts: List[TrustedFact] = []
    try:
        from modules.ai.brain.facts.commerce_facts import DefaultFactsLoader  # noqa: PLC0415

        store_facts = DefaultFactsLoader().load(db, tenant_id)
        _append(facts, _fact(
            domain=TrustedDomain.MERCHANT_POLICY,
            key="store_name",
            value=getattr(store_facts, "store_name", ""),
            source=TruthSource.STORE_SNAPSHOT,
            path="commerce_facts.store_name",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.MERCHANT_POLICY,
            key="shipping_policy",
            value=getattr(store_facts, "shipping_policy", ""),
            source=TruthSource.STORE_SNAPSHOT,
            path="commerce_facts.shipping_policy",
        ))
        _append(facts, _fact(
            domain=TrustedDomain.MERCHANT_POLICY,
            key="has_coupons",
            value=bool(getattr(store_facts, "has_coupons", False)),
            source=TruthSource.COUPON_TABLE,
            path="commerce_facts.has_coupons",
        ))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[TRUSTED_CONTEXT] merchant_policy failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return facts


def build_trusted_context_snapshot(
    *,
    db: Any,
    tenant_id: int,
    customer_phone: str,
    message: str = "",
    conversation: Any = None,
    conversation_id: Optional[int] = None,
    brain_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> TrustedContextSnapshot:
    """Build the canonical trusted context snapshot for one inbound turn."""
    facts: List[TrustedFact] = []
    sources: List[str] = []
    loaded_domains: List[str] = []

    commerce_bundle: Dict[str, Any] = {}
    if db is not None and tenant_id and customer_phone:
        try:
            from core.active_order_context import load_commerce_bundle_from_db  # noqa: PLC0415

            commerce_bundle = load_commerce_bundle_from_db(
                db, tenant_id, customer_phone,
            )
            if commerce_bundle:
                sources.append("active_order_context")
        except Exception:  # noqa: BLE001  # noqa: silent-ok — commerce bundle is optional enrichment
            pass

    customer_facts = _load_customer_order_facts(
        db=db,
        tenant_id=tenant_id,
        conversation=conversation,
        customer_phone=customer_phone,
        brain_state=brain_state,
        inbound_metadata=inbound_metadata,
        message=message,
    )
    if customer_facts:
        facts.extend(customer_facts)
        loaded_domains.append(TrustedDomain.CUSTOMER.value)
        loaded_domains.append(TrustedDomain.ORDER.value)
        sources.append("order_context_builder")

    state_facts = _load_state_order_facts(brain_state)
    if state_facts:
        facts.extend(state_facts)
        if TrustedDomain.ORDER.value not in loaded_domains:
            loaded_domains.append(TrustedDomain.ORDER.value)
        sources.append("brain_state.order_prep")

    payment_facts = _load_payment_shipment_facts(
        db=db,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        inbound_metadata=inbound_metadata,
        commerce_bundle=commerce_bundle,
    )
    if payment_facts:
        facts.extend(payment_facts)
        loaded_domains.extend([
            TrustedDomain.PAYMENT.value,
            TrustedDomain.SHIPMENT.value,
        ])
        sources.append("payment_shipment_evidence")

    cap_facts = _load_capability_facts(db, tenant_id)
    if cap_facts:
        facts.extend(cap_facts)
        loaded_domains.append(TrustedDomain.CAPABILITIES.value)
        sources.append("capability_resolver")

    policy_facts = _load_merchant_policy_facts(db, tenant_id)
    if policy_facts:
        facts.extend(policy_facts)
        loaded_domains.append(TrustedDomain.MERCHANT_POLICY.value)
        sources.append("commerce_facts")

    shadow_observability: Dict[str, Any] = {}
    try:
        from .coupon_offer_loader import (  # noqa: PLC0415
            load_coupon_promotion_facts,
            should_load_coupon_promotion_facts,
        )

        if should_load_coupon_promotion_facts(
            message=message,
            brain_state=brain_state,
            inbound_metadata=inbound_metadata,
        ):
            coupon_facts, coupon_obs = load_coupon_promotion_facts(
                db=db,
                tenant_id=tenant_id,
                customer_phone=customer_phone,
                message=message,
                brain_state=brain_state,
                inbound_metadata=inbound_metadata,
                conversation=conversation,
            )
            if coupon_facts:
                facts.extend(coupon_facts)
                loaded_domains.extend([
                    TrustedDomain.COUPONS.value,
                    TrustedDomain.PROMOTIONS.value,
                ])
                sources.append("coupon_offer_loader")
                shadow_observability.update(coupon_obs)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[TRUSTED_CONTEXT] coupon_promotion_loader failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    snapshot = TrustedContextSnapshot(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or "").strip(),
        conversation_id=conversation_id or getattr(conversation, "id", None),
        facts=facts,
        loaded_domains=sorted(set(loaded_domains)),
        sources=sorted(set(sources)),
        shadow_observability=shadow_observability,
    )
    snapshot.ensure_snapshot_id()
    return snapshot


def set_current_trusted_context(snapshot: Optional[TrustedContextSnapshot]) -> None:
    _current.set(snapshot)


def current_trusted_context() -> Optional[TrustedContextSnapshot]:
    return _current.get()


def clear_trusted_context() -> None:
    _current.set(None)


def run_trusted_context_shadow(
    *,
    db: Any,
    tenant_id: int,
    customer_phone: str,
    message: str = "",
    conversation: Any = None,
    conversation_id: Optional[int] = None,
    brain_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[TrustedContextSnapshot]:
    """
    Build trusted context snapshot and emit shadow telemetry.

    Returns ``None`` when shadow flag is disabled.
    """
    if not is_trusted_context_shadow_enabled():
        return None

    try:
        snapshot = build_trusted_context_snapshot(
            db=db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            message=message,
            conversation=conversation,
            conversation_id=conversation_id,
            brain_state=brain_state,
            inbound_metadata=inbound_metadata,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[TRUSTED_CONTEXT_SHADOW] build_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None

    set_current_trusted_context(snapshot)

    try:
        from core.turn_provenance import current_turn_provenance  # noqa: PLC0415

        prov = current_turn_provenance()
        if prov is not None:
            prov.set_facts_version("trusted_context_v1")
            prov.metadata["facts_snapshot_id"] = snapshot.snapshot_id
            prov.metadata["trusted_context"] = snapshot.to_metadata()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — provenance attach must not break shadow
        pass

    try:
        logger.info(
            "[TRUSTED_CONTEXT_SHADOW] %s",
            json.dumps(snapshot.to_log_dict(), ensure_ascii=False),
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry emit must not break shadow
        pass

    return snapshot


def trusted_context_projection_for_compose(
    domains: Optional[List[TrustedDomain]] = None,
) -> Dict[str, Any]:
    """Small compose-safe projection from the current snapshot."""
    snapshot = _current.get()
    if snapshot is None:
        return {}
    return snapshot.projection(domains=domains)


__all__ = [
    "build_trusted_context_snapshot",
    "clear_trusted_context",
    "current_trusted_context",
    "run_trusted_context_shadow",
    "set_current_trusted_context",
    "trusted_context_projection_for_compose",
]
