"""
stub_reply_guard_context.py
───────────────────────────
Shared predicates for when generic stub replies («وصلت رسالتك»,
«حصل خطأ مؤقت») must NOT replace contextual commerce or social turns.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.stub_reply_guard_context")

from modules.ai.brain.intent_priority.types import (
    GOAL_GREETING_ONLY,
    GOAL_ORDER_REQUEST,
    GOAL_SOCIAL_ONLY,
)

_GENERIC_STUB_MARKERS = (
    "وصلت رسالتك",
    "حصل خطأ مؤقت",
    "حصل خلل مؤقت",
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_EMOJI_ONLY_RE = re.compile(
    r"^[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
    r"\U0000200D\s\u200c\u200d\u064b-\u065f\u0670]+$",
    re.UNICODE,
)

_STICKER_MEDIA_TYPES = frozenset({"sticker", "animated_sticker"})

_COMMERCE_INTENTS = frozenset({
    "solution_seeking_commerce",
    "ask_product",
    "ask_price",
    "product_availability",
    "product_reference",
    "ask_shipping",
    "start_order",
    "order_request",
})

_BARE_AFFIRMATIVES = frozenset({
    "نعم", "ايوه", "ايوة", "أيوه", "أيوة", "اي", "أي", "تمام", "طيب",
    "اوكي", "ok", "okay", "ماشي", "موافق", "يلا", "حاضر",
})

_STAFF_ROUTE_REJECTION_RE = re.compile(
    r"(?:"
    r"ما\s+ا?ب(?:ي|غ(?:ى|a)?)\s+(?:أ?مين|امين|الموظف|موظف|المعرض|الفرع|خدمة\s*العملاء)"
    r"|(?:ما|مو)\s+(?:أ?بي|ابي|أبغى|ابغى)\s+(?:أ?مين|امين|المعرض|الفرع)"
    r"|(?:أ?ب(?:ي|غ(?:ى|a)?)|اريد|أريد|اب(?:ي|غ(?:ى|a)?))\s+(?:أ?شتري|اشتري|أ?طلب|اطلب|أ?كمل|اكمل)\s+(?:ال)?(?:طلب|عسل|منتج)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def is_generic_stub_reply(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return any(marker in raw for marker in _GENERIC_STUB_MARKERS)


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


def is_lightweight_social_turn(
    inbound_text: str,
    *,
    intent_name: str = "",
    primary_customer_goal: str = "",
    inbound_metadata: Optional[dict] = None,
) -> bool:
    meta = inbound_metadata or {}
    media_type = str(meta.get("normalized_type") or meta.get("msg_type") or "").strip().lower()
    if media_type in _STICKER_MEDIA_TYPES:
        return True
    raw = (inbound_text or "").strip()
    if not raw:
        return media_type in _STICKER_MEDIA_TYPES
    norm = _normalize(raw)
    if norm in _BARE_AFFIRMATIVES:
        return False
    if _EMOJI_ONLY_RE.match(raw):
        return True
    goal = (primary_customer_goal or "").strip().lower()
    intent = (intent_name or "").strip().lower()
    if goal in {GOAL_SOCIAL_ONLY, GOAL_GREETING_ONLY} or intent in {
        "social", "greeting", "persona_interaction",
    }:
        if len(norm) <= 12 and not re.search(
            r"(?:عسل|طلب|سعر|منتج|توصيل|كم|ابغ|أبغ|ابي|أبي)",
            norm,
        ):
            return True
    return False


def _order_prep_from_state(state: Any) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return state.get("order_prep") or state
    return getattr(state, "order_prep", None)


def has_active_commerce_from_state(state: Any) -> bool:
    op = _order_prep_from_state(state)
    if op is None:
        return False
    if isinstance(op, dict):
        line_items = list(op.get("line_items") or op.get("cart_items") or [])
        cart_items = list(op.get("cart_items") or [])
        order_status = str(op.get("order_status") or "").strip().lower()
        missing = list(op.get("missing_fields") or [])
        product_id = str(op.get("product_id") or "").strip()
        product_name = str(op.get("product_name") or "").strip()
    else:
        line_items = list(getattr(op, "line_items", None) or getattr(op, "cart_items", None) or [])
        cart_items = list(getattr(op, "cart_items", None) or [])
        order_status = str(getattr(op, "order_status", "") or "").strip().lower()
        missing = list(getattr(op, "missing_fields", None) or [])
        product_id = str(getattr(op, "product_id", "") or "").strip()
        product_name = str(getattr(op, "product_name", "") or "").strip()

    if line_items or cart_items:
        return True
    if product_id or product_name:
        return True
    if missing:
        return True
    if order_status and order_status not in {"", "idle", "none", "closed", "cancelled"}:
        return True

    if isinstance(state, dict):
        if state.get("awaiting_option_confirmation"):
            return True
        if str(state.get("last_question_asked") or "").strip():
            return True
        pending = state.get("pending_cart_confirmation") or {}
        if isinstance(pending, dict) and pending.get("items"):
            return True
    else:
        if getattr(state, "awaiting_option_confirmation", False):
            return True
        if str(getattr(state, "last_question_asked", "") or "").strip():
            return True
        prep = _order_prep_from_state(state)
        if prep is not None:
            pending = getattr(prep, "pending_cart_confirmation", None)
            if isinstance(pending, dict) and pending.get("items"):
                return True
    return False


def is_staff_route_rejection_message(message: str) -> bool:
    """Customer explicitly rejects staff/showroom and wants to continue buying."""
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_STAFF_ROUTE_REJECTION_RE.search(raw))


def resolve_staff_rejection_commerce_resume(state: Any) -> str:
    """Resume active order after staff-route rejection — no generic ACK."""
    op = _order_prep_from_state(state)
    lines = ["أبشر."]
    if op is not None:
        if isinstance(op, dict):
            city = str(op.get("city") or "").strip()
            qty = str(op.get("quantity_label") or op.get("quantity") or "").strip()
            product = str(op.get("product_name") or "").strip()
        else:
            city = str(getattr(op, "city", "") or "").strip()
            qty = str(getattr(op, "quantity_label", "") or getattr(op, "quantity", "") or "").strip()
            product = str(getattr(op, "product_name", "") or "").strip()
        detail_parts: list[str] = []
        if qty:
            detail_parts.append(qty)
        if product and product not in qty:
            detail_parts.append(product)
        if city:
            if detail_parts:
                lines.append(f"{' '.join(detail_parts)} والتوصيل ل{city}.")
            else:
                lines.append(f"تمام، التوصيل ل{city}.")
        elif detail_parts:
            lines.append(f"{' '.join(detail_parts)}.")
    lines.append("أي نوع عسل تفضّل؟")
    return "\n".join(lines)


def should_suppress_generic_stub_injection(
    *,
    inbound_text: str = "",
    intent_name: str = "",
    primary_customer_goal: str = "",
    conversation_objective: str = "",
    state: Any = None,
    inbound_metadata: Optional[dict] = None,
) -> bool:
    """True when guards must not inject «وصلت رسالتك» / receipt-style stubs."""
    if is_staff_route_rejection_message(inbound_text):
        return True
    goal = (primary_customer_goal or "").strip().lower()
    intent = (intent_name or "").strip().lower()
    try:
        from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: PLC0415
            message_has_bare_quantity_or_variant_signal,
        )

        if message_has_bare_quantity_or_variant_signal(inbound_text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — qty signal must not break stub guard
        pass
    if has_active_commerce_from_state(state):
        return True
    if goal == GOAL_ORDER_REQUEST or intent in _COMMERCE_INTENTS:
        return True
    if conversation_objective:
        return True
    if is_lightweight_social_turn(
        inbound_text,
        intent_name=intent_name,
        primary_customer_goal=primary_customer_goal,
        inbound_metadata=inbound_metadata,
    ):
        return True
    try:
        from modules.ai.brain.commerce.commerce_conversation_guard import (  # noqa: PLC0415
            is_social_ack_message,
        )

        if is_social_ack_message(inbound_text):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[STUB_REPLY_GUARD] social_ack_probe_failed")
    return False


def resolve_social_thanks_guard_reply(inbound_text: str) -> Optional[str]:
    """Reciprocal social reply for thanks/closing — never generic receipt ACK."""
    raw = (inbound_text or "").strip()
    if not raw:
        return None
    try:
        from modules.ai.brain.commerce.commerce_conversation_guard import (  # noqa: PLC0415
            is_social_ack_message,
        )

        if not is_social_ack_message(raw):
            return None
    except Exception:  # noqa: BLE001
        logger.exception("[STUB_REPLY_GUARD] social_thanks_resolve_probe_failed")
        return None
    try:
        from modules.ai.brain.compose.mirror_replies import mirror_reply  # noqa: PLC0415

        mirrored = mirror_reply(raw)
        if mirrored and mirrored.strip():
            return mirrored.strip()
    except Exception:  # noqa: BLE001
        logger.exception("[STUB_REPLY_GUARD] social_thanks_mirror_failed")
    return "الله يعافيك ويسعدك 🌷"


def strip_escalation_claim_sentences(reply: str) -> str:
    """Remove sentences containing false staff-escalation claims."""
    from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: PLC0415
        reply_contains_escalation_claim,
    )

    raw = (reply or "").strip()
    if not raw or not reply_contains_escalation_claim(raw):
        return raw

    kept: list[str] = []
    for chunk in re.split(r"(?<=[.!?؟])\s+|\n+", raw):
        part = chunk.strip()
        if part and not reply_contains_escalation_claim(part):
            kept.append(part)
    return " ".join(kept).strip()


__all__ = [
    "has_active_commerce_from_state",
    "is_generic_stub_reply",
    "is_lightweight_social_turn",
    "is_staff_route_rejection_message",
    "resolve_social_thanks_guard_reply",
    "resolve_staff_rejection_commerce_resume",
    "should_suppress_generic_stub_injection",
    "strip_escalation_claim_sentences",
]
