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
    INTENT_ASK_LOCATION,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HESITATION,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_PAY_NOW,
    INTENT_PICK_LIST_ITEM,
    INTENT_PLATFORM_INQUIRY,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_EMPLOYEE_NOT_RESPONDING,
    INTENT_PERSONA_INTERACTION,
    INTENT_TRACK_ORDER,
    INTENT_WHO_ARE_YOU,
    Intent,
)
from .platform_classifier import classify_platform
from .social_classifier import classify_social
from .non_commerce_classifier import classify_non_commerce
from .need_based_product_classifier import classify_need_based_product_advice
from ..commerce.solution_seeking import classify_solution_seeking_commerce
from ..commerce.contact_escalation import classify_employee_not_responding
from .persona_interaction_classifier import classify_persona_interaction


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
        # Playful / persona probes — must beat non-commerce mis-tags (e.g. "تنامين؟").
        r"(?:هل\s*)?(?:انت|أنت|انتي|أنتِ)\s*(?:نحله|نحلة|بوت|روبوت|bot|chatbot|إنسان|انسان|بشر|موجود(?:ه|ة)?|ذكاء\s*اصطناعي|ذكاء|برنامج|ai)",
        r"(?:نحله|نحلة)\s*(?:انت|أنت|انتي|أنتِ|هذا|هذي|هي)",
        r"^(?:هل\s*)?(?:تنامين|تنام|ما\s*تنام|تنام\s*ولا|تنام\s*ولا\s*لا)\s*[\?؟]?$",
        r"(?:مو\s*انسان|مو\s*إنسان|مو\s*بشر|هل\s*انت\s*بشر)",
    ],
    confidence=0.98,
))

# ── Greeting ─────────────────────────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_GREETING,
    patterns=[
        r"^(السلام عليكم|وعليكم السلام|مرحبا?ً?|أهلاً?|هلا|صباح الخير|مساء الخير|كيف حالك|هاي|هلو|hello|hi\b|hey\b)",
        r"^(أهلين|يا هلا|هلأ|هلأً|أهلا وسهلا|حياك الله|حياك)",
    ],
    confidence=0.95,
))

