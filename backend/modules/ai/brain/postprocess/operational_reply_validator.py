"""
operational_reply_validator.py
──────────────────────────────
Post-compose validator for constrained operational replies.

Guards enforce operational truth — they validate and fail closed to
legacy copy; they must not become the primary copy author long-term.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

from core.reply_instruction import (
    CONSTRAINT_NO_PAYMENT_CONFIRM,
    CONSTRAINT_NO_SHIPPING_PROMISE,
    FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
    ReplyInstruction,
)

_DIA = re.compile(r"[\u064B-\u065F\u0670]")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _DIA.sub("", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return _WS.sub(" ", t).strip().lower()


_SHIP_PROMISE_MARKERS = (
    "تم الشحن",
    "شحناه",
    "في الطريق",
    "سيتم التوصيل",
    "بيوصلك",
)


@dataclass(frozen=True)
class OperationalReplyValidation:
    ok: bool
    reason: str = ""


def validate_operational_reply(
    reply: str,
    instruction: ReplyInstruction,
) -> OperationalReplyValidation:
    """Return ok=False when reply violates instruction constraints."""
    text = (reply or "").strip()
    if not text:
        return OperationalReplyValidation(ok=False, reason="empty_reply")
    if len(text) > 800:
        return OperationalReplyValidation(ok=False, reason="reply_too_long")

    norm = _norm(text)
    constraints = set(instruction.constraints or ())

    if CONSTRAINT_NO_PAYMENT_CONFIRM in constraints:
        markers = instruction.forbidden_claims or FORBIDDEN_PAYMENT_CONFIRM_MARKERS
        for marker in markers:
            if _norm(marker) in norm:
                return OperationalReplyValidation(
                    ok=False,
                    reason=f"forbidden_payment_marker:{marker[:40]}",
                )

    if CONSTRAINT_NO_SHIPPING_PROMISE in constraints:
        for marker in _SHIP_PROMISE_MARKERS:
            if _norm(marker) in norm:
                return OperationalReplyValidation(
                    ok=False,
                    reason=f"forbidden_ship_promise:{marker}",
                )

    return OperationalReplyValidation(ok=True)


__all__ = [
    "OperationalReplyValidation",
    "validate_operational_reply",
]
