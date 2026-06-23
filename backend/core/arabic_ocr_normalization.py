"""
core/arabic_ocr_normalization.py
──────────────────────────────────
Platform-wide Arabic text normalization for OCR / PDF extraction.

PDF engines (pypdf, some bank exports) often emit Arabic Presentation
Forms (U+FB50–U+FDFF). Payment and document classifiers match on
canonical Arabic substrings — NFKC plus alef/ya normalization keeps
those paths aligned without merchant-specific rules.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

_AR_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670]")


def normalize_arabic_ocr_text(text: Optional[str], *, lowercase: bool = True) -> str:
    """Normalize OCR/PDF Arabic for deterministic keyword and regex matching.

    Applies NFKC (Presentation Forms → standard letters), strips diacritics
    and tatweel, collapses common alef/ya/ta-marbuta variants. When
    *lowercase* is True (default), lowercases for lexicon matching; set False
    when extracting structured Latin reference tokens.
    Never raises.
    """
    if not text:
        return ""
    try:
        t = unicodedata.normalize("NFKC", str(text))
        t = _AR_DIACRITICS.sub("", t)
        t = t.replace("ـ", "")
        t = (
            t.replace("أ", "ا")
            .replace("إ", "ا")
            .replace("آ", "ا")
            .replace("ى", "ي")
            .replace("ة", "ه")
        )
        if lowercase:
            t = t.lower()
        return t
    except Exception:  # noqa: BLE001
        return ""


def normalize_arabic_presentation_forms(text: Optional[str]) -> str:
    """NFKC-only normalization — preserves readable field text for extraction."""
    if not text:
        return ""
    try:
        return unicodedata.normalize("NFKC", str(text))
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["normalize_arabic_ocr_text", "normalize_arabic_presentation_forms"]
