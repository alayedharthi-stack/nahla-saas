"""
commerce/gift_order_gate.py
───────────────────────────
P0 — Gift / delivery order confirmation gate.

Deterministic pre-decide extraction, ready-for-order routing, and
pending cart confirmation consumption. Platform-wide; no merchant hardcoding.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.brain.commerce.gift_order_gate")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_ORDER_SHAPED_MARKERS = (
    "طلب توصيل",
    "طلب توص",
    "ابغى",
    "أبغى",
    "ابي",
    "أبي",
    "أطلب",
    "اطلب",
    "أرسل",
    "ارسل",
    "وصل",
    "توصيل",
    "ربع كيلo",
    "ربع كيلو",
    "نص كيلo",
    "نص كيلو",
    "كيلo",
    "كيلو",
    "عسل",
    "لهذا الشخص",
    "هدية",
)

_GIFT_MARKERS = (
    "لهذا الشخص",
    "لشخص",
    "لواحد",
    "هدية",
    "وصل له",
    "أرسل له",
    "ارسل له",
    "توصيل له",
    "لصديق",
    "لأحد",
)

_BARE_AFFIRMATIVES = frozenset({
    "نعم", "ايه", "أيه", "ايوه", "أيوه", "ايوة", "أيوة", "اي", "أي",
    "صح", "صحيح", "تمام", "أكيد", "اكيد", "توكل", "موافق", "ماشي",
    "ok", "okay", "yes", "yep",
})

_BARE_CART_REJECTIONS = frozenset({
    "لا", "لأ", "no", "cancel", "الغ", "الغاء", "الغي", "مو", "مش",
})

_ARABIC_NAME_LINE_RE = re.compile(
    r"^[\u0600-\u06FF\u0750-\u077Fa-zA-Z][\u0600-\u06FF\u0750-\u077Fa-zA-Z\s]{2,58}$",
    re.UNICODE,
)

_PRODUCT_LINE_RE = re.compile(
    r"(?:"
    r"(?:نصف|نص|ربع|كilo|كيلo|كيلو|1\s*kg|500\s*g|250\s*g)"
    r".*?(?:طلح|سمر|سدر|شوك|صيف|صيفي|عسل)"
    r"|(?:طلح|سمر|سدر|شوك|صيف|صيفي|عسل).*?(?:نصف|نص|ربع|كilo|كيلo|كيلo)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def is_order_shaped_message(message: str) -> bool:
    """True when inbound likely carries cart / delivery / order intent."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _normalize(raw)
    if any(_normalize(marker) in norm for marker in _ORDER_SHAPED_MARKERS):
        return True
    if _PRODUCT_LINE_RE.search(raw):
        return True
    if "\n" in raw and "عسل" in norm:
        return True
    return False


def message_has_gift_markers(message: str) -> bool:
    norm = _normalize(message or "")
    return any(_normalize(m) in norm for m in _GIFT_MARKERS)


