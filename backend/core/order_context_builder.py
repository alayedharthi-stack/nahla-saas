"""
core/order_context_builder.py
─────────────────────────────
Read-only OrderContext projection from existing customer, brain, order,
catalog, and shipping sources. Phase A: shadow logging + divergence
comparison only — no DB writes, no reply/routing changes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.customer_identity_resolver import (
    STATUS_MISSING,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    can_use_name_for_operations,
    is_manual_name_locked,
    is_official_name_status,
    read_customer_identity,
)
from core.wa_order_lifecycle import has_accepted_delivery_address
from services.nahla_order_bridge import nahla_wa_external_id

from core.order_context_prefill import (  # noqa: E402
    OrderPrefillState,
    build_prefill_state,
    enrich_identity_context,
    enrich_shipping_context,
    shadow_missing_fields_from_modes,
)

logger = logging.getLogger("nahla.order_context_builder")

_CATALOG_SOURCE = "catalog_order"
_COMPLETED_ORDER_STATUSES = frozenset(
    {"paid", "completed", "processing", "confirmed", "shipped", "delivered"}
)


@dataclass(frozen=True)
class FieldEvidence:
    field: str
    value: Any
    source: str
    confidence: float
    message_id: Optional[str] = None
    locked: bool = False
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class OrderIdentityContext:
    customer_id: Optional[int]
    phone: str
    display_name: str
    operational_name: str
    first_name: str
    last_name: str
    name_source: str
    name_status: str
    confidence: float
    locked_by_merchant: bool
    has_verified_name: bool
    has_proposed_name: bool
    missing_name_reason: str = ""
    missing_mode: str = "ask"
    can_use_for_shipping_label: bool = False


@dataclass(frozen=True)
class ShippingContext:
    city: str
    district: str
    street: str
    address_line: str
    maps_url: str
    short_address: str
    latitude: Optional[float]
    longitude: Optional[float]
    source: str
    confidence: float
    accepted_delivery_address: bool
    locked_by_merchant: bool = False
    missing_mode: str = "ask"
    requires_merchant_review: bool = False


@dataclass(frozen=True)
class ActiveDraftContext:
    order_id: Optional[int]
    external_id: str
    status: str
    lifecycle: str
    line_items: list
    total: Optional[float]
    currency: str
    missing_fields: list
    merchant_edit_locked: bool


@dataclass(frozen=True)
class CatalogOrderSnapshot:
    has_catalog_order: bool
    item_count: int
    total_price: Optional[float]
    currency: str
    product_items: list
    message_id: Optional[str]


@dataclass(frozen=True)
class OrderContext:
    tenant_id: int
    conversation_id: Optional[int]
    customer_id: Optional[int]
    identity: OrderIdentityContext
    shipping: ShippingContext
    active_draft: Optional[ActiveDraftContext]
    catalog_order: CatalogOrderSnapshot
    brain_order_prep: dict
    legacy_missing_fields: list
    shadow_missing_fields: list
    field_evidence: dict
    build_source: str
    divergence_flags: dict
    prefill: OrderPrefillState
    known_previous_address: Optional[ShippingContext] = None
    shadow_missing_modes: Optional[dict] = None
    missing_fields_result: Optional[Any] = None


def _prep_dict(brain_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    bs = brain_state if isinstance(brain_state, dict) else {}
    prep = bs.get("order_prep") or {}
    return dict(prep) if isinstance(prep, dict) else {}


def _meta_dict(obj: Any) -> Dict[str, Any]:
    return dict(getattr(obj, "extra_metadata", None) or {})


def _prep_str(prep: Dict[str, Any], key: str) -> str:
    return str(prep.get(key) or "").strip()


def _split_name(full: str) -> Tuple[str, str]:
    text = (full or "").strip()
    if not text:
        return "", ""
    parts = text.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("ر.س", "").replace("SAR", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_lat_lng(prep: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lat = prep.get("latitude") or prep.get("delivery_location_lat")
    lng = prep.get("longitude") or prep.get("delivery_location_lng")
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None


def mask_phone(phone: str) -> str:
    """Mask phone for logs — keep last 4 digits only."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return "****"
    if len(digits) <= 4:
        return "****"
    return "*" * (len(digits) - 4) + digits[-4:]


