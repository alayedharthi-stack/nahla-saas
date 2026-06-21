"""
post_purchase_feedback_guard.py
───────────────────────────────
P0 — Post-purchase product feedback / quality review routing.

When the customer responds to a delivery review request (including
externally sent outbound messages) with product-quality commentary,
route to support — never catalog selection, variant clarification,
or price confirmation.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

from ..decision.actions import ACTION_LLM_REPLY
from ..types import Decision

logger = logging.getLogger("nahla.brain.post_purchase_feedback_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_SESSION_FLAG = "post_purchase_feedback_active"

_QUALITY_FEEDBACK_RE = re.compile(
    r"(?:"
    r"خفيف|ثقيل|"
    r"زايد\s*حلا(?:ه|ة)?|"
    r"مو\s*زي|مش\s*زي|مو\s*مثل|مش\s*مثل|مو\s*نفس|مش\s*نفس|"
    r"مو\s*زي\s*دايم|مش\s*زي\s*دايم|مو\s*مثل\s*اول|مش\s*مثل\s*اول|"
    r"ما\s*هو\s*زي|ماهو\s*زي|"
    r"الجود(?:ة|ه)|الطعم|القوام|"
    r"تغ(?:ي|)ر|مختلف|غير\s*المعتاد|"
    r"طعم(?:ه|ها)\s*(?:غريب|سي(?:ئ|ء)|different)|"
    r"ما\s*عجب(?:ني|تني)|"
    r"not\s*the\s*same|different\s*taste|lighter|too\s*sweet"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_MENTION_RE = re.compile(
    r"(?:"
    r"\bعسل\b|honey|"
    r"المنتج|الطلب|الشحنة|العبو(?:ة|ه)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _commerce_session(state: Any) -> dict:
    if isinstance(state, dict):
        raw = state.get("commerce_session")
    else:
        raw = getattr(state, "commerce_session", None)
    return dict(raw or {}) if isinstance(raw, dict) else {}


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


_WANT_PREFIXES = tuple(
    _norm(p)
    for p in (
        "نبغى", "نبي", "ابغى", "ابي", "اريد", "بغيت", "ودي", "حاب",
        "اشتري", "اطلب",
    )
)


def classify_product_quality_feedback(message: str) -> bool:
    """True when inbound reads as delivered-product quality commentary."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if not norm:
        return False
    if any(norm.startswith(prefix) for prefix in _WANT_PREFIXES):
        return False
    if re.search(r"(?:اشتري|اطلب|نشتري)\s+", norm):
        return False
    if not _QUALITY_FEEDBACK_RE.search(norm):
        return False
    if _PRODUCT_MENTION_RE.search(norm):
        return True
    try:
        from .fallback_guard import _semantic_product_entity  # noqa: PLC0415

        if _semantic_product_entity(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional entity probe
        pass
    return False


def is_post_purchase_feedback_active(state: Any) -> bool:
    cs = _commerce_session(state)
    return bool(cs.get(_SESSION_FLAG))


def mark_post_purchase_feedback_active(state: Any, *, active: bool = True) -> None:
    if isinstance(state, dict):
        cs = _commerce_session(state)
        if active:
            cs[_SESSION_FLAG] = True
        else:
            cs.pop(_SESSION_FLAG, None)
        state["commerce_session"] = cs
        return
    cs = _commerce_session(state)
    if active:
        cs[_SESSION_FLAG] = True
    else:
        cs.pop(_SESSION_FLAG, None)
    try:
        state.commerce_session = cs
    except Exception:  # noqa: BLE001  # noqa: silent-ok — duck-typed state patch is best-effort
        pass


def should_block_post_purchase_order_flow(
    *,
    brain_state: Any = None,
    customer_message: str = "",
    decision: Any = None,
    history: Any = None,
) -> bool:
    """True when order/catalog injection must not run for post-purchase turns."""
    if is_post_purchase_feedback_active(brain_state):
        return True
    try:
        from .external_outbound_context import is_post_purchase_context_active  # noqa: PLC0415

        if is_post_purchase_context_active(state=brain_state, history=history):
            if classify_product_quality_feedback(customer_message or ""):
                return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — post-purchase block probe must not break flow
        pass
    args = getattr(decision, "args", None) or {}
    topic = str(args.get("topic") or "")
    if topic in {"support_product_feedback", "support_complaint_refund"}:
        return True
    if args.get("block_order_flow"):
        return True
    return False


def apply_post_purchase_feedback_session_flags(
    state: Any,
    message: str,
    decision: Any = None,
    *,
    history: Any = None,
) -> None:
    triggered = False
    args = getattr(decision, "args", None) or {}
    topic = str(args.get("topic") or "")
    if topic in {"support_product_feedback", "support_complaint_refund"}:
        triggered = True
    elif classify_product_quality_feedback(message or ""):
        try:
            from .external_outbound_context import is_post_purchase_context_active  # noqa: PLC0415

            triggered = is_post_purchase_context_active(state=state, history=history)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — context probe is best-effort
            triggered = False
    if not triggered:
        return
    mark_post_purchase_feedback_active(state, active=True)
    try:
        from .commerce_objective import transition_commerce_objective_for_post_purchase  # noqa: PLC0415

        transition_commerce_objective_for_post_purchase(
            state,
            reason="post_purchase_product_feedback",
        )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — objective shift must not block route
        logger.debug("[POST_PURCHASE_FEEDBACK] objective_shift_failed err=%s", exc)
    try:
        from .commerce_objective import transition_commerce_objective_for_complaint  # noqa: PLC0415

        if topic == "support_complaint_refund":
            transition_commerce_objective_for_complaint(state)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — complaint shift is best-effort
        pass


def try_post_purchase_feedback_decision(ctx: Any) -> Optional[Decision]:
    msg = str(getattr(ctx, "message", "") or "")
    if not classify_product_quality_feedback(msg):
        return None

    try:
        from .external_outbound_context import is_post_purchase_context_active  # noqa: PLC0415

        if not is_post_purchase_context_active(
            state=getattr(ctx, "state", None),
            history=list(getattr(ctx, "history", None) or []),
        ):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — context gate must not break routing
        return None

    topic = "support_product_feedback"
    reason = "post-purchase product feedback — support intake"
    try:
        from .complaint_refund_topic_guard import classify_complaint_refund  # noqa: PLC0415

        if classify_complaint_refund(msg):
            topic = "support_complaint_refund"
            reason = "post-purchase quality issue escalates to complaint/refund support"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — complaint probe is best-effort
        pass

    logger.info(
        "[POST_PURCHASE_FEEDBACK] route=%s tenant=%s preview=%r",
        topic,
        getattr(ctx, "tenant_id", None),
        msg[:72],
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": topic,
            "block_commerce_escalation": True,
            "block_order_flow": True,
        },
        reason=reason,
        confidence=0.96,
    )


__all__ = [
    "apply_post_purchase_feedback_session_flags",
    "classify_product_quality_feedback",
    "is_post_purchase_feedback_active",
    "mark_post_purchase_feedback_active",
    "should_block_post_purchase_order_flow",
    "try_post_purchase_feedback_decision",
]
