"""
brain/order_context_gate.py
───────────────────────────
Order-fulfillment context gate — block product discovery during active orders.

Production regression (May 2026): customers mid-checkout sent a Google Maps
link + "أبغى الطلبية تجي الموقع ذا" and the brain ran catalog search /
top_products instead of attaching the location to the active order.

Invariants (tenant-agnostic, persisted-state driven — NOT history window):

  1. Fulfillment update: map / location / delivery phrases during active
     order → ``ACTION_ORDER_CONTEXT_UPDATE``.

  2. Fulfillment lock: while checkout / fulfillment session is active,
     suppress product discovery (top_products, replay, addons, safety nets,
     catalog preload) unless the customer explicitly changes topic with strong
     commerce intent ("أبي منتج ثاني", "ورني العروض", …).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from .decision.actions import (
    ACTION_LLM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from .state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING
from .types import BrainContext, Decision

logger = logging.getLogger("nahla.brain.order_context_gate")

FULFILLMENT_LOCATION = "location_update"
FULFILLMENT_DELIVERY_SWITCH = "pickup_to_delivery"
FULFILLMENT_SHIPPING_INTENT = "shipping_intent"

# order_prep.order_status funnel markers (free-form but stable in brain).
_AWAITING_FULFILLMENT_STATUSES = frozenset({
    "awaiting_product",
    "awaiting_address",
    "awaiting_payment",
    "awaiting_receipt",
    "awaiting_review",
    "under_review",
    "pending_review",
    "payment_pending",
    "awaiting_payment_receipt",
})

_ADDRESS_MISSING_FIELDS = frozenset({
    "address",
    "address_location",
    "address_line",
    "short_address_code",
    "google_maps_url",
    "city",
    "district",
    "street",
    "postal_code",
})

_FULFILLMENT_PHRASES: tuple[str, ...] = (
    "موقعي",
    "الموقع ذا",
    "هذا الموقع",
    "الموقع هذا",
    "تجي الموقع",
    "تيجي الموقع",
    "تجيني الموقع",
    "تيجيني الموقع",
    "تجي لي",
    "تيجي لي",
    "تجي لموقع",
    "تيجي لموقع",
    "الطلبيه تجي",
    "الطلبية تجي",
    "طلبيتي تجي",
    "ابغى الطلبيه",
    "ابغى الطلبية",
    "أبغى الطلبية",
    "أبغى الطلبيه",
    "وصلوها",
    "وصلها",
    "وصلها هنا",
    "وصلوها هنا",
    "استلام من الموقع",
    "توصيل للموقع",
    "توصيل لموقع",
    "التوصيل للموقع",
    "ارسلوها للموقع",
    "أرسلوها للموقع",
)

_PICKUP_TO_DELIVERY_PHRASES: tuple[str, ...] = (
    "غيرت إلى توصيل",
    "غيرت الى توصيل",
    "غيرت للتوصيل",
    "ابغى توصيل",
    "أبغى توصيل",
    "ابي توصيل",
    "أبي توصيل",
    "اريد توصيل",
    "أريد توصيل",
    "بدل استلام",
    "بدل الاستلام",
    "مو توصيل",
    "مش توصيل",
)

_ORDER_PHRASE_PRODUCT_DISQUALIFIERS: tuple[str, ...] = (
    "الطلبيه",
    "الطلبية",
    "طلبيتي",
    "طلبي",
    "تجي",
    "توصل",
    "وصل",
    "الموقع",
    "موقعي",
    "هنا",
    "استلام",
    "توصيل",
    "delivery",
)

# Strong explicit topic-change — unlocks discovery during fulfillment lock.
_EXPLICIT_COMMERCE_TOPIC_CHANGE: tuple[str, ...] = (
    "منتج ثاني",
    "منتج آخر",
    "منتج اخر",
    "منتجات ثانيه",
    "منتجات ثانية",
    "منتجات أخرى",
    "منتجات اخرى",
    "منتجات ثانيه",
    "أضف معي",
    "اضف معي",
    "ضيف معي",
    "ضيفلي",
    "ورني العروض",
    "وريني العروض",
    "اعرض العروض",
    "أعرض العروض",
    "أبي أشوف منتجات",
    "ابي اشوف منتجات",
    "أبغى أشوف منتجات",
    "ابغى اشوف منتجات",
    "منتجات أخرى",
    "منتجات اخرى",
    "شي ثاني",
    "شيء ثاني",
    "اطلب شي ثاني",
    "أطلب شي ثاني",
    "show me other products",
    "other products",
    "browse products",
)

_CHECKOUT_SLOT_KEYS = frozenset({
    "customer_first_name",
    "customer_last_name",
    "customer_name",
    "full_name",
    "city",
    "short_address_code",
    "google_maps_url",
    "location_url",
    "address",
    "address_line",
    "street",
    "district",
    "postal_code",
    "zip_code",
    "building_number",
    "additional_number",
    "latitude",
    "longitude",
})


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\u0621-\u064Aa-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _order_prep_has_progress(op: Any) -> bool:
    if op is None:
        return False
    return bool(
        str(getattr(op, "product_id", "") or "").strip()
        or str(getattr(op, "customer_first_name", "") or "").strip()
        or str(getattr(op, "city", "") or "").strip()
        or str(getattr(op, "short_address_code", "") or "").strip()
        or str(getattr(op, "google_maps_url", "") or "").strip()
        or bool(getattr(op, "missing_fields", None))
        or getattr(op, "awaiting_payment_receipt", False)
        or getattr(op, "payment_receipt_received", False)
        or str(getattr(op, "order_status", "") or "").strip()
    )


def _order_prep_dict_has_progress(raw: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(raw, dict):
        return False
    try:
        from .types import OrderPreparationState  # noqa: PLC0415

        return _order_prep_has_progress(OrderPreparationState.from_dict(raw))
    except Exception:  # noqa: BLE001
        return bool(raw.get("product_id") or raw.get("customer_first_name"))


def _awaiting_fulfillment_fields(op: Any) -> bool:
    if op is None:
        return False
    if getattr(op, "awaiting_payment_receipt", False):
        return True
    if getattr(op, "awaiting_variant_choice", False):
        return True
    if getattr(op, "awaiting_option_confirmation", False):
        return True
    missing = {str(x).strip().lower() for x in (getattr(op, "missing_fields", None) or []) if x}
    if missing & _ADDRESS_MISSING_FIELDS:
        return True
    status = _normalize_ar(str(getattr(op, "order_status", "") or ""))
    if status in {_normalize_ar(s) for s in _AWAITING_FULFILLMENT_STATUSES}:
        return True
    # Name + product pinned but no deliverable address yet.
    has_product = bool(str(getattr(op, "product_id", "") or "").strip())
    has_name = bool(str(getattr(op, "customer_first_name", "") or "").strip())
    has_address = bool(
        str(getattr(op, "city", "") or "").strip()
        or str(getattr(op, "short_address_code", "") or "").strip()
        or str(getattr(op, "google_maps_url", "") or "").strip()
    )
    if has_product and has_name and not has_address:
        return True
    return False


def _structured_pre_ship_locked(bundle: Optional[Dict[str, Any]]) -> bool:
    bundle = bundle or {}
    try:
        from core.active_order_context import (  # noqa: PLC0415
            is_pre_ship_canonical,
            structured_indicates_post_order,
        )

        if not structured_indicates_post_order(bundle):
            return False
        ctx_obj = bundle.get("active_order_context") or {}
        status = str(ctx_obj.get("order_status") or "")
        return is_pre_ship_canonical(status)
    except Exception:  # noqa: BLE001
        return False


def is_fulfillment_session_locked(ctx: BrainContext) -> bool:
    """True when checkout / fulfillment must not reopen product discovery."""
    state = ctx.state
    op = getattr(state, "order_prep", None)

    if _order_prep_has_progress(op):
        return True

    if _awaiting_fulfillment_fields(op):
        return True

    if str(getattr(state, "draft_order_id", "") or "").strip():
        return True

    if state.stage in (STAGE_ORDERING, STAGE_DECIDING, STAGE_CHECKOUT):
        if state.current_product_focus or _order_prep_has_progress(op):
            return True

    if _structured_pre_ship_locked(getattr(ctx, "commerce_bundle", None) or {}):
        return True

    return False


def has_active_order_context(ctx: BrainContext) -> bool:
    """Alias for fulfillment lock — active order survives quiet gaps via state."""
    return is_fulfillment_session_locked(ctx)


def has_explicit_commerce_topic_change(message: str) -> bool:
    """Customer explicitly asks to browse / add another product."""
    norm = _normalize_ar(message or "")
    if not norm:
        return False
    return any(_normalize_ar(p) in norm for p in _EXPLICIT_COMMERCE_TOPIC_CHANGE)


def fulfillment_lock_reason(ctx: BrainContext) -> Optional[str]:
    if not is_fulfillment_session_locked(ctx):
        return None
    op = getattr(ctx.state, "order_prep", None)
    if getattr(op, "awaiting_payment_receipt", False):
        return "awaiting_payment_receipt"
    if getattr(op, "awaiting_variant_choice", False):
        return "awaiting_variant_choice"
    if getattr(op, "missing_fields", None):
        return "missing_fields"
    if str(getattr(op, "order_status", "") or "").strip():
        return f"order_status={getattr(op, 'order_status', '')}"
    if _structured_pre_ship_locked(getattr(ctx, "commerce_bundle", None) or {}):
        return "structured_pre_ship_order"
    if _order_prep_has_progress(op):
        return "order_prep_progress"
    if str(getattr(ctx.state, "draft_order_id", "") or "").strip():
        return "draft_order_id"
    return "fulfillment_session"


def _extract_address_signals(message: str) -> Dict[str, Any]:
    try:
        from services.address_resolution import extract_address_signals  # noqa: PLC0415

        return dict(extract_address_signals(message or "") or {})
    except Exception:  # noqa: BLE001
        return {}


def detect_fulfillment_update(
    message: str,
    intent_slots: Optional[Dict[str, Any]] = None,
    *,
    intent_name: Optional[str] = None,
) -> Optional[str]:
    """Return fulfillment kind or ``None`` when message is not order-location."""
    msg = message or ""
    slots = dict(intent_slots or {})
    norm = _normalize_ar(msg)
    signals = _extract_address_signals(msg)

    has_maps = bool(
        signals.get("google_maps_url")
        or slots.get("google_maps_url")
        or slots.get("location_url")
    )
    has_short = bool(signals.get("short_address_code") or slots.get("short_address_code"))
    has_coords = bool(
        signals.get("latitude")
        or slots.get("latitude")
        or signals.get("longitude")
        or slots.get("longitude")
    )

    if any(_normalize_ar(p) in norm for p in _PICKUP_TO_DELIVERY_PHRASES):
        return FULFILLMENT_DELIVERY_SWITCH

    if has_maps or has_short or has_coords:
        return FULFILLMENT_LOCATION

    if any(_normalize_ar(p) in norm for p in _FULFILLMENT_PHRASES):
        return FULFILLMENT_SHIPPING_INTENT

    if intent_name == "ask_shipping" and (
        has_maps or "موقع" in norm or "عنوان" in norm or "توصيل" in norm
    ):
        return FULFILLMENT_SHIPPING_INTENT

    return None


def is_order_fulfillment_product_query(extracted_query: str) -> bool:
    """True when an order-prefix regex captured a fulfillment phrase, not a SKU."""
    norm = _normalize_ar(extracted_query or "")
    if not norm:
        return False
    return any(marker in norm for marker in _ORDER_PHRASE_PRODUCT_DISQUALIFIERS)


def should_block_product_discovery(ctx: BrainContext, message: Optional[str] = None) -> bool:
    """Block catalog search / recommendations during fulfillment session."""
    msg = message if message is not None else (ctx.message or "")

    if has_explicit_commerce_topic_change(msg):
        return False

    if not is_fulfillment_session_locked(ctx):
        return False

    return True


def should_suppress_product_escalation(
    *,
    message: str = "",
    brain_state: Optional[Dict[str, Any]] = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    intent_name: Optional[str] = None,
) -> bool:
    """Webhook / post-process helper using persisted brain_state only."""
    try:
        from .types import (  # noqa: PLC0415
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
        )

        state = MerchantConversationState.from_dict(dict(brain_state or {}))
        ctx = BrainContext(
            tenant_id=0,
            customer_phone="",
            message=message or "",
            intent=Intent(
                name=intent_name or "general",
                confidence=0.5,
                raw_message=message or "",
            ),
            state=state,
            facts=CommerceFacts(),
            commerce_bundle=commerce_bundle or {},
        )
        return should_block_product_discovery(ctx, message)
    except Exception:  # noqa: BLE001
        if has_explicit_commerce_topic_change(message):
            return False
        return _order_prep_dict_has_progress((brain_state or {}).get("order_prep"))


def should_skip_catalog_preload(
    *,
    message: str,
    state: Any,
    intent: Any,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> bool:
    """Pipeline helper — skip ``build_merchant_context`` catalog preload."""
    try:
        from .types import BrainContext, CommerceFacts, Intent  # noqa: PLC0415

        ctx = BrainContext(
            tenant_id=0,
            customer_phone="",
            message=message or "",
            intent=intent if isinstance(intent, Intent) else Intent(name="general", confidence=0.5),
            state=state,
            facts=CommerceFacts(),
            commerce_bundle=commerce_bundle or {},
        )
        return should_block_product_discovery(ctx, message)
    except Exception:  # noqa: BLE001
        return False


def _find_product_by_external_id(
    external_id: str,
    *candidate_lists: Any,
) -> Optional[Dict[str, Any]]:
    needle = str(external_id or "").strip().lower()
    if not needle:
        return None
    for cands in candidate_lists:
        for prod in (cands or []):
            ext = str((prod or {}).get("external_id") or "").strip().lower()
            if ext and ext == needle:
                return dict(prod)
    return None


def _resolve_product_for_update(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    state = ctx.state
    if state.current_product_focus:
        return dict(state.current_product_focus)

    op = getattr(state, "order_prep", None)
    prep_id = str(getattr(op, "product_id", "") or "").strip()
    if prep_id:
        recovered = _find_product_by_external_id(
            prep_id,
            state.last_search_candidates or [],
            state.last_recommended_products or [],
        )
        if recovered:
            return recovered
        return {"external_id": prep_id, "title": prep_id}

    bundle = getattr(ctx, "commerce_bundle", None) or {}
    ctx_obj = bundle.get("active_order_context") or {}
    ext = str(ctx_obj.get("external_id") or "").strip()
    if ext:
        return {"external_id": ext, "title": str(ctx_obj.get("product_summary") or ext)}

    return None


def _collect_fulfillment_slots(
    message: str,
    intent_slots: Optional[Dict[str, Any]],
    signals: Dict[str, Any],
) -> Dict[str, Any]:
    slots = dict(intent_slots or {})
    out: Dict[str, Any] = {}
    for key in _CHECKOUT_SLOT_KEYS:
        val = slots.get(key)
        if val:
            out[key] = val
    if signals.get("google_maps_url") and not out.get("google_maps_url"):
        out["google_maps_url"] = signals["google_maps_url"]
    if signals.get("short_address_code") and not out.get("short_address_code"):
        out["short_address_code"] = signals["short_address_code"]
    if signals.get("latitude") is not None and "latitude" not in out:
        out["latitude"] = signals["latitude"]
    if signals.get("longitude") is not None and "longitude" not in out:
        out["longitude"] = signals["longitude"]
    try:
        from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots  # noqa: PLC0415

        extracted = extract_ordering_slots(message or "") or {}
        for key in _CHECKOUT_SLOT_KEYS:
            if extracted.get(key) and not out.get(key):
                out[key] = extracted[key]
    except Exception:  # noqa: BLE001
        pass
    return out


def try_order_context_update_decision(ctx: BrainContext) -> Optional[Decision]:
    """Return ``ACTION_ORDER_CONTEXT_UPDATE`` or ``None``."""
    if not has_active_order_context(ctx):
        return None

    msg = ctx.message or ""
    intent_slots = getattr(ctx.intent, "slots", None) or {}
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    kind = detect_fulfillment_update(msg, intent_slots, intent_name=intent_name)

    signals = _extract_address_signals(msg)
    if not kind and (signals.get("google_maps_url") or signals.get("short_address_code")):
        kind = FULFILLMENT_LOCATION

    if not kind:
        return None

    product = _resolve_product_for_update(ctx)
    fulfillment_slots = _collect_fulfillment_slots(msg, intent_slots, signals)

    logger.info(
        "[ORDER_CONTEXT_UPDATE] tenant=%s kind=%s product=%r maps=%s "
        "short_code=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        kind,
        (product or {}).get("title"),
        bool(fulfillment_slots.get("google_maps_url")),
        bool(fulfillment_slots.get("short_address_code")),
        msg[:80],
    )

    args: Dict[str, Any] = {
        "order_context_update": True,
        "fulfillment_kind": kind,
        **fulfillment_slots,
    }
    if product:
        args["product"] = product
        args["forced_product"] = product

    return Decision(
        action=ACTION_ORDER_CONTEXT_UPDATE,
        args=args,
        reason=f"active order fulfillment update ({kind})",
        confidence=0.96,
    )


def try_fulfillment_lock_continuation(ctx: BrainContext) -> Optional[Decision]:
    """When discovery is locked, keep the checkout funnel alive."""
    if not is_fulfillment_session_locked(ctx):
        return None
    if has_explicit_commerce_topic_change(ctx.message or ""):
        return None

    try:
        from .state.state_relevance import (  # noqa: PLC0415
            log_state_resurrection_blocked,
            should_block_workflow_resume,
            validate_state_relevance,
        )
        _verdict = getattr(ctx, "state_relevance", None) or validate_state_relevance(ctx)
        if should_block_workflow_resume("active_fulfillment", _verdict):
            log_state_resurrection_blocked(
                tenant_id=getattr(ctx, "tenant_id", None),
                blocked_state="active_fulfillment",
                reason="semantic_mismatch",
                preview=(ctx.message or "")[:80],
                intent_hint=_verdict.current_intent_hint,
            )
            return None
    except Exception:  # noqa: BLE001
        pass

    product = _resolve_product_for_update(ctx)
    if product:
        logger.info(
            "[FULFILLMENT_LOCK] tenant=%s continue_checkout product=%r reason=%s",
            getattr(ctx, "tenant_id", None),
            product.get("title"),
            fulfillment_lock_reason(ctx),
        )
        return Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "product": product,
                "forced_product": product,
                "source": "fulfillment_lock_continuation",
                "fulfillment_lock": True,
            },
            reason=f"fulfillment lock — continue order ({fulfillment_lock_reason(ctx)})",
            confidence=0.91,
        )

    op = getattr(ctx.state, "order_prep", None)
    if _order_prep_has_progress(op):
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "order_recovery",
                "fulfillment_lock": True,
            },
            reason="fulfillment lock — order_prep present, recover funnel via LLM",
            confidence=0.82,
        )
    return None


def log_order_context_block(*, tenant_id: Any, reason: str, preview: str = "") -> None:
    try:
        logger.info(
            "[ORDER_CONTEXT_GATE] tenant=%s block_product_discovery=1 reason=%s preview=%r",
            tenant_id,
            reason,
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_fulfillment_lock(*, tenant_id: Any, reason: str, preview: str = "") -> None:
    try:
        logger.info(
            "[FULFILLMENT_LOCK] tenant=%s locked=1 reason=%s preview=%r",
            tenant_id,
            reason,
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "FULFILLMENT_DELIVERY_SWITCH",
    "FULFILLMENT_LOCATION",
    "FULFILLMENT_SHIPPING_INTENT",
    "detect_fulfillment_update",
    "fulfillment_lock_reason",
    "has_active_order_context",
    "has_explicit_commerce_topic_change",
    "is_fulfillment_session_locked",
    "is_order_fulfillment_product_query",
    "log_fulfillment_lock",
    "log_order_context_block",
    "should_block_product_discovery",
    "should_skip_catalog_preload",
    "should_suppress_product_escalation",
    "try_fulfillment_lock_continuation",
    "try_order_context_update_decision",
]
