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

# Courier / role announcements — «معك مندوب سمسا», «I am SMSA courier», …
_ROLE_CONTACT_LEAD_RE = re.compile(
    r"^(?:"
    r"معك|معاك|معكم|"
    r"انا|أنا|an?\b|"
    r"i am|i'm|im\b|this is"
    r")\s+",
    re.UNICODE | re.IGNORECASE,
)

# Courier *role* words — not bare carrier names in post-order shipping asks.
_COURIER_ROLE_WORD_RE = re.compile(
    r"(?:مندوب|موصل|ساعي|courier|delivery\s+agent)",
    re.UNICODE | re.IGNORECASE,
)

_COURIER_ROLE_ANNOUNCE_RE = re.compile(
    r"(?:"
    r"مندوب\s+(?:ال)?(?:شحن|توصيل)|"
    r"مندوب\s+(?:ال)?(?:شحن|توصيل)\s+معك|"
    r"(?:^|\s)(?:i am|i'm|im\b)\s+.*\bcourier\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Existing-order / shipment anchor — not a courier role announcement.
_ORDER_SHIPPING_ANCHOR_RE = re.compile(
    r"(?:"
    r"طلبي|طلبيتي|شحنتي|الشحنه|الشحنة|"
    r"رقم\s*الطلب|رقم\s*التتبع|رابط\s*التتبع|tracking|"
    r"طلبت|سويت\s*طلب|عملت\s*طلب|قدمت\s*طلب|"
    r"order\s*status|track\s*my\s*order|where\s*is\s*my\s*order"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_PRODUCT_INTENT_IN_ROLE_LEAD_RE = re.compile(
    r"(?:"
    r"ابي|ابغى|أبي|أبغى|بدي|اريد|أريد|"
    r"هل|عندكم|عندك|do you have|"
    r"عسل|منتج|product|order|price|سعر|كم\s+سعر"
    r")",
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
    try:
        from ..state.product_information_topic import (  # noqa: PLC0415
            detect_product_information_topic_shift,
        )

        if detect_product_information_topic_shift(text):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product info topic import
        pass
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


def is_negative_logistics_or_contact_context(text: str) -> bool:
    """
    True when inbound must never be adopted as a catalog product label.

    Courier *role* announcements and staff-contact asks — not post-order
    shipping questions that merely mention a carrier name (``طلبي … سمسا``).
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            is_staff_or_contact_context,
        )

        if is_staff_or_contact_context(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional staff guard import
        pass
    try:
        from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
            product_browse_negative_context_reason,
        )

        reason = product_browse_negative_context_reason(raw)
        if reason in {
            "contact_context",
            "staff_contact_context",
            "showroom_escalation_context",
        }:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional discovery gate import
        pass
    return _looks_like_role_or_courier_intro(raw)


def _has_order_shipping_anchor(text: str) -> bool:
    norm = normalize_label_text(text)
    if not norm:
        return False
    return bool(_ORDER_SHIPPING_ANCHOR_RE.search(norm))


def _looks_like_role_or_courier_intro(text: str) -> bool:
    norm = normalize_label_text(text)
    if not norm:
        return False
    if _has_order_shipping_anchor(norm):
        return False
    if _COURIER_ROLE_ANNOUNCE_RE.search(norm):
        return True
    if _ROLE_CONTACT_LEAD_RE.search(norm) and _COURIER_ROLE_WORD_RE.search(norm):
        if not _EXPLICIT_PRODUCT_INTENT_IN_ROLE_LEAD_RE.search(norm):
            return True
    return False


def is_non_product_label(text: str) -> bool:
    """True when text is a meta phrase, not a product name."""
    norm = normalize_label_text(text)
    if not norm or len(norm) < 2:
        return True
    if is_conversational_non_product_inbound(text):
        return True
    if is_negative_logistics_or_contact_context(text):
        return True
    try:
        from modules.ai.brain.commerce.solution_seeking import (  # noqa: PLC0415
            is_use_case_commerce_phrase,
        )

        if is_use_case_commerce_phrase(text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional use-case guard import
        pass
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
    "is_negative_logistics_or_contact_context",
    "is_non_product_label",
    "looks_like_sentence_not_product",
    "normalize_label_text",
    "sanitize_product_label",
]
