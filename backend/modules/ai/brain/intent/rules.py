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
    INTENT_ASK_PAYMENT_INFO,
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
    INTENT_PLATFORM_INQUIRY,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    INTENT_WHO_ARE_YOU,
    Intent,
)
from .platform_classifier import classify_platform
from .social_classifier import classify_social


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

# ── Payment info / bank transfer / IBAN / barcode (registered BEFORE
#    owner_contact so a request like "ارسل حساب الراجحي" doesn't fall
#    through to the static "هذه وسائل التواصل المتاحة" FAQ template).
#
# Confidence 0.95 ensures this beats both INTENT_ASK_OWNER_CONTACT (0.92)
# and INTENT_ASK_PRODUCT (0.88) when the customer is asking for payment
# details — even if the message also brushes against generic "ارسل" /
# "أبغى" verbs that other rules look for. The decision engine then
# routes this intent to the brain compose path so GPT can attach the
# matching AI Media Library item (e.g. bank-transfer barcode) instead
# of replying with the generic "contact owner" FAQ.
_register(RuleSet(
    intent=INTENT_ASK_PAYMENT_INFO,
    patterns=[
        # Bank account / transfer phrasings — Saudi & GCC dialects.
        r"(حساب الراجحي|حساب راجحي|راجحي|الراجحي|الأهلي|أهلي|الرياض|الرياض بنك|"
        r"حساب البنك|حساب بنك|حساب بنكي|رقم الحساب|رقم حساب|"
        r"الآيبان|الايبان|آيبان|ايبان|iban|"
        r"تحويل بنكي|تحويل بنكى|التحويل البنكي|بيانات التحويل|بيانات الدفع|"
        r"الإيداع|إيداع|تحويلة|ترانزفر)",
        # Payment barcode / QR phrasings.
        r"(باركود التحويل|باركود الدفع|باركود البنك|باركود الراجحي|"
        r"qr code|كود qr|كيو ?ار|كيوار)",
        # Common explicit asks ("ارسل حساب / صور لي الباركود / أبغى الآيبان").
        r"((ارسل|أرسل|ابعث|ابغى|ابغي|ودي|ابي|أبي|ابعتلي|ارسلي|"
        r"\bsend me\b|\bgive me\b)\s*(لي\s+|له\s+|لها\s+)?"
        r"(حساب|الحساب|الايبان|الآيبان|باركود|الباركود|بيانات\s+التحويل|بيانات\s+الدفع|"
        r"تحويل|الراجحي|راجحي|بنك\s+\S*|الأهلي|أهلي))",
        # English fallbacks — covers customers who use mixed messaging.
        r"(bank (account|details|transfer)|payment (barcode|qr)|iban (number)?)",
    ],
    confidence=0.95,
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

    Evaluation order:
      0. Single-digit fast path → INTENT_PICK_LIST_ITEM (deterministic).
      1. INTENT_SOCIAL — social / courtesy / religious messages
         ("جزاك الله خير", "بيض الله وجهك", "صلى الله عليه وسلم",
         "بسم الله", "كفو", etc.). Conservative; only fires when the
         message is dominantly social (short + no commercial signal)
         or unmistakably religious (prophet invocation / basmala).
         Confidence 0.92–0.97. Slot: ``social_category``.
      2. INTENT_PLATFORM_INQUIRY — questions about NAHLA the SaaS
         platform ("كم اشتراك نحلة؟"، "كيف أربط مع Meta؟"، "API"،
         "لوحة التحكم"، voice notes about "الذكاء، الباقات، الربط").
         Confidence 0.93–0.95. Slot: ``platform_topic``.
      3. Regex chain — commerce / FAQ / order intents as before.

    Both new classifiers run BEFORE the regex chain so their higher
    confidences (0.92+) beat the commerce defaults (0.82–0.90) on
    overlap. The classifiers themselves are deterministic and run in
    O(message length) — safe on the synchronous critical path.
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

    candidates: List[Tuple[float, Intent]] = []

    # ── Layer 1: social / courtesy / religious ──────────────────────────
    social = classify_social(message)
    if social is not None:
        candidates.append((
            social.confidence,
            Intent(
                name=INTENT_SOCIAL,
                confidence=social.confidence,
                slots={"social_category": social.category},
                raw_message=message,
                extraction_method="rules",
            ),
        ))

    # ── Layer 2: platform / SaaS inquiry ────────────────────────────────
    platform = classify_platform(message)
    if platform is not None:
        candidates.append((
            platform.confidence,
            Intent(
                name=INTENT_PLATFORM_INQUIRY,
                confidence=platform.confidence,
                slots={"platform_topic": platform.topic},
                raw_message=message,
                extraction_method="rules",
            ),
        ))

    # ── Layer 3: regex chain (commerce / FAQ / order intents) ───────────
    for ruleset, compiled in _RULES:
        for pattern in compiled:
            if pattern.search(message):
                candidates.append((
                    ruleset.confidence,
                    Intent(
                        name=ruleset.intent,
                        confidence=ruleset.confidence,
                        slots=dict(ruleset.slots),
                        raw_message=message,
                        extraction_method="rules",
                    ),
                ))
                break   # first pattern that fires for this ruleset is enough

    if not candidates:
        return None

    # Default: highest-confidence wins.
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_conf, best_intent = candidates[0]

    # ── First-contact welcome gate ───────────────────────────────────────
    # When the customer sends "السلام عليكم أبي سعر العسل" the bare
    # confidence ranking can pick ``INTENT_GREETING`` (0.95) and route to
    # ACTION_GREET — but the customer ALSO asked a real question and
    # ignoring it feels robotic. Two cases to handle:
    #
    #   (a) GREETING is the best → demote to the strongest actionable
    #       sibling (price / product / order / payment / shipping /
    #       platform / store-info / owner-contact / track).
    #   (b) GREETING fired *alongside* an actionable best (e.g.
    #       PLATFORM_INQUIRY 0.95 + GREETING 0.95 — platform wins on
    #       tie-breaker insertion order) → keep the actionable intent
    #       and just decorate it with ``embedded_greeting=True``.
    #
    # In both cases the composer/pipeline prepends a warm salaam line so
    # the salutation is honoured and the actionable answer follows.
    has_greeting = any(intent.name == INTENT_GREETING for _, intent in candidates)

    if best_intent.name == INTENT_GREETING:
        actionable = _pick_embedded_actionable(candidates)
        if actionable is not None:
            embedded_slots = dict(actionable.slots or {})
            embedded_slots["embedded_greeting"] = True
            return Intent(
                name=actionable.name,
                confidence=actionable.confidence,
                slots=embedded_slots,
                raw_message=message,
                extraction_method="rules+welcome_gate",
            )
        return best_intent

    if has_greeting and best_intent.name in _FIRST_CONTACT_ACTIONABLE_INTENTS:
        embedded_slots = dict(best_intent.slots or {})
        embedded_slots["embedded_greeting"] = True
        return Intent(
            name=best_intent.name,
            confidence=best_intent.confidence,
            slots=embedded_slots,
            raw_message=message,
            extraction_method="rules+welcome_gate",
        )

    return best_intent


# Intents we treat as "actionable" enough on the very first turn that a
# leading greeting should NOT short-circuit the conversation into the
# welcome card. Kept tight on purpose:
#   * commerce (price/product/order/payment) — the core sales reasons a
#     customer would salaam-and-ask in the same breath.
#   * platform inquiry — onboarding/subscription questions deserve their
#     KB-aware reply instead of "هلا بك في المتجر".
#   * shipping / store_info / owner_contact / track_order — FAQ-grade
#     replies still beat a generic welcome.
# Deliberately EXCLUDED:
#   * INTENT_SOCIAL — short courtesies; if the customer said salaam +
#     blessing we still want the greeting card (they've offered nothing
#     to act on).
#   * INTENT_GREETING / INTENT_GENERAL / INTENT_WHO_ARE_YOU /
#     INTENT_PICK_LIST_ITEM / INTENT_HESITATION / INTENT_TALK_HUMAN —
#     either redundant or wrong fit for an embedded-greeting case.
_FIRST_CONTACT_ACTIONABLE_INTENTS: frozenset[str] = frozenset({
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_TRACK_ORDER,
    INTENT_PLATFORM_INQUIRY,
})


def _pick_embedded_actionable(
    candidates: List[Tuple[float, Intent]],
) -> Optional[Intent]:
    """Return the strongest actionable candidate or ``None``.

    Requires confidence ≥ 0.80 so we only demote the greeting for a
    clearly intentional secondary signal. Sorted by confidence descending.
    """
    actionable = [
        (conf, intent)
        for conf, intent in candidates
        if intent.name in _FIRST_CONTACT_ACTIONABLE_INTENTS
        and conf >= 0.80
    ]
    if not actionable:
        return None
    actionable.sort(key=lambda x: x[0], reverse=True)
    return actionable[0][1]
