"""Shared helpers for merchant assistant constitution regression tests."""
from __future__ import annotations

import re
import unicodedata
from typing import FrozenSet, Sequence

from core.generic_line_item_guard import (  # noqa: F401
    GENERIC_PLACEHOLDER_PRODUCT_NAMES,
    is_generic_placeholder_product_name,
    line_items_contain_only_generic_placeholders,
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
        "اسمك الكامل",
        "محتاج اسمك الكامل",
        "نحتاج اسمك الكامل",
        "عشان نكمل الطلب",
        "عشان نخلص الطلب",
        "نكمل معك",
        "ممكن اسمك",
        "الاسم الكامل لو تكرمت",
    }
)

_PURE_SOCIAL_INBOUND_MARKERS: FrozenSet[str] = frozenset(
    {
        "كيف الحال",
        "الله يعطيك العافية",
        "شكراً",
        "السلام عليكم",
        "انت وش أخبارك؟",
        "ما قصرت",
    }
)

# Policy §5.1 — re-asking known customer facts (anti-patterns).
KNOWN_CUSTOMER_NAME_REASK_PHRASES: FrozenSet[str] = frozenset(
    {
        "اسمك الكامل",
        "وش اسمك الكامل",
        "ممكن تذكر اسمك",
        "اكتب اسمك",
        "محتاج اسمك الكامل",
        "نحتاج اسمك الكامل",
    }
)

KNOWN_CUSTOMER_PHONE_REASK_PHRASES: FrozenSet[str] = frozenset(
    {
        "رقم جوالك",
        "رقم الجوال",
        "رقم هاتفك",
        "الجوال للتواصل",
        "رقم جوال",
    }
)

KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES: FrozenSet[str] = frozenset(
    {
        "أرسل عنوانك",
        "أرسل لي عنوانك",
        "شاركنا عنوانك",
    }
)

_SAVED_ADDRESS_CONFIRM_MARKERS: FrozenSet[str] = frozenset(
    {
        "نعتمده",
        "نعتمد نفس العنوان",
        "العنوان المسجل",
        "العنوان السابق",
        "المحفوظ عندنا",
        "هل نعتمد",
    }
)


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


def contains_known_customer_name_reask(reply: str) -> bool:
    """True when outbound asks for full name (policy §5.1 anti-pattern)."""
    raw = str(reply or "")
    return any(phrase in raw for phrase in KNOWN_CUSTOMER_NAME_REASK_PHRASES)


def contains_phone_number_reask(reply: str) -> bool:
    """True when outbound asks for phone (policy §5.1 — use WhatsApp sender)."""
    raw = str(reply or "")
    return any(phrase in raw for phrase in KNOWN_CUSTOMER_PHONE_REASK_PHRASES)


def is_blunt_address_collect_ask(reply: str) -> bool:
    """True when outbound bluntly demands address without saved-address confirm."""
    raw = str(reply or "")
    if not any(phrase in raw for phrase in KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES):
        return False
    return not any(marker in raw for marker in _SAVED_ADDRESS_CONFIRM_MARKERS)


def prefers_saved_address_confirm(reply: str) -> bool:
    """True when outbound confirms a saved/on-file address instead of blunt collect."""
    raw = str(reply or "")
    return any(marker in raw for marker in _SAVED_ADDRESS_CONFIRM_MARKERS)


_EMOJI_CHAR_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
_EMOJI_UNIT_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\uFE0F"
    "]",
    flags=re.UNICODE,
)

# Policy §11.3 — context buckets for marketing emoji vocabulary.
MARKETING_EMOJI_WARMTH: FrozenSet[str] = frozenset({"😊", "🙂", "😄", "🤍", "🌷", "✨"})
MARKETING_EMOJI_SHOPPING: FrozenSet[str] = frozenset(
    {"🛒", "🛍️", "🛍", "🧺", "🏷️", "🏷", "💳", "💰"}
)
MARKETING_EMOJI_OFFERS: FrozenSet[str] = frozenset(
    {"🔥", "⚡", "🚀", "⏳", "⏰", "🎯", "💥", "✨", "🏷️", "🏷"}
)
MARKETING_EMOJI_DELIVERY: FrozenSet[str] = frozenset(
    {"🚚", "📦", "🛵", "🚛", "🏠", "🚪", "📍", "🗺️"}
)
MARKETING_EMOJI_CONFIRMATION: FrozenSet[str] = frozenset({"✅", "☑️", "👍", "👌"})
MARKETING_EMOJI_PAYMENT: FrozenSet[str] = frozenset({"💳", "🧾", "🏦", "✅"})
MARKETING_EMOJI_ALL_APPROVED: FrozenSet[str] = frozenset().union(
    MARKETING_EMOJI_WARMTH,
    MARKETING_EMOJI_SHOPPING,
    MARKETING_EMOJI_OFFERS,
    MARKETING_EMOJI_DELIVERY,
    MARKETING_EMOJI_CONFIRMATION,
    MARKETING_EMOJI_PAYMENT,
    frozenset({"🎁", "🎉", "🎊", "💝", "🔔", "📣", "👀", "⭐", "🌟", "👑", "💎", "🍯", "🐝", "🤝", "🙏"}),
)

