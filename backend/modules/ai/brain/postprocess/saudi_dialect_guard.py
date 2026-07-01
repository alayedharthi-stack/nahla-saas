"""
postprocess/saudi_dialect_guard.py
──────────────────────────────────
Pre-send Saudi Arabic neutralizer (P0-A).

Surgical token/phrase correction only — replaces forbidden dialect markers
in-place. Does NOT rewrite whole replies, inject templates, or normalize tone.

Operational facts are preserved. Deterministic replacement map — no LLM.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("nahla.brain.postprocess.saudi_dialect_guard")

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")

# Longer phrases first so partial replacements do not block full phrases.
_REPLACEMENTS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"شنو\s+بالذات", re.UNICODE | re.IGNORECASE), "وش اللي تبحث عنه"),
    (re.compile(r"عنوان(?:ك|كم)\s+بتاع(?:ك|كم)", re.UNICODE | re.IGNORECASE), "عنوانك أو موقعك"),
    (re.compile(r"الكمية\s+كام", re.UNICODE | re.IGNORECASE), "الكمية كم"),
    (re.compile(r"(?<![\w\u0600-\u06FF])كام(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "كم"),
    (re.compile(r"بتاع(?:نا|ك|كم|كن|ه)", re.UNICODE | re.IGNORECASE), "الخاص"),
    (re.compile(r"بتاعت(?:نا|ك|كم|كن|ه)", re.UNICODE | re.IGNORECASE), "الخاص"),
    (re.compile(r"(?<![\w\u0600-\u06FF])لسه(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "باقي"),
    (re.compile(r"(?<![\w\u0600-\u06FF])شنو(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "وش"),
    (re.compile(r"(?<![\w\u0600-\u06FF])عايز(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "أبي"),
    (re.compile(r"(?<![\w\u0600-\u06FF])إ?زاي(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "كيف"),
    (re.compile(r"(?<![\w\u0600-\u06FF])شلون(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "كيف"),
    (re.compile(r"(?<![\w\u0600-\u06FF])هسة(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "الحين"),
    (re.compile(r"(?<![\w\u0600-\u06FF])هوا?ية(?![\w\u0600-\u06FF])", re.UNICODE | re.IGNORECASE), "كثير"),
)


@dataclass(frozen=True)
class SaudiDialectGuardResult:
    reply: str
    replaced: bool
    hits: Tuple[str, ...] = ()


def _normalize_for_scan(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = _DIA.sub("", t)
    return t


def _detect_hits(text: str) -> List[str]:
    norm = _normalize_for_scan(text).lower()
    hits: List[str] = []
    markers = (
        "شنو", "شلون", "هسة", "هواية", "هوية",
        "بتاع", "بتاعك", "بتاعنا", "بتاعت", "بتاعتنا",
        "كام", "عايز", "إزاي", "ازاي", "لسه",
    )
    for marker in markers:
        if marker in norm:
            hits.append(marker)
    return hits


def apply_saudi_dialect_guard(
    reply: str,
    *,
    locale: str = "ar",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> SaudiDialectGuardResult:
    raw = reply or ""
    loc = (locale or "ar").strip().lower()
    if not raw.strip() or not loc.startswith("ar"):
        return SaudiDialectGuardResult(reply=raw, replaced=False)

    hits_before = _detect_hits(raw)
    if not hits_before:
        return SaudiDialectGuardResult(reply=raw, replaced=False)

    updated = raw
    for pattern, replacement in _REPLACEMENTS:
        updated = pattern.sub(replacement, updated)

    updated = re.sub(r"\s{2,}", " ", updated)
    updated = re.sub(r"\s+([،.!؟?])", r"\1", updated)
    updated = updated.strip()

    replaced = updated != raw
    if replaced:
        logger.info(
            "[SAUDI_DIALECT_GUARD] replaced tenant=%s conversation=%s hits=%s",
            tenant_id,
            conversation_id,
            hits_before,
        )
    return SaudiDialectGuardResult(
        reply=updated,
        replaced=replaced,
        hits=tuple(hits_before),
    )


__all__ = [
    "SaudiDialectGuardResult",
    "apply_saudi_dialect_guard",
]