def _missing_name_reason(customer: Any, snap: Any) -> str:
    if customer is None:
        return "no_customer"
    meta = _meta_dict(customer)
    if bool(meta.get("manual_name_cleared")) and not snap.customer_name:
        return "merchant_cleared"
    if snap.customer_name_status == STATUS_REJECTED:
        return "rejected"
    if can_use_name_for_operations(customer):
        return ""
    if snap.proposed_name:
        return "proposed_only"
    if snap.customer_name_status == STATUS_MISSING or not snap.customer_name:
        return "missing"
    if snap.customer_name_status == STATUS_PROPOSED:
        return "proposed_only"
    return "not_verified"


def build_order_identity(
    *,
    customer: Any,
    prep: Dict[str, Any],
    phone: str,
    draft_customer_info: Optional[Dict[str, Any]] = None,
) -> OrderIdentityContext:
    draft_ci = dict(draft_customer_info or {})
    customer_id = getattr(customer, "id", None) if customer is not None else None

    snap = read_customer_identity(customer) if customer is not None else None
    if snap is None:
        from core.customer_identity_resolver import CustomerIdentitySnapshot  # noqa: PLC0415

        snap = CustomerIdentitySnapshot(
            customer_name="",
            customer_name_source="",
            customer_name_status=STATUS_MISSING,
            customer_name_confidence=0.0,
            customer_name_updated_at=None,
            proposed_name="",
            display_name="",
        )

    first = _prep_str(prep, "customer_first_name") or str(draft_ci.get("first_name") or "").strip()
    last = _prep_str(prep, "customer_last_name") or str(draft_ci.get("last_name") or "").strip()
    if not first and not last and can_use_name_for_operations(customer):
        first, last = _split_name(snap.customer_name)

    operational = ""
    if can_use_name_for_operations(customer):
        operational = snap.customer_name
    elif first or last:
        operational = f"{first} {last}".strip()

    locked = is_manual_name_locked(customer) if customer is not None else False
    has_verified = bool(
        customer is not None
        and is_official_name_status(snap.customer_name_status)
        and bool(snap.customer_name)
    )

    return OrderIdentityContext(
        customer_id=customer_id,
        phone=phone or str(getattr(customer, "phone", None) or ""),
        display_name=snap.display_name,
        operational_name=operational,
        first_name=first,
        last_name=last,
        name_source=snap.customer_name_source,
        name_status=snap.customer_name_status,
        confidence=float(snap.customer_name_confidence or 0.0),
        locked_by_merchant=locked,
        has_verified_name=has_verified,
        has_proposed_name=bool(snap.proposed_name),
        missing_name_reason=_missing_name_reason(customer, snap),
    )


def build_shipping_context(
    prep: Dict[str, Any],
    *,
    order_customer_info: Optional[Dict[str, Any]] = None,
) -> ShippingContext:
    ci = dict(order_customer_info or {})
    city = _prep_str(prep, "city") or str(ci.get("city") or "").strip()
    district = _prep_str(prep, "district") or str(ci.get("district") or "").strip()
    street = _prep_str(prep, "street") or str(ci.get("street") or "").strip()
    address_line = (
        _prep_str(prep, "address_line")
        or str(ci.get("address_line") or ci.get("address") or "").strip()
    )
    maps_url = (
        _prep_str(prep, "google_maps_url")
        or _prep_str(prep, "delivery_address_url")
        or str(ci.get("google_maps_url") or ci.get("maps_url") or "").strip()
    )
    short_address = _prep_str(prep, "short_address_code") or str(
        ci.get("short_address_code") or ci.get("short_address") or ""
    ).strip()
    lat, lng = _to_lat_lng(prep)
    if lat is None and ci.get("latitude") is not None:
        lat, lng = _to_lat_lng(ci)

    source = "order_prep"
    confidence = 0.9 if city or maps_url or short_address or lat is not None else 0.0
    if not (city or maps_url or short_address or lat is not None) and ci:
        source = "order_customer_info"
        confidence = 0.6

    return ShippingContext(
        city=city,
        district=district,
        street=street,
        address_line=address_line,
        maps_url=maps_url,
        short_address=short_address,
        latitude=lat,
        longitude=lng,
        source=source,
        confidence=confidence,
        accepted_delivery_address=has_accepted_delivery_address(prep),
    )