_PAYMENT_SUCCESS_EMOJI: FrozenSet[str] = frozenset({"✅", "☑️", "👍", "👌"})
_PAYMENT_SUCCESS_CLAIMS: FrozenSet[str] = frozenset(
    {
        "تم الدفع",
        "تم استلام الدفع",
        "تم التحويل بنجاح",
        "تم الدفع بنجاح",
        "استلمنا الدفع",
    }
)


def _emoji_in_approved_set(segment: str) -> bool:
    seg = unicodedata.normalize("NFC", str(segment or ""))
    if seg in MARKETING_EMOJI_ALL_APPROVED:
        return True
    if seg == "\ufe0f":
        return True
    base = seg.replace("\ufe0f", "").replace("\uFE0F", "")
    return base in MARKETING_EMOJI_ALL_APPROVED


def _emoji_units_for_policy(text: str) -> list[str]:
    """Collapse base emoji + optional FE0F into single units for approval checks."""
    raw = unicodedata.normalize("NFC", str(text or ""))
    units: list[str] = []
    i = 0
    chars = list(raw)
    while i < len(chars):
        ch = chars[i]
        if _EMOJI_UNIT_RE.fullmatch(ch):
            if ch == "\ufe0f" and units:
                units[-1] = unicodedata.normalize("NFC", units[-1] + ch)
            elif ch != "\ufe0f":
                if i + 1 < len(chars) and chars[i + 1] == "\ufe0f":
                    units.append(unicodedata.normalize("NFC", ch + chars[i + 1]))
                    i += 1
                else:
                    units.append(ch)
            i += 1
            continue
        i += 1
    return units


def count_emojis(text: str) -> int:
    """Count emoji graphemes in outbound text (policy §11.3 density rules)."""
    return len(_emoji_units_for_policy(text))


def is_excessive_emoji_density(text: str, *, max_emojis: int = 2) -> bool:
    """True when emoji count exceeds normal/campaign limits."""
    return count_emojis(text) > max_emojis


def rejects_emoji_spam(text: str) -> bool:
    """True when outbound has spammy emoji repetition or density."""
    raw = str(text or "")
    if is_excessive_emoji_density(raw, max_emojis=3):
        return True
    for emoji in MARKETING_EMOJI_ALL_APPROVED:
        if raw.count(emoji) >= 3:
            return True
    if re.search(r"(.)\1{4,}", raw):
        return True
    return False


def rejects_fixed_emoji_template_opener(text: str) -> bool:
    """True when text uses banned fixed emoji opener patterns."""
    raw = str(text or "")
    if contains_banned_template_opener(raw):
        return True
    return raw.strip().startswith("أكيد 🌷")


def payment_emoji_implies_success_without_evidence(
    text: str,
    *,
    payment_confirmed: bool = False,
) -> bool:
    """True when success emoji accompanies an unverified payment-success claim."""
    if payment_confirmed:
        return False
    raw = str(text or "")
    if not any(e in raw for e in _PAYMENT_SUCCESS_EMOJI):
        return False
    return any(claim in raw for claim in _PAYMENT_SUCCESS_CLAIMS)


def accepts_context_appropriate_light_emoji(text: str) -> bool:
    """True when reply has light approved emoji and no spam/fixed opener."""
    raw = str(text or "")
    if not raw.strip():
        return False
    if rejects_emoji_spam(raw) or rejects_fixed_emoji_template_opener(raw):
        return False
    emojis = _emoji_units_for_policy(raw)
    if not emojis:
        return True
    if len(emojis) > 2:
        return False
    return all(_emoji_in_approved_set(ch) for ch in emojis)


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