def extract_gift_recipient_name(message: str) -> Optional[str]:
    """
    When line N has a gift marker and line N+1 looks like an Arabic full name,
    return recipient_name (not customer name).
    """
    lines = [ln.strip() for ln in (message or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    for idx, line in enumerate(lines[:-1]):
        if not message_has_gift_markers(line):
            continue
        candidate = lines[idx + 1].strip()
        if not candidate or _PRODUCT_LINE_RE.search(candidate):
            continue
        if message_has_gift_markers(candidate):
            continue
        if not _ARABIC_NAME_LINE_RE.match(candidate):
            continue
        tokens = [t for t in candidate.split() if t.strip()]
        if len(tokens) < 2 or len(tokens) > 4:
            continue
        return candidate
    return None


def apply_gift_recipient_to_prep(prep: Any, message: str) -> bool:
    """Stamp gift recipient + fulfillment_kind on order_prep."""
    recipient = extract_gift_recipient_name(message)
    if not recipient:
        if message_has_gift_markers(message):
            prep.fulfillment_kind = "gift_delivery"
            return True
        return False
    existing = str(getattr(prep, "recipient_name", "") or "").strip()
    if existing and _normalize(existing) == _normalize(recipient):
        return False
    prep.recipient_name = recipient
    prep.fulfillment_kind = "gift_delivery"
    try:
        prov = dict(getattr(prep, "identity_provenance", None) or {})
        prov["recipient_name"] = "gift_order_gate"
        prep.identity_provenance = prov
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "[GIFT_ORDER_GATE] recipient_name=%r fulfillment_kind=gift_delivery",
        recipient[:80],
    )
    return True


def _prep_dict(prep: Any) -> Dict[str, Any]:
    if isinstance(prep, dict):
        return prep
    if hasattr(prep, "to_dict"):
        return dict(prep.to_dict())
    return {}


def get_pending_delivery_location(state: Any) -> Dict[str, Any]:
    loc = getattr(state, "pending_delivery_location", None)
    if isinstance(loc, dict) and loc:
        return dict(loc)
    prep = getattr(state, "order_prep", None)
    if prep is not None:
        nested = getattr(prep, "pending_delivery_location", None)
        if isinstance(nested, dict) and nested:
            return dict(nested)
        pd = _prep_dict(prep)
        nested = pd.get("pending_delivery_location")
        if isinstance(nested, dict) and nested:
            return dict(nested)
    return {}


def consume_pending_delivery_location(state: Any, prep: Any, *, gift: bool = False) -> bool:
    """Attach stashed location pin to order_prep; clear stash."""
    loc = get_pending_delivery_location(state)
    if not loc:
        return False
    applied = False
    for key, prep_key in (
        ("latitude", "latitude"),
        ("longitude", "longitude"),
        ("google_maps_url", "google_maps_url"),
        ("short_address_code", "short_address_code"),
        ("city", "city"),
        ("address_line", "address_line"),
    ):
        val = loc.get(key)
        if val is None or val == "":
            continue
        if not str(getattr(prep, prep_key, "") or "").strip():
            setattr(prep, prep_key, val)
            applied = True
    if gift:
        prep.fulfillment_kind = str(getattr(prep, "fulfillment_kind", "") or "gift_delivery")
    if applied:
        prep.resolution_source = "pending_delivery_location"
        clear_pending_delivery_location(state, prep)
        logger.info(
            "[GIFT_ORDER_GATE] consumed pending_delivery_location gift=%s",
            gift,
        )
    return applied


def build_pending_delivery_location_patch(
    address_patch: Dict[str, Any],
    *,
    gift: bool = False,
) -> Dict[str, Any]:
    payload = dict(address_patch or {})
    payload["source"] = "whatsapp_location_pin"
    if gift:
        payload["gift_delivery"] = True
    return {"pending_delivery_location": payload}


def get_pending_cart_confirmation(prep: Any) -> Dict[str, Any]:
    raw = getattr(prep, "pending_cart_confirmation", None)
    if isinstance(raw, dict) and raw:
        return dict(raw)
    pd = _prep_dict(prep)
    nested = pd.get("pending_cart_confirmation")
    return dict(nested) if isinstance(nested, dict) else {}


def set_pending_cart_confirmation(
    prep: Any,
    *,
    items: List[Dict[str, Any]],
    source: str,
    turn: int,
) -> None:
    prep.pending_cart_confirmation = {
        "items": list(items or []),
        "source": source,
        "created_turn": int(turn or 0),
    }


def clear_pending_cart_confirmation(prep: Any) -> None:
    if prep is None:
        return
    prep.pending_cart_confirmation = {}


def is_bare_cart_confirmation(message: str) -> bool:
    norm = _normalize((message or "").strip())
    return norm in {_normalize(w) for w in _BARE_AFFIRMATIVES}


def is_bare_cart_rejection(message: str) -> bool:
    norm = _normalize((message or "").strip())
    return norm in {_normalize(w) for w in _BARE_CART_REJECTIONS}


def clear_pending_delivery_location(state: Any, prep: Any) -> None:
    """Drop stashed pin so it cannot attach to a later unrelated order."""
    if state is not None:
        state.pending_delivery_location = {}
    if prep is not None:
        prep.pending_delivery_location = {}


def maybe_clear_pending_cart_confirmation(
    *,
    prep: Any,
    decision: Any,
    message: str = "",
    intent_name: str = "",
) -> bool:
    """
    Clear operational pending cart confirmation when consumed or obsolete.

    Clears after draft proposal, bare rejection, or route to a non-cart intent.
    """
    if prep is None or not get_pending_cart_confirmation(prep):
        return False

    from ..decision.actions import (  # noqa: PLC0415
        ACTION_GREET,
        ACTION_HANDOFF,
        ACTION_LLM_REPLY,
        ACTION_OUT_OF_SCOPE,
        ACTION_PLATFORM_REPLY,
        ACTION_PROPOSE_DRAFT_ORDER,
        ACTION_SOCIAL_REPLY,
        ACTION_TRACK_ORDER,
    )

    if is_bare_cart_rejection(message):
        clear_pending_cart_confirmation(prep)
        logger.info("[GIFT_ORDER_GATE] cleared pending_cart_confirmation reason=rejection")
        return True

    action = str(getattr(decision, "action", "") or "")
    if action == ACTION_PROPOSE_DRAFT_ORDER:
        clear_pending_cart_confirmation(prep)
        logger.info("[GIFT_ORDER_GATE] cleared pending_cart_confirmation reason=draft_proposed")
        return True

    if action in {
        ACTION_HANDOFF,
        ACTION_TRACK_ORDER,
        ACTION_PLATFORM_REPLY,
        ACTION_GREET,
        ACTION_SOCIAL_REPLY,
        ACTION_OUT_OF_SCOPE,
    }:
        clear_pending_cart_confirmation(prep)
        logger.info(
            "[GIFT_ORDER_GATE] cleared pending_cart_confirmation reason=clear_intent action=%s",
            action,
        )
        return True

    # LLM fallback on a non-order turn supersedes a stale cart confirm offer.
    if action == ACTION_LLM_REPLY and not is_order_shaped_message(message):
        topic = str((getattr(decision, "args", None) or {}).get("topic") or "")
        if topic not in {"order_recovery", "execute_pending_offer"}:
            clear_pending_cart_confirmation(prep)
            logger.info(
                "[GIFT_ORDER_GATE] cleared pending_cart_confirmation reason=llm_non_order topic=%s",
                topic or "-",
            )
            return True

    return False


def stamp_pending_cart_confirmation_from_reply(
    prep: Any,
    reply: str,
    *,
    cart_items: List[Dict[str, Any]],
    turn: int,
) -> bool:
    """Set operational flag when outbound asks cart/qty confirmation."""
    if not cart_items:
        return False
    text = (reply or "").strip()
    if not text:
        return False
    if not re.search(r"(?:صحيح|صح\?|تأكد|تأكيد|confirm)", text, re.I | re.UNICODE):
        return False
    if not re.search(r"(?:كilo|كيلo|كيلو|ربع|نص|نصف|طلح|سمر|صيف|عسل)", text, re.I | re.UNICODE):
        return False
    set_pending_cart_confirmation(
        prep,
        items=cart_items,
        source="cart_summary",
        turn=turn,
    )
    return True


def has_catalog_ambiguity(prep: Any, state: Any) -> bool:
    prep_d = _prep_dict(prep)
    raw = prep_d.get("wa_cart_catalog_resolution") or {}
    if isinstance(raw, dict) and raw.get("needs_clarification"):
        return True
    return False


def evaluate_ready_for_order_creation(ctx: Any) -> Tuple[bool, str, List[str]]:
    """
    Return (ready, reason, missing_fields).

    Ready when cart + delivery location exist and only provably missing
    customer/recipient slots remain (no catalog ambiguity).
    """
    from core.wa_order_lifecycle import (  # noqa: PLC0415
        compute_wa_missing_fields,
        has_accepted_delivery_address,
    )

    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None)
    if prep is None:
        return False, "no_order_prep", []

    cart = list(getattr(state, "cart_items", None) or getattr(prep, "line_items", None) or [])
    if not cart:
        return False, "no_cart_items", []

    if has_catalog_ambiguity(prep, state):
        return False, "catalog_ambiguity", []

    if not has_accepted_delivery_address(_prep_dict(prep)):
        pending = get_pending_delivery_location(state)
        if not pending:
            return False, "missing_delivery_address", ["delivery_address"]

    prep_d = _prep_dict(prep)
    missing = compute_wa_missing_fields(
        prep_d,
        brain_state={
            "cart_items": cart,
            "current_product_focus": getattr(state, "current_product_focus", None),
        },
        line_items=cart,
    )
    allowed_tail = {
        "customer_first_name",
        "customer_last_name",
        "city",
    }
    if message_has_gift_markers(getattr(ctx, "message", "") or ""):
        allowed_tail.add("customer_first_name")
        allowed_tail.add("customer_last_name")
    blocking = [m for m in missing if m not in allowed_tail]
    if blocking:
        return False, f"blocking_missing:{','.join(blocking)}", missing

    if missing:
        return True, "ready_missing_recipient_or_name_only", missing
    return True, "ready_for_order_creation", []


