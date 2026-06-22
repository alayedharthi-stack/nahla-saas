"""
complaint_refund_topic_guard.py
───────────────────────────────
P0 — Complaint / refund / fraud detection (operational, platform-wide).

When the customer reports quality issues, fraud, refund requests, or
health-related product harm, the brain must NOT enter order/checkout/
address collection. Routes to ``support_complaint_refund`` with honest
intake copy — no refund promises without evidence.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

from ..decision.actions import ACTION_LLM_REPLY
from ..types import Decision

logger = logging.getLogger("nahla.brain.complaint_refund_topic_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

COMPLAINT_INTAKE_REPLY_AR = (
    "وصلتنا ملاحظتك ونعتذر عن التجربة.\n\n"
    "فضلاً أرسل رقم الطلب أو صورة المنتج أو الفاتورة حتى نراجع الحالة."
)

_COMPLAINT_REFUND_RE = re.compile(
    r"(?:"
    r"خدعت|خداع|محتال|نصاب|نصب|احتيال|مغش(?:وش|وش)?|"
    r"استرجاع|استرجع|ارجع(?:وا|و)?|ارجع(?:وا|و)?\s*(?:لي|ل)?\s*(?:نقود|فلوس|مال|مبلغ)?|"
    r"فلوس(?:ي|ك)?|نقود(?:ي|ك)?|"
    r"ارج(?:ع|و)(?:وا|و)?\s*(?:لي|ل)?\s*(?:نقود|فلوس|مال|مبلغ)?|"
    r"جود(?:ة)?\s*سي(?:ئ|ء)|"
    r"(?:مو|مش|ما\s*هو)\s*عسل|(?:ليس|مو)\s*عسل(?:اً|ا)?|العسل\s*(?:ليس|مو|مش)\s*عسل|"
    r"شكو(?:ى|ي)|ت(?:ع|ق)ويض|"
    r"مريض(?:ة|ه)?|حساس(?:ية|يه)?|"
    r"رد\s*(?:ال)?(?:فلوس|نقود|مال|مبلغ)|"
    r"refund|scam|fraud|complaint"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SESSION_FLAG = "complaint_refund_active"


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
    )
    return _WS_RE.sub(" ", t).strip()


def classify_complaint_refund(message: str) -> bool:
    """Return True when the inbound message is a complaint/refund/fraud signal."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if not norm:
        return False
    return bool(_COMPLAINT_REFUND_RE.search(norm))


def is_complaint_refund_active(state: Any) -> bool:
    cs = getattr(state, "commerce_session", None) or {}
    if isinstance(cs, dict):
        return bool(cs.get(_SESSION_FLAG))
    return False


def _current_turn_exits_complaint_session(state: Any, message: str) -> bool:
    """Current-turn evidence that a stale complaint flag should not own routing."""
    if classify_complaint_refund(message or ""):
        return False
    try:
        from ..catalog.catalog_browse_turn_policy import is_catalog_browse_message  # noqa: PLC0415

        if is_catalog_browse_message(message or ""):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse probe
        pass
    try:
        from .checkout_slot_contact_guard import message_fulfills_checkout_slot  # noqa: PLC0415

        order_prep = None
        if isinstance(state, dict):
            order_prep = state.get("order_prep")
        else:
            order_prep = getattr(state, "order_prep", None)
        if message_fulfills_checkout_slot(message or "", order_prep=order_prep):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional checkout slot probe
        pass
    return False


def mark_complaint_refund_active(state: Any, *, active: bool = True) -> None:
    cs = dict(getattr(state, "commerce_session", None) or {})
    if active:
        cs[_SESSION_FLAG] = True
    else:
        cs.pop(_SESSION_FLAG, None)
    try:
        state.commerce_session = cs
    except Exception:  # noqa: BLE001  # noqa: silent-ok — duck-typed state patch is best-effort
        pass


def should_block_order_draft_injection(
    *,
    brain_state: Any = None,
    customer_message: str = "",
    decision: Any = None,
    history: Any = None,
) -> bool:
    """True when WA draft/order-flow injection must not run."""
    try:
        from .post_purchase_feedback_guard import should_block_post_purchase_order_flow  # noqa: PLC0415

        if should_block_post_purchase_order_flow(
            brain_state=brain_state,
            customer_message=customer_message or "",
            decision=decision,
            history=history,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — post-purchase block probe must not break guard
        pass
    if classify_complaint_refund(customer_message or ""):
        return True
    if is_complaint_refund_active(brain_state) and not _current_turn_exits_complaint_session(
        brain_state,
        customer_message or "",
    ):
        return True
    args = getattr(decision, "args", None) or {}
    if str(args.get("topic") or "") == "support_complaint_refund":
        return True
    if args.get("block_order_flow"):
        return True
    return False


def apply_complaint_refund_session_flags(
    state: Any,
    message: str,
    decision: Any = None,
) -> None:
    if is_complaint_refund_active(state) and _current_turn_exits_complaint_session(
        state,
        message or "",
    ):
        mark_complaint_refund_active(state, active=False)
        return

    triggered = False
    if classify_complaint_refund(message or ""):
        triggered = True
    else:
        args = getattr(decision, "args", None) or {}
        if str(args.get("topic") or "") == "support_complaint_refund":
            triggered = True
    if not triggered:
        return
    mark_complaint_refund_active(state, active=True)
    try:
        from .commerce_objective import transition_commerce_objective_for_complaint  # noqa: PLC0415

        transition_commerce_objective_for_complaint(state)
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — objective shift must not block complaint route
        logger.debug("[COMPLAINT_REFUND_GUARD] objective_shift_failed err=%s", exc)


def try_complaint_refund_decision(ctx: Any) -> Optional[Decision]:
    msg = str(getattr(ctx, "message", "") or "")
    if not classify_complaint_refund(msg):
        return None
    logger.info(
        "[COMPLAINT_REFUND_GUARD] route=support_complaint_refund tenant=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        msg[:72],
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "support_complaint_refund",
            "block_commerce_escalation": True,
            "block_order_flow": True,
        },
        reason="complaint/refund/fraud detected — support intake",
        confidence=0.97,
    )


__all__ = [
    "COMPLAINT_INTAKE_REPLY_AR",
    "apply_complaint_refund_session_flags",
    "classify_complaint_refund",
    "is_complaint_refund_active",
    "mark_complaint_refund_active",
    "should_block_order_draft_injection",
    "try_complaint_refund_decision",
]
