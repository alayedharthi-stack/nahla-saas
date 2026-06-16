"""
product_label_hygiene.py
────────────────────────
Platform-wide guard: meta-phrases must not become product labels.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

_NORM_RE = re.compile(r"[\u064B-\u065F\u0670]")

# Meta / follow-up phrases — not catalog product names.
_NON_PRODUCT_LABEL_RE = re.compile(
    r"(?:"
    r"^(?:وش|ما|كم|أ?رسل|ارسل|أ?رسلي|send|show|list|what|which|how many)\b"
    r"|"
    r"\b(?:الخيارات|خيارات|options|choices|variants|الأنواع|انواع|types|"
    r"التفاصيل|details|المقاس|مقاس|size|sizes|الحجم|حجم|"
    r"الكمية|كمية|quantity|qty|العدد|عدد|"
    r"المتوفر|available|catalog)\b"
    r"|"
    r"^(?:ال)?(?:خيارات|options|choices|types|أنواع)(?:\s|$)"
    r"|"
    r"(?:أ?رسل|ارسل|send)\s*(?:لي\s+)?(?:ال)?(?:خيارات|options|choices|types|أنواع)"
    r"|"
    r"^(?:كم\s+)?(?:عدد|quantity)\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SEND_OPTIONS_LEADING_RE = re.compile(
    r"^(?:أ?رسل|ارسل|send)\s*(?:لي\s+)?(?:ال)?",
    re.UNICODE | re.IGNORECASE,
)


def normalize_label_text(text: str) -> str:
    raw = unicodedata.normalize("NFKC", (text or "").strip())
    raw = _NORM_RE.sub("", raw)
    return re.sub(r"\s+", " ", raw).strip("؟?.,! ")


def is_non_product_label(text: str) -> bool:
    """True when text is a meta phrase, not a product name."""
    norm = normalize_label_text(text)
    if not norm or len(norm) < 2:
        return True
    if _NON_PRODUCT_LABEL_RE.search(norm):
        return True
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            is_order_tracking_follow_up,
        )

        if is_order_tracking_follow_up(norm):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import at label boundary
        pass
    # Bare "خيارات" / "options" with optional leading send verb residue.
    stripped = _SEND_OPTIONS_LEADING_RE.sub("", norm).strip()
    if stripped in {
        "الخيارات",
        "خيارات",
        "options",
        "choices",
        "الأنواع",
        "انواع",
        "types",
        "التفاصيل",
        "details",
    }:
        return True
    return False


def sanitize_product_label(
    text: str,
    *,
    fallback: Optional[str] = None,
) -> str:
    """Return cleaned product label or fallback when text is not a product name."""
    cleaned = normalize_label_text(text)
    if not cleaned or is_non_product_label(cleaned):
        return normalize_label_text(fallback or "")
    return cleaned


__all__ = [
    "is_non_product_label",
    "normalize_label_text",
    "sanitize_product_label",
]
