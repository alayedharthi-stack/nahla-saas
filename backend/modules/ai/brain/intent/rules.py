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
    INTENT_PICK_LIST_ITEM,
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
        # Post-hesitation confirmations — "خلاص أبيه" / "تمام خذه" / standalone "أبيه"
        r"(خلاص|تمام|يلا|موافق|ماشي|حسناً|اوكي|ok|okay).{0,15}(أبيه|أبي|خذه|آخذه|اشتريه|طلبه|جهزه|حجزه)",
        r"^\s*(أبيه|آخذه|اشتريه|خذه|طلبه|جهزه|حجزه|اجهزه)\s*$",
        # Urgent continuations — "الآن" / "الحين" / "الان" after product selection
        r"^\s*(الآن|الان|الحين|هلا|هلق|حالاً|فوراً|فورا|حالا)\s*$",
        # "اطلبه" / "خذه" / "جهزه" alone
        r"^\s*(اطلبه|اطلبها|اطلبهم|اطلب|جهزه|جهزها|احجزه|احجزها)\s*$",
    ],
    confidence=0.88,
))

# ── Pay / checkout ────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_PAY_NOW,
    patterns=[
        r"(ادفع|أدفع|دفع|سدد|أسدد|إتمام الدفع|الدفع الآن|أكمل الدفع|دفع الآن|تحصيل)",
        r"(رابط الدفع|رابط الطلب|وين الرابط|ارسل الرابط|أرسل الرابط|ابعث الرابط)",
        r"(pay|payment link|checkout link)",
        # "هل أنشأت الطلب؟" / "هل تم الطلب؟" → retry order/payment
        r"(هل أنشأت|هل انشأت|هل تم الطلب|هل تم إنشاء|هل اكتمل الطلب|اكتمل الطلب)",
        r"(لم يصلني الرابط|لم يأت الرابط|ما وصل الرابط|ما جاء الرابط|ما جالي رابط)",
    ],
    confidence=0.90,
))

# ── Shipping / delivery ───────────────────────────────────────────────────────
#
# IMPORTANT: the previous rule used a bare ``شحن`` token, which fired
# whenever the customer typed "الشحنة" / "شحنتي" / "وصلت الشحنة" — a
# personal-shipment status question — and locked them into the generic
# shipping-policy FAQ template instead of the order-tracking flow.
#
# We now require explicit shipping-POLICY context (rates, methods,
# duration, free shipping, etc.) and let everything else fall through to
# the LLM where the brain can look at the customer's order history.
_register(RuleSet(
    intent=INTENT_ASK_SHIPPING,
    patterns=[
        # Cost / fee questions
        r"(كم.{0,8}(الشحن|التوصيل)|رسوم.{0,5}(الشحن|التوصيل)|سعر.{0,5}(الشحن|التوصيل))",
        # Method / area questions
        r"(طرق.{0,5}(الشحن|التوصيل)|سياسة.{0,5}(الشحن|التوصيل)|شحن مجاني|توصيل مجاني|مناطق.{0,5}(الشحن|التوصيل))",
        # Duration questions ("how many days", "when does it arrive")
        r"(مدة.{0,5}(الشحن|التوصيل)|كم يوم|كم تأخذ|كم يستغرق|متى يوصل الطلب|متى توصل الطلبية)",
        r"(shipping (cost|fee|price|policy|methods?|areas?)|how (long|many days)|free shipping|delivery (cost|fee|time))",
    ],
    confidence=0.85,
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
#
# Personal shipment questions ("هل وصلت الشحنة؟" / "وصلت طلبيتي؟") used
# to be misclassified as INTENT_ASK_SHIPPING because of the bare "شحن"
# token in the shipping rule. They belong here — the customer is asking
# about THEIR shipment, not about shipping policy in general.
_register(RuleSet(
    intent=INTENT_TRACK_ORDER,
    patterns=[
        # "where is my X?" — high specificity, beats ASK_PRODUCT's broad "وين"
        r"(وين|أين|فين)\s*(طلبي|طلبيتي|شحنتي|الشحنة|الطلبية|أمري)",
        r"(طلبي|طلبيتي|شحنتي)\s*(وين|أين|فين)",
        r"(تتبع الطلب|متى يوصل طلبي|رقم الطلب)",
        r"(هل وصل|هل وصلت).{0,15}(الشحنة|الطلبية|الطلب|طلبي|طلبيتي|شحنتي|الشحنه)",
        r"(وصلت|وصلتني).{0,8}(الشحنة|الطلبية|طلبيتي|شحنتي)",
        r"(متى توصل طلبيتي|متى توصل شحنتي)",
        r"(track|track my order|where is my order|order status|did my (order|shipment) arrive)",
    ],
    # Bumped above the default 0.88 so it wins ties against ASK_PRODUCT's
    # broad "وين" pattern: "وين شحنتي" must classify as TRACK_ORDER, not
    # ASK_PRODUCT, otherwise we lose the order-tracking flow.
    confidence=0.92,
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

# Pre-compiled pattern to detect a standalone digit (1-9 or Arabic ١-٩)
_SINGLE_DIGIT = re.compile(r'^\s*([١٢٣٤٥٦٧٨٩1-9])\s*$', re.UNICODE)
_ARABIC_DIGIT_MAP = {'١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5',
                     '٦': '6', '٧': '7', '٨': '8', '٩': '9'}


def match(message: str) -> Optional[Intent]:
    """
    Try all rule-sets against *message*.
    Returns the best-matching Intent or None when nothing fires.
    """
    # ── Fast path: single digit → pick from last shown list ──────────────
    m = _SINGLE_DIGIT.match(message)
    if m:
        digit = m.group(1)
        latin = _ARABIC_DIGIT_MAP.get(digit, digit)
        return Intent(
            name=INTENT_PICK_LIST_ITEM,
            confidence=0.97,
            slots={"list_index": int(latin)},
            raw_message=message,
            extraction_method="rules",
        )

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
