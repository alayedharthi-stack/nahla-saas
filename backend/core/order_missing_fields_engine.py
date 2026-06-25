"""
core/order_missing_fields_engine.py
───────────────────────────────────
Phase D — unified missing-fields projection from ``OrderContext``.

Shadow-first rollout: compare against legacy missing fields without
replacing ``compute_wa_missing_fields`` until explicitly enabled.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.order_context_prefill import MODE_ASK, MODE_CONFIRM, MODE_EDIT_REQUESTED, MODE_SKIP
from core.wa_order_lifecycle import has_accepted_delivery_address

logger = logging.getLogger("nahla.order_missing_fields_engine")

MODE_BLOCKED = "blocked"
MODE_REVIEW = "review"
MODE_COMPUTE_PENDING = "compute_pending"

READINESS_DRAFT_INCOMPLETE = "draft_incomplete"
READINESS_COLLECTING_IDENTITY = "collecting_identity"
READINESS_CONFIRMING_SHIPPING = "confirming_shipping"
READINESS_COLLECTING_SHIPPING = "collecting_shipping"
READINESS_READY_FOR_PAYMENT = "ready_for_payment"
READINESS_AWAITING_PAYMENT = "awaiting_payment"
READINESS_AWAITING_MERCHANT_REVIEW = "awaiting_merchant_review"
READINESS_READY_FOR_CONFIRMATION = "ready_for_confirmation"

_ENGINE_FIELDS = (
    "product",
    "total",
    "name",
    "city",
    "delivery_address",
    "payment_method",
)


@dataclass(frozen=True)
class MissingFieldState:
    field: str
    mode: str
    reason: str
    source: str
    confidence: float
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MissingFieldsResult:
    missing_fields: List[str]
    missing_modes: Dict[str, str]
    blockers: List[str]
    readiness_state: str
    field_states: Dict[str, MissingFieldState]
    evidence_flags: Dict[str, bool]
    divergence_flags: Dict[str, bool]


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


def missing_fields_engine_shadow_enabled() -> bool:
    return _flag("ORDER_MISSING_FIELDS_ENGINE_SHADOW_ENABLED", "true")


def missing_fields_engine_enabled() -> bool:
    return _flag("ORDER_MISSING_FIELDS_ENGINE_ENABLED", "false")


def _prep_str(prep: Dict[str, Any], key: str) -> str:
    return str(prep.get(key) or "").strip()


def _line_items_total(items: list) -> Optional[float]:
    total = 0.0
    found = False
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        price = raw.get("price") or raw.get("item_price") or raw.get("unit_price")
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        qty_raw = raw.get("quantity") or raw.get("qty") or 1
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            qty = 1.0
        total += price_f * qty
        found = True
    return total if found else None


def _has_product(ctx: Any) -> Tuple[bool, str]:
    prep = ctx.brain_order_prep or {}
    if ctx.active_draft and ctx.active_draft.line_items:
        return True, "active_draft.line_items"
    if ctx.catalog_order.has_catalog_order and ctx.catalog_order.product_items:
        return True, "catalog_order.product_items"
    for key in ("line_items", "cart_items", "items"):
        raw = prep.get(key)
        if isinstance(raw, list) and raw:
            return True, f"order_prep.{key}"
    if _prep_str(prep, "product_id"):
        return True, "order_prep.product_id"
    return False, ""


def _has_total(ctx: Any, *, has_product: bool) -> Tuple[bool, str]:
    if ctx.active_draft and ctx.active_draft.total is not None:
        return True, "active_draft.total"
    if ctx.catalog_order.has_catalog_order and ctx.catalog_order.total_price is not None:
        return True, "catalog_order.total_price"
    prep = ctx.brain_order_prep or {}
    for key in ("total", "order_total", "catalog_total", "total_price", "catalog_checkout_total"):
        if prep.get(key) not in (None, ""):
            return True, f"order_prep.{key}"
    items: list = []
    if ctx.active_draft:
        items.extend(ctx.active_draft.line_items or [])
    items.extend(prep.get("line_items") or prep.get("cart_items") or [])
    if ctx.catalog_order.product_items:
        items.extend(ctx.catalog_order.product_items)
    if _line_items_total(items) is not None:
        return True, "line_items_sum"
    return False, "product_without_total" if has_product else ""


def _shipping_has_acceptable(snapshot: Any) -> bool:
    if snapshot is None:
        return False
    prep_like = {
        "google_maps_url": getattr(snapshot, "maps_url", "") or "",
        "short_address_code": getattr(snapshot, "short_address", "") or "",
        "latitude": getattr(snapshot, "latitude", None),
        "longitude": getattr(snapshot, "longitude", None),
    }
    return has_accepted_delivery_address(prep_like)


def _resolve_name_state(ctx: Any) -> MissingFieldState:
    identity = ctx.identity
    prefill = ctx.prefill
    locked = bool(identity.locked_by_merchant)

    if prefill.identity_missing_mode == MODE_EDIT_REQUESTED:
        return MissingFieldState(
            field="name",
            mode=MODE_EDIT_REQUESTED,
            reason="customer_requested_name_edit",
            source=identity.name_source or "customer_message",
            confidence=identity.confidence,
            evidence={"locked": locked},
        )
    if locked and identity.operational_name:
        return MissingFieldState(
            field="name",
            mode=MODE_SKIP,
            reason="merchant_locked_name",
            source="merchant_edit",
            confidence=1.0,
            evidence={"locked": True},
        )
    if identity.has_verified_name and identity.operational_name:
        return MissingFieldState(
            field="name",
            mode=MODE_SKIP,
            reason="verified_operational_name",
            source=identity.name_source or "customer",
            confidence=identity.confidence,
            evidence={"operational_name": identity.operational_name},
        )
    prep = ctx.brain_order_prep or {}
    first = _prep_str(prep, "customer_first_name") or str(
        getattr(identity, "first_name", "") or ""
    ).strip()
    last = _prep_str(prep, "customer_last_name") or str(
        getattr(identity, "last_name", "") or ""
    ).strip()
    if first and last:
        return MissingFieldState(
            field="name",
            mode=MODE_SKIP,
            reason="persisted_order_customer_name",
            source="order_customer_info",
            confidence=0.95,
            evidence={"customer_first_name": first, "customer_last_name": last},
        )
    if identity.has_proposed_name and not identity.has_verified_name:
        return MissingFieldState(
            field="name",
            mode=MODE_CONFIRM,
            reason="proposed_name_only",
            source="whatsapp_profile",
            confidence=identity.confidence,
            evidence={"display_name": identity.display_name},
        )
    if identity.operational_name:
        return MissingFieldState(
            field="name",
            mode=MODE_CONFIRM,
            reason="unverified_operational_name",
            source=identity.name_source or "inferred",
            confidence=identity.confidence,
            evidence={"operational_name": identity.operational_name},
        )
    return MissingFieldState(
        field="name",
        mode=MODE_ASK,
        reason="missing_name",
        source="none",
        confidence=0.0,
        evidence={},
    )


def _resolve_city_state(ctx: Any) -> MissingFieldState:
    shipping = ctx.shipping
    prefill = ctx.prefill
    previous = ctx.known_previous_address
    prep = ctx.brain_order_prep or {}

    if prefill.shipping_city_mode == MODE_EDIT_REQUESTED:
        if shipping.locked_by_merchant:
            return MissingFieldState(
                field="city",
                mode=MODE_REVIEW,
                reason="merchant_locked_edit_requested",
                source="merchant_edit",
                confidence=1.0,
                evidence={"locked": True, "requires_merchant_review": True},
            )
        return MissingFieldState(
            field="city",
            mode=MODE_EDIT_REQUESTED,
            reason="customer_requested_shipping_edit",
            source="customer_message",
            confidence=0.8,
            evidence={"locked": shipping.locked_by_merchant},
        )
    if shipping.city:
        mode = MODE_SKIP
        if shipping.locked_by_merchant:
            mode = MODE_SKIP
        return MissingFieldState(
            field="city",
            mode=mode,
            reason="current_shipping_city",
            source=shipping.source,
            confidence=shipping.confidence,
            evidence={"city": shipping.city, "locked": shipping.locked_by_merchant},
        )
    if previous and getattr(previous, "city", ""):
        return MissingFieldState(
            field="city",
            mode=MODE_CONFIRM,
            reason="known_previous_city",
            source=getattr(previous, "source", "") or "customer_addresses",
            confidence=getattr(previous, "confidence", 0.5),
            evidence={"city": previous.city},
        )
    if shipping.locked_by_merchant and prefill.requires_merchant_review:
        return MissingFieldState(
            field="city",
            mode=MODE_REVIEW,
            reason="merchant_locked_incomplete_city",
            source="merchant_edit",
            confidence=1.0,
            evidence={"locked": True, "requires_merchant_review": True},
        )
    return MissingFieldState(
        field="city",
        mode=MODE_ASK,
        reason="missing_city",
        source="none",
        confidence=0.0,
        evidence={},
    )


def _resolve_delivery_state(ctx: Any) -> MissingFieldState:
    shipping = ctx.shipping
    prefill = ctx.prefill
    previous = ctx.known_previous_address
    prep = ctx.brain_order_prep or {}

    if prefill.shipping_delivery_mode == MODE_EDIT_REQUESTED:
        if shipping.locked_by_merchant:
            return MissingFieldState(
                field="delivery_address",
                mode=MODE_REVIEW,
                reason="merchant_locked_edit_requested",
                source="merchant_edit",
                confidence=1.0,
                evidence={"locked": True, "requires_merchant_review": True},
            )
        return MissingFieldState(
            field="delivery_address",
            mode=MODE_EDIT_REQUESTED,
            reason="customer_requested_shipping_edit",
            source="customer_message",
            confidence=0.8,
            evidence={"locked": shipping.locked_by_merchant},
        )

    if shipping.accepted_delivery_address or _shipping_has_acceptable(shipping):
        return MissingFieldState(
            field="delivery_address",
            mode=MODE_SKIP,
            reason="current_accepted_delivery_address",
            source=shipping.source,
            confidence=shipping.confidence,
            evidence={
                "maps_url": bool(shipping.maps_url),
                "short_address": bool(shipping.short_address),
                "locked": shipping.locked_by_merchant,
            },
        )

    if prep.get("customer_confirmed_previous_address") and _shipping_has_acceptable(shipping):
        return MissingFieldState(
            field="delivery_address",
            mode=MODE_SKIP,
            reason="customer_confirmed_previous_address",
            source=str(prep.get("shipping_source") or "customer_confirmed_previous_address"),
            confidence=0.95,
            evidence={"confirmed": True},
        )

    if previous and _shipping_has_acceptable(previous):
        return MissingFieldState(
            field="delivery_address",
            mode=MODE_CONFIRM,
            reason="known_previous_delivery_address",
            source=getattr(previous, "source", "") or "customer_addresses",
            confidence=getattr(previous, "confidence", 0.5),
            evidence={
                "maps_url": bool(getattr(previous, "maps_url", "")),
                "short_address": bool(getattr(previous, "short_address", "")),
            },
        )

    if shipping.locked_by_merchant:
        return MissingFieldState(
            field="delivery_address",
            mode=MODE_REVIEW,
            reason="merchant_locked_incomplete_delivery",
            source="merchant_edit",
            confidence=1.0,
            evidence={"locked": True, "requires_merchant_review": prefill.requires_merchant_review},
        )

    return MissingFieldState(
        field="delivery_address",
        mode=MODE_ASK,
        reason="missing_delivery_address",
        source="none",
        confidence=0.0,
        evidence={},
    )


def _resolve_payment_state(ctx: Any, *, prerequisites_met: bool) -> MissingFieldState:
    prep = ctx.brain_order_prep or {}
    if prep.get("payment_confirmed") or prep.get("payment_verified"):
        return MissingFieldState(
            field="payment_method",
            mode=MODE_SKIP,
            reason="payment_verified",
            source="payment_evidence",
            confidence=1.0,
            evidence={"verified": True},
        )
    if prep.get("payment_receipt_received") or prep.get("payment_submission_received"):
        return MissingFieldState(
            field="payment_method",
            mode=MODE_REVIEW,
            reason="payment_submitted_awaiting_merchant_verification",
            source="payment_submission",
            confidence=0.7,
            evidence={"awaiting_merchant_verification": True},
        )
    if _prep_str(prep, "payment_method"):
        return MissingFieldState(
            field="payment_method",
            mode=MODE_SKIP,
            reason="payment_method_selected",
            source="order_prep",
            confidence=0.9,
            evidence={"payment_method": prep.get("payment_method")},
        )
    if not prerequisites_met:
        return MissingFieldState(
            field="payment_method",
            mode=MODE_BLOCKED,
            reason="checkout_prerequisites_incomplete",
            source="policy",
            confidence=1.0,
            evidence={"blocked_until": "product,name,city,delivery_address"},
        )
    return MissingFieldState(
        field="payment_method",
        mode=MODE_ASK,
        reason="payment_method_required",
        source="policy",
        confidence=0.8,
        evidence={},
    )


def _field_is_missing(mode: str) -> bool:
    return mode in {MODE_ASK, MODE_CONFIRM, MODE_EDIT_REQUESTED, MODE_REVIEW, MODE_COMPUTE_PENDING}


def _resolve_readiness(field_states: Dict[str, MissingFieldState]) -> str:
    if field_states["payment_method"].mode == MODE_REVIEW:
        if field_states["payment_method"].evidence.get("awaiting_merchant_verification"):
            return READINESS_AWAITING_MERCHANT_REVIEW
    if any(
        field_states[f].evidence.get("requires_merchant_review")
        for f in ("city", "delivery_address", "name")
        if f in field_states
    ):
        return READINESS_AWAITING_MERCHANT_REVIEW

    product_mode = field_states["product"].mode
    total_mode = field_states["total"].mode
    if product_mode != MODE_SKIP or total_mode in {MODE_ASK, MODE_REVIEW, MODE_COMPUTE_PENDING}:
        return READINESS_DRAFT_INCOMPLETE

    name_mode = field_states["name"].mode
    if name_mode in {MODE_ASK, MODE_EDIT_REQUESTED}:
        return READINESS_COLLECTING_IDENTITY
    if name_mode == MODE_CONFIRM:
        return READINESS_COLLECTING_IDENTITY

    city_mode = field_states["city"].mode
    delivery_mode = field_states["delivery_address"].mode
    if city_mode == MODE_CONFIRM or delivery_mode == MODE_CONFIRM:
        return READINESS_CONFIRMING_SHIPPING
    if city_mode in {MODE_ASK, MODE_EDIT_REQUESTED} or delivery_mode in {MODE_ASK, MODE_EDIT_REQUESTED}:
        return READINESS_COLLECTING_SHIPPING

    if field_states["payment_method"].mode == MODE_ASK:
        return READINESS_READY_FOR_PAYMENT
    if field_states["payment_method"].mode == MODE_REVIEW:
        return READINESS_AWAITING_PAYMENT
    if all(field_states[f].mode == MODE_SKIP for f in _ENGINE_FIELDS):
        return READINESS_READY_FOR_CONFIRMATION
    return READINESS_DRAFT_INCOMPLETE


def compute_missing_fields(ctx: Any) -> MissingFieldsResult:
    """Compute unified missing-field projection from a built ``OrderContext``."""
    has_product, product_source = _has_product(ctx)
    has_total, total_source = _has_total(ctx, has_product=has_product)

    if has_product:
        product_state = MissingFieldState(
            field="product",
            mode=MODE_SKIP,
            reason="product_present",
            source=product_source,
            confidence=1.0,
            evidence={"source": product_source},
        )
    else:
        product_state = MissingFieldState(
            field="product",
            mode=MODE_ASK,
            reason="missing_product",
            source="none",
            confidence=0.0,
            evidence={},
        )

    if has_total:
        total_state = MissingFieldState(
            field="total",
            mode=MODE_SKIP,
            reason="total_present",
            source=total_source,
            confidence=1.0,
            evidence={"source": total_source},
        )
    elif has_product:
        total_state = MissingFieldState(
            field="total",
            mode=MODE_COMPUTE_PENDING,
            reason="product_without_total",
            source=total_source or "line_items",
            confidence=0.6,
            evidence={"review": True},
        )
    else:
        total_state = MissingFieldState(
            field="total",
            mode=MODE_ASK,
            reason="missing_total",
            source="none",
            confidence=0.0,
            evidence={},
        )

    name_state = _resolve_name_state(ctx)
    city_state = _resolve_city_state(ctx)
    delivery_state = _resolve_delivery_state(ctx)

    prerequisites_met = all(
        state.mode == MODE_SKIP
        for state in (product_state, name_state, city_state, delivery_state)
    ) and total_state.mode in {MODE_SKIP, MODE_COMPUTE_PENDING}
    payment_state = _resolve_payment_state(ctx, prerequisites_met=prerequisites_met)

    field_states = {
        "product": product_state,
        "total": total_state,
        "name": name_state,
        "city": city_state,
        "delivery_address": delivery_state,
        "payment_method": payment_state,
    }

    missing_modes = {f: s.mode for f, s in field_states.items()}
    missing_fields = [f for f, s in field_states.items() if _field_is_missing(s.mode)]

    blockers: List[str] = []
    if payment_state.mode == MODE_BLOCKED:
        blockers.append("checkout_prerequisites_incomplete")
    if ctx.prefill.requires_merchant_review:
        blockers.append("requires_merchant_review")
    if ctx.prefill.locked_field_edit_requested:
        blockers.append("locked_field_edit_requested")

    evidence_flags = {
        "has_product": has_product,
        "has_total": has_total,
        "has_verified_name": bool(ctx.identity.has_verified_name),
        "has_accepted_delivery_address": bool(ctx.shipping.accepted_delivery_address),
        "has_known_previous_address": ctx.known_previous_address is not None,
        "merchant_edit_locked": bool(ctx.active_draft and ctx.active_draft.merchant_edit_locked),
    }

    readiness = _resolve_readiness(field_states)
    divergence = compute_divergence_vs_legacy(ctx.legacy_missing_fields, field_states)

    return MissingFieldsResult(
        missing_fields=missing_fields,
        missing_modes=missing_modes,
        blockers=blockers,
        readiness_state=readiness,
        field_states=field_states,
        evidence_flags=evidence_flags,
        divergence_flags=divergence,
    )


def _legacy_to_engine_fields(legacy: List[str]) -> set:
    fields = set()
    for raw in legacy or []:
        key = str(raw or "").strip()
        if key in {"customer_first_name", "customer_last_name", "customer_name"}:
            fields.add("name")
        elif key:
            fields.add(key)
    return fields


def _engine_missing_set(field_states: Dict[str, MissingFieldState]) -> set:
    out = set()
    for fname, state in field_states.items():
        if _field_is_missing(state.mode):
            out.add(fname)
    return out


def compute_divergence_vs_legacy(
    legacy_missing: List[str],
    field_states: Dict[str, MissingFieldState],
) -> Dict[str, bool]:
    legacy_set = _legacy_to_engine_fields(legacy_missing)
    engine_set = _engine_missing_set(field_states)
    return {
        "missing_fields_differ": legacy_set != engine_set,
        "legacy_only": bool(legacy_set - engine_set),
        "engine_only": bool(engine_set - legacy_set),
        "name_divergence": ("name" in engine_set) != (
            "customer_first_name" in (legacy_missing or [])
            or "customer_last_name" in (legacy_missing or [])
        ),
        "city_divergence": ("city" in engine_set) != ("city" in legacy_set),
        "delivery_address_divergence": ("delivery_address" in engine_set)
        != ("delivery_address" in legacy_set),
        "product_divergence": ("product" in engine_set) != ("product" in legacy_set),
        "total_divergence": ("total" in engine_set),
    }


def to_legacy_missing_fields(result: MissingFieldsResult) -> List[str]:
    """Map engine canonical fields to legacy bridge missing_fields keys."""
    legacy: List[str] = []
    for fname in result.missing_fields:
        if fname == "name":
            legacy.extend(["customer_first_name", "customer_last_name"])
        elif fname == "product":
            legacy.append("product")
        elif fname in {"city", "delivery_address"}:
            legacy.append(fname)
        # total/payment_method stay engine-only metadata unless bridge adopts later
    return legacy


def missing_fields_result_to_api_dict(result: MissingFieldsResult) -> Dict[str, Any]:
    return {
        "available": True,
        "reason": "",
        "missing_fields": list(result.missing_fields),
        "missing_modes": dict(result.missing_modes),
        "blockers": list(result.blockers),
        "readiness_state": result.readiness_state,
        "field_states": {
            key: {
                "field": state.field,
                "mode": state.mode,
                "reason": state.reason,
                "source": state.source,
                "confidence": state.confidence,
                "evidence": dict(state.evidence),
            }
            for key, state in result.field_states.items()
        },
        "evidence_flags": dict(result.evidence_flags),
        "divergence_flags": dict(result.divergence_flags),
    }


def missing_fields_engine_unavailable_dict(reason: str) -> Dict[str, Any]:
    """Explicit unavailable payload — never omit the key silently on WA detail."""
    return {
        "available": False,
        "reason": str(reason or "unavailable").strip() or "unavailable",
        "missing_fields": [],
        "missing_modes": {},
        "blockers": [],
        "readiness_state": "unavailable",
        "field_states": {},
        "evidence_flags": {},
        "divergence_flags": {},
    }


def augment_divergence_with_confirm_blockers(
    result: MissingFieldsResult,
    *,
    confirm_blockers: List[str],
) -> MissingFieldsResult:
    """Compare engine projection with dashboard ``confirm_blockers`` (stale metadata)."""
    flags = dict(result.divergence_flags)
    legacy_set = set(str(x).strip() for x in (confirm_blockers or []) if str(x).strip())
    engine_legacy = set(to_legacy_missing_fields(result))
    flags["confirm_blockers_differ"] = legacy_set != engine_legacy
    flags["confirm_blockers_stale"] = bool(legacy_set - engine_legacy)
    name_state = result.field_states.get("name")
    flags["confirm_blockers_name_stale"] = bool(
        ("customer_first_name" in legacy_set or "customer_last_name" in legacy_set)
        and name_state is not None
        and name_state.mode == MODE_SKIP
    )
    return MissingFieldsResult(
        missing_fields=list(result.missing_fields),
        missing_modes=dict(result.missing_modes),
        blockers=list(result.blockers),
        readiness_state=result.readiness_state,
        field_states=dict(result.field_states),
        evidence_flags=dict(result.evidence_flags),
        divergence_flags=flags,
    )


def log_missing_fields_engine_detail(
    *,
    order_id: Any,
    tenant_id: int,
    available: bool,
    reason: str = "",
    result: Optional[MissingFieldsResult] = None,
    legacy_missing: Optional[List[str]] = None,
    confirm_blockers: Optional[List[str]] = None,
    build_source: str = "",
) -> None:
    logger.info(
        "[MISSING_FIELDS_ENGINE_DETAIL] order_id=%s available=%s reason=%s tenant=%s "
        "enabled=%s shadow=%s source=%s readiness=%s missing=%s legacy=%s "
        "confirm_blockers=%s divergence=%s",
        order_id,
        available,
        reason or "-",
        tenant_id,
        missing_fields_engine_enabled(),
        missing_fields_engine_shadow_enabled(),
        build_source,
        result.readiness_state if result is not None else "unavailable",
        result.missing_fields if result is not None else [],
        legacy_missing or [],
        confirm_blockers or [],
        result.divergence_flags if result is not None else {},
    )
    if (
        available
        and result is not None
        and missing_fields_engine_shadow_enabled()
        and result.divergence_flags.get("missing_fields_differ")
    ):
        logger.info(
            "[MISSING_FIELDS_ENGINE_SHADOW] order_id=%s legacy=%s engine=%s divergence=%s",
            order_id,
            legacy_missing or [],
            result.missing_fields,
            result.divergence_flags,
        )


def engine_result_to_v2_missing_fields(result: MissingFieldsResult) -> List[str]:
    """Map engine canonical fields to OrderFlowV2 slot names."""
    missing: List[str] = []
    modes = result.missing_modes
    if modes.get("product") in {MODE_ASK, MODE_REVIEW, MODE_COMPUTE_PENDING}:
        missing.append("product")
    if modes.get("name") in {MODE_ASK, MODE_EDIT_REQUESTED, MODE_CONFIRM}:
        missing.append("customer_name")
    if modes.get("city") in {MODE_ASK, MODE_EDIT_REQUESTED, MODE_CONFIRM}:
        missing.append("city")
    if modes.get("delivery_address") in {
        MODE_ASK,
        MODE_EDIT_REQUESTED,
        MODE_CONFIRM,
    }:
        missing.append("delivery_address")
    if modes.get("payment_method") in {MODE_ASK}:
        missing.append("payment_method")
    return missing


def resolve_flow_missing_fields(
    order_prep: Dict[str, Any],
    *,
    brain_state: Optional[Dict[str, Any]] = None,
    whatsapp_phone: Optional[str] = None,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], Optional[MissingFieldsResult]]:
    """
    Checkout missing fields for Brain/OrderFlow.

    When the engine flag is enabled, recompute from ``OrderContext`` instead of
    trusting stale ``order_prep.missing_fields``.
    """
    if not missing_fields_engine_enabled():
        return [], None
    if db is None or tenant_id is None:
        return [], None
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415

        ctx = build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            phone=str(whatsapp_phone or ""),
            brain_state=brain_state,
            inbound_metadata=inbound_metadata,
            build_source="order_flow_missing_fields",
        )
        result = ctx.missing_fields_result
        if result is None:
            return [], None
        return engine_result_to_v2_missing_fields(result), result
    except Exception:  # noqa: BLE001
        logger.exception(
            "[MISSING_FIELDS_ENGINE] flow resolve failed tenant=%s",
            tenant_id,
        )
        return [], None


def apply_missing_fields_engine_to_metadata(
    base_meta: Dict[str, Any],
    *,
    result: MissingFieldsResult,
    legacy_missing: List[str],
) -> Dict[str, Any]:
    """Shadow and/or enabled merge into order metadata."""
    payload = missing_fields_result_to_api_dict(result)
    base_meta["missing_fields_engine"] = payload
    base_meta["missing_fields_engine_readiness"] = result.readiness_state
    base_meta["missing_fields_engine_divergence"] = dict(result.divergence_flags)

    if missing_fields_engine_shadow_enabled():
        logger.info(
            "[MISSING_FIELDS_ENGINE_SHADOW] legacy=%s engine=%s readiness=%s divergence=%s",
            legacy_missing,
            result.missing_fields,
            result.readiness_state,
            result.divergence_flags,
        )

    if missing_fields_engine_enabled():
        base_meta["missing_fields"] = to_legacy_missing_fields(result)
        base_meta["missing_fields_source"] = "missing_fields_engine"
    return base_meta


__all__ = [
    "MissingFieldState",
    "MissingFieldsResult",
    "apply_missing_fields_engine_to_metadata",
    "augment_divergence_with_confirm_blockers",
    "compute_divergence_vs_legacy",
    "compute_missing_fields",
    "engine_result_to_v2_missing_fields",
    "log_missing_fields_engine_detail",
    "missing_fields_engine_enabled",
    "missing_fields_engine_shadow_enabled",
    "missing_fields_engine_unavailable_dict",
    "missing_fields_result_to_api_dict",
    "resolve_flow_missing_fields",
    "to_legacy_missing_fields",
]
