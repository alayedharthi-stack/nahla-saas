"""
core/customer_name_validator.py
───────────────────────────────
Deterministic gate for customer identity names.

Operational rule (AGENTS.md): never adopt conversational filler, product
requests, cities, or message fragments as a human name. Bias toward
false-negatives — a missed name is recoverable; a wrong name pollutes
orders, invoices, and shipping labels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple

# Major Saudi cities — a bare city is never a personal name.
_SAUDI_CITY_TOKENS: FrozenSet[str] = frozenset({
    "الرياض", "رياض", "جدة", "جده", "مكة", "مكه", "مكة المكرمة",
    "المدينة", "المدينه", "المدينة المنورة", "الدمام", "الخبر",
    "الظهران", "الطائف", "طائف", "تبوك", "أبها", "ابها", "خميس",
    "خميس مشيط", "القصيم", "بريدة", "بريده", "حائل", "حايل",
    "نجران", "جازان", "جazan", "ينبع", "الجبيل", "الجبيل",
    "الاحساء", "الأحساء", "احساء", "عرعر", "سكاكا", "الباحة",
    "الدمام", "القطيف", "الخرج", "القنفذة", "القنفذه",
})

_CONVERSATIONAL_TOKENS: FrozenSet[str] = frozenset({
    "نعم", "لا", "ايوه", "ايوة", "أيوه", "اوكي", "اوك", "تمام", "طيب",
    "حسنا", "حسناً", "ابشر", "ابشري", "اكيد", "موافق", "ممتاز", "شكرا",
    "شكراً", "مرحبا", "اهلا", "أهلا", "هلا", "السلام", "عليكم",
    "وصلت", "وصل", "جاي", "جايه", "جاية", "راجع", "رايح", "موجود",
    "جاهز", "حاضر", "بانتظار", "منتظر",
    # Question / small-talk — never names
    "وش", "ايش", "كيف", "متى", "وين", "ليش", "ليه", "حقي", "حال", "الحال",
    "طبعا", "اه", "آه", "ابد", "ابدا",
    # Conversation / complaint fragments (Jun 2026 P0 — production echo leak)
    "ايه", "اية", "وقف", "شغلتنا", "شغلنا", "النحلة", "نحلة",
    # Deictic / pronoun fragments — never personal names (Jun 2026 P0)
    "هذا", "هذه", "هذي", "انت", "أنت",
})

# Full deictic phrases (normalised) — e.g. WA profile / caption "هذا انت".
_DEICTIC_NAME_PHRASES_RAW: FrozenSet[str] = frozenset({
    "هذا انت", "هذه انت", "هذا أنت", "هذه أنت",
    "هذي انت", "هذي أنت",
    "this is you",
})

_COMMERCE_TOKENS: FrozenSet[str] = frozenset({
    "ابغى", "ابغي", "ابي", "اريد", "أرسل", "ارسل", "ارسلي", "ارسل الموقع",
    "موقع", "موقعي", "طلب", "طلبية", "منتج", "سعر", "توصيل", "شحن",
    "فاتورة", "دفع", "تحويل", "حواله", "حوالة", "حساب", "متجر", "سلة",
    "عسل", "ورد", "زهور", "باقة", "كيلو", "جرام", "قطعة", "افضل", "أفضل",
    "الدفع", "عند", "الاستلام", "cod", "bank", "transfer",
    "سمسا", "smsa", "ارامكس", "أرامكس", "aramex", "spl", "ناقل",
    "الشحن", "شركة", "خدمة", "العملاء", "المعرض", "الإدارة", "الاداره",
    "الموقع",
})

_ROLE_LABELS: FrozenSet[str] = frozenset({
    "عميل", "عميلة", "زبون", "زبونة", "ضيف", "customer", "user", "guest",
    "مندوب", "موظف", "courier",
})

_NAME_PREFIXES: FrozenSet[str] = frozenset({
    "أبو", "أبا", "أبي", "ابو", "ابا", "ابي",
    "أم", "أما", "أمي", "ام", "اما", "امي",
    "عبد", "آل", "ال", "بن", "ابن", "ابنة", "بنت",
})

_ARABIC_LETTERS_RE = re.compile(r"^[\u0621-\u064A][\u0621-\u064A\s'\-]*$")
_LATIN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\s.\-']{1,58}$")
_DIGIT_RE = re.compile(r"\d")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U0001F600-\U0001F64F\U00002600-\U000027BF]+",
    flags=re.UNICODE,
)


@dataclass(frozen=True)
class NameValidationResult:
    valid: bool
    cleaned: str = ""
    reason: str = ""
    confidence: float = 0.0


def _normalize_arabic(token: str) -> str:
    t = (token or "").strip()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower()


def _normalize_full(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _is_pure_letter_token(token: str) -> bool:
    if not token:
        return False
    for ch in token:
        if ch.isspace() or ch.isdigit():
            return False
        if ch in ("'", "-", "."):
            continue
        if "\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F":
            continue
        if ch.isalpha():
            continue
        return False
    return True


_DEICTIC_NAME_PHRASES = frozenset(_normalize_arabic(p) for p in _DEICTIC_NAME_PHRASES_RAW)


def is_deictic_or_conversational_name_phrase(raw: Optional[str]) -> bool:
    """True when text is a deictic/conversational phrase, not a human name."""
    text = _normalize_full(_EMOJI_RE.sub(" ", str(raw or "")))
    if not text:
        return False
    if _normalize_arabic(text) in _DEICTIC_NAME_PHRASES:
        return True
    tokens = [_normalize_arabic(t) for t in text.split() if t]
    return bool(tokens) and all(t in _CONVERSATIONAL_TOKENS for t in tokens)


def validate_customer_name(raw: Optional[str]) -> NameValidationResult:
    """
    Return ``valid=True`` only for plausible human names.

    Rejects fillers (``طيب``), duplicated tokens (``طيب طيب``), cities,
    product phrases (``ورد عسل السم``), long message-like strings, and
    role labels.
    """
    if raw is None or not isinstance(raw, str):
        return NameValidationResult(valid=False, reason="empty")

    text = _normalize_full(_EMOJI_RE.sub(" ", raw))
    if not text:
        return NameValidationResult(valid=False, reason="empty")
    if len(text) > 60:
        return NameValidationResult(valid=False, reason="too_long")
    if _DIGIT_RE.search(text):
        return NameValidationResult(valid=False, reason="contains_digits")

    tokens = [t for t in text.split(" ") if t]
    if not tokens or len(tokens) > 4:
        return NameValidationResult(valid=False, reason="token_count")

    norm_tokens = [_normalize_arabic(t) for t in tokens]
    full_norm = " ".join(norm_tokens)

    if is_deictic_or_conversational_name_phrase(text):
        return NameValidationResult(valid=False, reason="deictic_phrase")

    if full_norm in {_normalize_arabic(c) for c in _SAUDI_CITY_TOKENS}:
        return NameValidationResult(valid=False, reason="city_only")

    if len(tokens) >= 2 and len(set(norm_tokens)) == 1:
        return NameValidationResult(valid=False, reason="repeated_token")

    non_prefix = 0
    for tok, norm in zip(tokens, norm_tokens):
        if not _is_pure_letter_token(tok):
            return NameValidationResult(valid=False, reason="invalid_chars")
        if len(tok) < 2 and tok not in _NAME_PREFIXES and _normalize_arabic(tok) not in _NAME_PREFIXES:
            return NameValidationResult(valid=False, reason="token_too_short")
        if norm in _CONVERSATIONAL_TOKENS:
            return NameValidationResult(valid=False, reason="conversational")
        if norm in _COMMERCE_TOKENS:
            return NameValidationResult(valid=False, reason="commerce")
        if norm in _ROLE_LABELS or tok.lower() in _ROLE_LABELS:
            return NameValidationResult(valid=False, reason="role_label")
        if norm in {_normalize_arabic(c) for c in _SAUDI_CITY_TOKENS}:
            return NameValidationResult(valid=False, reason="city_token")
        if tok not in _NAME_PREFIXES and _normalize_arabic(tok) not in _NAME_PREFIXES:
            non_prefix += 1

    if non_prefix == 0:
        return NameValidationResult(valid=False, reason="prefix_only")

    if not (_ARABIC_LETTERS_RE.match(text) or _LATIN_NAME_RE.match(text)):
        return NameValidationResult(valid=False, reason="pattern_mismatch")

    confidence = 0.85 if len(tokens) >= 2 else 0.7
    return NameValidationResult(
        valid=True,
        cleaned=text,
        reason="ok",
        confidence=confidence,
    )


def is_valid_customer_name(raw: Optional[str]) -> bool:
    return validate_customer_name(raw).valid
