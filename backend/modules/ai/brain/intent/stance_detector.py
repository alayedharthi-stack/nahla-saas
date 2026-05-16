"""
modules/ai/brain/intent/stance_detector.py
──────────────────────────────────────────
Semantic "relational frame" detector — May 2026 #7.

Production gap this module closes
─────────────────────────────────
Many WhatsApp messages do NOT carry a direct commercial request, yet
they carry a HEAVY relational meaning that the response must honour:

    العميل: "الحبل الأول باقي منه عندي ماخلص لكن المرات الجاية إن شاء الله،
             الله يحفظك ويرزقك حبيبًا"

The legacy stack treats this as ``INTENT_GENERAL`` (no rule fires) →
``ACTION_LLM_REPLY`` with a vague goal ("no rule matched") → the LLM
defaults to a sales-shaped "كيف أقدر أخدمك؟" follow-up. The reply is
*tone-deaf*: the customer just said "I still have honey, no need to
buy now" and the bot pushed for the next sale.

The fix is NOT a new intent (would force a new action), NOT a canned
reply (brittle and obvious), and NOT more regex-rules-per-phrase
(unmaintainable). It is a small SEMANTIC ANNOTATION — one of nine
closed labels — that travels into the LLM's ``response_goal`` so the
model knows the *lens* through which to read the message.

Design
──────
* Pure function. No DB, no HTTP, no logger. Deterministic ⇒
  testable in milliseconds.
* Conservative — returns ``STANCE_UNKNOWN`` for anything ambiguous.
  An UNKNOWN stance produces NO directive in the response goal, so
  the pipeline behaves exactly as before. False negatives are safe;
  false positives would impose the wrong frame and ARE the failure
  mode we guard against.
* Patterns are tuned for Gulf / Standard Arabic and the production
  honey-shop merchants this platform serves first. They never look
  at sentiment in isolation — every stance combines a textual signal
  with EITHER a context anchor (recent turns) OR a hard linguistic
  marker (e.g. ``باقي عندي`` for the "I still have stock from last
  time" deferred pattern).
* No external state. The caller passes ``state_hints`` so we can
  read e.g. ``greeted=True`` (so "السلام عليكم" doesn't get treated
  as a re-greeting) without importing the whole brain module here.

Closed enum — adding a stance means adding it HERE first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Closed enum — exhaustive list of relational frames the brain understands.
# A consumer that branches on stance MUST cover all nine cases (or fall back
# to STANCE_UNKNOWN behaviour for the residue).
# ─────────────────────────────────────────────────────────────────────────────

STANCE_BUYING_NOW       = "buying_now"       # explicit purchase intent — "أبغى أطلب"
STANCE_DEFERRED         = "deferred"         # "later", "still have some", "next time"
STANCE_BROWSING         = "browsing"         # exploring, comparing, no buy verb
STANCE_OBJECTION        = "objection"        # price/quality/trust pushback
STANCE_POLITE_CLOSE     = "polite_close"     # soft farewell, blessing, gratitude as closer
STANCE_SOCIAL_BONDING   = "social_bonding"   # mid-conversation chitchat / blessing
STANCE_INFO_ONLY        = "info_only"        # pure information seek, no commercial pull
STANCE_SUPPORT_REQUEST  = "support_request"  # issue with prior order / delivery
STANCE_UNKNOWN          = "unknown"          # default — produces no override


ALL_STANCES: tuple[str, ...] = (
    STANCE_BUYING_NOW,
    STANCE_DEFERRED,
    STANCE_BROWSING,
    STANCE_OBJECTION,
    STANCE_POLITE_CLOSE,
    STANCE_SOCIAL_BONDING,
    STANCE_INFO_ONLY,
    STANCE_SUPPORT_REQUEST,
    STANCE_UNKNOWN,
)


@dataclass(frozen=True)
class StanceResult:
    """Output of :func:`detect_stance`.

    Attributes:
      stance     — one of the ``STANCE_*`` constants.
      evidence   — short Arabic phrase explaining the trigger. Used
                   by the response_goal so the LLM can SEE what we
                   saw (e.g. "العميل قال «باقي عندي» — يؤجّل بلطف").
      confidence — informal 0.0–1.0 hint for downstream logging.
                   Patterns with hard linguistic markers report
                   ≥0.8; soft / context-only matches report 0.55–0.7;
                   STANCE_UNKNOWN reports 0.0.
    """
    stance: str
    evidence: str = ""
    confidence: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Pattern packs. Each pack is a list of compiled regexes — the first match in
# the priority order below wins. Order is deliberate:
#
#   1. Hard buying signals (would be a regression to mark as anything else).
#   2. Support requests (active issue → must NOT push for new sale).
#   3. Deferred / polite_close (the long-tail "no thanks, but warmly" bucket
#      this module exists to detect).
#   4. Objection (price / trust pushback — the LLM needs a different frame).
#   5. Social bonding / info_only / browsing — softer fallbacks.
#
# Every pattern is anchored with at LEAST one strong lexical marker; bare
# courtesy tokens ("شكرًا" alone) DO NOT trigger polite_close because the
# customer might still be mid-purchase. We require a closing or deferral
# marker to fire.
# ─────────────────────────────────────────────────────────────────────────────

def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    """Compile each pattern with Unicode + IGNORECASE flags once."""
    return tuple(re.compile(p, re.UNICODE | re.IGNORECASE) for p in patterns)


# 1. BUYING NOW — explicit imperative verbs on a product/checkout target.
_BUY_PATTERNS = _compile(
    r"(أبغى|أبي|ابي|ابغى|ودي|أريد|بدي|بغيت)\s+(أطلب|اطلب|أشتري|اشتري|اخذ|آخذ|أحجز|احجز)",
    r"(اطلبه|اطلبها|اشتريه|اشتريها|خذه لي|خذيه لي|سو لي طلب|سوّ لي طلب)",
    r"(تمم الطلب|أكمل الطلب|اكمل الطلب|تأكيد الطلب|كمل|كمّل)",
    r"\b(buy|order|checkout|place order|confirm)\b",
)

# 2. SUPPORT REQUEST — issue with an already-placed order / shipment.
_SUPPORT_PATTERNS = _compile(
    r"(طلبي|طلبيتي|شحنتي|الشحنة|الطلبية)\s*(تأخر|تأخرت|متأخر|متأخرة|ما وصل|ماوصل|لم يصل|لسه ما)",
    r"(ما وصل|ماوصل|لم يصل|لسه ما وصل|لسه ماوصل)\s*(طلبي|شحنتي|الطلب|الطلبية|الشحنة)",
    r"(فيه مشكلة|عندي مشكلة|مشكلة في الطلب|مشكلة في الشحنة|الشحنة غلط|الطلب غلط)",
    r"(مكسور|تالف|مفتوح|ناقص|اختلف|مو نفس|مو زي)",
    r"(ارجاع|إرجاع|استرجاع|استبدال|أرجع الطلب|أرجع المنتج)",
)

# 3a. DEFERRED — "still have some" / "next time" / "not now but later".
# This is the EXACT bucket the production case fell into. We require BOTH
# a "no-need-now" marker AND a future-friendly marker on the same message
# OR one of them alone if extremely unambiguous.
_DEFERRED_HARD = _compile(
    # "Still have some" — strong inventory / leftover signal.
    r"(لسه|لسا|لازال|ما زال|مازال|باقي|باقية|متبقي)\s*(عندي|معي|بالبيت|في البيت|ل?دي)",
    r"(ما خلص|ماخلص|ماخلصت|لم ينتهِ|لم ينتهي|لسا فيه|لسه فيه|باقي منه|باقي منها)",
    # "Next time" — explicit future deferral.
    r"(المرة|المرات|الجولة)\s*(الجاية|القادمة|المقبلة|الثانية)",
    r"(إن شاء الله|ان شاء الله|انشالله)\s*(المرة|المرات)?\s*(الجاية|القادمة|المقبلة|بعدين|لاحق)",
    r"(لاحقًا|لاحقا|بعدين|بعد شوي|بعد فترة|بعد أسبوع|بعد شهر)\s*(إن شاء الله|ان شاء الله|بإذن الله)",
    # "Not now but later" — combined.
    r"(مو الحين|مش الحين|ليس الآن|مو هلا|مش هلق)\s*(بس|لكن|إنما)?\s*(بعدين|لاحقًا|بعد فترة)",
)

# 3b. POLITE CLOSE — soft farewell / blessing as session terminator.
# Distinguished from social_bonding by appearing at message-end alongside
# a closing marker, or as a standalone message after the bot delivered
# what was asked. The vocative suffix ("يا غالي" / "يا الغالي" / "يا
# طيب") is COMMON in Gulf closings — we accept it after the closing
# verb without forcing it.
_VOCATIVE_TAIL = (
    r"(?:\s*يا\s*(?:غالي|الغالي|طيب|الطيب|أخ|الأخ|أخت|الأخت|"
    r"كريم|الكريم|محترم|المحترم|حبيب|الحبيب))?"
)
_POLITE_CLOSE_HARD = _compile(
    # Closing verb (+ optional dative + optional vocative) anchored
    # to message end. Matches "شكرًا" / "شكرًا لك" / "تسلم يا غالي"
    # / "الله يحفظك يا الغالي" — but NOT "شكرًا، عندي سؤال" (no end
    # anchor → falls through).
    r"^(شكرًا|شكرا|الله\s+يعافيك|الله\s+يجزاك|تسلم|تسلمي|تسلموا|"
    r"يعطيك\s+العافية|الله\s+يعطيك\s+العافية)"
    r"\s*(لك|لكم)?" + _VOCATIVE_TAIL + r"\s*[.،!]?\s*$",
    r"^(مع\s+السلامة|في\s+أمان\s+الله|الله\s+معك|الله\s+يحفظك)"
    + _VOCATIVE_TAIL + r"\s*[.،!]?\s*$",
    # Extended thanks with intensifier — still anchored to end.
    r"^(شكرًا|شكرا|تسلم|تسلمي)\s*(جزيلًا|جزيلا|كثير|كثيرًا|كثيراً)?"
    r"\s*(لك|لكم)?" + _VOCATIVE_TAIL + r"\s*[.،!]?\s*$",
    # Combined ack + thanks ("تمام شكرًا" / "أوكي تسلم").
    r"^(تمام|أوكي|اوكي|طيب|ماشي)\s+(شكرًا|شكرا|الله\s+يعافيك|تسلم|تسلمي)"
    + _VOCATIVE_TAIL + r"\s*[.،!]?\s*$",
)

# 4. OBJECTION — price / quality / trust pushback.
# IMPORTANT: bare "غالي" / "غالية" is also Gulf vocative ("يا غالي" =
# "my dear") — VERY common in closing salutations. We require either:
#   * an explicit cost noun in the same message (السعر/المبلغ/...), OR
#   * a "غالي على فلان" prepositional phrase that locks the meaning to
#     "expensive for me / for my budget", OR
#   * a comparative ("أعلى من السوق" / "أكثر من ميزانيتي").
# This protects the polite-close path: "تسلم يا غالي" / "الله يحفظك
# يا الغالي" must NEVER be flagged as an objection.
_OBJECTION_PATTERNS = _compile(
    # Explicit price-noun + expensive modifier in either order.
    r"(السعر|المبلغ|التكلفة|القيمة|الدفع|الفلوس)\s*(غالي|غالية|مرتفع|كثير|كثيرة|عالي|عالية|زايد|زائد)",
    r"(غالي|غالية|مرتفع|كثير|كثيرة|عالي|عالية|زايد|زائد)\s*(السعر|المبلغ|التكلفة|القيمة)",
    # "Expensive ON me / my budget" — locked meaning.
    r"(غالي|غالية|كثير|كثيرة)\s*(جدًا|جداً)?\s*(عل[يى]|عليّ|عليه|عليها|على ميزانيتي|على جيبي|على الجيبة)",
    # Comparatives.
    r"(أعلى|اعلى|أكثر|اكثر|أغلى|اغلى)\s*(من|عن)\s*(السوق|المعتاد|ميزانيتي|الأسعار|الأسواق)",
    # Affordability statements (no ambiguity).
    r"(ما اقدر|ماقدر|مو قادر|مش قادر|صعب|ما يمدّيني|مايمديني)\s*(أدفع|اشتري|أتحمّل|أتحمل|اشتريه|اشتريها)",
    # Competitor comparison.
    r"(عند فلان|عند غيركم|في مكان ثاني|في محل ثاني)\s*(أرخص|أقل|بسعر أقل|أحسن سعر)",
    # Trust pushback.
    r"(ما أثق|ماثق|مو واثق|مش واثق|غير متأكد)\s*(من|في|ب)?\s*(المنتج|الجودة|البائع|المحل|المتجر)",
)

# 5. SOCIAL BONDING — mid-conversation courtesy / religious phrasing.
# Set deliberately CONSERVATIVE: this is the catch-most-noise bucket so
# false-firing here hurts least. Anything matching gets a "honor the
# courtesy, then continue the prior thread naturally" frame.
_SOCIAL_BONDING_PATTERNS = _compile(
    r"(جزاك الله|بارك الله|الله يبارك|الله يرزقك|الله يحفظك|الله يجزيك)",
    r"(صلى الله عليه وسلم|ﷺ|صلوات الله عليه)",
    r"(بسم الله|الحمد لله|ما شاء الله|ماشاء الله|سبحان الله)",
    r"(كفو|ما قصرت|ماقصرت|الله يعطيكم العافية)",
)

# 6. INFO ONLY — question marker without a buy verb. Catches "كم يوم
# للتوصيل؟" / "عندكم شحن لجدة؟" type asks.
_INFO_ONLY_PATTERNS = _compile(
    r"^\s*(كم|متى|أين|وين|كيف|هل|ايش|إيش|وش|ماذا|ما)\s+[^؟?]*[؟?]\s*$",
)

# 7. BROWSING — soft exploration verbs. Lowest priority — only fires when
# nothing stronger matched.
_BROWSING_PATTERNS = _compile(
    r"(عندكم إيه|عندكم ايش|ايش عندكم|ايش متوفر|إيش متوفر|وش متوفر)",
    r"(أبي أتفرّج|أبغى أتفرج|ودي أتفرج|أتفرّج فقط|أتفرج بس)",
    r"(أشوف الخيارات|أشوف الأنواع|أشوف المتوفر)",
)


# Map stance → human-readable Arabic evidence template. Caller substitutes
# the matched trigger via .format(trigger=...).
_EVIDENCE_TEMPLATES: Dict[str, str] = {
    STANCE_BUYING_NOW:       "العميل أبدى نية شراء صريحة («{trigger}»).",
    STANCE_SUPPORT_REQUEST:  "العميل لديه مشكلة/استفسار عن طلب سابق («{trigger}»).",
    STANCE_DEFERRED:         "العميل يؤجّل الشراء بلطف («{trigger}») — لا يزال عنده مخزون أو يخطّط للمرة القادمة.",
    STANCE_POLITE_CLOSE:     "العميل يُغلق المحادثة بلطف («{trigger}»).",
    STANCE_OBJECTION:        "العميل يبدي اعتراضًا (سعر/جودة/ثقة): «{trigger}».",
    STANCE_SOCIAL_BONDING:   "العميل يُرسل عبارة ودّية/دينية («{trigger}») — تعزيز للعلاقة، ليس طلبًا.",
    STANCE_INFO_ONLY:        "العميل يطلب معلومة فقط («{trigger}») — لا توجد إشارة شراء.",
    STANCE_BROWSING:         "العميل يستكشف المتجر دون نية شراء فورية («{trigger}»).",
    STANCE_UNKNOWN:          "",
}


# Stance → directive line embedded in response_goal. Closed table; consumed
# by pipeline._compose_response_goal. Each directive teaches the LLM HOW to
# read the customer's stance without prescribing exact words.
STANCE_DIRECTIVES: Dict[str, str] = {
    STANCE_BUYING_NOW: (
        "relational_frame=buying_now — العميل يريد الشراء الآن. أكمل خطوات الطلب "
        "مباشرة بدون أسئلة ترحيبية أو تأخير."
    ),
    STANCE_DEFERRED: (
        "relational_frame=deferred — العميل يؤجّل الشراء بهدوء (مازال عنده مخزون "
        "أو ينوي المرة القادمة). ممنوع أي pitch بيعي أو سؤال «تحب أرشّح لك؟» — "
        "اعترفي بكلامه، اشكري الثقة، اتركي باب الرجوع مفتوحًا بسطر واحد فقط "
        "بدون CTA إلزامي."
    ),
    STANCE_POLITE_CLOSE: (
        "relational_frame=polite_close — العميل يُغلق المحادثة بلطف. ردّي بدعوة "
        "مقابلة قصيرة («وإياك / الله يحفظك / تحت أمرك أي وقت») ولا تطرحي سؤال "
        "متابعة بيعي."
    ),
    STANCE_OBJECTION: (
        "relational_frame=objection — العميل لديه اعتراض (سعر/جودة/ثقة). تجاوبي "
        "بصدق وثقة دون دفاعية، اذكري قيمة المنتج باختصار، واتركي قرار الشراء له. "
        "ممنوع تقديم خصم أو كوبون من تلقاء نفسك."
    ),
    STANCE_SOCIAL_BONDING: (
        "relational_frame=social_bonding — رسالة دافئة/دينية لتعزيز العلاقة. ردّي "
        "بمقابل دافئ مختصر («وإياك / آمين / حياك الله») ثم استكملي خيط الحوار "
        "الموضوعي السابق إن وُجد — لا تُحوّليها إلى بداية محادثة جديدة."
    ),
    STANCE_INFO_ONLY: (
        "relational_frame=info_only — العميل يطلب معلومة محددة. أجيبي مباشرة "
        "وباختصار، بدون pitch بيعي إضافي ولا اقتراح منتجات لم يطلبها."
    ),
    STANCE_BROWSING: (
        "relational_frame=browsing — العميل يستكشف. اعرضي خيارًا واحدًا أو سؤالًا "
        "موجّهًا واحدًا (نوع/استخدام/ميزانية) بدون قائمة طويلة."
    ),
    STANCE_SUPPORT_REQUEST: (
        "relational_frame=support_request — العميل لديه مشكلة بطلب/شحنة سابقة. "
        "أكدي استلام الرسالة، اسألي عن رقم الطلب لو غير معروف، وامتنعي عن أي "
        "اقتراح بيعي حتى تُحلّ المشكلة."
    ),
    STANCE_UNKNOWN: "",
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_stance(
    message: str,
    *,
    recent_customer_messages: Optional[List[str]] = None,
    state_hints: Optional[Dict[str, object]] = None,
) -> StanceResult:
    """Classify the customer's relational frame for THIS turn.

    Parameters
    ----------
    message
        The raw inbound text (already normalised by the pipeline —
        we still defensively strip / lowercase as a cheap safeguard).
    recent_customer_messages
        Optional list of the customer's last 2–3 messages, NEWEST
        LAST. Used to disambiguate "تمام" / "أوكي" / standalone
        blessings — these only count as ``polite_close`` when they
        follow a bot turn that delivered the asked content.
        Defaults to empty when omitted.
    state_hints
        Optional dict of light flags from ``MerchantConversationState``
        the caller wants the detector to consider. Currently honoured
        keys:
          * ``greeted`` (bool)            — soft, currently unused
          * ``has_focus_product`` (bool)  — soft, currently unused
        Future stances may consume more; the dict shape stays open.

    Returns
    -------
    StanceResult
        ``stance`` is one of the ``STANCE_*`` constants. When no
        pattern fires, returns ``STANCE_UNKNOWN`` with empty
        evidence — the caller MUST treat this as "no override".

    Guarantees
    ----------
    * Pure. No logging, no I/O.
    * Never raises (treats ``None`` inputs as empty).
    * Patterns are tested individually — see
      ``backend/tests/test_semantic_stance.py``.
    """
    text = (message or "").strip()
    if not text:
        return StanceResult(STANCE_UNKNOWN, "", 0.0)

    # Defensive — never trust the caller to have lowered already.
    norm = " ".join(text.split())

    # Priority cascade. Each helper returns the trigger string when it
    # matched, otherwise ``""``. We construct the StanceResult only on
    # the first hit so subsequent patterns can't accidentally win.

    if trigger := _first_match(norm, _BUY_PATTERNS):
        return _build(STANCE_BUYING_NOW, trigger, 0.92)

    if trigger := _first_match(norm, _SUPPORT_PATTERNS):
        return _build(STANCE_SUPPORT_REQUEST, trigger, 0.88)

    if trigger := _first_match(norm, _DEFERRED_HARD):
        return _build(STANCE_DEFERRED, trigger, 0.85)

    if trigger := _first_match(norm, _POLITE_CLOSE_HARD):
        return _build(STANCE_POLITE_CLOSE, trigger, 0.82)

    if trigger := _first_match(norm, _OBJECTION_PATTERNS):
        return _build(STANCE_OBJECTION, trigger, 0.80)

    if trigger := _first_match(norm, _SOCIAL_BONDING_PATTERNS):
        # Social bonding alone is a SOFT signal — only adopt it if the
        # customer's message is short enough that the courtesy IS the
        # message (long messages usually carry a commercial intent
        # alongside the blessing and are handled by other layers).
        if len(norm.split()) <= 8:
            return _build(STANCE_SOCIAL_BONDING, trigger, 0.72)

    # Order matters: BROWSING patterns are more specific ("ايش عندكم؟"
    # / "وش متوفر؟") than the generic question-marker INFO_ONLY pattern,
    # which would otherwise hijack browsing asks. Keeping browsing
    # FIRST also matches operator intuition — those phrasings ARE
    # exploratory commercial intent, not pure information seeks.
    if trigger := _first_match(norm, _BROWSING_PATTERNS):
        return _build(STANCE_BROWSING, trigger, 0.65)

    if trigger := _first_match(norm, _INFO_ONLY_PATTERNS):
        return _build(STANCE_INFO_ONLY, trigger, 0.70)

    # Soft context-rescue: a bare "شكرًا" / "تمام" / "أوكي" on its own
    # AFTER the bot delivered content (the recent_customer_messages
    # carry the customer's prior turn — if the LAST bot message
    # already addressed a request, the bare ack is a polite_close).
    # We approximate "bot delivered content" by checking that recent
    # customer messages are sparse (i.e. the bot has been carrying the
    # last few turns). This is intentionally soft — confidence stays
    # low so the directive is mild.
    recent = recent_customer_messages or []
    if (
        len(norm.split()) <= 3
        and re.fullmatch(
            r"^(شكرًا|شكرا|تسلم|تمام|أوكي|اوكي|طيب|الله يعافيك|تسلمي|تسلموا)\s*[.!]?\s*$",
            norm,
            re.UNICODE | re.IGNORECASE,
        )
        and len(recent) <= 1
    ):
        return _build(STANCE_POLITE_CLOSE, norm, 0.55)

    return StanceResult(STANCE_UNKNOWN, "", 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _first_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    """Return the matched substring of the FIRST pattern that hits."""
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(0).strip()
    return ""


def _build(stance: str, trigger: str, confidence: float) -> StanceResult:
    """Build a ``StanceResult`` with templated evidence text."""
    template = _EVIDENCE_TEMPLATES.get(stance, "")
    evidence = template.format(trigger=(trigger or "")[:60]) if template else ""
    return StanceResult(stance, evidence, confidence)


__all__ = [
    "STANCE_BUYING_NOW",
    "STANCE_DEFERRED",
    "STANCE_BROWSING",
    "STANCE_OBJECTION",
    "STANCE_POLITE_CLOSE",
    "STANCE_SOCIAL_BONDING",
    "STANCE_INFO_ONLY",
    "STANCE_SUPPORT_REQUEST",
    "STANCE_UNKNOWN",
    "ALL_STANCES",
    "STANCE_DIRECTIVES",
    "StanceResult",
    "detect_stance",
]
