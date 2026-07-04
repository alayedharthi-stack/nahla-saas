"""Shared persona policy term sets (production-safe; mirrored in constitution tests)."""
from __future__ import annotations

import re
from typing import FrozenSet

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

BANNED_SUPPORT_BOT_OPENERS: FrozenSet[str] = frozenset(
    {
        "أكيد 🌷 تفضل",
        "كيف أقدر أساعدك اليوم؟",
        "تم استلام رسالتك",
    }
)

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

_FIXED_EMOJI_OPENER = "أكيد 🌷 تفضل"


def find_non_saudi_arabic_terms(text: str) -> list[str]:
    raw = str(text or "")
    if not raw.strip():
        return []
    found: list[str] = []
    for term in NON_SAUDI_ARABIC_DIALECT_TERMS:
        if re.search(rf"(?<!\S){re.escape(term)}(?!\S)", raw):
            found.append(term)
    return found


def rejects_social_support_bot_phrase(text: str) -> bool:
    raw = str(text or "")
    for phrase in BANNED_SUPPORT_BOT_OPENERS:
        if phrase in raw:
            return True
    return False


def rejects_fixed_emoji_template_opener(text: str) -> bool:
    return _FIXED_EMOJI_OPENER in str(text or "")


def looks_like_invented_payment_credential(text: str) -> bool:
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
