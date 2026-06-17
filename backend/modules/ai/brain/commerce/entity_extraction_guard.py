"""
entity_extraction_guard.py
──────────────────────────
Platform-wide guard: inbound text must not become catalog or staff entities
without explicit evidence (Nahla doctrine — operations need evidence).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_STORE_PRONOUN_TOKENS = frozenset({
    "ك", "اك", "كم", "كن", "كם", "ه", "ها", "هم", "هن",
    "معك", "معاك", "معكم", "معكن", "معاكم", "معاك", "معا", "مع",
})

_STORE_CHANNEL_PHONE_TOKENS = frozenset({
    "تليفونكم", "تلفونكم", "هاتفكم", "رقمكم", "جوالكم", "موبايلكم",
    "تليفونك", "تلفونك", "هاتفك", "رقمك", "جوالك", "موبايلك",
    "تليفون", "تلفون", "هاتف", "جوال", "موبايل",
    "الهاتف", "الجوال", "التلفون", "التليفون",
    "رقم", "phone", "contact",
})

_STORE_CHANNEL_PHONE_PHRASE_RE = re.compile(
    r"(?:"
    r"(?:ع(?:ل|)?(?:ي|يه|ا)\s+)?(?:رقم\s+)?(?:ال)?(?:هاتف|تليفون|تلفون|جوال|موبايل)(?:كم|ك|ه|ها|هم)?"
    r"|(?:رقم(?:كم|ك|ه|ها|هم)?)(?:\s*(?:ال)?(?:هاتف|تليفون|تلفون|جوال|موبايل))?"
    r"|(?:هاتف|تليفون|تلفون|جوال|موبايل)(?:كم|ك|ه|ها|هم)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_THANKS_LEAD_RE = re.compile(
    r"^(?:شكر|شكرا|شكراً|thanks|thx|مشكور|تسلم|يعطيك\s+العاف)",
    re.UNICODE | re.IGNORECASE,
)

_NAMED_STAFF_EXTRACT_RE = re.compile(
    r"(?:"
    r"رقم\s+(?:ال)?(.+?)(?:[\?؟.,!]|$)"
    r"|(?:ارسل|أرسل|ارسلي|أرسلي)\s+(?:لي\s+)?(?:رقم\s+)?(?:ال)?(.+?)(?:[\?؟.,!]|$)"
    r"|(?:اكلم|أكلم|اتواصل|أتواصل|اتكلم|أتكلم|تواصل)\s+(?:مع\s+)?(.+?)(?:[\?؟.,!]|$)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_HOW_TO_CONTACT_RE = re.compile(
    r"^(?:كيف|وش\s*طريقة|ايش\s*طريقة|how)\s",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def _clean_staff_candidate(raw: str) -> str:
    cand = _WS_RE.sub(" ", (raw or "").strip()).strip("؟?.,! ")
    if not cand:
        return ""
    norm_cand = _norm(cand)
    if norm_cand in _STORE_CHANNEL_PHONE_TOKENS:
        return ""
    tokens = norm_cand.split()
    if all(tok in _STORE_PRONOUN_TOKENS for tok in tokens):
        return ""
    if all(tok in (_STORE_PRONOUN_TOKENS | _STORE_CHANNEL_PHONE_TOKENS) for tok in tokens):
        return ""
    if cand in _STORE_PRONOUN_TOKENS:
        return ""
    if re.fullmatch(r"(?:مع)?(?:اك|اكم|كم|كن|ك)", cand):
        return ""
    if re.fullmatch(
        r"رقم(?:\s+ال)?(?:هاتف|تليفون|تلفون|جوال|موبايل)?(?:كم|ك|ه|ها|هم)?",
        norm_cand,
    ):
        return ""
    if len(cand) <= 2:
        return ""
    return cand


def is_store_channel_phone_phrase(message: str) -> bool:
    """
    True for store-wide phone/channel asks — not a named staff member.

    Examples: «عليه رقم تليفونكم», «رقمكم», «رقم الهاتف».
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if extract_staff_name_candidate(raw):
        return False
    norm = _norm(raw)
    if _STORE_CHANNEL_PHONE_PHRASE_RE.search(norm):
        return True
    if re.fullmatch(r"رقم(?:كم|ك|ه|ها|هم)?", norm):
        return True
    if re.fullmatch(
        r"رقم(?:\s+ال)?(?:هاتف|تليفون|تلفون|جوال|موبايل)",
        norm,
    ):
        return True
    return False


