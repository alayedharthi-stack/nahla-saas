"""
Safe document/PDF display helpers for merchant dashboard cards.

Internal extraction (``pdf_text_preview``, OCR output) must not surface
when garbled, too long, or obviously raw dump material.
"""
from __future__ import annotations

import re
from typing import Optional

DOCUMENT_CARD_FALLBACK_AR = "تم استلام ملف PDF ويمكن فتحه أو تحميله."

_MAX_SUMMARY_LEN = 280

_MOJIBAKE_RE = re.compile(
    r"[\u00c0-\u00ff]{2,}|Ã.|Ø.|Ù.|â.|ï¿½",
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"\d")

_INTERNAL_DUMP_MARKERS = (
    "نص الملف المستخرج",
    "pdf_text_full",
    "[وثيقة PDF",
)


def is_readable_document_summary(text: str, *, max_len: int = _MAX_SUMMARY_LEN) -> bool:
    """True when *text* is safe to show as a short merchant-facing summary."""
    raw = (text or "").strip()
    if not raw:
        return False
    if len(raw) > max_len:
        raw = raw[:max_len]
    for marker in _INTERNAL_DUMP_MARKERS:
        if marker in raw:
            return False
    if "\ufffd" in raw or "�" in raw:
        return False
    if _CONTROL_RE.search(raw):
        return False
    if _MOJIBAKE_RE.search(raw):
        return False

    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return False

    arabic = len(_ARABIC_RE.findall(raw))
    latin = len(_LATIN_RE.findall(raw))
    digits = len(_DIGIT_RE.findall(raw))
    readable = arabic + latin + digits
    total = len(compact)
    if total == 0 or readable / total < 0.55:
        return False

    symbols = total - readable
    if symbols / total > 0.2:
        return False

    # Long blobs without word structure are usually OCR dumps.
    words = [w for w in raw.split() if w.strip()]
    if len(words) < 2 and len(raw) > 48:
        return False

    return True


def safe_document_summary_for_display(
    preview: Optional[str],
    *,
    max_len: int = _MAX_SUMMARY_LEN,
) -> Optional[str]:
    """Return a trimmed summary or ``None`` when preview is not display-safe."""
    raw = (preview or "").strip()
    if not raw:
        return None
    snippet = raw
    if len(snippet) > max_len:
        snippet = snippet[:max_len].rstrip() + "…"
    if not is_readable_document_summary(snippet, max_len=max_len):
        return None
    return snippet
