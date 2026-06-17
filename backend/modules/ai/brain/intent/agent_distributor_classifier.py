"""
agent_distributor_classifier.py
───────────────────────────────
Platform-wide detection of agent / distributor / authorized-dealer inquiries.
Not product availability — must not route to inventory fallback.
"""
from __future__ import annotations

import re
import unicodedata

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_WS = re.compile(r"\s+")

_AGENT_DISTRIBUTOR_RE = re.compile(
    r"(?:"
    r"(?:مين|من|اين|أين|وين|فين|عند(?:كم|ك|ك)?)\s*(?:ال)?(?:و(?:كيل|کل)|موزع|ممثل|موزعين|وكلاء)"
    r"|(?:و(?:كيل|کل)|موزع|ممثل|موزعين|وكلاء)\s*(?:في|ب|عند)?"
    r"|(?:نقط(?:ه|ة)|نقطة)\s*(?:بيع|توزيع)"
    r"|(?:جه(?:ه|ة)|جهه)\s*(?:معتمد(?:ه|ة)|رسم(?:ي|ية))"
    r"|(?:معرض|فرع)\s*(?:معتمد|رسمي)?"
    r"|(?:authorized|official)\s*(?:dealer|distributor|reseller|agent)"
    r"|(?:distributor|reseller|dealer)\s*(?:in|for|near)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _DIA.sub("", s)
    s = (
        s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    )
    return _WS.sub(" ", s.lower()).strip()


def is_agent_distributor_inquiry(message: str) -> bool:
    """True when the customer asks about agents, distributors, or authorized outlets."""
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_AGENT_DISTRIBUTOR_RE.search(_norm(raw)))


__all__ = ["is_agent_distributor_inquiry"]