def extract_staff_name_candidate(message: str) -> str:
    """Return explicit staff name/role target, excluding store pronouns (معكم)."""
    raw = (message or "").strip()
    if not raw:
        return ""
    norm = _norm(raw)
    m = _NAMED_STAFF_EXTRACT_RE.search(norm)
    if not m:
        return ""
    for group in m.groups():
        if not group:
            continue
        cand = _clean_staff_candidate(group)
        if cand:
            return cand
    return ""


def is_thanks_with_contact_phrase(message: str) -> bool:
    """Thank-you + contact wording — not a staff lookup."""
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_THANKS_LEAD_RE.search(_norm(raw)))


def is_generic_store_contact_phrase(message: str) -> bool:
    """
    True when the customer asks to reach the store/channel, not a named person.

    Examples: «أتواصل معكم», «كيف أتواصل معاكم؟», «أرجع أتواصل معكم بعدين».
    """
    raw = (message or "").strip()
    if not raw or is_thanks_with_contact_phrase(raw):
        return False
    if is_store_channel_phone_phrase(raw):
        return True
    if extract_staff_name_candidate(raw):
        return False
    norm = _norm(raw)
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            _CONTACT_ASK_RE,
        )
    except Exception:  # noqa: BLE001
        return False
    if not _CONTACT_ASK_RE.search(norm):
        return False
    if _HOW_TO_CONTACT_RE.search(raw) or _HOW_TO_CONTACT_RE.search(norm):
        return True
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_customer_defer_or_return_later,
        )

        if is_customer_defer_or_return_later(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import at guard boundary
        pass
    tail = re.sub(
        r"^.*?(?:تواصل|اتواصل|اكلم|أكلم|اتكلم|أتكلم|كلم)\s*",
        "",
        norm,
        count=1,
        flags=re.UNICODE,
    )
    tail = re.sub(r"^(?:مع\s+)?", "", tail).strip("؟?.,! ")
    if not tail or tail in _STORE_PRONOUN_TOKENS:
        return True
    if all(part in _STORE_PRONOUN_TOKENS for part in tail.split()):
        return True
    return False


def has_explicit_purchase_intent(message: str) -> bool:
    """True when customer explicitly signals buying — overrides identity guard."""
    norm = _norm(message or "")
    if not norm:
        return False
    if re.search(
        r"(?:"
        r"(?:ابي|ابغى|أبي|أبغى|بدي|اريد|أريد|want\s+to)\s*(?:اشتري|أشتري|buy|order|purchase)"
        r"|(?:اشتري|أشتري|buy|order)\s"
        r")",
        norm,
        flags=re.UNICODE | re.IGNORECASE,
    ):
        return True
    if re.search(
        r"(?:ابي|ابغى|أبي|أبغى|بدي)\s+(?:طرود|طرد|نحل|عسل|منتج|\d+)",
        norm,
        flags=re.UNICODE | re.IGNORECASE,
    ):
        return True
    return False


def is_identity_collaboration_without_purchase(message: str) -> bool:
    """Self-intro / collaboration / experience without explicit buy intent."""
    raw = (message or "").strip()
    if not raw or has_explicit_purchase_intent(raw):
        return False
    try:
        from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
            is_conversational_non_product_inbound,
        )

        return is_conversational_non_product_inbound(raw)
    except Exception:  # noqa: BLE001
        return False


MSG_GENERAL_CONTACT_IN_CHANNEL = (
    "تقدر تكتب استفسارك هنا، ونخدمك بإذن الله."
)
MSG_GENERAL_CONTACT_HOW_TO = (
    "تقدر تتواصل معنا هنا، أو اكتب طلبك ونخدمك."
)


def general_contact_reply_for_message(message: str) -> str:
    raw = (message or "").strip()
    if _HOW_TO_CONTACT_RE.search(raw) or _HOW_TO_CONTACT_RE.search(_norm(raw)):
        return MSG_GENERAL_CONTACT_HOW_TO
    return MSG_GENERAL_CONTACT_IN_CHANNEL


__all__ = [
    "MSG_GENERAL_CONTACT_HOW_TO",
    "MSG_GENERAL_CONTACT_IN_CHANNEL",
    "extract_staff_name_candidate",
    "general_contact_reply_for_message",
    "has_explicit_purchase_intent",
    "is_generic_store_contact_phrase",
    "is_identity_collaboration_without_purchase",
    "is_store_channel_phone_phrase",
    "is_thanks_with_contact_phrase",
]
