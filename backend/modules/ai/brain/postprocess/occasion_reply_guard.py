"""
occasion_reply_guard.py
───────────────────────
Post-compose guard: strip holiday/occasion template lines unless inbound
carries explicit occasion signal (P1-D-3).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from modules.ai.brain.intent.non_commerce_classifier import inbound_has_occasion_signal

logger = logging.getLogger("nahla.brain.postprocess.occasion_reply_guard")

_OCCASION_OUTBOUND_MARKERS: tuple[str, ...] = (
    "كل عام وانت بخير",
    "كل عام وأنت بخير",
    "كل عام وانتم بخير",
    "كل عام وأنتم بخير",
    "عيدكم مبارك",
    "عيد مبارك",
    "ايامكم مباركه",
    "أيامكم مباركة",
    "الله يجعل أيامكم مباركة",
    "تقبل الله",
    "رمضان مبارك",
    "جمعة مباركة",
)


def _normalize_ar(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def contains_occasion_outbound(text: str) -> bool:
    norm = _normalize_ar(text)
    if not norm:
        return False
    return any(_normalize_ar(m) in norm for m in _OCCASION_OUTBOUND_MARKERS)


def strip_occasion_segments(text: str) -> tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    kept_paragraphs: list[str] = []

    for paragraph in re.split(r"\n\s*\n", raw):
        p = paragraph.strip()
        if not p:
            continue
        p_norm = _normalize_ar(p)
        if any(_normalize_ar(m) in p_norm for m in _OCCASION_OUTBOUND_MARKERS):
            stripped_any = True
            continue
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        kept_lines = [
            ln for ln in lines
            if not any(_normalize_ar(m) in _normalize_ar(ln) for m in _OCCASION_OUTBOUND_MARKERS)
        ]
        if len(kept_lines) < len(lines):
            stripped_any = True
        if kept_lines:
            kept_paragraphs.append("\n".join(kept_lines))

    return "\n\n".join(kept_paragraphs).strip(), stripped_any


@dataclass(frozen=True)
class OccasionReplyGuardResult:
    reply: str
    stripped: bool


def apply_occasion_reply_guard(
    reply: str,
    *,
    inbound_text: str = "",
    inbound_metadata: Optional[dict[str, Any]] = None,
    tenant_id: Optional[int] = None,
) -> OccasionReplyGuardResult:
    text = (reply or "").strip()
    if not text:
        return OccasionReplyGuardResult(reply="", stripped=False)

    meta = inbound_metadata if isinstance(inbound_metadata, dict) else {}
    if meta.get("inbound_occasion_signal") or inbound_has_occasion_signal(inbound_text):
        return OccasionReplyGuardResult(reply=text, stripped=False)

    if not contains_occasion_outbound(text):
        return OccasionReplyGuardResult(reply=text, stripped=False)

    cleaned, stripped = strip_occasion_segments(text)
    if stripped:
        logger.info(
            "[OCCASION_REPLY_GUARD] tenant=%s orig_len=%d new_len=%d preview_in=%r",
            tenant_id if tenant_id is not None else "-",
            len(text),
            len(cleaned),
            (inbound_text or "")[:60],
        )

    return OccasionReplyGuardResult(reply=cleaned, stripped=stripped)


__all__ = [
    "OccasionReplyGuardResult",
    "apply_occasion_reply_guard",
    "contains_occasion_outbound",
    "strip_occasion_segments",
]