# ── Product visual / image request ────────────────────────────────────────────
_register(RuleSet(
    intent=INTENT_PRODUCT_VISUAL_REQUEST,
    patterns=[
        r"(?:ال)?صور(?:ة)?\s*(?:وين|فين|وينها|فينها|مو\s*موجود|م[\s]?و)",
        r"(?:وين|فين)\s*(?:ال)?صور(?:ة)?",
        r"(?:اب(?:ي|غ(?:ى|ا)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?)|ودي|"
        r"ار(?:سل|سل)|أ(?:رسل|رس(?:ل)?)|ابعث|أبعث|ور(?:ي|)ني|ور(?:ي|)ن(?:ي|a))"
        r"\s*(?:ال)?(?:صور(?:ة)?|صور|شكل(?:ه|ها)?|المنتج|منتج)",
        r"(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?))\s*أ?شوف\s*(?:ال)?(?:صور(?:ة)?|صور|المنتج|منتج)",
        r"صور(?:ة)?\s*(?:ل|لـ|ال)\s+\S.{1,40}",
        r"\S.{1,30}\s+صور(?:ة)?",
        r"صور\s+(?:ال)?(?:عسل|منتج|طلح|سدر|سمر|ضهيان|سمر)",
        r"(?:show|send)\s+(?:me\s+)?(?:the\s+)?(?:product\s+)?(?:image|photo|picture)",
    ],
    confidence=0.93,
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
        # Urgent continuations — "الآن" / "الحين" / "الان" after product selection.
        # ``هلا`` is intentionally excluded — it is a pure salaam matched by
        # ``INTENT_GREETING``; commerce demotion requires residue (see
        # ``_has_commerce_residue`` in the welcome gate).
        r"^\s*(الآن|الان|الحين|هلق|حالاً|فوراً|فورا|حالا)\s*$",
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
# We require explicit shipping-POLICY context (rates, methods, duration,
# free shipping, etc.) and let everything else fall through to the LLM
# where the brain can look at the customer's order history.
#
# May 2026 #17 — production regression: customer asked
# "وشلون طريقة توصيل الطلبات عندكم?" and the message DID NOT match any
# of the original patterns:
#   * "طريقة" (singular) vs "طرق" (plural) — only plural was covered.
#   * "وشلون / كيف" "how" colloquial questions weren't covered at all.
#   * "توصلون / تشحنون" verb forms weren't covered.
# So the message fell through to ``INTENT_ASK_PRODUCT`` (which fires on
# the substring "طلب" inside "الطلبات", confidence 0.82) and ultimately
# the soft-retry fallback.
#
# This rule now covers:
#   * Cost: "كم الشحن / رسوم التوصيل / كم سعر الشحن"
#   * "How do you ship?" verb forms: "وشلون التوصيل / كيف توصلون /
#     شلون الشحن / كيفية التوصيل"
#   * Method / area: "طريقة|طرق الشحن|التوصيل / سياسة الشحن /
#     شحن مجاني / مناطق التوصيل"
#   * Existence: "هل تشحنون / هل عندكم توصيل / عندكم مندوب /
#     عندكم شحن"
#   * Duration: "مدة الشحن / كم يوم / متى يوصل الطلب"
#   * Carrier: "هل التوصيل سمسا / عن طريق سمسا / اراميكس / dhl"
#     (Saudi shipping carriers are domain-specific enough that
#     mentioning one in a question is unambiguous shipping intent)
#   * Destination city: "الشحن للرياض / تشحنون لجدة / التوصيل للدمام"
#
# Confidence bumped 0.85 → 0.90 so it beats the bare ASK_PRODUCT match
# on "طلب" substring (conf 0.82) AND the rules-only threshold (0.85)
# so we short-circuit the LLM extractor for these very common asks.
_register(RuleSet(
    intent=INTENT_ASK_SHIPPING,
    patterns=[
        # Cost / fee
        r"(كم.{0,8}(الشحن|التوصيل)|رسوم.{0,5}(الشحن|التوصيل)|سعر.{0,5}(الشحن|التوصيل))",
        # "How do you ship?" — colloquial + formal Arabic interrogatives.
        # Covers: وشلون / شلون / كيف / كيفية + (الشحن | التوصيل |
        # توصلون | توصيل | تشحنون | الشحنات | شحنكم).
        r"(وشلون|شلون|كيف|كيفية).{0,15}(الشحن|التوصيل|توصلون|توصيل|تشحنون|الشحنات|شحنكم|التوصيلات)",
        # Method / area — singular AND plural now covered ("طريقة" vs "طرق").
        r"((طريقة|طرق).{0,8}(الشحن|التوصيل|توصيل|توصيلكم|شحنكم|التوصيلات)|سياسة.{0,5}(الشحن|التوصيل)|شحن مجاني|توصيل مجاني|مناطق.{0,5}(الشحن|التوصيل))",
        # Existence / "do you have"
        r"(هل.{0,8}(تشحنون|توصلون|توصيل|عندكم.{0,5}(توصيل|شحن|مندوب))|عندكم.{0,5}(توصيل|شحن|مندوب|مناديب))",
        # Carrier-by-name question — "توصيلكم عن طريق مين؟" / "الشحن
        # عن طريق شركة" / "بواسطة سمسا". The "by-whom" framing is
        # unambiguous shipping intent regardless of which carrier
        # name follows.
        r"((توصيلكم|التوصيل|الشحن|شحنكم|تشحنون|توصلون).{0,12}(عن طريق|بواسطة|مع شركة|شركة الشحن|مع مين|عن طريق مين))",
        # Duration. Word-boundary on Arabic is tricky (re.findall on
        # naked tokens like "كم يوم" matches the "-كم" suffix of any
        # verb followed by "يوم" — production bug May 2026: greeting
        # card with "ويبلغكم يوم النحر" mis-classified as shipping).
        # We now require either:
        #   * the duration question to be ABOUT shipping/delivery
        #     ("كم يوم للتوصيل / كم يوم يستغرق الشحن"), OR
        #   * a true sentence-initial / whitespace-anchored ask
        #     ("^كم يوم" or "كم يوم[?؟]?$" — short duration query).
        r"(مدة.{0,5}(الشحن|التوصيل|الطلب|الطلبية|التوصيلات))",
        r"(?<![\u0600-\u06FF])كم\s+يوم\s+(?:(?:يـ|للـ?|على|من|في|للشحن|للتوصيل|للوصول|للتسليم|تأخذ|تأخذون|توصل|يوصل|يأخذ|يستغرق))",
        r"(?<![\u0600-\u06FF])كم\s+يوم\b(?:\s*[\u061F\u003F])?\s*$",
        r"(?<![\u0600-\u06FF])كم\s+(?:تأخذ|تأخذون|يستغرق|يستغرقها)\s+(?:الشحن|التوصيل|الطلب|الطلبية|التوصيلات|الوصول|الشحنة)",
        r"(متى يوصل الطلب|متى توصل الطلبية|متى تشحنون|متى يتم الشحن|متى يجي الطلب)",
        # Carrier / courier — Saudi market specific. Customers naming a
        # carrier in a question are unambiguously asking about shipping
        # ("هل التوصيل سمسا؟" / "تشحنون مع اراميكس؟").
        r"(سمسا|اراميكس|ارامكس|دي ?اتش ?ال|\bdhl\b|\baramex\b|\bsmsa\b)",
        # Destination city for shipping. Whitelisted to major Saudi
        # cities (with/without the ل prefix) so we don't misfire on
        # unrelated questions that happen to mention a city. The
        # outer alternation lists each city WITH its ل prefix
        # variant — a previous attempt at factoring "ل" outside the
        # group produced "للجدة" / "للالرياض" double prefixes.
        r"((الشحن|التوصيل|تشحنون|توصلون).{0,8}(للرياض|للجدة|لجدة|لجده|للجده|للطائف|للمدينة|للدمام|لمكة|للمكة|للقصيم|للأحساء|الرياض|جدة|جده|الدمام|الطائف|مكة|الأحساء))",
        # English fallbacks
        r"((?:فيه|في|هل\s+(?:فيه|في|عندكم))\s*.{0,12}(?:توصيل|شحن|مندوب|يوصل|تشحن|توصلون|تشحنون))",
        r"((?:توصيل|شحن|يوصل|تشحن).{0,30}(?:اذا|لو|لما|الحين|الان|الآن|الآن))",
        r"((?:موقعي|الموقع|موقع).{0,35}(?:توصيل|يوصل|تشحن|تجي|تيجي|توصل))",
        r"(shipping (cost|fee|price|policy|methods?|areas?)|how (do|long|many days)|free shipping|delivery (cost|fee|time|method))",
        r"(do you (ship|deliver)|where (do|can) you (ship|deliver))",
    ],
    confidence=0.90,
))

# ── Store info / e-commerce link ─────────────────────────────────────────────
# The patterns here are intentionally narrowed to the *online store* —
# physical-shop / Google-Maps phrasings were carved out into a
# dedicated INTENT_ASK_LOCATION rule below (May 2026 #36) so the
# brain can ship the maps URL deterministically instead of returning
# the e-commerce storefront link for "وين موقعكم".
_register(RuleSet(
    intent=INTENT_ASK_STORE_INFO,
    patterns=[
        r"(رابط المتجر|رابط متجركم|متجركم الإلكتروني|المتجر الإلكتروني)",
        r"(لينك المتجر|الموقع الإلكتروني|موقع(?:كم|ك|نا)\s*(?:ال)?(?:إ|ا)?لكتروني|عن المتجر|تعريف المتجر|وين متجركم الإلكتروني)",
        r"(رابط الطلب|(?:ا|أ)?(?:بي|بغى)\s+(?:أ?)?(?:طلب|اطلب)\s+من\s+الموقع|(?:^|\s)(?:ا|أ)?(?:ون)?(?:لاين)(?:\s|$))",
        r"(store link|store url|website link|online store|where is your store online|about the store)",
    ],
    confidence=0.92,
))


# ── Physical location / Google Maps / branch address ─────────────────────────
# Confidence 0.93 (one notch above ASK_STORE_INFO 0.92) so a phrasing
# like "وين موقعكم؟" — which COULD theoretically also brush
# ASK_STORE_INFO by virtue of containing "موقع" — picks the
# location intent first. The downstream maps resolver is
# deterministic; if no maps URL is configured, the FAQ template
# falls back to an honest clarifying line instead of the
# e-commerce store URL.
_register(RuleSet(
    intent=INTENT_ASK_LOCATION,
    patterns=[
        # Location / map phrasings — Saudi & GCC dialects.
        r"(وين موقعكم|أين موقعكم|وين الموقع|وين المحل|وين مقركم|مقر شركتكم)",
        r"(موقع المتجر|موقع المعرض|موقع المحل|وين أنتم|وين انتم)",
        r"(لوكيشن|لوكيشن المحل|لوكيشن المتجر|لوكيشن المعرض|لوكيشن الفرع|عنوان المحل|عنوان الفرع|عنوانكم)",
        r"((?:ارسل|أرسل|ارسلي|أرسلي|ابعث|أبعث|ابعثلي|أبعثلي|ابي|أبي|ابغى|أبغى)\s*(?:لي\s+)?(?:ال)?لوكيشن)",
        r"(الفرع|فروعكم|(?:^|\s)(?:ال)?فروع(?:\s|[؟?!.]|$)|(?:ابغ|ابي|أبغ|أبي|اريد|أريد)\s*(?:لي\s+)?(?:ال)?فروع|"
        r"عندكم فرع|وين فرعكم|أبي أزوركم|أبي أجي للمحل|نزور المحل|نزوركم)",
        r"\bbranches\b",
        r"(خرايط|الخرائط|خريطة|على الخريطة|رابط الموقع|رابط الخريطة|رابط الخرايط|رابط اللوكيشن)",
        # English / mixed
        r"(google maps|google\s*map|location|address|where is your shop|where is your branch|"
        r"map link|store location|branch location|physical store)",
    ],
    confidence=0.93,
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
        r"(حساب الراجحي|حساب راجحي|راجحي|الراجحي|الأهلي|أهلي|بنك\s*الرياض|الرياض\s*بنك|"
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
        # Vague payment/finance mentions — "لك فلوس معاي", "عندي مبلغ".
        r"((?:لك|ليك|عندي|معاي|معي|معاك)\s*(?:فلوس|مبلغ|حوال(?:ه|ة)|تحويل))",
        r"((?:فلوس|مبلغ|حوال(?:ه|ة))\s*(?:معاي|معي|عندي|لك|ليك|معاك))",
        r"^\s*(?:فلوس|مبلغ|تحويل|حوال(?:ه|ة))\s*[\?؟]?\s*$",
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
        # Tracking / shipment link follow-up (May 2026 — post-order guard).
        # Beats bare PAY_NOW "ارسل الرابط" when the customer is asking
        # for a tracking URL after checkout, not a store/checkout link.
        r"(رابط التتبع|رقم التتبع|رابط الشحن|رابط شحن)",
        r"(ارسل|أرسل|ارسلو|أرسلوا|ارسلي|أرسلي|ابعث|أبعث).{0,20}(التتبع|رقم التتبع|رابط التتبع|رابط الشحن)",
        r"(تشحن|تشحنو|تشحنه|تشحنها|انشحن|إذا شحن|اذا شحن|لما يشحن|لما تشحن).{0,35}(الرابط|رابط|التتبع|تتبع|اللينك|link)",
        r"(متى يوصل).{0,20}(رابط|التتبع|تتبع|link)",
        r"(track|track my order|where is my order|order status|did my (order|shipment) arrive|tracking link|tracking number)",
    ],
    # Bumped above PAY_NOW (0.90) and ASK_PRODUCT's broad "وين" pattern so
    # post-order tracking-link asks stay on the order-support path.
    confidence=0.96,
))

# ── Talk to human ─────────────────────────────────────────────────────────────
# Patterns are intentionally broad: in production we observed merchants
# losing sales because the brain kept "helping" customers who had
# already typed clear escalation requests like "كلموني" or "حولني". The
# patterns now cover:
#   * direct asks for a staff member (موظف / مختص / مسؤول / مشرف)
#   * "talk to / call me / transfer me" phrasing in Saudi/Gulf dialect
#     (كلموني / كلميني / اتصلوا فيني / حولني / حوّلني)
#   * "is there anyone there?" patterns (في أحد / في حد يرد / في موظف)
#   * existing English fallback (human agent / real person / …)
# Every variant is anchored on a unique token so the patterns don't
# fire on unrelated messages — e.g. "موظف" is paired with verbs/
# prefixes so we don't escalate on "أنا موظف لدى …".
_register(RuleSet(
    intent=INTENT_TALK_HUMAN,
    patterns=[
        # "talk to human / real person / not a bot"
        r"(تحدث مع إنسان|تحدث مع بشر|تواصل مع شخص|إنسان حقيقي|"
        r"مو روبوت|مو بوت|مش بوت|مش روبوت)",
        # Standalone customer-service mention (e.g. "خدمة العملاء من
        # فضلك", "خدمة العملاء لو سمحت") — the phrase is specific
        # enough to escalate on its own without an انتي/أبي prefix.
        r"^\s*(?:خدمة\s*(?:ال)?عملاء|خدمه\s*(?:ال)?عملاء|الدعم\s*ال?فني|دعم\s*ال?عملاء)\s*[\?؟]?\s*$",
        r"(خدمة العملاء|خدمه العملاء|الدعم الفني|دعم العملاء)",
        # Direct asks for a staff member (موظف / مختص / مسؤول / مشرف /
        # خدمة العملاء / شخص). The "أبي / أبغى / أريد / أحتاج / لو سمحت
        # / ممكن / في" prefix anchors guard against "أنا موظف …"
        # phrasing that should NOT escalate.
        r"(أبي|أبغى|أبغا|ابغى|ابغا|ابي|أريد|اريد|أحتاج|احتاج|محتاج|"
        r"ممكن|لو سمحت|في|فيه|هل في|هل يوجد|يوجد)"
        r"\s*"
        r"(موظف|مختص|مسؤول|مشرف|خدمة العملاء|خدمه العملاء|شخص|بشري|"
        r"إنسان|انسان)",
        # "حولني / حوّلني / حولوني (لموظف|للموظف|لخدمة|لشخص|للدعم)"
        r"(حولني|حوّلني|حولوني|حوّلوني|حولني|حولونا|حولنا)"
        r"\s*"
        r"(ل|لـ|الى|إلى)?"
        r"\s*"
        r"(موظف|مختص|مسؤول|مشرف|خدمة العملاء|شخص|بشري|إنسان|انسان|الدعم|دعم)?",
        # "كلموني / كلميني / كلمني / اتصلوا (فيني|بي|عليّ) / ردوا عليّ"
        r"(كلموني|كلميني|كلمني|كلموننا|كلمونا|اتصلوا فيني|اتصلوا بي|"
        r"اتصلوا علي|اتصل فيني|اتصل بي|ردوا علي|ردو علي|ردوا عليّ)",
        # "أبي أكلم أحد / أبغى أكلم موظف / أبي أتكلم مع أحد /
        #  أبغى أتحدث مع موظف / ودي أكلم واحد منكم".
        # The (?:\s+(?:مع|ل|لـ|الى|إلى|بـ|ب|من|معاكم|معاكم))? slot
        # captures Saudi/Gulf insertions between the verb and the
        # target token — without it, "أبي أتكلم مع أحد" misses the
        # rule because "مع" sits between "أتكلم" and "أحد".
        r"(أبي|أبغى|أبغا|ابغى|ابغا|ابي|أريد|اريد|أحتاج|احتاج|محتاج|"
        r"ممكن|لو سمحت|ودي|بدي|عندي رغبة|رغبتي)"
        r"\s*(أكلم|اكلم|اتكلم|أتكلم|اتحدث|أتحدث|كلم|احكي|أحكي|اتواصل|أتواصل)"
        r"(?:\s+(?:مع|ل|لـ|الى|إلى|بـ|ب|من|معاكم|معكم))?"
        r"\s*(?:\S+\s+){0,2}?"
        r"(أحد|احد|واحد|موظف|موظفه|موظفة|مختص|مختصه|مختصة|مسؤول|"
        r"مسؤوله|مسؤولة|مشرف|مشرفه|مشرفة|شخص|بشري|بشريه|بشرية|"
        r"إنسان|انسان|واحدمنكم|واحد منكم|"
        # May 2026 #42 — owner / management target nouns. The
        # customer's choice of framing ("المالك" / "الإدارة" /
        # "صاحب المحل") used to fall through to the default LLM
        # because it wasn't enumerated here. Adding them keeps the
        # routing on the staff-escalation path even when the
        # PRE-BRAIN handoff guard misses a regional phrasing.
        r"المالك|مالك|صاحب المحل|صاحب المتجر|صاحبك|"
        r"الإدارة|الادارة|إدارة|ادارة)?",
        # "في أحد يرد / فيه أحد يرد" — response verb required (ARCH-HANDOFF-001).
        r"(في|فيه|هل في|هل يوجد|يوجد|ما في|مافي|محد|ماحد)"
        r"\s*(أحد|احد|واحد|حد)"
        r"\s*(يرد|يردّ|يرد علي|يكلمني|يتواصل|يحكي|يجاوبني|يجاوب علي)",
        # Bare "anyone there?" — short message ending after أحد (not service X).
        r"(?:^|\s)(في|فيه|هل في|هل يوجد|يوجد)"
        r"\s*(أحد|احد|واحد|حد)\s*[\?؟]?\s*$",
        # ``فيه أحد هنا`` — presence check, not service availability.
        r"(?:^|\s)(في|فيه|هل في|هل يوجد|يوجد)"
        r"\s*(أحد|احد|واحد|حد)\s+هنا",
        # "محد رد علي / ما حد رد علي / محد يرد"
        r"(محد|ماحد|ما\s*أحد|ما\s*احد)\s*(رد|يرد|يجاوب|يكلمني)",
        # Standalone polite escalations seen in production:
        # "ودي اكلم احد" / "ودي اتكلم مع احد" / "ابي اكلم اي حد"
        r"ودي\s*(أكلم|اكلم|اتكلم|أتكلم|اتحدث|أتحدث)",
        # Soft "I want to talk to someone" without an explicit verb of
        # request — "اتكلم مع احد" / "اتكلم مع موظف" alone.
        r"(?:^|\s)(أتكلم|اتكلم|أتحدث|اتحدث)\s+(?:مع|لـ|ل)\s*"
        r"(أحد|احد|واحد|موظف|مختص|مسؤول|مشرف|شخص|بشري|إنسان|انسان)",
        # English fallback — kept intact.
        r"(human agent|real person|customer service|speak to someone|"
        r"talk to agent|talk to a human|connect me to|transfer me to)",
    ],
    # Bumped above the default 0.88 so the broader new patterns still
    # win cleanly against generic "ask_product"-style matches when a
    # customer mentions a product name in the same sentence.
    confidence=0.92,
))


# ─────────────────────────────────────────────────────────────────────────────

# Pre-compiled pattern to detect a standalone digit (1-9 or Arabic ١-٩)
_SINGLE_DIGIT = re.compile(r'^\s*([١٢٣٤٥٦٧٨٩1-9])\s*$', re.UNICODE)
_ARABIC_DIGIT_MAP = {'١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5',
                     '٦': '6', '٧': '7', '٨': '8', '٩': '9'}


def match_top_k(message: str, *, k: int = 3) -> List[Tuple[float, "Intent"]]:
    """Return the top-k intent candidates ranked by confidence.

    Equivalent to :func:`match` but exposes the runner-up intents
    instead of collapsing to the single winner. Built for the
    observability layer (``TurnTrace``) so the production log line
    can show:

        top_intents=[(ask_shipping, 0.90), (ask_product, 0.82),
                     (greeting, 0.50)]

    which makes "why did this turn route to X?" answerable from a
    single grep instead of replaying the regex chain in the head.

    The function is a SEPARATE entry point on purpose:

      * ``match()`` keeps its existing contract (single ``Intent`` or
        ``None``) so all downstream consumers — pipeline, decision
        engine, slot-extractor gating — stay untouched.
      * ``match_top_k()`` is observability-only; it does NOT apply
        the welcome-gate demotion (the runner-up list is more useful
        for debugging when shown WITHOUT post-processing).

    Returns
    -------
    List[Tuple[confidence, Intent]] sorted descending by confidence.
    Length is clamped to ``k`` (default 3). Empty list when no rule
    fires.
    """
    out: List[Tuple[float, "Intent"]] = []

    # Single-digit fast path — short-circuit and return a single
    # high-confidence intent (matches the welcome-gate-free behaviour
    # of the original match() for digits).
    m = _SINGLE_DIGIT.match(message)
    if m:
        digit = m.group(1)
        latin = _ARABIC_DIGIT_MAP.get(digit, digit)
        return [(
            0.97,
            Intent(
                name=INTENT_PICK_LIST_ITEM, confidence=0.97,
                slots={"list_index": int(latin)},
                raw_message=message, extraction_method="rules",
            ),
        )]

    social = classify_social(message)
    if social is not None:
        out.append((
            social.confidence,
            Intent(
                name=INTENT_SOCIAL, confidence=social.confidence,
                slots={"social_category": social.category},
                raw_message=message, extraction_method="rules",
            ),
        ))

    non_commerce = classify_non_commerce(message)
    if non_commerce is not None:
        out.append((
            non_commerce.confidence,
            Intent(
                name=INTENT_SOCIAL,
                confidence=non_commerce.confidence,
                slots={
                    "social_category": non_commerce.social_category,
                    "block_commerce_escalation": True,
                    "non_commerce_source": non_commerce.source,
                },
                raw_message=message,
                extraction_method="rules",
            ),
        ))

    platform = classify_platform(message)
    if platform is not None:
        out.append((
            platform.confidence,
            Intent(
                name=INTENT_PLATFORM_INQUIRY, confidence=platform.confidence,
                slots={"platform_topic": platform.topic},
                raw_message=message, extraction_method="rules",
            ),
        ))

    need_based = classify_solution_seeking_commerce(message) or classify_need_based_product_advice(message)
    if need_based is not None:
        out.append((
            need_based.confidence,
            Intent(
                name=INTENT_NEED_BASED_PRODUCT_ADVICE,
                confidence=need_based.confidence,
                slots={
                    "need_category": need_based.axis,
                    "solution_axis": need_based.axis,
                },
                raw_message=message,
                extraction_method="rules",
            ),
        ))

    for ruleset, compiled in _RULES:
        for pattern in compiled:
            if not pattern.search(message):
                continue
            if ruleset.intent == INTENT_TALK_HUMAN:
                from .service_availability_gate import (  # noqa: PLC0415
                    is_service_availability_inquiry,
                )
                if is_service_availability_inquiry(message):
                    break
            out.append((
                ruleset.confidence,
                Intent(
                    name=ruleset.intent, confidence=ruleset.confidence,
                    slots=dict(ruleset.slots),
                    raw_message=message, extraction_method="rules",
                ),
            ))
            break

    out.sort(key=lambda x: x[0], reverse=True)
    return out[: max(1, int(k))]


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

    # ── Layer 1b: non-commerce media / long OCR greetings (May 2026) ───
    non_commerce = classify_non_commerce(message)
    if non_commerce is not None:
        candidates.append((
            non_commerce.confidence,
            Intent(
                name=INTENT_SOCIAL,
                confidence=non_commerce.confidence,
                slots={
                    "social_category": non_commerce.social_category,
                    "block_commerce_escalation": True,
                    "non_commerce_source": non_commerce.source,
                },
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

    # ── Layer 2b: need-based advisory product questions ───────────────
    need_based = classify_solution_seeking_commerce(message) or classify_need_based_product_advice(message)
    if need_based is not None:
        candidates.append((
            need_based.confidence,
            Intent(
                name=INTENT_NEED_BASED_PRODUCT_ADVICE,
                confidence=need_based.confidence,
                slots={
                    "need_category": need_based.axis,
                    "solution_axis": need_based.axis,
                },
                raw_message=message,
                extraction_method="rules",
            ),
        ))

    # ── Layer 2c: staff-not-responding follow-up ──────────────────────
    # Runs before the regex chain so track_order (0.96) still wins
    # when order/shipment nouns are present; this layer only fires
    # when :func:`classify_employee_not_responding` passes exclusion
    # guards (no competing track/delivery/complaint/fresh-handoff).
    enr = classify_employee_not_responding(message)
    if enr is not None:
        candidates.append((
            enr.confidence,
            Intent(
                name=INTENT_EMPLOYEE_NOT_RESPONDING,
                confidence=enr.confidence,
                slots={"trigger_pattern": enr.pattern},
                raw_message=message,
                extraction_method="rules+employee_not_responding",
            ),
        ))

    # ── Layer 2d: persona social / emotional probes (Phase 2) ─────────
    # Playful, affectionate, appearance, tease, mild upset — must beat
    # generic commerce fallback. Excludes operational/complaint context
    # (see persona_interaction_classifier).
    _persona = classify_persona_interaction(message)
    if _persona is not None:
        candidates.append((
            _persona.confidence,
            Intent(
                name=INTENT_PERSONA_INTERACTION,
                confidence=_persona.confidence,
                slots={
                    "persona_topic": _persona.persona_topic,
                    "persona_kind": _persona.persona_kind,
                    "block_commerce_escalation": True,
                },
                raw_message=message,
                extraction_method="rules+persona_interaction",
            ),
        ))

    # ── Layer 3: regex chain (commerce / FAQ / order intents) ───────────
    # ARCH-MEDIA-001 Wave 0: match on customer/vision body only — never on
    # normalizer framing like ``[وصف الصورة المرسلة] وصف الصورة``.
    try:
        from ..commerce.product_visual import (  # noqa: PLC0415
            is_product_visual_request,
            strip_bot_media_framing,
        )
        _regex_surface = strip_bot_media_framing(message) or message
    except Exception:  # noqa: BLE001
        is_product_visual_request = None  # type: ignore[misc, assignment]
        _regex_surface = message

    for ruleset, compiled in _RULES:
        for pattern in compiled:
            if not pattern.search(_regex_surface):
                continue
            if (
                ruleset.intent == INTENT_PRODUCT_VISUAL_REQUEST
                and is_product_visual_request is not None
                and not is_product_visual_request(_regex_surface)
            ):
                continue
            if ruleset.intent == INTENT_TALK_HUMAN:
                from .service_availability_gate import (  # noqa: PLC0415
                    is_service_availability_inquiry,
                )
                if is_service_availability_inquiry(message):
                    break
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
    best_intent = _apply_need_based_priority(candidates, best_intent)

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
        # Pure salaam must stay GREETING — demote only when commerce residue
        # survives the greeting strip (e.g. "السلام عليكم أبي سعر العسل").
        if actionable is not None and _has_commerce_residue(message):
            embedded_slots = dict(actionable.slots or {})
            embedded_slots["embedded_greeting"] = True
            return Intent(
                name=actionable.name,
                confidence=actionable.confidence,
                slots=embedded_slots,
                raw_message=message,
                extraction_method="rules+welcome_gate",
            )
        # May 2026 #19 — open-ended question hidden behind the salaam.
        # When no specific rule (price/product/order/…) matched but the
        # message still carries substantive content beyond the greeting
        # phrase ("مساء الخير نحلة وش نشاطهم"), demote to INTENT_GENERAL
        # so the LLM brain gets to see and answer the embedded ask.
        # Pure salaams (residue ≤ 2 chars) keep the greeting card.
        if _has_substantive_residue(message):
            return Intent(
                name=INTENT_GENERAL,
                # Confidence intentionally lower than the original
                # greeting (0.95) but still above the LLM-fallback
                # floor — the downstream pipeline reads this as
                # "needs LLM interpretation" without short-circuiting.
                confidence=0.80,
                slots={"embedded_greeting": True},
                raw_message=message,
                extraction_method="rules+welcome_gate+residue",
            )
        return best_intent

    if (
        has_greeting
        and best_intent.name in _FIRST_CONTACT_ACTIONABLE_INTENTS
        and not is_pure_greeting_without_commerce(message)
    ):
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
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_ASK_PRICE,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_ASK_LOCATION,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_TRACK_ORDER,
    INTENT_PLATFORM_INQUIRY,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
})

# Solution-seeking loses to delivery / payment / support / order intents.
_PRIORITY_OVER_NEED_BASED: frozenset[str] = frozenset({
    INTENT_ASK_SHIPPING,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_PAY_NOW,
    INTENT_TRACK_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_LOCATION,
})


def _apply_need_based_priority(
    candidates: List[Tuple[float, Intent]],
    best: Intent,
) -> Intent:
    """Demote advisory product intent when a higher-priority intent matched."""
    if best.name != INTENT_NEED_BASED_PRODUCT_ADVICE:
        return best
    for conf, intent in candidates:
        if intent.name in _PRIORITY_OVER_NEED_BASED and conf >= 0.82:
            return intent
    return best


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


# ── Greeting-residue detector (May 2026 #19) ───────────────────────────────────
# Customer reported: "مساء الخير نحلة كيف حالك بسألك عن العايد وش نشاطهم"
# was classified purely as INTENT_GREETING and the bot replied with the
# generic welcome card — completely ignoring the embedded question
# "وش نشاطهم". The existing welcome-gate code only demoted the greeting
# when ANOTHER rule (price/product/order/…) ALSO matched. Questions that
# don't trip a specific rule (open-ended "what do you sell?", store
# nature questions, broad about-us asks) fell through and the customer
# was canned.
#
# Design choice (per merchant directive — no keyword→reply rules):
# instead of adding regex patterns for "وش نشاطكم" / "ايش تبيعون" /
# "نشاط المتجر" etc., we use a STRUCTURAL test:
#
#   1. Iteratively strip leading greeting / vocative / honorific tokens
#      (using the SAME phrases already declared in INTENT_GREETING
#      patterns above — no new vocabulary).
#   2. Strip the bot's own name ("نحلة" + variants), since customers
#      routinely tag the bot in their salaam.
#   3. Strip pleasant-question filler ("كيف حالك" / "كيف الحال").
#   4. Whatever is LEFT — if it has any substantive content
#      (≥ 3 Arabic/Latin characters after the strip pass) — is a real
#      question the LLM should see.
#
# When residue is non-trivial AND no actionable rule matched, we
# demote the intent to INTENT_GENERAL with ``embedded_greeting=True``.
# The pipeline (pipeline.py:1060) already knows how to handle that
# flag — it prepends a brief warm acknowledgement and lets the LLM
# answer the substantive part.
#
# This is the OPPOSITE of adding rigid rules: we widened the LLM's
# reach by removing a short-circuit. The bot stays warm on pure
# salaam ("السلام عليكم" → still INTENT_GREETING) and gets smarter
# on mixed turns ("salaam + open question" → INTENT_GENERAL).

# Greeting / courtesy tokens used by the residue stripper. Source-of-
# truth is the INTENT_GREETING patterns above; this list mirrors them
# in plain-text form so we can iteratively peel them off the front of
# the message. Keep in sync if those patterns ever change. Anchored
# from the START only — the stripper walks the message left-to-right.
_GREETING_RESIDUE_LEAD_TOKENS = (
    # ── Religious / formal greetings ──
    "السلام عليكم ورحمة الله وبركاته",
    "السلام عليكم ورحمة الله",
    "السلام عليكم",
    "وعليكم السلام ورحمة الله وبركاته",
    "وعليكم السلام ورحمة الله",
    "وعليكم السلام",
    # ── Time-of-day ──
    "صباح الخير", "صباح النور", "صباح الفل", "صباح الورد",
    "مساء الخير", "مساء النور", "مساء الفل", "مساء الورد",
    # ── Casual ──
    "أهلاً وسهلاً", "أهلا وسهلا", "اهلا وسهلا",     "أهلين", "اهلين",
    "حياك الله", "حياك",
    "أهلاً", "أهلا", "اهلاً", "اهلا",
    "مرحباً", "مرحبا", "مرحبًا",
    "هلا والله", "هلا وغلا", "يا هلا", "يا مية هلا",
    "هلاً", "هلا", "هلأ", "هلو",
    # ── How-are-you fillers — these are still pure courtesy, NOT
    #    substantive questions. The stripper removes them so the
    #    residue check only sees REAL content. ──
    "كيف حالك", "كيف الحال", "كيف الحال اليوم", "كيفك", "كيف أحوالك",
    "كيف انت", "كيف أنت", "كيف الأحوال", "كيف الاحوال",
    "ان شاء الله بخير", "إن شاء الله بخير", "ان شالله بخير",
    "اخبارك", "أخبارك", "وش الأخبار", "وش الاخبار", "ايش الاخبار",
    # ── English ──
    "good morning", "good afternoon", "good evening", "good day",
    "hello", "hi there", "hi", "hey there", "hey",
    "how are you", "how are u", "how r u",
)

# Bot name tags & informal vocatives customers add to the salaam.
# Stripping these is mandatory — the residue check would otherwise
# treat "نحلة" itself as substantive content and route every greeting
# to the LLM. We deliberately do NOT strip personal vocatives like
# "يا غالي" / "يا محمد" — those carry no question content, so the
# residue length check below filters them out via the min-character
# threshold instead of needing a token list.
_GREETING_RESIDUE_BOT_TAGS = (
    "يا نحلة", "يا نحله", "نحلة", "نحله",
    "نحلتي", "يا نحلتي",
)


def _strip_greeting_residue(message: str) -> str:
    """Iteratively strip leading greeting / courtesy / bot-tag tokens.

    Returns whatever substantive content is LEFT after one greeting-
    pass over the front of the message. Robust to common punctuation
    (commas, ellipses, exclamation marks, emojis) between tokens.

    Examples
    ────────
    "مساء الخير نحلة كيف حالك بسألك عن العايد وش نشاطهم"
       → "بسألك عن العايد وش نشاطهم"   (non-trivial residue → LLM)

    "السلام عليكم"
       → ""                              (pure greeting → keep INTENT_GREETING)

    "صباح الخير يا نحلة كيف حالك"
       → ""                              (pure greeting → keep INTENT_GREETING)

    "hello"
       → ""

    "hi how are you i need honey"
       → "i need honey"                  (residue → LLM)
    """
    if not message:
        return ""
    text = str(message).strip()
    # Lowercase + drop diacritics + collapse hamza variants so the
    # stripper matches the same surface forms regardless of input
    # orthography. We do NOT normalise the RETURN value — callers
    # need the original characters in case downstream wants to log /
    # re-render the residual.
    norm = re.sub(r"[\u064B-\u0652\u0670\u0640]", "", text)
    norm = (
        norm.replace("أ", "ا")
            .replace("إ", "ا")
            .replace("آ", "ا")
            .replace("ة", "ه")
            .replace("ؤ", "و")
            .replace("ئ", "ي")
            .replace("ى", "ي")
    )
    norm = norm.lower()

    # Same normalisation applied to the token lists so the comparison
    # is apples-to-apples regardless of how the customer typed it.
    def _normalise_token(tok: str) -> str:
        s = re.sub(r"[\u064B-\u0652\u0670\u0640]", "", tok)
        s = (
            s.replace("أ", "ا")
             .replace("إ", "ا")
             .replace("آ", "ا")
             .replace("ة", "ه")
             .replace("ؤ", "و")
             .replace("ئ", "ي")
             .replace("ى", "ي")
        )
        return s.lower()

    # Sort longest-first so "السلام عليكم ورحمة الله" wins over
    # "السلام عليكم" on the same prefix.
    lead_norm = sorted(
        {_normalise_token(t) for t in _GREETING_RESIDUE_LEAD_TOKENS},
        key=len, reverse=True,
    )
    tag_norm = sorted(
        {_normalise_token(t) for t in _GREETING_RESIDUE_BOT_TAGS},
        key=len, reverse=True,
    )

    # Punctuation / whitespace / emoji separator we peel between tokens.
    _SEP_RE = re.compile(r"^[\s,.،؛!?؟…\-—\u2000-\u206F\U0001F300-\U0001FAFF]+")

    def _peel(buf: str) -> Tuple[str, bool]:
        """Try to peel one greeting / vocative token off the front.

        Returns ``(remainder, peeled)`` where ``peeled`` is True if
        anything was removed this round.
        """
        # Leading punctuation / whitespace / emoji
        m = _SEP_RE.match(buf)
        if m:
            buf = buf[m.end():]
            if not buf:
                return buf, True
        for tok in lead_norm:
            if buf.startswith(tok):
                return buf[len(tok):], True
        for tag in tag_norm:
            if buf.startswith(tag):
                return buf[len(tag):], True
        return buf, False

    cur = norm
    # Bounded loop — there are at most a handful of greeting/vocative
    # tokens on the front in real customer messages. 6 iterations is
    # comfortable without risking a runaway on adversarial input.
    for _ in range(6):
        cur, peeled = _peel(cur)
        if not peeled:
            break

    # Drop any trailing leading separators after the last peel.
    m = _SEP_RE.match(cur)
    if m:
        cur = cur[m.end():]

    return cur.strip()


# Minimum residue size (in word characters) that counts as "substantive
# trailing content". Three characters is a fair floor — "هل" alone is
# only 2 chars and is rarely a complete question on its own, but a
# residue like "وش" or "ايش" or "كيف اشتري" is unambiguously a real
# ask. Counted after the stripper drops punctuation, so tiny ack
# tokens ("👋", "🌹", "!") never trigger a demotion.
_GREETING_RESIDUE_MIN_CHARS = 3
_GREETING_RESIDUE_WORD_CHARS_RE = re.compile(r"[\w\u0600-\u06FF]")


def _has_substantive_residue(message: str) -> bool:
    """True when stripping the leading greeting leaves real content.

    Used by ``classify`` to decide whether a customer's salaam was a
    pure greeting (keep INTENT_GREETING) or a mixed turn with an
    embedded open question (demote to INTENT_GENERAL so the LLM sees
    the actual ask). See `_strip_greeting_residue` for the strategy.
    """
    residue = _strip_greeting_residue(message)
    if not residue:
        return False
    n = len(_GREETING_RESIDUE_WORD_CHARS_RE.findall(residue))
    return n >= _GREETING_RESIDUE_MIN_CHARS


# Commerce residue — trailing content that justifies welcome-gate demotion
# from GREETING to an actionable commerce intent. Platform-wide structural
# test (not keyword→reply); mirrors common commerce ask stems already used
# across INTENT_ASK_* / INTENT_START_ORDER patterns.
_COMMERCE_RESIDUE_RE = re.compile(
    r"(?:"
    r"سعر|تكلف|ثمن|بكم|كم\s+سعر|كم\s+ثمن|how\s+much|\bprice\b|\bcost\b|"
    r"طلب|اطلب|"
    r"اشتري|شراء|\border\b|\bbuy\b|\bpurchase\b|"
    r"اب(?:ي|غ(?:ى|y|a)?)\s+اطلب|بغ(?:يت|ى)\s+اطلب|"
    r"اب(?:ي|غ(?:ى|y|a)?)|اريد|اود|ودي|بغيت|بدي|"
    r"خذ\s+لي|حجز|"
    r"منتج|بضاع|سلع|صنف|\bproduct\b|"
    r"عند(?:كم|ك)|لديك(?:م|)?|do\s+you\s+have|"
    r"شحن|توصيل|\bshipping\b|\bdeliver\b|"
    r"حساب|تحويل|"
    r"رابط|"
    r"مقاس|وزن|كم(?:ية)?|"
    r"show\s+me|looking\s+for|add\s+to\s+cart"
    r")",
    re.IGNORECASE | re.UNICODE,
)


def _normalize_residue_text(text: str) -> str:
    norm = re.sub(r"[\u064B-\u0652\u0670\u0640]", "", text)
    return (
        norm.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
        .replace("ؤ", "و")
        .replace("ئ", "ي")
        .replace("ى", "ي")
        .lower()
    )


def _has_commerce_residue(message: str) -> bool:
    """True when residue after greeting strip carries commerce intent."""
    if not message:
        return False
    residue = _strip_greeting_residue(message)
    if not residue:
        return False
    return _COMMERCE_RESIDUE_RE.search(_normalize_residue_text(residue)) is not None


def is_pure_greeting_without_commerce(message: str) -> bool:
    """Pure salaam — no commerce residue and no open-ended ask residue."""
    if not message or not str(message).strip():
        return False
    if _has_commerce_residue(message):
        return False
    if _has_substantive_residue(message):
        return False
    return True
