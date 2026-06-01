"""
brain/compose/greeting_etiquette.py
───────────────────────────────────
Universal Arabic greeting etiquette — return salam before persona intro.

Tenant-agnostic base layer: when the customer opens with
``السلام عليكم`` (any level), the assistant MUST reciprocate at the
matching level before continuing with the warm intro or answer.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.greeting_etiquette")

SALAM_BASIC = "basic"
SALAM_RAHMA = "rahma"
SALAM_BARAKA = "baraka"

_SALAM_REPEAT_COOLDOWN_TURNS = 3

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = re.sub(r"[^\w\s\u0600-\u06FF]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def detect_salam_level(message: str) -> Optional[str]:
    """
    Detect customer salam level in inbound text.

    Returns ``basic`` | ``rahma`` | ``baraka`` or ``None``.
    """
    norm = _norm(message or "")
    if not norm:
        return None
    has_salam = bool(
        re.search(r"(?:ال)?سلام\s+عل", norm)
        or re.search(r"assalamu?\s*ala", norm)
        or norm.startswith("سلام عليك")
    )
    if not has_salam and "وعليكم" not in norm:
        return None
    if re.search(r"وبركات", norm):
        return SALAM_BARAKA
    if re.search(r"ورحمه|ورحمة", norm):
        return SALAM_RAHMA
    return SALAM_BASIC


def salam_return_text(level: str) -> str:
    """Matching reciprocal salam — concise, one line."""
    if level == SALAM_BARAKA:
        return "وعليكم السلام ورحمة الله وبركاته 🌷"
    if level == SALAM_RAHMA:
        return "وعليكم السلام ورحمة الله 🌷"
    return "وعليكم السلام 🌷"


def reply_already_has_salam_return(reply: str) -> bool:
    head = (reply or "").lstrip()[:120]
    return "وعليكم السلام" in head


def should_skip_repeat_salam_return(state: Any, *, current_turn: int) -> bool:
    """Avoid salam-return spam when customer repeats salaam within a few turns."""
    last_turn = int(getattr(state, "last_salam_return_turn", 0) or 0)
    if last_turn <= 0:
        return False
    turn = int(current_turn or getattr(state, "turn", 0) or 0)
    return (turn - last_turn) < _SALAM_REPEAT_COOLDOWN_TURNS


def log_greeting_return(
    *,
    tenant_id: Any = None,
    level: str = "",
    skipped: bool = False,
    reason: str = "",
) -> None:
    try:
        if skipped:
            logger.info(
                "[GREETING_RETURN] detected=salam response_level=%s skipped=1 reason=%s tenant=%s",
                level or "-",
                reason or "repeat",
                tenant_id,
            )
        else:
            logger.info(
                "[GREETING_RETURN] detected=salam response_level=%s tenant=%s",
                level or "-",
                tenant_id,
            )
    except Exception:  # noqa: BLE001
        pass


def apply_greeting_etiquette(
    reply: str,
    message: str,
    state: Any = None,
    *,
    tenant_id: Any = None,
    current_turn: Optional[int] = None,
) -> str:
    """
    Prepend a level-matched salam return when the customer greeted with salam.

    Does not duplicate if the reply already opens with ``وعليكم السلام``.
    """
    if not isinstance(reply, str) or not reply.strip():
        return reply

    level = detect_salam_level(message)
    if not level:
        return reply

    if reply_already_has_salam_return(reply):
        log_greeting_return(tenant_id=tenant_id, level=level, skipped=True, reason="already_in_reply")
        return reply

    turn = int(
        current_turn if current_turn is not None
        else getattr(state, "turn", 0) or 0
    )
    if state is not None and should_skip_repeat_salam_return(state, current_turn=turn):
        log_greeting_return(tenant_id=tenant_id, level=level, skipped=True, reason="repeat_cooldown")
        return reply

    salam_line = salam_return_text(level)
    log_greeting_return(tenant_id=tenant_id, level=level)

    if state is not None:
        state.last_salam_return_turn = turn
        state.last_salam_return_level = level

    body = reply.strip()
    return f"{salam_line}\n{body}"


def customer_message_for_etiquette(ctx: Any) -> str:
    """Prefer raw inbound text over semantic-repaired message."""
    raw = str(getattr(ctx, "raw_message", "") or "").strip()
    if raw:
        return raw
    return str(getattr(ctx, "message", "") or "").strip()


__all__ = [
    "SALAM_BARAKA",
    "SALAM_BASIC",
    "SALAM_RAHMA",
    "apply_greeting_etiquette",
    "customer_message_for_etiquette",
    "detect_salam_level",
    "log_greeting_return",
    "reply_already_has_salam_return",
    "salam_return_text",
    "should_skip_repeat_salam_return",
]
