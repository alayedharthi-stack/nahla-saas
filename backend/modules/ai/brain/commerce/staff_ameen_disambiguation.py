"""
staff_ameen_disambiguation.py
─────────────────────────────
Distinguish religious/conversational **آمين** from showroom staff **أمين**.

After alif normalization both collapse to ``امين``; staff routing must require
explicit contact/role intent, not a bare token match.
"""
from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger("nahla.brain.commerce.staff_ameen_disambiguation")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Raw text — madd-alif آمين is religious even before normalization.
_RAW_RELIGIOUS_AMEEN_RE = re.compile(
    r"آ\s*م\s*ي\s*ن",
    re.UNICODE,
)

_RELIGIOUS_AMEEN_NORM_RE = re.compile(
    r"(?:"
    r"^امين(?:\s+يا\s*رب|\s+يارب)?(?:\s*[!.؟?🌷🤍💛🌹]|$)"
    r"|^اللهم\s+امين"
    r"|^يا\s*رب"
    r"|^امين\s*$"
    r"|جزاك(?:\s+الله)?(?:\s+خير)?[^\n]{0,40}امين"
    r"|^امين\s+يا\s*رب"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_STAFF_AMEEN_RE = re.compile(
    r"(?:"
    r"(?:رقم|اكلم|أكلم|اتصل|اتواصل|تواصل|كلم|"
    r"ابي|ابغى|أبي|أبغى|"
    r"ما\s*يرد|مايرد|"
    r"حولني\s*(?:ل|الى|إلى)?\s*(?:موظف|شخص|بشر))"
    r"\s+.*?\b(?:أ|ا)?مين\b"
    r"|\b(?:أ|ا)?مين\b\s+(?:ما\s*يرد|مايرد|بائع|المعرض|موظف|كاشير|البائع)"
    r"|\b(?:أ|ا)?مين\s+(?:بائع|المعرض)\b"
    r"|(?:ما\s*يرد|مايرد)\s+.*?\b(?:أ|ا)?مين\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_AMEEN_STAFF_TOKENS = frozenset({"امين", "أمين", "آمين"})


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


def is_religious_ameen_context(message: str) -> bool:
    """True for dua/social amen — must not route to staff «أمين»."""
    raw = (message or "").strip()
    if not raw:
        return False
    if has_explicit_staff_ameen_intent(raw):
        return False
    if _RAW_RELIGIOUS_AMEEN_RE.search(raw):
        return True
    norm = _norm(raw)
    if _RELIGIOUS_AMEEN_NORM_RE.search(norm):
        return True
    try:
        from .commerce_conversation_guard import is_social_ack_message  # noqa: PLC0415

        if is_social_ack_message(raw) and re.search(
            r"^(?:امين|اللهم|يا\s*رب|جزاك)",
            norm,
            re.UNICODE,
        ):
            return True
    except Exception:
        logger.exception(
            "[STAFF_AMEEN_DISAMBIGUATION] social_ack_probe_failed preview=%r",
            raw[:80],
        )
    return False


def has_explicit_staff_ameen_intent(message: str) -> bool:
    """True when customer explicitly asks for staff member أمين / showroom seller."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    return bool(_EXPLICIT_STAFF_AMEEN_RE.search(norm) or _EXPLICIT_STAFF_AMEEN_RE.search(raw))


def staff_name_token_allowed(message: str, candidate: str) -> bool:
    """Gate staff alias «امين»/«أمين» — other names pass through unchanged."""
    cand = _norm(candidate or "")
    if cand not in _AMEEN_STAFF_TOKENS and candidate not in _AMEEN_STAFF_TOKENS:
        return True
    if is_religious_ameen_context(message):
        return False
    return has_explicit_staff_ameen_intent(message)


__all__ = [
    "has_explicit_staff_ameen_intent",
    "is_religious_ameen_context",
    "staff_name_token_allowed",
]
