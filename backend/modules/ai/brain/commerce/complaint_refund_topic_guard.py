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

# Affect / harm / fraud — fire unconditionally (not topic words).
_GRIEVANCE_AFFECT_RE = re.compile(
    r"(?:"
    r"خدعت|خداع|محتال|نصاب|نصب|احتيال|مغش(?:وش|وش)?|"
    r"جود(?:ة)?\s*سي(?:ئ|ء)|"
    r"(?:مو|مش|ما\s*هو)\s*عسل|(?:ليس|مو)\s*عسل(?:اً|ا)?|العسل\s*(?:ليس|مو|مش)\s*عسل|"
    r"شكو(?:ى|ي)|ت(?:ع|ق)ويض|"
    r"مريض(?:ة|ه)?|حساس(?:ية|يه)?|"
    r"scam|fraud|complaint"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Refund/return topic tokens — require operational qualifier (Pack A3).
# Patterns match against _norm()'d text (hamza folded to ا).
_REFUND_TOPIC_RE = re.compile(
    r"(?:"
    r"استرجاع|استرجع|استرداد|استرد|ارجاع|استبدال|استبدل|"
    r"ارجع(?:وا|و)?|"
    r"فلوس(?:ي|ك)?|نقود(?:ي|ك)?|"
    r"رد\s*(?:ال)?(?:فلوس|نقود|مال|مبلغ)|"
    r"refund|return|exchange"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SELF_OR_ORDER_REF_RE = re.compile(
    r"(?:"
    r"طلب(?:ي|نا|تي)|شحن(?:تي|تي)|"
    r"فلوسي|نقودي|"
    r"المنتج\s*(?:اللي|الذي)\s*(?:وصل|طلب)|"
    r"رقم\s*(?:ال)?طلب|"
    r"ابي\s*(?:ارجع|استرد|استبدل)|ابغى\s*(?:ارجع|استرد|استبدل)|"
    r"اريد\s*(?:ارجع|استرداد|استبدال)|"
    r"my\s*order|refund\s*me|return\s*my"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_POLICY_ARTIFACT_RE = re.compile(
    r"(?:"
    r"سياس[ةه]|شروط|أحكام|كيف\s*(?:نظام|تتم|يصير)|كم\s*(?:سياس[ةه]|مدة)|"
    r"\bpolic(?:y|ies)\b|\bterms\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Legacy combined pattern kept for reference/tests that import the name.
_COMPLAINT_REFUND_RE = re.compile(
    r"(?:"
    + _GRIEVANCE_AFFECT_RE.pattern[3:-1]
    + r"|"
    + _REFUND_TOPIC_RE.pattern[3:-1]
    + r")",
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


def classify_complaint_refund_kind(message: str) -> str:
    """Typed complaint classification for Pack A3 ownership.

    Returns one of:
      grievance | operational_refund | informational_policy | none
    """
    raw = (message or "").strip()
    if not raw:
        return "none"
    norm = _norm(raw)
    if not norm:
        return "none"
    if _GRIEVANCE_AFFECT_RE.search(norm):
        return "grievance"
    if _REFUND_TOPIC_RE.search(norm):
        # Informational policy artifact without self/order reference → not complaint.
        if _POLICY_ARTIFACT_RE.search(norm) and not _SELF_OR_ORDER_REF_RE.search(norm):
            return "informational_policy"
        if _SELF_OR_ORDER_REF_RE.search(norm):
            return "operational_refund"
        # Bare topic token alone is insufficient for complaint ownership.
        if _POLICY_ARTIFACT_RE.search(norm):
            return "informational_policy"
        # "أبغى أرجع طلبي" matched via self-ref; bare "استرجاع" alone → not complaint.
        return "none"
    return "none"


def classify_complaint_refund(message: str) -> bool:
    """Return True when the inbound message is an operational complaint/refund signal.

    Boolean contract preserved for existing call sites. Informational policy
    questions (e.g. «وش سياسة الاسترجاع؟») return False.
    """
    kind = classify_complaint_refund_kind(message)
    return kind in {"grievance", "operational_refund"}


def policy_information_turn_yields_complaint(message: str) -> bool:
    """True when current turn is informational policy/story and must suspend complaint ownership."""
    if classify_complaint_refund_kind(message) == "informational_policy":
        return True
    try:
        from .merchant_policy_intents import (  # noqa: PLC0415
            is_informational_policy_or_story_question,
        )

        return bool(is_informational_policy_or_story_question(message))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional Pack A3 probe
        return False


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
    inbound_metadata: Any = None,
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
    # Sticky complaint: yield ownership for explicit informational policy/story
    # turns WITHOUT clearing the session flag (Pack A3).
    if is_complaint_refund_active(brain_state):
        if policy_information_turn_yields_complaint(customer_message or ""):
            pass
        elif not _current_turn_exits_complaint_session(
            brain_state,
            customer_message or "",
        ):
            return True
    args = getattr(decision, "args", None) or {}
    if str(args.get("topic") or "") == "support_complaint_refund":
        return True
    if args.get("block_order_flow"):
        return True
    try:
        from .commerce_turn_contract import is_placed_order_statement  # noqa: PLC0415
        from .order_tracking_intent_guard import (  # noqa: PLC0415
            has_pending_order_reference_evidence,
            is_order_support_operational_follow_up,
        )
        from modules.ai.media.routing_guard import (  # noqa: PLC0415
            should_route_unclear_audio_to_existing_order_support,
        )

        if has_pending_order_reference_evidence(state=brain_state, history=history) and (
            is_order_support_operational_follow_up(
                customer_message or "",
                state=brain_state,
                history=history,
            )
            or is_placed_order_statement(customer_message or "")
            or should_route_unclear_audio_to_existing_order_support(
                inbound_metadata=inbound_metadata if isinstance(inbound_metadata, dict) else None,
                semantic_message=customer_message or "",
                history=history,
                brain_state=brain_state,
            )
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order-support draft block is best-effort
        pass
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
    # Pack A3: informational policy/story turns suspend complaint ownership
    # for THIS turn only (do not clear sticky session here).
    if policy_information_turn_yields_complaint(msg):
        return None
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
    "classify_complaint_refund_kind",
    "is_complaint_refund_active",
    "mark_complaint_refund_active",
    "policy_information_turn_yields_complaint",
    "should_block_order_draft_injection",
    "try_complaint_refund_decision",
]