def _load_active_draft(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int],
) -> Optional[ActiveDraftContext]:
    if not conversation_id:
        return None
    try:
        from models import Order  # noqa: PLC0415

        external_id = nahla_wa_external_id(tenant_id, int(conversation_id))
        order = (
            db.query(Order)
            .filter_by(tenant_id=tenant_id, external_id=external_id)
            .first()
        )
        if order is None:
            return None
        meta = _meta_dict(order)
        lifecycle = str(meta.get("lifecycle") or "")
        if lifecycle and lifecycle != "whatsapp_draft":
            # Still expose draft row when lifecycle unset (legacy rows).
            if lifecycle not in {"", "whatsapp_draft"}:
                pass  # include non-draft WA rows for read-only snapshot
        missing = list(meta.get("missing_fields") or [])
        total_raw = getattr(order, "total", None)
        return ActiveDraftContext(
            order_id=getattr(order, "id", None),
            external_id=external_id,
            status=str(getattr(order, "status", "") or ""),
            lifecycle=lifecycle or "whatsapp_draft",
            line_items=list(getattr(order, "line_items", None) or []),
            total=_to_float(total_raw),
            currency=str(meta.get("currency") or "SAR"),
            missing_fields=missing,
            merchant_edit_locked=bool(meta.get("merchant_edit_locked")),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ORDER_CONTEXT] active_draft load failed tenant=%s", tenant_id, exc_info=True)
        return None


