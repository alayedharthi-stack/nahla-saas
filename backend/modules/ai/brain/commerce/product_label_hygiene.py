"""
product_label_hygiene.py
────────────────────────
Platform-wide guard: meta-phrases must not become product labels.

Layer 1 — identity / collaboration / experience phrases (not catalog names).
Layer 2 — sentence structure (pronouns, intro verbs, word-count cap).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

_NORM_RE = re.compile(r"[\u064B-\u065F\u0670]")
_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")

# Catalog product names are short noun phrases — not full self-intro sentences.
_MAX_PRODUCT_LABEL_WORDS = 5

# Layer 1 — explicit identity / collaboration / experience phrases.
_IDENTITY_INTRO_PHRASES_RAW = (
    "انا معلم نحل",
    "انا معلم في النحل",
    "معلم نحل",
    "معلم في النحل",
    "مربي نحل",
    "نحال",
    "نحّال",
    "عندي خبرة",
    "عندي خبره",
    "اشتغل بالنحل",
    "اشتغل في النحل",
    "ابحث عن عمل",
    "أبحث عن عمل",
    "حاب اتعاون معكم",
    "حاب أتعاون معكم",
    "حاب ادوم معكم",
    "حاب أدام معكم",
    "حبيت ادوم معاكم",
    "حبيت أدام معاكم",
    "i am a beekeeper",
    "beekeeper",
    "looking for work",
    "want to collaborate",
)

# Layer 2 — structural signals (first-person lead, intro/collaboration verbs).
_IDENTITY_PRONOUN_LEAD_RE = re.compile(
    r"^(?:"
    r"انا|أنا|ان|أن|"
    r"عندي|عندنا|"
    r"احنا|إحنا|نحن|"
    r"my name is|i am|i'm|im\b"
    r")\b",
    re.UNICODE | re.IGNORECASE,
)

_IDENTITY_INTRO_MARKER_RE = re.compile(
    r"(?:"
    r"معلم|مربي|نحال|نحّال|beekeeper|"
    r"خبر[ةه]|experience|"
    r"اشتغل|اعمل|أعمل|"
    r"تعاون|ادوم|أدام|join|work with|"
    r"(?:اتواصل|أتواصل|تواصل|اكلم|أكلم)\s*مع(?:ك|اك|كم|كن|اكم|اكم)|"
    r"اب[يى]?\s+(?:عمل|تعاون|انضم)|"
    r"حاب\s+(?:اتعاون|أتعاون|ادوم|أدام|اعمل|أعمل)|"
    r"حبيت\s+(?:ادوم|أدام|اتعاون|أتعاون)|"
    r"ابحث\s+عن\s+عمل"
    r")",
    re.UNICODE | re.IGNORECASE,
)

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


def _norm_ar_for_match(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s.lower()).strip()


_IDENTITY_INTRO_PHRASES = tuple(
    _norm_ar_for_match(p) for p in _IDENTITY_INTRO_PHRASES_RAW
)


def is_identity_or_intro_phrase(text: str) -> bool:
    """Layer 1 — self-intro, experience, or collaboration phrases."""
    norm = _norm_ar_for_match(text or "")
    if not norm:
        return False
    if norm in _IDENTITY_INTRO_PHRASES:
        return True
    return any(phrase in norm for phrase in _IDENTITY_INTRO_PHRASES if len(phrase) >= 8)


def looks_like_sentence_not_product(text: str) -> bool:
    """Layer 2 — structural heuristics: not a short catalog product name."""
    norm = normalize_label_text(text)
    if not norm:
        return False
    words = [w for w in norm.split() if w]
    has_identity_signal = bool(
        _IDENTITY_PRONOUN_LEAD_RE.search(norm)
        or _IDENTITY_INTRO_MARKER_RE.search(norm)
    )
    if has_identity_signal:
        return True
    # Very long free-text inbounds are never bare product names.
    if len(words) > 8:
        return True
    return False


def is_conversational_non_product_inbound(text: str) -> bool:
    """True when inbound is identity/conversation — never a product label."""
    return is_identity_or_intro_phrase(text) or looks_like_sentence_not_product(text)


def is_non_product_label(text: str) -> bool:
    """True when text is a meta phrase, not a product name."""
    norm = normalize_label_text(text)
    if not norm or len(norm) < 2:
        return True
    if is_conversational_non_product_inbound(text):
        return True
    if _NON_PRODUCT_LABEL_RE.search(norm):
        return True
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            is_shipping_tracking_non_product_label,
        )

        if is_shipping_tracking_non_product_label(text):
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
    "is_conversational_non_product_inbound",
    "is_identity_or_intro_phrase",
    "is_non_product_label",
    "looks_like_sentence_not_product",
    "normalize_label_text",
    "sanitize_product_label",
]
