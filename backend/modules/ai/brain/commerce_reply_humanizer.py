"""
commerce_reply_humanizer.py
───────────────────────────
Deterministic warm-up for cheap commerce WhatsApp replies.

No extra LLM calls — rewrites dry/formal Arabic phrasing and adds at most
two context-appropriate emojis without changing availability, price, or
delivery facts.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from modules.ai.brain.intent_priority.types import (
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import inbound_is_arabic

logger = logging.getLogger("nahla.brain.commerce_reply_humanizer")

EMOJI_BY_INTENT: Dict[str, Tuple[str, ...]] = {
    "greeting": ("🤍", "😊", "🌷"),
    "thanks": ("🤍", "🌷"),
    "dua": ("🤍", "🌷"),
    "ask_product": ("🛒", "✨"),
    "product_availability": ("🛒", "✨"),
    "solution_seeking_commerce": ("🛒", "✨"),
    "ask_price": ("✨", "🛒"),
    "ask_shipping": ("🚚", "📍"),
    "ask_location": ("📍", "🚚"),
    "track_order": ("✅", "🧾"),
    "start_order": ("🛒", "✅"),
    "pay_now": ("✅", "🧾"),
    "ask_payment_info": ("✅", "🧾"),
}

GENERAL_EMOJI_BY_PURPOSE: Dict[str, Tuple[str, ...]] = {
    "shopping": ("🛒", "✨"),
    "offer": ("🔥", "✨", "🛒"),
    "discount": ("🏷️", "🔥", "✨"),
    "delivery": ("🚚", "📍"),
    "location": ("📍", "🚚"),
    "payment": ("✅", "🧾"),
    "order": ("🛒", "✅", "🧾"),
    "confirmation": ("✅", "🤍"),
    "thanks": ("🤍", "🌷"),
    "greeting": ("🤍", "😊", "🌷"),
    "support": ("🤍", "✅"),
}

EMOJI_BY_PRODUCT_CATEGORY: Dict[str, Tuple[str, ...]] = {
    "honey": ("🍯", "🌿", "✨"),
    "food": ("🍽️", "✨"),
    "coffee": ("☕", "✨"),
    "dates": ("🌴", "✨"),
    "perfume": ("🪷", "✨"),
    "oud": ("🪵", "✨"),
    "clothes": ("👕", "✨"),
    "dress": ("👗", "✨"),
    "abaya": ("🖤", "✨"),
    "shoes": ("👟", "✨"),
    "bags": ("👜", "✨"),
    "beauty": ("💄", "✨"),
    "skincare": ("🧴", "✨"),
    "electronics": ("🔌", "📱", "✨"),
    "mobile": ("📱", "✨"),
    "computer": ("💻", "✨"),
    "accessories": ("🎧", "⌚", "✨"),
    "stationery": ("✏️", "📚", "✨"),
    "books": ("📚", "✨"),
    "toys": ("🧸", "✨"),
    "flowers": ("💐", "🌷", "✨"),
    "home": ("🏠", "✨"),
    "furniture": ("🛋️", "✨"),
    "car": ("🚗", "✨"),
    "tools": ("🛠️", "✨"),
    "sports": ("⚽", "🏋️", "✨"),
    "gifts": ("🎁", "✨"),
    "general": ("🛒", "✨"),
}

_PRODUCT_CATEGORY_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("عسل", "honey"),
    ("طلح", "honey"),
    ("سدر", "honey"),
    ("قهوة", "coffee"),
    ("كoffee", "coffee"),
    ("تمر", "dates"),
    ("عطر", "perfume"),
    ("عود", "oud"),
    ("فساتين", "dress"),
    ("فستان", "dress"),
    ("عباية", "abaya"),
    ("عباء", "abaya"),
    ("ملابس", "clothes"),
    ("ثوب", "clothes"),
    ("حذاء", "shoes"),
    ("أحذية", "shoes"),
    ("حقيبة", "bags"),
    ("شنطة", "bags"),
    ("مكياج", "beauty"),
    ("تجميل", "beauty"),
    ("عناية", "skincare"),
    ("كريم", "skincare"),
    ("إلكترون", "electronics"),
    ("جوال", "mobile"),
    ("جوالات", "mobile"),
    ("موبايل", "mobile"),
    ("لابتوب", "computer"),
    ("كمبيوتر", "computer"),
    ("قرطاسية", "stationery"),
    ("أقلام", "stationery"),
    ("قلم", "stationery"),
    ("كتب", "books"),
    ("كتاب", "books"),
    ("ألعاب", "toys"),
    ("لعبة", "toys"),
    ("ورد", "flowers"),
    ("زهور", "flowers"),
    ("أثاث", "furniture"),
    ("سيارة", "car"),
    ("رياضة", "sports"),
    ("هدايا", "gifts"),
    ("هدية", "gifts"),
    ("طعام", "food"),
    ("أكل", "food"),
)

_COMMERCE_INTENTS: FrozenSet[str] = frozenset({
    "solution_seeking_commerce",
    "ask_product",
    "ask_price",
    "product_availability",
    "product_reference",
    "ask_shipping",
    "ask_location",
    "track_order",
    "start_order",
    "pay_now",
    "ask_payment_info",
})

_SENSITIVE_INTENTS: FrozenSet[str] = frozenset({
    "talk_to_human",
    "employee_not_responding",
})

_COMPLAINT_INBOUND_RE = re.compile(
    r"(?:شكو|اشتك|زعل|غاضب|ما\s*راض|سي+[ءئ]| refund|"
    r"استرجاع|تعويض|احتيال|نصب|غش|مشكلة\s*كبير)",
    re.UNICODE | re.IGNORECASE,
)

_UNAVAILABLE_RE = re.compile(
    r"(?:غير\s*متوفر|غير\s*متاح|لا\s*يتوفر|لا\s*يوجد|نفذ|"
    r"unavailable|out\s+of\s+stock)",
    re.UNICODE | re.IGNORECASE,
)

_VERIFYING_RE = re.compile(
    r"(?:أتحقق|س+[اأ]تحقق|جاري\s+التحقق|س+[اأ]راجع|under\s+review)",
    re.UNICODE | re.IGNORECASE,
)

_POSITIVE_AVAILABILITY_RE = re.compile(
    r"(?:متوفر|متاح|عندنا|لدينا|^نعم\b|أبشر)",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_RE = re.compile(
    r"(?:\d+\s*ريال|\d+\s*ر\.?\s*س|SAR|\$|\d+\s*sr\b)",
    re.UNICODE | re.IGNORECASE,
)

_DELIVERY_CONFIRMED_RE = re.compile(
    r"(?:التوصيل\s+متاح|نوصل|س+[اأ]وصل|تم\s+التأكيد\s+على\s+التوصيل|"
    r"delivery\s+confirmed|same-day)",
    re.UNICODE | re.IGNORECASE,
)

_MARKETING_RE = re.compile(
    r"(?:عرض|خصم|تخفيض|promo|discount|sale|🔥|🏷️)",
    re.UNICODE | re.IGNORECASE,
)

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_WARM_OPENERS = ("أبشر", "أبشري", "تمام", "حاضر", "يا هلا")

_QMARK = r"[?\u061f]"

_FORMAL_REPLACEMENTS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"هل\s+ترغب(?:ين)?(?:\s+بال)?", re.UNICODE), "تحب "),
    (re.compile(r"يمكنني\s+مساعدتك(?:\s+في)?", re.UNICODE), ""),
    (re.compile(r"يرجى\s+تحديد", re.UNICODE), "وش"),
    (
        re.compile(
            rf"[،,]?\s*تبي\s+أي\s+حجم\s+أو\s+نوع\s+معين\s*{_QMARK}",
            re.UNICODE,
        ),
        "\nوش الحجم اللي يناسبك؟",
    ),
    (
        re.compile(
            rf"[،,]?\s*أي\s+حجم\s+تريد(?:ين)?\s*{_QMARK}",
            re.UNICODE,
        ),
        "\nوش الحجم اللي يناسبك؟",
    ),
    (
        re.compile(r"لدينا\s+عدة\s+أحجام[،,]?\s*", re.UNICODE),
        "متوفر بعدة أحجام ",
    ),
    (
        re.compile(r"^لدينا\s+", re.UNICODE),
        "متوفر عندنا ",
    ),
    (
        re.compile(r"^نعم[،,]\s*عندنا\s+", re.UNICODE),
        "نعم متوفر ",
    ),
    (
        re.compile(r"^نعم[،,]\s*لدينا\s+", re.UNICODE),
        "نعم متوفر عندنا ",
    ),
    (
        re.compile(r"^نعم[،,]\s+", re.UNICODE),
        "نعم ",
    ),
    (
        re.compile(
            rf"ما\s+هو\s+موقعك\s+للتوصيل\s*{_QMARK}",
            re.UNICODE,
        ),
        "أرسل لي موقعك وأتأكد لك من التوصيل.",
    ),
    (
        re.compile(rf"ما\s+هو\s+عنوانك\s*{_QMARK}", re.UNICODE),
        "أرسل لي موقعك وأتأكد لك من التوصيل.",
    ),
    (
        re.compile(r"تحب\s+أن\s+أرسل", re.UNICODE),
        "تحب أرسل",
    ),
)


@dataclass(frozen=True)
class CommerceReplyHumanizerResult:
    reply: str
    replaced: bool
    warmed_tone: bool
    added_emojis: bool


def detect_product_category(
    text: str,
    *,
    product_title: str = "",
) -> str:
    combined = _normalize_for_match(f"{product_title} {text}")
    for keyword, category in _PRODUCT_CATEGORY_KEYWORDS:
        if keyword in combined:
            return category
    return "general"


def _normalize_for_match(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640\u06D6-\u06ED]", "", t)
    return t


def _count_emojis(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


def _is_sensitive_turn(
    *,
    intent_name: str,
    inbound_text: str,
    human_priority: bool = False,
) -> bool:
    intent = (intent_name or "").strip().lower()
    if intent in _SENSITIVE_INTENTS or human_priority:
        return True
    return bool(_COMPLAINT_INBOUND_RE.search(inbound_text or ""))


def _resolve_purpose(
    *,
    intent_name: str,
    primary_customer_goal: str,
    reply: str,
) -> str:
    intent = (intent_name or "").strip().lower()
    goal = (primary_customer_goal or "").strip().lower()
    if intent in {"ask_shipping"} or goal == "shipping_inquiry":
        return "delivery"
    if intent in {"ask_location"}:
        return "location"
    if intent in {"track_order", "start_order", "pay_now", "ask_payment_info"}:
        return "order"
    if _MARKETING_RE.search(reply or ""):
        if re.search(r"(?:خصم|تخفيض|discount|sale)", reply or "", re.IGNORECASE):
            return "discount"
        return "offer"
    if intent in {"ask_price"} or goal == GOAL_PRICE_INQUIRY:
        return "shopping"
    return "shopping"


def _pick_emojis(
    *,
    intent_name: str,
    purpose: str,
    product_category: str,
    reply: str,
    existing_count: int,
    max_total: int = 2,
) -> List[str]:
    slots = max(0, max_total - existing_count)
    if slots <= 0:
        return []

    candidates: List[str] = []
    if product_category in EMOJI_BY_PRODUCT_CATEGORY:
        candidates.extend(EMOJI_BY_PRODUCT_CATEGORY[product_category])
    intent = (intent_name or "").strip().lower()
    if intent in EMOJI_BY_INTENT:
        candidates.extend(EMOJI_BY_INTENT[intent])
    if purpose in GENERAL_EMOJI_BY_PURPOSE:
        candidates.extend(GENERAL_EMOJI_BY_PURPOSE[purpose])

    if purpose not in {"offer", "discount"} and not _MARKETING_RE.search(reply or ""):
        candidates = [e for e in candidates if e not in {"🔥", "🏷️"}]

    picked: List[str] = []
    seen: set[str] = set()
    for emoji in candidates:
        if emoji in seen:
            continue
        seen.add(emoji)
        picked.append(emoji)
        if len(picked) >= slots:
            break
    return picked


def _has_warm_opener(text: str) -> bool:
    stripped = (text or "").strip()
    for opener in _WARM_OPENERS:
        if stripped.startswith(opener):
            return True
    return False


def _warm_formal_phrases(text: str) -> Tuple[str, bool]:
    original = (text or "").strip()
    if not original:
        return original, False

    warmed = original
    changed = False
    for pattern, repl in _FORMAL_REPLACEMENTS:
        new = pattern.sub(repl, warmed)
        if new != warmed:
            changed = True
            warmed = new

    warmed = re.sub(r"\n{3,}", "\n\n", warmed)
    warmed = re.sub(r"[ \t]{2,}", " ", warmed)
    warmed = re.sub(r" *\n *", "\n", warmed).strip()
    return warmed, changed or warmed != original


def _maybe_add_delivery_opener(text: str, *, purpose: str) -> Tuple[str, bool]:
    if purpose != "delivery":
        return text, False
    stripped = (text or "").strip()
    if not stripped:
        return stripped, False
    if _has_warm_opener(stripped):
        return stripped, False
    if "أرسل" in stripped or "موقع" in stripped or "عنوان" in stripped:
        if "🚚" not in stripped:
            return f"أبشر 🚚\n{stripped}", True
        return stripped, False
    return stripped, False


def _maybe_add_abshar_opener(text: str, *, purpose: str) -> Tuple[str, bool]:
    stripped = (text or "").strip()
    if not stripped or _has_warm_opener(stripped):
        return stripped, False
    if purpose == "delivery":
        return stripped, False
    if _UNAVAILABLE_RE.search(stripped):
        return stripped, False
    if stripped.startswith("نعم") or _POSITIVE_AVAILABILITY_RE.search(stripped):
        return f"أبشر، {stripped.lstrip('،, ')}", True
    return stripped, False


def _inject_emojis(text: str, emojis: Sequence[str]) -> str:
    if not emojis:
        return text
    emoji_str = "".join(emojis)
    lines = (text or "").strip().split("\n", 1)
    first = lines[0].rstrip()
    if _EMOJI_RE.search(first):
        return text
    first = f"{first} {emoji_str}".strip()
    if len(lines) > 1:
        return f"{first}\n{lines[1]}"
    return first


def _facts_preserved(original: str, candidate: str) -> bool:
    orig = (original or "").strip()
    cand = (candidate or "").strip()
    if not orig:
        return cand == orig

    if _UNAVAILABLE_RE.search(orig):
        if not _UNAVAILABLE_RE.search(cand):
            return False

    if not _POSITIVE_AVAILABILITY_RE.search(orig) and _VERIFYING_RE.search(orig):
        if _POSITIVE_AVAILABILITY_RE.search(cand) and not _VERIFYING_RE.search(cand):
            if re.search(r"\bمتوفر\b", cand, re.UNICODE):
                return False

    orig_prices = set(_PRICE_RE.findall(orig))
    cand_prices = set(_PRICE_RE.findall(cand))
    if cand_prices - orig_prices:
        return False

    if not _DELIVERY_CONFIRMED_RE.search(orig) and _DELIVERY_CONFIRMED_RE.search(cand):
        return False

    return True


def should_apply_commerce_humanizer(
    *,
    reply: str,
    inbound_text: str,
    intent_name: str = "",
    primary_customer_goal: str = "",
    locale: str = "ar",
    chosen_path: str = "",
    human_priority: bool = False,
) -> bool:
    if not (reply or "").strip():
        return False
    if not inbound_is_arabic(inbound_text, locale=locale):
        return False
    intent = (intent_name or "").strip().lower()
    goal = (primary_customer_goal or "").strip().lower()
    if intent not in _COMMERCE_INTENTS and goal not in {
        GOAL_PRODUCT_AVAILABILITY,
        GOAL_PRICE_INQUIRY,
        "shipping_inquiry",
        "product_reference",
    }:
        return False
    path = (chosen_path or "").strip().lower()
    if path and not path.startswith("llm"):
        return False
    if _is_sensitive_turn(
        intent_name=intent_name,
        inbound_text=inbound_text,
        human_priority=human_priority,
    ):
        return False
    return True


def apply_commerce_reply_humanizer(
    reply: str,
    *,
    inbound_text: str = "",
    intent_name: str = "",
    primary_customer_goal: str = "",
    locale: str = "ar",
    chosen_path: str = "",
    human_priority: bool = False,
    product_title: str = "",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> CommerceReplyHumanizerResult:
    original = (reply or "").strip()
    if not should_apply_commerce_humanizer(
        reply=original,
        inbound_text=inbound_text,
        intent_name=intent_name,
        primary_customer_goal=primary_customer_goal,
        locale=locale,
        chosen_path=chosen_path,
        human_priority=human_priority,
    ):
        return CommerceReplyHumanizerResult(
            reply=original,
            replaced=False,
            warmed_tone=False,
            added_emojis=False,
        )

    purpose = _resolve_purpose(
        intent_name=intent_name,
        primary_customer_goal=primary_customer_goal,
        reply=original,
    )
    category = detect_product_category(original, product_title=product_title)

    text = original
    warmed_tone = False

    warmed, did_warm = _warm_formal_phrases(text)
    if did_warm:
        warmed_tone = True
        text = warmed

    with_opener, did_opener = _maybe_add_delivery_opener(text, purpose=purpose)
    if did_opener:
        warmed_tone = True
        text = with_opener
    elif purpose != "delivery":
        with_abshar, did_abshar = _maybe_add_abshar_opener(text, purpose=purpose)
        if did_abshar:
            warmed_tone = True
            text = with_abshar

    existing_emoji_count = _count_emojis(text)
    emojis = _pick_emojis(
        intent_name=intent_name,
        purpose=purpose,
        product_category=category,
        reply=text,
        existing_count=existing_emoji_count,
    )
    added_emojis = False
    if emojis:
        candidate = _inject_emojis(text, emojis)
        if _count_emojis(candidate) <= 2 and _facts_preserved(original, candidate):
            text = candidate
            added_emojis = True

    if not _facts_preserved(original, text):
        logger.info(
            "[COMMERCE_REPLY_HUMANIZER] reverted — facts would change tenant=%s "
            "conversation=%s intent=%s",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            intent_name or "-",
        )
        return CommerceReplyHumanizerResult(
            reply=original,
            replaced=False,
            warmed_tone=False,
            added_emojis=False,
        )

    replaced = text != original
    if replaced:
        logger.info(
            "[COMMERCE_REPLY_HUMANIZER] tenant=%s conversation=%s intent=%s "
            "warmed=%s emojis=%s orig_len=%d new_len=%d",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            intent_name or "-",
            warmed_tone,
            added_emojis,
            len(original),
            len(text),
        )

    return CommerceReplyHumanizerResult(
        reply=text,
        replaced=replaced,
        warmed_tone=warmed_tone,
        added_emojis=added_emojis,
    )


__all__ = [
    "CommerceReplyHumanizerResult",
    "EMOJI_BY_INTENT",
    "EMOJI_BY_PRODUCT_CATEGORY",
    "GENERAL_EMOJI_BY_PURPOSE",
    "apply_commerce_reply_humanizer",
    "detect_product_category",
    "should_apply_commerce_humanizer",
]
