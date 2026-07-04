"""
commerce_tail_guard.py
──────────────────────
Post-compose guard: strip generic CS/commerce closers from pure social replies.

Context-aware — preserves operational tails tied to orders/fulfillment.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from core.fallback_policy import strip_closer_segments

try:
    from modules.ai.brain.postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
        is_pure_phatic_bypass_turn,
    )
except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import boundary

    def is_pure_phatic_bypass_turn(inbound_text: str) -> bool:  # type: ignore[misc]
        return False


logger = logging.getLogger("nahla.brain.postprocess.commerce_tail_guard")

_OPERATIONAL_TAIL_MARKERS: tuple[str, ...] = (
    "تعديل على الطلب",
    "تعديل الطلب",
    "تعديل طلبك",
    "تعديل على طلبك",
    "متابعة الطلب",
    "متابعة طلبك",
    "رقم الطلب",
    "تم تسجيل طلب",
    "تم انشاء طلب",
    "تم إنشاء طلب",
    "حالة الطلب",
    "تتبع الطلب",
    "رابط الدفع",
    "اكمل الدفع",
    "أكمل الدفع",
)

_OPERATIONAL_OBJECTIVES = frozenset({
    "order",
    "checkout",
    "fulfillment",
    "payment",
    "shipping",
    "track_order",
    "order_confirmation",
})

_OPERATIONAL_CHOSEN_PATH_TOKENS = (
    "order", "checkout", "payment", "track", "fulfillment", "receipt",
)


@dataclass(frozen=True)
class CommerceTailGuardResult:
    reply: str
    stripped: bool
    reason: str


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t)


def _has_operational_tail(reply: str) -> bool:
    norm = _norm_ar(reply)
    return any(_norm_ar(m) in norm for m in _OPERATIONAL_TAIL_MARKERS)


def _resolve_reply_type(
    *,
    ctx: Any,
    intent_name: str,
    chosen_path: str,
) -> str:
    shc = getattr(ctx, "social_human_context", None) if ctx else None
    if shc is not None:
        rt = str(getattr(shc, "reply_type", "") or "").strip()
        if rt:
            return rt
    slots: dict = {}
    if ctx is not None:
        intent = getattr(ctx, "intent", None)
        slots = dict(getattr(intent, "slots", None) or {})
    rt = str(slots.get("social_human_reply_type") or "").strip()
    if rt:
        return rt
    if intent_name == "social":
        return "social"
    cp = chosen_path.lower()
    if any(tok in cp for tok in _OPERATIONAL_CHOSEN_PATH_TOKENS):
        return "operational"
    return "commerce"


def _should_strip_tail(
    *,
    ctx: Any,
    intent_name: str,
    reply_type: str,
    conversation_objective: str,
    chosen_path: str,
    inbound_text: str = "",
) -> tuple[bool, str]:
    if is_pure_phatic_bypass_turn(inbound_text):
        return True, "phatic_bypass_checkout_pressure"

    if reply_type in {"commerce", "operational", "mixed"}:
        return False, f"reply_type:{reply_type}"

    obj = str(conversation_objective or "").strip().lower()
    if obj in _OPERATIONAL_OBJECTIVES:
        return False, f"objective:{obj}"

    cp = str(chosen_path or "").lower()
    if any(tok in cp for tok in _OPERATIONAL_CHOSEN_PATH_TOKENS):
        return False, f"chosen_path:{cp[:40]}"

    shc = getattr(ctx, "social_human_context", None) if ctx else None
    if shc is not None:
        if not getattr(shc, "is_pure_social_turn", False):
            return False, "mixed_turn_commercial_primary"
        if not getattr(shc, "block_commerce_tail", False):
            return False, "shc_no_block"
        if getattr(shc, "in_commerce_context", False):
            return False, "in_commerce_context"

    if intent_name == "social" and shc is None:
        return True, "intent_social"

    if shc is not None and getattr(shc, "block_commerce_tail", False):
        return True, f"social_human:{getattr(shc, 'category', '')}"

    return False, ""


def apply_commerce_tail_guard(
    reply: str,
    *,
    ctx: Any = None,
    intent_name: str = "",
    inbound_text: str = "",
    conversation_objective: str = "",
    chosen_path: str = "",
    tenant_id: Optional[int] = None,
) -> CommerceTailGuardResult:
    text = (reply or "").strip()
    if not text:
        return CommerceTailGuardResult(reply="", stripped=False, reason="empty")

    if _has_operational_tail(text):
        return CommerceTailGuardResult(
            reply=text,
            stripped=False,
            reason="operational_tail_preserved",
        )

    reply_type = _resolve_reply_type(
        ctx=ctx,
        intent_name=intent_name,
        chosen_path=chosen_path,
    )
    should_strip, reason = _should_strip_tail(
        ctx=ctx,
        intent_name=intent_name,
        reply_type=reply_type,
        conversation_objective=conversation_objective,
        chosen_path=chosen_path,
        inbound_text=inbound_text,
    )
    if not should_strip:
        return CommerceTailGuardResult(reply=text, stripped=False, reason=reason)

    cleaned, stripped = strip_closer_segments(text, non_commerce=True)
    if stripped:
        logger.info(
            "[COMMERCE_TAIL_GUARD] tenant=%s reason=%s reply_type=%s "
            "objective=%s orig_len=%d new_len=%d preview_in=%r",
            tenant_id if tenant_id is not None else "-",
            reason,
            reply_type,
            conversation_objective or "-",
            len(text),
            len(cleaned),
            (inbound_text or "")[:60],
        )

    return CommerceTailGuardResult(
        reply=cleaned,
        stripped=stripped,
        reason=reason if stripped else reason,
    )


__all__ = ["CommerceTailGuardResult", "apply_commerce_tail_guard"]
