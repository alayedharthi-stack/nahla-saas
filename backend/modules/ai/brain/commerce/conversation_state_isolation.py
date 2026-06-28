"""
commerce/conversation_state_isolation.py
────────────────────────────────────────
P0 — Conversation state isolation: current intent owns the turn.

Prevents stale checkout clarifications (last_question_asked, pending slots)
from overriding unrelated inbound messages (discount, feedback, social, support).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.conversation_state_isolation")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Turns that break fulfillment / checkout clarification ownership.
_FULFILLMENT_OWNERSHIP_BREAK_RE = re.compile(
    r"(?:"
    r"كود|خصم|خصوم|coupon|discount|promo|promotion|"
    r"عرض|عروض|تخفيض|offer|sale|"
    r"refund|complaint|"
    r"ما\s*عندكم|عندكم\s*كود|"
    r"who\s+are\s+you|من\s+انت|من\s+انتم"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CHECKOUT_QUESTION_MARKERS = (
    "last name",
    "first name",
    "your name",
    "which country",
    "which city",
    "delivery address",
    "google maps",
    "اسم العائلة",
    "اسمك الأول",
    "اسمك الاول",
    "ما اسمك",
    "ما المدينة",
    "المدينة التي",
    "العنوان",
    "country should we ship",
    "city should we ship",
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def inbound_breaks_fulfillment_ownership(message: str) -> bool:
    """True when inbound should not continue a stale checkout clarification."""
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.order_flow_v2.triggers import (  # noqa: PLC0415
            is_checkout_escape_inquiry,
        )

        if is_checkout_escape_inquiry(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — isolation must not depend on V2 availability
        pass
    norm = _norm(raw)
    if _FULFILLMENT_OWNERSHIP_BREAK_RE.search(norm):
        return True
    try:
        from .complaint_refund_topic_guard import classify_complaint_refund  # noqa: PLC0415

        if classify_complaint_refund(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — complaint probe is best-effort
        pass
    try:
        from .post_purchase_feedback_guard import classify_product_quality_feedback  # noqa: PLC0415

        if classify_product_quality_feedback(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — feedback probe is best-effort
        pass
    try:
        from .commerce_conversation_guard import is_delivery_social_thanks  # noqa: PLC0415

        if is_delivery_social_thanks(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — social probe is best-effort
        pass
    return False


def _pending_question_is_checkout_slot(last_question: str) -> bool:
    lq = (last_question or "").lower()
    return any(marker in lq for marker in _CHECKOUT_QUESTION_MARKERS)


def should_replay_pending_question(
    *,
    inbound_text: str = "",
    last_question: str = "",
) -> bool:
    """
    True only when replaying ``last_question_asked`` is conversationally valid.

    Default deny — stale checkout slots must not hijack unrelated turns.
    """
    last_q = (last_question or "").strip()
    if not last_q:
        return False
    raw = (inbound_text or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: PLC0415
            resolve_current_turn_social_non_commerce,
        )

        current_turn = resolve_current_turn_social_non_commerce(
            raw,
            last_question=last_q,
        )
        if current_turn.matched:
            logger.info(
                "[CONVERSATION_STATE_ISOLATION] pending_question_replay_blocked "
                "category=%s reason=%s preview=%r",
                current_turn.category or "-",
                current_turn.reason or "-",
                raw[:72],
            )
            return False
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CONVERSATION_STATE_ISOLATION] social_noncommerce_replay_probe_failed err=%s",
            exc,
        )
    try:
        from modules.ai.order_flow_v2.triggers import (  # noqa: PLC0415
            is_checkout_escape_inquiry,
        )

        if is_checkout_escape_inquiry(raw):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — replay guard must fail closed locally
        pass
    if inbound_breaks_fulfillment_ownership(raw):
        return False
    if not _pending_question_is_checkout_slot(last_q):
        # Non-checkout bot questions may still clarify when inbound is short.
        return len(_norm(raw).split()) <= 4
    norm = _norm(raw)
    # Checkout slot answers are usually short and slot-shaped — not new asks.
    if "?" in raw or "؟" in raw:
        return False
    if len(norm.split()) > 5:
        return False
    return True


def clear_stale_fulfillment_clarification(state: Any) -> bool:
    """Clear pending checkout clarification markers on topic break."""
    if state is None:
        return False
    cleared = False
    try:
        if str(getattr(state, "last_question_asked", "") or "").strip():
            state.last_question_asked = ""
            state.last_question_answered = True
            cleared = True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — duck-typed state patch
        pass
    if isinstance(state, dict):
        if str(state.get("last_question_asked") or "").strip():
            state["last_question_asked"] = ""
            state["last_question_answered"] = True
            cleared = True
    return cleared


def maybe_isolate_conversation_on_topic_break(
    *,
    message: str = "",
    state: Any = None,
) -> bool:
    """
    When inbound breaks fulfillment ownership, clear stale clarification state.

    Returns True when isolation was applied.
    """
    if not inbound_breaks_fulfillment_ownership(message):
        return False
    if clear_stale_fulfillment_clarification(state):
        logger.info(
            "[CONVERSATION_STATE_ISOLATION] cleared stale clarification preview=%r",
            (message or "")[:72],
        )
        return True
    return False


__all__ = [
    "clear_stale_fulfillment_clarification",
    "inbound_breaks_fulfillment_ownership",
    "maybe_isolate_conversation_on_topic_break",
    "should_replay_pending_question",
]
