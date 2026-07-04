"""Shared persona policy term sets (production-safe; mirrored in constitution tests)."""
from __future__ import annotations

import re
import unicodedata
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

# Rare valid tokens ending in كا (not second-person possessive artifacts).
_VALID_KA_TERMINATIONS: FrozenSet[str] = frozenset()

# Second-person possessive with an extra attached alif (e.g. كيفكا → كيفك).
_MALFORMED_POSSESSIVE_KA = re.compile(
    r"(?<![\u0600-\u06FF])([\u0621-\u064A\u0671-\u06D3\u06FA\u06FF]{1,40})كا(?![\u0600-\u06FF\u064B-\u065F])",
    re.UNICODE,
)


def _normalize_arabic_scan_text(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).replace("\u0640", "")


def find_malformed_saudi_ka_suffix_tokens(text: str) -> list[str]:
    """Tokens with abnormal attached كا suffix (composer artifact)."""
    raw = _normalize_arabic_scan_text(text)
    if not raw.strip():
        return []
    found: list[str] = []
    for match in _MALFORMED_POSSESSIVE_KA.finditer(raw):
        token = match.group(0)
        if token in _VALID_KA_TERMINATIONS:
            continue
        found.append(token)
    return found


def repair_malformed_saudi_ka_suffix(text: str) -> tuple[str, bool]:
    """Repair spurious final ا on second-person ك suffixes."""
    raw = str(text or "")
    if not raw.strip():
        return raw, False
    working = _normalize_arabic_scan_text(raw)
    changed = False

    def _repl(match: re.Match[str]) -> str:
        nonlocal changed
        token = match.group(0)
        stem = match.group(1)
        if token in _VALID_KA_TERMINATIONS:
            return token
        changed = True
        return f"{stem}ك"

    repaired = _MALFORMED_POSSESSIVE_KA.sub(_repl, working)
    if not changed:
        return raw, False
    repaired = re.sub(r"\s{2,}", " ", repaired).strip()
    return repaired, True


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
