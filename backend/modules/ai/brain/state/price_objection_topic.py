"""
brain/state/price_objection_topic.py
Detect wholesale / competitor price objections.
"""
from __future__ import annotations

import re

# Built from normalized Arabic commerce phrases — avoid matching "ya ghali" alone.
_WHOLESALE = "\u0634\u0628\u0647 \u062c\u0645\u0644"
_COMPETITOR = "\u0645\u0646\u0627\u0641\u0633"
_PRICE_WORD = "\u0633\u0639\u0631"
_EXPENSIVE = "\u063a\u0627\u0644\u064a"
_WHY = "\u0644\u0645\u0627\u0630\u0627"
_WHY2 = "\u0644\u064a\u0634"
_RIYAL = "\u0631\u064a\u0627\u0644"

_PRICE_OBJECTION_RE = re.compile(
    r"(?:"
    + _WHY + r"\s*(?:\u0627\u0644)?(?:" + _PRICE_WORD + r"|\u0627\u0633\u0639\u0627\u0631|\u062b\u0645\u0646)"
    + r"|(?:" + _WHY2 + r"|\u0644\u064a\u0647|" + _WHY + r")\s*(?:\u0643\u0630\u0627|\u0627\u0644\u0633\u0639\u0631|\u0628\u0647\u0630\u0627\s*\u0627\u0644\u0633\u0639\u0631|\u063a(?:\u0627\u0644\u064a|\u0644\u0649))"
    + r"|(?:" + _WHY + r"|" + _WHY2 + r"|\u0644\u064a\u0647).{0,60}(?:" + _PRICE_WORD + r"|\u062b\u0645\u0646|\u063a(?:\u0627\u0644\u064a|\u0644\u0649))"
    + r"|(?:\u0639\u0646\u062f|\u0639\u0646\u062f\u0643\u0645|" + _COMPETITOR + r")"
    + r"|(?:\u0623?\u0631\u062e\u0635|\u0627\u0631\u062e\u0635)\s*\u0645\u0646(?:\u0643\u0645|\u0643)?"
    + r"|(?:" + _WHOLESALE + r"[\u0629\u0647]?)"
    + r"|(?:\u062a\u062e\u0641\u064a\u0636)\s*\u062c(?:\u0645\u0644|\u0645\u0644)[\u0629\u0647]?"
    + r"|(?:\u0627\u0644)?(?:" + _PRICE_WORD + r"|\u062b\u0645\u0646)\s*(?:\u063a(?:\u0627\u0644\u064a|\u0644\u0649)|\u0639(?:\u0627\u0644\u064a|\u0627\u0644\u064a))"
    + r"|\d+\s*" + _RIYAL + r"?"
    + r")",
    re.UNICODE | re.IGNORECASE,
)

_YA_GHALI_RE = re.compile(r"\u064a\u0627\s*\u063a(?:\u0627\u0644\u064a|\u0644\u0649)\b", re.UNICODE)


def _normalize(text: str) -> str:
    try:
        from ..interpret.semantic_turn_interpreter import normalize_ar  # noqa: PLC0415

        return normalize_ar(text or "")
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


def detect_price_objection_topic_shift(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _normalize(raw)
    if not norm:
        return False
    if _YA_GHALI_RE.search(norm) and not _PRICE_OBJECTION_RE.search(
        _YA_GHALI_RE.sub(" ", norm),
    ):
        return False
    return bool(_PRICE_OBJECTION_RE.search(norm))


__all__ = ["detect_price_objection_topic_shift"]