def run_pre_decide_order_extraction(
    ctx: Any,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Apply cart, gift recipient, and pending location before decide().

    Returns a summary dict for tracing/tests.
    """
    from modules.ai.brain.commerce.cart_state import maybe_apply_cart_message  # noqa: PLC0415

    message = str(getattr(ctx, "message", "") or "")
    state = getattr(ctx, "state", None)
    if state is None or not is_order_shaped_message(message):
        return {"applied": False, "reason": "not_order_shaped"}

    prep = getattr(state, "order_prep", None)
    if prep is None:
        return {"applied": False, "reason": "no_order_prep"}

    summary: Dict[str, Any] = {"applied": True, "cart_changed": False}

    gift = message_has_gift_markers(message)
    if apply_gift_recipient_to_prep(prep, message):
        summary["gift_recipient"] = str(getattr(prep, "recipient_name", "") or "")

    if consume_pending_delivery_location(state, prep, gift=gift):
        summary["location_consumed"] = True

    _cart_before = list(getattr(state, "cart_items", None) or [])
    maybe_apply_cart_message(
        state=state,
        prep=prep,
        message=message,
        product_info=getattr(state, "current_product_focus", None),
    )
    _cart_after = list(getattr(state, "cart_items", None) or [])
    summary["cart_changed"] = _cart_before != _cart_after
    summary["cart_size"] = len(_cart_after)

    if _cart_after and db is not None and tenant_id is not None:
        try:
            from core.wa_cart_catalog_resolver import resolve_and_enrich_cart_state  # noqa: PLC0415

            resolve_and_enrich_cart_state(db, tenant_id, state, prep)
            summary["catalog_resolved"] = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GIFT_ORDER_GATE] catalog resolve skipped err=%s", exc)

    try:
        from modules.ai.brain.commerce.customer_identity import (  # noqa: PLC0415
            apply_customer_identity_during_order_flow,
        )

        apply_customer_identity_during_order_flow(ctx, db=db)
        summary["identity_applied"] = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GIFT_ORDER_GATE] identity apply skipped err=%s", exc)

    if _cart_after:
        state.stage = "ordering"
        prep.order_status = str(getattr(prep, "order_status", "") or "awaiting_address")

    ready, reason, missing = evaluate_ready_for_order_creation(ctx)
    summary["ready_for_order_creation"] = ready
    summary["ready_reason"] = reason
    summary["missing_fields"] = missing
    return summary


def try_pending_cart_confirmation_decision(ctx: Any) -> Optional[Any]:
    """Bare affirmative while pending_cart_confirmation → draft order."""
    from ..decision.actions import ACTION_PROPOSE_DRAFT_ORDER  # noqa: PLC0415
    from ..types import Decision  # noqa: PLC0415

    message = str(getattr(ctx, "message", "") or "")
    if not is_bare_cart_confirmation(message):
        return None
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state else None
    pending = get_pending_cart_confirmation(prep) if prep else {}
    if not pending.get("items"):
        return None

    clear_pending_cart_confirmation(prep)
    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args={
            "source": "pending_cart_confirmation",
            "confirmed_items": list(pending.get("items") or []),
        },
        reason="pending_cart_confirmation — bare affirmative consumed",
        confidence=0.99,
    )


def try_ready_for_order_decision(ctx: Any) -> Optional[Any]:
    """Deterministic draft/sync when order is operationally complete enough."""
    from ..decision.actions import ACTION_PROPOSE_DRAFT_ORDER  # noqa: PLC0415
    from ..types import Decision  # noqa: PLC0415

    if not is_order_shaped_message(getattr(ctx, "message", "") or ""):
        return None

    ready, reason, missing = evaluate_ready_for_order_creation(ctx)
    if not ready:
        return None

    clear_pending_cart_confirmation(getattr(getattr(ctx, "state", None), "order_prep", None))

    args: Dict[str, Any] = {"source": "ready_for_order_creation", "ready_reason": reason}
    if missing:
        args["missing_fields"] = missing
    cart = list(getattr(getattr(ctx, "state", None), "cart_items", None) or [])
    if cart:
        args["line_items"] = cart

    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args=args,
        reason=f"ready_for_order_creation — {reason}",
        confidence=0.98,
    )


__all__ = [
    "apply_gift_recipient_to_prep",
    "build_pending_delivery_location_patch",
    "clear_pending_cart_confirmation",
    "clear_pending_delivery_location",
    "consume_pending_delivery_location",
    "evaluate_ready_for_order_creation",
    "extract_gift_recipient_name",
    "get_pending_cart_confirmation",
    "get_pending_delivery_location",
    "is_bare_cart_confirmation",
    "is_bare_cart_rejection",
    "is_order_shaped_message",
    "maybe_clear_pending_cart_confirmation",
    "message_has_gift_markers",
    "run_pre_decide_order_extraction",
    "set_pending_cart_confirmation",
    "stamp_pending_cart_confirmation_from_reply",
    "try_pending_cart_confirmation_decision",
    "try_ready_for_order_decision",
]
