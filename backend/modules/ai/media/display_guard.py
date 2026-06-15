"""
Media Display Guard
───────────────────
Prevents raw file-extraction dumps (PDF text, OCR output, internal
metadata) from reaching the customer on outbound WhatsApp replies.

Operational truth stays internal (``metadata.pdf_text_full``,
``MediaNormalizationResult.text``); customer-facing copy must be
short and honest.
"""
from __future__ import annotations

import logging
import re
from typing import Tuple

logger = logging.getLogger("nahla.ai.media.display_guard")

# Markers that indicate brain/internal extraction leaked into outbound.
_EXTRACTION_MARKERS: tuple[str, ...] = (
    "نص الملف المستخرج",
    "نص الملف المستخرج:",
    "[وثيقة PDF — تصنيف:",
    "pdf_text_full",
    "…[تم اقتطاع النص الزائد]",
)

# Provider / storage URLs must never appear in customer messages.
_INTERNAL_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"lookaside\.fbsbx\.com", re.I),
    re.compile(r"/media/inbound/\d+/", re.I),
    re.compile(r"waba-v2\.360dialog\.io", re.I),
)

_SAFE_REPLACEMENT_AR = (
    "وصل الملف، شكراً لك. راجعناه وسنكمل معك حسب السياق."
)

# Outbound replies longer than this that match extraction heuristics
# are treated as dumps even without an explicit marker.
_MAX_EXTRACTION_DUMP_LEN = 900


def looks_like_media_extraction_dump(text: str) -> bool:
    """Return True when *text* looks like internal file extraction."""
    if not text or not text.strip():
        return False
    body = text.strip()
    for marker in _EXTRACTION_MARKERS:
        if marker in body:
            return True
    for pat in _INTERNAL_URL_PATTERNS:
        if pat.search(body):
            return True
    if body.lstrip().startswith(("{", "[")) and '"pdf_' in body:
        return True
    if len(body) > _MAX_EXTRACTION_DUMP_LEN and (
        "مستخرج" in body
        or "pdf" in body.lower()
        or "ocr" in body.lower()
    ):
        return True
    return False


def apply_media_display_outbound_guard(reply_text: str) -> Tuple[str, bool]:
    """Replace extraction dumps with a short safe acknowledgement.

    Returns ``(possibly_scrubbed_text, was_scrubbed)``.
    Never raises.
    """
    if not looks_like_media_extraction_dump(reply_text or ""):
        return reply_text or "", False
    logger.info(
        "[MEDIA_DISPLAY_GUARD] blocked extraction dump outbound "
        "orig_len=%d",
        len(reply_text or ""),
    )
    return _SAFE_REPLACEMENT_AR, True
