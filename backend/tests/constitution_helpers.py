"""Shared helpers for merchant assistant constitution regression tests."""
from __future__ import annotations

import re
import unicodedata
from typing import FrozenSet, Sequence

# Policy §6 / §13 — generic ungrounded line-item placeholders.
GENERIC_PLACEHOLDER_PRODUCT_NAMES: FrozenSet[str] = frozenset(
    {
        "منتج",
        "product",
        "item",
        "شيء",
        "شي",
        "غير محدد",
        "المطلوب",
    }
)

# Policy §11 — support-bot / template-engine openers to flag in anti-template tests.
CONSTITUTION_BANNED_CUSTOMER_OPENERS: FrozenSet[str] = frozenset(
    {
        "أكيد 🌷 تفضل",
        "كيف أقدر أساعدك اليوم؟",
        "تم استلام رسالتك",
    }
)

# Policy §11.1 — non-Saudi Arabic dialect markers (word-boundary match).
NON_SAUDI_ARABIC_DIALECT_TERMS: FrozenSet[str] = frozenset(
    {
        "شنو",
        "بتاعك",
        "إزاي",
        "عامل إيه",
        "دلوقتي",
        "عايز",
        "كيفك",
        "شو",
        "هلأ",
        "بدك",
    }
)

# Checkout pressure appended after pure social inbound (policy §11.2 anti-patterns).
CHECKOUT_PRESSURE_AFTER_SOCIAL_PHRASES: FrozenSet[str] = frozenset(
    {
        "وش طريقة الدفع المناسبة لك؟",
        "وش طريقة الدفع",
        "أرسل عنوانك",
        "أرسل عنوان",
        "نكمل طلبك السابق",
    }
)

_PURE_SOCIAL_INBOUND_MARKERS: FrozenSet[str] = frozenset(
    {
        "كيف الحال",
        "الله يعطيك العافية",
        "شكراً",
        "السلام عليكم",
    }
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")


def _norm_name(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).strip().lower())
    t = _NORM_RE.sub("", t)
    return _WS_RE.sub(" ", t).strip()


def is_generic_placeholder_product_name(name: str) -> bool:
    """True when line-item name is an ungrounded placeholder per policy §6."""
    return _norm_name(name) in {_norm_name(x) for x in GENERIC_PLACEHOLDER_PRODUCT_NAMES}


def line_items_contain_only_generic_placeholders(
    line_items: Sequence[dict],
) -> bool:
    if not line_items:
        return False
    for item in line_items:
        if not isinstance(item, dict):
            return False
        name = str(
            item.get("product_name")
            or item.get("title")
            or item.get("name")
            or ""
        ).strip()
        pid = item.get("product_id") or item.get("sku") or item.get("external_id")
        if pid and not is_generic_placeholder_product_name(name):
            return False
        if not is_generic_placeholder_product_name(name):
            return False
    return True


def contains_banned_template_opener(text: str) -> bool:
    raw = str(text or "")
    for phrase in CONSTITUTION_BANNED_CUSTOMER_OPENERS:
        if phrase in raw:
            return True
    return False


def find_non_saudi_arabic_terms(text: str) -> list[str]:
    """Return banned non-Saudi dialect terms found in *text* (policy §11.1)."""
    raw = str(text or "")
    if not raw.strip():
        return []
    found: list[str] = []
    for term in NON_SAUDI_ARABIC_DIALECT_TERMS:
        if re.search(rf"(?<!\S){re.escape(term)}(?!\S)", raw):
            found.append(term)
    return found


def assert_no_non_saudi_arabic(text: str) -> None:
    """Assert Arabic outbound does not contain banned non-Saudi dialect words."""
    hits = find_non_saudi_arabic_terms(text)
    assert not hits, f"Non-Saudi Arabic dialect terms found: {hits!r} in {text!r}"


def rejects_social_support_bot_phrase(text: str) -> bool:
    """True when text contains a banned support-bot opener (should be rejected)."""
    return contains_banned_template_opener(text)


def rejects_checkout_pressure_after_social(reply: str, inbound_social: str) -> bool:
    """True when a pure social inbound gets checkout slot pressure in the reply."""
    inbound = str(inbound_social or "").strip()
    if inbound not in _PURE_SOCIAL_INBOUND_MARKERS:
        return False
    raw = str(reply or "")
    return any(phrase in raw for phrase in CHECKOUT_PRESSURE_AFTER_SOCIAL_PHRASES)


def social_replies_are_non_deterministic(replies: Sequence[str], *, min_unique: int = 2) -> bool:
    """True when compose outputs show wording variation (policy §11.2)."""
    normalized = {str(r or "").strip() for r in replies if str(r or "").strip()}
    return len(normalized) >= min_unique


def try_compose_persona_samples(
    surface: str,
    inbound: str,
    *,
    samples: int = 5,
) -> list[str]:
    """Target API for FactBoundPersonaComposer — runtime not implemented yet."""
    _ = (surface, inbound, samples)
    try:
        from modules.ai.brain.persona.fact_bound_composer import (  # noqa: PLC0415
            FactBoundPersonaComposer,
        )
    except ImportError as exc:
        raise NotImplementedError(
            "pending FactBoundPersonaComposer runtime"
        ) from exc
    composer = FactBoundPersonaComposer()
    raise NotImplementedError("pending FactBoundPersonaComposer runtime")


def looks_like_invented_payment_credential(text: str) -> bool:
    """Heuristic: IBAN or payment URL in outbound (policy F)."""
    if not text:
        return False
    if re.search(r"\bSA\d{22}\b", text, re.I):
        return True
    if re.search(
        r"https?://[^\s]+(?:pay|payment|checkout|moyasar|stripe|tap)[^\s]*",
        text,
        re.I,
    ):
        return True
    return False
