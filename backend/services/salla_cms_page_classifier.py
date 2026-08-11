"""
DEFERRED — Deterministic Salla CMS page classifier (NOT Pack A1 runtime).

Preserved for a future CMS-import pack. Pack A1 kinds registry remains
source-independent in ``knowledge_section_kinds`` / retrieval DOCUMENT_KINDS.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Canonical kinds used by Pack A1. ``about_us`` maps to existing ``store_story``.
# ``warranty_policy`` maps to existing ``warranty``.
STORE_STORY_KIND = "store_story"
RETURN_POLICY_KIND = "return_policy"
REFUND_POLICY_KIND = "refund_policy"
EXCHANGE_POLICY_KIND = "exchange_policy"
SHIPPING_POLICY_KIND = "shipping_policy"
TERMS_POLICY_KIND = "terms_policy"
PRIVACY_POLICY_KIND = "privacy_policy"
WARRANTY_KIND = "warranty"
FAQ_KIND = "faq"
CUSTOM_KIND = "custom"

# Policy kinds eligible for MERCHANT_POLICY tri-state existence facts.
POLICY_KIND_KEYS = (
    RETURN_POLICY_KIND,
    REFUND_POLICY_KIND,
    EXCHANGE_POLICY_KIND,
    SHIPPING_POLICY_KIND,
    TERMS_POLICY_KIND,
    PRIVACY_POLICY_KIND,
    WARRANTY_KIND,
)

# Story / about kinds eligible for store-story retrieval.
STORY_KINDS = frozenset({STORE_STORY_KIND})

# Patterns ordered most-specific first. Each entry: (kind, compiled regex).
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        PRIVACY_POLICY_KIND,
        re.compile(
            r"("
            r"خصوصي|"
            r"privacy|"
            r"data[\s_-]*protection|"
            r"حماية[\s_-]*البيانات"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        SHIPPING_POLICY_KIND,
        re.compile(
            r"("
            r"سياسة[\s_-]*(?:الشحن|التوصيل)|"
            r"شروط[\s_-]*(?:الشحن|التوصيل)|"
            r"shipping[\s_-]*polic|"
            r"delivery[\s_-]*polic|"
            r"shipping[\s_-]*terms|"
            r"delivery[\s_-]*terms"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        TERMS_POLICY_KIND,
        re.compile(
            r"("
            r"شروط[\s_-]*(?:الاستخدام|الخدمة|المتجر)|"
            r"الأحكام[\s_-]*والشروط|"
            r"الشروط[\s_-]*والأحكام|"
            r"terms(?:[\s_-]*and[\s_-]*conditions)?|"
            r"conditions[\s_-]*of[\s_-]*(?:use|sale)|"
            r"tos|"
            r"usage[\s_-]*policy|"
            r"اتفاقية[\s_-]*الاستخدام"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        REFUND_POLICY_KIND,
        re.compile(
            r"("
            r"استرداد|"
            r"refund|"
            r"money[\s_-]*back"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        EXCHANGE_POLICY_KIND,
        re.compile(
            r"("
            r"استبدال|"
            r"تبديل|"
            r"exchange|"
            r"swap"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        RETURN_POLICY_KIND,
        re.compile(
            r"("
            r"استرجاع|"
            r"إرجاع|"
            r"ارجاع|"
            r"return|"
            r"returns"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        WARRANTY_KIND,
        re.compile(
            r"("
            r"ضمان|"
            r"warranty|"
            r"guarantee"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        FAQ_KIND,
        re.compile(
            r"("
            r"أسئلة|"
            r"سؤال|"
            r"faq|"
            r"frequently|"
            r"common[\s_-]*questions"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        STORE_STORY_KIND,
        re.compile(
            r"("
            r"قص[ةه]|"
            r"من[\s_-]*نحن|"
            r"عن[\s_-]*(?:المتجر|الشركة|نا)|"
            r"about|"
            r"our[\s_-]*story|"
            r"store[\s_-]*story|"
            r"who[\s_-]*we[\s_-]*are"
            r")",
            re.IGNORECASE,
        ),
    ),
)

# Ambiguous multi-hit pairs: return+exchange on same page is common in KSA
# and maps safely to return_policy (covers both in one doc). Other multi-hits
# that are unrelated → custom.
_COMPATIBLE_MULTI = frozenset({
    frozenset({RETURN_POLICY_KIND, EXCHANGE_POLICY_KIND}),
    frozenset({RETURN_POLICY_KIND, REFUND_POLICY_KIND}),
    frozenset({RETURN_POLICY_KIND, EXCHANGE_POLICY_KIND, REFUND_POLICY_KIND}),
})


def _normalize_blob(title: str, slug: str) -> str:
    raw = f"{title or ''} {slug or ''}".strip().lower()
    raw = unicodedata.normalize("NFKC", raw)
    raw = raw.replace("_", " ").replace("-", " ")
    raw = re.sub(r"\s+", " ", raw)
    return raw


def classify_salla_cms_page(
    *,
    title: str = "",
    slug: str = "",
) -> str:
    """Return a registry kind for a Salla CMS page.

    Uncertain / conflicting unrelated matches → ``custom``.
    Never invents merchant-specific meaning.
    """
    blob = _normalize_blob(title, slug)
    if not blob:
        return CUSTOM_KIND

    hits: list[str] = []
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(blob):
            hits.append(kind)

    if not hits:
        return CUSTOM_KIND
    if len(hits) == 1:
        return hits[0]

    hit_set = frozenset(hits)
    if hit_set in _COMPATIBLE_MULTI:
        return RETURN_POLICY_KIND

    # Shipping + company names is still shipping_policy for CMS prose.
    # Unrelated pairs (e.g. privacy + shipping) → custom.
    if len(hit_set) > 1:
        return CUSTOM_KIND
    return hits[0]


def is_policy_truth_kind(kind: Optional[str]) -> bool:
    """True when kind may drive MERCHANT_POLICY existence facts."""
    k = (kind or "").strip().lower()
    return k in POLICY_KIND_KEYS