def _extract_catalog_meta(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(meta, dict):
        return None
    if meta.get("source_type") == _CATALOG_SOURCE:
        return meta
    nested = meta.get("normalized_inbound")
    if isinstance(nested, dict) and nested.get("source_type") == _CATALOG_SOURCE:
        return nested
    return None


def _load_catalog_order_snapshot(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int],
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> CatalogOrderSnapshot:
    empty = CatalogOrderSnapshot(
        has_catalog_order=False,
        item_count=0,
        total_price=None,
        currency="",
        product_items=[],
        message_id=None,
    )
    catalog_meta = _extract_catalog_meta(dict(inbound_metadata or {}))
    message_id: Optional[str] = None

    if catalog_meta is None and conversation_id:
        try:
            from models import MessageEvent  # noqa: PLC0415

            events = (
                db.query(MessageEvent)
                .filter_by(tenant_id=tenant_id, conversation_id=int(conversation_id))
                .order_by(MessageEvent.id.desc())
                .limit(30)
                .all()
            )
            for ev in events:
                found = _extract_catalog_meta(_meta_dict(ev))
                if found:
                    catalog_meta = found
                    message_id = str(getattr(ev, "id", None) or "") or None
                    break
        except Exception:  # noqa: BLE001
            logger.exception(
                "[ORDER_CONTEXT] catalog snapshot load failed tenant=%s",
                tenant_id,
            )

    if not catalog_meta:
        return empty

    product_items = list(catalog_meta.get("product_items") or [])
    item_count = int(catalog_meta.get("item_count") or len(product_items) or 0)
    return CatalogOrderSnapshot(
        has_catalog_order=True,
        item_count=item_count,
        total_price=_to_float(catalog_meta.get("total_price")),
        currency=str(catalog_meta.get("currency") or "SAR"),
        product_items=product_items,
        message_id=message_id,
    )


def _load_known_previous_address(
    db: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
) -> Optional[ShippingContext]:
    if not customer_id:
        return None
    try:
        from models import CustomerAddress  # noqa: PLC0415

        addr = (
            db.query(CustomerAddress)
            .filter_by(tenant_id=tenant_id, customer_id=int(customer_id))
            .order_by(CustomerAddress.id.desc())
            .first()
        )
        if addr is None:
            return None
        lat = _to_float(getattr(addr, "lat", None))
        lng = _to_float(getattr(addr, "lng", None))
        return ShippingContext(
            city=str(getattr(addr, "city", None) or "").strip(),
            district=str(getattr(addr, "district", None) or "").strip(),
            street="",
            address_line=str(getattr(addr, "address_text", None) or "").strip(),
            maps_url=str(getattr(addr, "google_maps_link", None) or "").strip(),
            short_address=str(getattr(addr, "saudi_national_address", None) or "").strip(),
            latitude=lat,
            longitude=lng,
            source="customer_addresses",
            confidence=0.5,
            accepted_delivery_address=bool(
                getattr(addr, "google_maps_link", None)
                or getattr(addr, "saudi_national_address", None)
                or (lat is not None and lng is not None)
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ORDER_CONTEXT] known_previous_address unavailable tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return None


def _has_product_signal(
    *,
    prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    active_draft: Optional[ActiveDraftContext],
    catalog: CatalogOrderSnapshot,
) -> bool:
    if active_draft and active_draft.line_items:
        return True
    if catalog.has_catalog_order and catalog.product_items:
        return True
    for container in (prep, brain_state):
        for key in ("line_items", "cart_items", "items"):
            raw = container.get(key) if isinstance(container, dict) else None
            if isinstance(raw, list) and raw:
                return True
    if _prep_str(prep, "product_id"):
        return True
    focus = brain_state.get("current_product_focus") or {}
    if isinstance(focus, dict) and (focus.get("id") or focus.get("title")):
        return True
    return False


def _has_name_signal(identity: OrderIdentityContext, prep: Dict[str, Any]) -> bool:
    if identity.operational_name:
        return True
    if identity.first_name or identity.last_name:
        return True
    if _prep_str(prep, "customer_first_name") or _prep_str(prep, "customer_last_name"):
        return True
    return False


def _line_items_total(items: list) -> Optional[float]:
    total = 0.0
    found = False
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        price = _to_float(raw.get("price") or raw.get("item_price") or raw.get("unit_price"))
        qty_raw = raw.get("quantity") or raw.get("qty") or 1
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            qty = 1.0
        if price is not None:
            total += price * qty
            found = True
    return total if found else None


def _has_total_signal(
    *,
    prep: Dict[str, Any],
    active_draft: Optional[ActiveDraftContext],
    catalog: CatalogOrderSnapshot,
) -> bool:
    if active_draft and active_draft.total is not None:
        return True
    if catalog.has_catalog_order and catalog.total_price is not None:
        return True
    for key in ("total", "order_total", "catalog_total", "total_price"):
        if _to_float(prep.get(key)) is not None:
            return True
    draft_items = active_draft.line_items if active_draft else []
    prep_items = prep.get("line_items") or prep.get("cart_items") or prep.get("items") or []
    if _line_items_total(list(draft_items) + list(prep_items)) is not None:
        return True
    if catalog.product_items and _line_items_total(catalog.product_items) is not None:
        return True
    return False


def compute_shadow_missing_fields(ctx: OrderContext) -> List[str]:
    """Read-only completeness projection — does not replace legacy missing fields."""
    missing: List[str] = []
    prep = ctx.brain_order_prep or {}
    brain_state = {"order_prep": prep}

    if not _has_product_signal(
        prep=prep,
        brain_state=brain_state,
        active_draft=ctx.active_draft,
        catalog=ctx.catalog_order,
    ):
        missing.append("product")

    if not _has_name_signal(ctx.identity, prep):
        missing.append("name")

    if not (ctx.shipping.city or "").strip():
        missing.append("city")

    if not ctx.shipping.accepted_delivery_address:
        missing.append("delivery_address")

    if not _has_total_signal(
        prep=prep,
        active_draft=ctx.active_draft,
        catalog=ctx.catalog_order,
    ):
        missing.append("total")

    return missing


def compute_divergence_flags(
    legacy_missing: List[str],
    shadow_missing: List[str],
) -> Dict[str, bool]:
    legacy = set(legacy_missing or [])
    shadow = set(shadow_missing or [])
    legacy_name = "customer_first_name" in legacy or "customer_last_name" in legacy
    shadow_name = "name" in shadow
    return {
        "missing_fields_differ": legacy != shadow,
        "legacy_only": bool(legacy - shadow),
        "shadow_only": bool(shadow - legacy),
        "product_divergence": ("product" in legacy) != ("product" in shadow),
        "name_divergence": legacy_name != shadow_name,
        "city_divergence": ("city" in legacy) != ("city" in shadow),
        "delivery_address_divergence": ("delivery_address" in legacy)
        != ("delivery_address" in shadow),
        "total_divergence": ("total" in shadow),
    }


def _build_field_evidence(
    *,
    identity: OrderIdentityContext,
    shipping: ShippingContext,
    prep: Dict[str, Any],
    identity_snap: Any,
    draft_locked: bool = False,
) -> Dict[str, FieldEvidence]:
    evidence: Dict[str, FieldEvidence] = {}
    if identity.operational_name:
        evidence["operational_name"] = FieldEvidence(
            field="operational_name",
            value=identity.operational_name,
            source=identity.name_source or "customer",
            confidence=identity.confidence,
            locked=identity.locked_by_merchant,
            updated_at=getattr(identity_snap, "customer_name_updated_at", None),
        )
    if shipping.city:
        evidence["shipping.city"] = FieldEvidence(
            field="shipping.city",
            value=shipping.city,
            source=shipping.source,
            confidence=shipping.confidence,
            locked=draft_locked or shipping.locked_by_merchant,
        )
    if shipping.maps_url or shipping.short_address:
        evidence["shipping.delivery_address"] = FieldEvidence(
            field="shipping.delivery_address",
            value=shipping.short_address or shipping.maps_url,
            source=shipping.source,
            confidence=shipping.confidence,
            locked=draft_locked or shipping.locked_by_merchant,
        )
    if _prep_str(prep, "product_id"):
        evidence["product"] = FieldEvidence(
            field="product",
            value=_prep_str(prep, "product_id"),
            source="order_prep",
            confidence=0.8,
        )
    return evidence


def _resolve_legacy_missing_fields(
    prep: Dict[str, Any],
    *,
    brain_state: Dict[str, Any],
    phone: str,
    active_draft: Optional[ActiveDraftContext],
) -> List[str]:
    legacy = list(prep.get("missing_fields") or [])
    if legacy:
        return legacy
    if active_draft and active_draft.missing_fields:
        return list(active_draft.missing_fields)
    from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

    line_items = active_draft.line_items if active_draft else None
    return compute_wa_missing_fields(
        prep,
        brain_state=brain_state,
        whatsapp_phone=phone,
        line_items=line_items,
    )


def build_order_context(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any = None,
    customer: Any = None,
    phone: str = "",
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    build_source: str = "whatsapp_webhook",
    message: str = "",
) -> OrderContext:
    bs = dict(brain_state or {})
    if not bs and conversation is not None:
        bs = dict(_meta_dict(conversation).get("brain_state") or {})

    prep = _prep_dict(bs)
    conversation_id = getattr(conversation, "id", None) if conversation is not None else None

    if customer is None and conversation is not None and getattr(conversation, "customer_id", None):
        try:
            from models import Customer  # noqa: PLC0415

            customer = db.query(Customer).filter_by(id=int(conversation.customer_id)).first()
        except Exception:  # noqa: BLE001
            customer = None

    active_draft = _load_active_draft(
        db, tenant_id=tenant_id, conversation_id=conversation_id
    )
    draft_ci = None
    if active_draft is not None:
        try:
            from models import Order  # noqa: PLC0415

            order_row = (
                db.query(Order)
                .filter_by(tenant_id=tenant_id, external_id=active_draft.external_id)
                .first()
            )
            if order_row is not None:
                draft_ci = dict(getattr(order_row, "customer_info", None) or {})
        except Exception:  # noqa: BLE001
            draft_ci = None

    identity = build_order_identity(
        customer=customer,
        prep=prep,
        phone=phone,
        draft_customer_info=draft_ci,
    )
    shipping = build_shipping_context(prep, order_customer_info=draft_ci)
    catalog = _load_catalog_order_snapshot(
        db,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        inbound_metadata=inbound_metadata,
    )
    known_previous = _load_known_previous_address(
        db,
        tenant_id=tenant_id,
        customer_id=identity.customer_id or getattr(conversation, "customer_id", None),
    )

    legacy_missing = _resolve_legacy_missing_fields(
        prep,
        brain_state=bs,
        phone=phone,
        active_draft=active_draft,
    )

    identity_snap = read_customer_identity(customer) if customer is not None else None
    field_evidence = _build_field_evidence(
        identity=identity,
        shipping=shipping,
        prep=prep,
        identity_snap=identity_snap,
        draft_locked=bool(active_draft and active_draft.merchant_edit_locked),
    )

    has_product = _has_product_signal(
        prep=prep,
        brain_state=bs,
        active_draft=active_draft,
        catalog=catalog,
    )
    has_total = _has_total_signal(
        prep=prep,
        active_draft=active_draft,
        catalog=catalog,
    )

    prefill = build_prefill_state(
        identity=identity,
        shipping=shipping,
        known_previous=known_previous,
        prep=prep,
        active_draft=active_draft,
        message=message,
        has_product=has_product,
        has_total=has_total,
    )

    shipping_locked = bool(active_draft and active_draft.merchant_edit_locked) or bool(
        prep.get("merchant_shipping_locked")
    )
    identity = enrich_identity_context(identity, missing_mode=prefill.identity_missing_mode)
    shipping = enrich_shipping_context(
        shipping,
        city_mode=prefill.shipping_city_mode,
        delivery_mode=prefill.shipping_delivery_mode,
        locked_by_merchant=shipping_locked,
        requires_merchant_review=prefill.requires_merchant_review,
    )

    ctx = OrderContext(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        customer_id=identity.customer_id or getattr(conversation, "customer_id", None),
        identity=identity,
        shipping=shipping,
        active_draft=active_draft,
        catalog_order=catalog,
        brain_order_prep=dict(prep),
        legacy_missing_fields=list(legacy_missing),
        shadow_missing_fields=[],
        field_evidence=field_evidence,
        build_source=build_source,
        divergence_flags={},
        prefill=prefill,
        known_previous_address=known_previous,
        shadow_missing_modes=dict(prefill.shadow_missing_modes),
    )

    shadow_missing = shadow_missing_fields_from_modes(prefill.shadow_missing_modes)
    divergence = compute_divergence_flags(legacy_missing, shadow_missing)

    from core.order_missing_fields_engine import compute_missing_fields  # noqa: PLC0415

    ctx_with_legacy = OrderContext(
        tenant_id=ctx.tenant_id,
        conversation_id=ctx.conversation_id,
        customer_id=ctx.customer_id,
        identity=ctx.identity,
        shipping=ctx.shipping,
        active_draft=ctx.active_draft,
        catalog_order=ctx.catalog_order,
        brain_order_prep=ctx.brain_order_prep,
        legacy_missing_fields=list(legacy_missing),
        shadow_missing_fields=shadow_missing,
        field_evidence=ctx.field_evidence,
        build_source=ctx.build_source,
        divergence_flags=divergence,
        prefill=ctx.prefill,
        known_previous_address=ctx.known_previous_address,
        shadow_missing_modes=ctx.shadow_missing_modes,
        missing_fields_result=None,
    )
    missing_result = compute_missing_fields(ctx_with_legacy)

    return OrderContext(
        tenant_id=ctx_with_legacy.tenant_id,
        conversation_id=ctx_with_legacy.conversation_id,
        customer_id=ctx_with_legacy.customer_id,
        identity=ctx_with_legacy.identity,
        shipping=ctx_with_legacy.shipping,
        active_draft=ctx_with_legacy.active_draft,
        catalog_order=ctx_with_legacy.catalog_order,
        brain_order_prep=ctx_with_legacy.brain_order_prep,
        legacy_missing_fields=ctx_with_legacy.legacy_missing_fields,
        shadow_missing_fields=ctx_with_legacy.shadow_missing_fields,
        field_evidence=ctx_with_legacy.field_evidence,
        build_source=ctx_with_legacy.build_source,
        divergence_flags=ctx_with_legacy.divergence_flags,
        prefill=ctx_with_legacy.prefill,
        known_previous_address=ctx_with_legacy.known_previous_address,
        shadow_missing_modes=ctx_with_legacy.shadow_missing_modes,
        missing_fields_result=missing_result,
    )


def log_order_context_shadow(ctx: OrderContext) -> None:
    """Structured shadow log — no full phone or street address."""
    identity = ctx.identity
    shipping = ctx.shipping
    draft = ctx.active_draft
    catalog = ctx.catalog_order
    has_total = _has_total_signal(
        prep=ctx.brain_order_prep,
        active_draft=draft,
        catalog=catalog,
    )
    logger.info(
        "[ORDER_CONTEXT_SHADOW] tenant=%s conversation=%s customer=%s phone=%s "
        "has_identity=%s identity_status=%s has_active_draft=%s has_catalog_order=%s "
        "line_items_count=%d has_total=%s has_shipping_city=%s has_delivery_address=%s "
        "legacy_missing_fields=%s shadow_missing_fields=%s divergence_flags=%s "
        "build_source=%s known_previous_address=%s",
        ctx.tenant_id,
        ctx.conversation_id,
        ctx.customer_id,
        mask_phone(identity.phone),
        bool(identity.display_name or identity.operational_name),
        identity.name_status or "-",
        draft is not None,
        catalog.has_catalog_order,
        len(draft.line_items) if draft else catalog.item_count,
        has_total,
        bool(shipping.city),
        shipping.accepted_delivery_address,
        ctx.legacy_missing_fields,
        ctx.shadow_missing_fields,
        ctx.divergence_flags,
        ctx.build_source,
        ctx.known_previous_address is not None,
    )


def maybe_log_order_context_shadow(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer: Any = None,
    phone: str = "",
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[OrderContext]:
    from core.config import ORDER_CONTEXT_SHADOW_ENABLED  # noqa: PLC0415

    if not ORDER_CONTEXT_SHADOW_ENABLED:
        return None
    try:
        ctx = build_order_context(
            db,
            tenant_id=tenant_id,
            conversation=conversation,
            customer=customer,
            phone=phone,
            brain_state=brain_state,
            inbound_metadata=inbound_metadata,
            build_source="whatsapp_webhook_shadow",
        )
        log_order_context_shadow(ctx)
        return ctx
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ORDER_CONTEXT_SHADOW] build failed tenant=%s conversation=%s",
            tenant_id,
            getattr(conversation, "id", None),
            exc_info=True,
        )
        return None


__all__ = [
    "ActiveDraftContext",
    "CatalogOrderSnapshot",
    "FieldEvidence",
    "OrderContext",
    "OrderIdentityContext",
    "ShippingContext",
    "build_order_context",
    "compute_divergence_flags",
    "compute_shadow_missing_fields",
    "log_order_context_shadow",
    "mask_phone",
    "maybe_log_order_context_shadow",
]
