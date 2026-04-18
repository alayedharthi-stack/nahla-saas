"""
brain/intent/rules.py
─────────────────────
Rule-based (zero-latency) intent matcher.

Strategy: try keyword/regex patterns first. If a pattern fires with
confidence >= 0.85 we return immediately and skip LLM slot extraction.
Confidence 0.60 – 0.84 means "possible" — the classifier will still run
LLM extraction to fill in slots.

Adding new intents: append a new RuleSet to _RULES. Order matters —
earlier rules have higher priority.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..types import (
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HESITATION,
    INTENT_PAY_NOW,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    INTENT_WHO_ARE_YOU,
    Intent,
)


@dataclass
class RuleSet:
    intent: str
    patterns: List[str]
    confidence: float = 0.90
    slots: Dict[str, str] = field(default_factory=dict)  # static slot overrides


def _compile(patterns: List[str]) -> List[re.Pattern]:
    return [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]


_RULES: List[Tuple[RuleSet, List[re.Pattern]]] = []


def _register(rs: RuleSet) -> None:
    _RULES.append((rs, _compile(rs.patterns)))


# ── Identity / who are you ───────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_WHO_ARE_YOU,
    patterns=[
        r"^(من أنت|من انت|من أنتِ|انت مين|انتي مين|مين أنت|وش أنت|وش انت|ايش انت|ايش أنت)",
        r"(عرفني بنفسك|عرفني عليك|وش تسوي|وش تقدر تسوي|مين انتي)",
    ],
    confidence=0.98,
))

# ── Greeting ─────────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_GREETING,
    patterns=[
        r"^(السلام عليكم|وعليكم السلام|مرحبا?ً?|أهلاً?|هلا|صباح الخير|مساء الخير|كيف حالك|هاي|هلو|hello|hi\b|hey\b)",
        r"^(أهلين|يا هلا|هلأ|هلأً|أهلا وسهلا)",
    ],
    confidence=0.95,
))

# ── Ask for a product ─────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_ASK_PRODUCT,
    patterns=[
        # عندكم / لديكم / يوجد + anything  →  asking about product availability
        r"(عندكم|عندك|لديكم|لديك|يوجد|موجود)\s+\S.{1,}",
        # ابحث / وين / فين + any text
        r"(ابحث|ابحثي|بحث|دور|دوري|فين|وين|أين|أبحث).{0,5}\s+\S.{1,}",
        r"(ابحث|بحث|دور|دوري|فين|وين|أين|عندكم|عندك|يوجد|موجود|لديكم|ودي|بغيت|أبي|أبغى|أريد|أودّ).{0,30}(منتج|بضاعة|سلعة|صنف|موديل|نوع|إصدار)",
        r"(أبغى|أريد|أبي|بدي|ودي|بغيت|اشتري|شراء|طلب).{0,40}",
        r"(منتج|بضاعة|صنف|سلعة|موديل).{0,30}(موجود|متاح|عندكم|لديكم)",
        r"(ما عندكم|ما عندك|ماعندكم)\s+.{2,30}",
        r"(شو عندكم|ايش عندكم|ماذا عندكم|ايش لديكم)",
        r"(show me|looking for|do you have|i want)\s+.{2,}",
    ],
    confidence=0.82,
))

# ── Ask for price ─────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_ASK_PRICE,
    patterns=[
        r"(سعر|تكلفة|كم سعر|كم ثمن|بكم|كم يساوي|ثمنه|كم تمنه|كم ثمنه|كم سعره)",
        r"(price|cost|how much|how much is)",
    ],
    confidence=0.90,
))

# ── Start order / buy ─────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_START_ORDER,
    patterns=[
        r"(أطلب|اطلب|اشتري|أشتري|بغيت أطلب|أبغى أطلب|أبي أطلب|خذ لي|حجز|احجز|أحجز|أضيف للسلة|أضف للسلة)",
        r"(order|buy|purchase|add to cart|checkout)\b",
        r"(نفس الطلب|طلب مرة ثانية|أعيد الطلب)",
        # Colloquial Gulf patterns — "تسوي لي طلب" / "تطلب لي" / "تعمل طلب"
        r"(تسوي|تطلب|تعمل|تحجز).{0,15}(طلب|أمر|حجز)",
        r"(طلب لي|اطلب لي|سوّ لي طلب|سوّيلي طلب|ودي أطلب|ابغى أطلب)",
    ],
    confidence=0.88,
))

# ── Pay / checkout ────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_PAY_NOW,
    patterns=[
        r"(ادفع|أدفع|دفع|سدد|أسدد|إتمام الدفع|الدفع الآن|أكمل الدفع|دفع الآن|تحصيل)",
        r"(رابط الدفع|رابط الطلب|وين الرابط|ارسل الرابط)",
        r"(pay|payment link|checkout link)",
    ],
    confidence=0.90,
))

# ── Shipping / delivery ───────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_ASK_SHIPPING,
    patterns=[
        r"(شحن|توصيل|يوصل|متى يوصل|مدة التوصيل|كم يوم|يوصل لين|يوصل إلى)",
        r"(shipping|delivery|when will|how long)",
    ],
    confidence=0.88,
))

# ── Store info / location / link ─────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_ASK_STORE_INFO,
    patterns=[
        r"(وين المتجر|أين المتجر|وين موقعكم|موقعكم|رابط المتجر|رابط الموقع|عن المتجر|تعريف المتجر)",
        r"(عندكم موقع|من وين أطلب|وين ألقى المتجر|لوكيشن المتجر|عنوان المتجر)",
        r"(store link|store url|where is your store|about the store)",
    ],
    confidence=0.92,
))

# ── Owner / support contact details ──────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_ASK_OWNER_CONTACT,
    patterns=[
        r"(رقمكم|رقم التواصل|رقم خدمة العملاء|كيف أتواصل|كيف اتواصل|وسيلة التواصل|رقم الواتساب)",
        r"(تواصل المالك|التواصل مع المالك|أبغى رقمكم|أرسل رقمكم|ابغى اكلمكم)",
        r"(contact number|contact info|customer service number|whatsapp number)",
    ],
    confidence=0.92,
))

# ── Hesitation ────────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_HESITATION,
    patterns=[
        r"(غالي|غلي|ما يستاهل|مو مناسب|مو بفائدة|بفكر|بشوف|لاحقاً|لاحقا|بعدين|مب ضروري|مش ضروري)",
        r"(expensive|too much|maybe later|not sure|i'll think)",
    ],
    confidence=0.85,
))

# ── Track order ───────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_TRACK_ORDER,
    patterns=[
        r"(وين طلبي|وين أمري|تتبع الطلب|متى يوصل طلبي|رقم الطلب|طلبي وين|شحنتي وين)",
        r"(track|track my order|where is my order|order status)",
    ],
    confidence=0.88,
))

# ── Talk to human ─────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_TALK_HUMAN,
    patterns=[
        r"(تحدث مع إنسان|تحدث مع بشر|موظف|خدمة العملاء|تواصل مع شخص|إنسان حقيقي|مو روبوت|مو بوت)",
        r"(human agent|real person|customer service|speak to someone|talk to agent)",
    ],
    confidence=0.90,
))


# ─────────────────────────────────────────────────────────────────────────────

def match(message: str) -> Optional[Intent]:
    """
    Try all rule-sets against *message*.
    Returns the best-matching Intent or None when nothing fires.
    """
    best: Optional[Tuple[float, Intent]] = None

    for ruleset, compiled in _RULES:
        for pattern in compiled:
            if pattern.search(message):
                candidate = Intent(
                    name=ruleset.intent,
                    confidence=ruleset.confidence,
                    slots=dict(ruleset.slots),
                    raw_message=message,
                    extraction_method="rules",
                )
                if best is None or ruleset.confidence > best[0]:
                    best = (ruleset.confidence, candidate)
                break   # first pattern that fires for this ruleset is enough

    return best[1] if best else None
