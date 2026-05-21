"""
tests/test_intent_social_platform.py
────────────────────────────────────
Regression tests for the May 2026 #4 architectural fix:
    * INTENT_SOCIAL  — courtesy / religious / thanks
    * INTENT_PLATFORM_INQUIRY — questions about NAHLA (the SaaS),
      not the merchant's products.

Why these tests exist
─────────────────────
Two real merchant-reported bugs drove this work:

1. Customers sending social phrases like "جزاك الله خير", "بيض الله
   وجهك", "اللهم صل وسلم على نبينا محمد" were either getting
   silence or being derailed into product pitches. The brain's
   sales-oriented prompt was misinterpreting social ACKs as
   "the customer is here, push a product".

2. A customer's voice note about "الذكاء، الاشتراك، الباقات، الربط"
   (asking about NAHLA the platform) was parsed as a product order
   because INTENT_ASK_PRODUCT's broad "أبغى/أريد" regex caught it.

The fix lives in:
    * intent/social_classifier.py
    * intent/platform_classifier.py
    * intent/rules.py        (wired both before commerce intents)
    * decision/engine.py     (early branches for both intents)
    * compose/responder.py   (canned-template dispatch)
    * compose/templates.py   (short, on-brand replies by category)

These tests lock in:
    * Each social phrase classifies as the right category.
    * Each platform phrase classifies as the right topic.
    * Real product / order / price / payment phrases are NOT
      misclassified (no regression).
    * Long mixed messages with both social AND commercial intent
      stay routed to the brain (the commercial half is honoured).

Run:
    cd backend
    python -m pytest tests/test_intent_social_platform.py -v
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from modules.ai.brain.intent.social_classifier import (
    SOCIAL_BASMALA,
    SOCIAL_BLESSING,
    SOCIAL_COMPLIMENT,
    SOCIAL_GENERAL_COURTESY,
    SOCIAL_PROPHET_INVOCATION,
    SOCIAL_THANKS,
    classify_social,
)
from modules.ai.brain.intent.platform_classifier import (
    PLATFORM_AI_CAPABILITIES,
    PLATFORM_API,
    PLATFORM_CAMPAIGNS,
    PLATFORM_DASHBOARD,
    PLATFORM_GENERAL,
    PLATFORM_INTEGRATION,
    PLATFORM_META_CONNECTION,
    PLATFORM_SUBSCRIPTION,
    classify_platform,
)
from modules.ai.brain.intent import rules as intent_rules
from modules.ai.brain.types import (
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_GREETING,
    INTENT_PLATFORM_INQUIRY,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Social classifier — category-level
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "جزاك الله خير",
    "جزاك الله خيراً",
    "الله يجزاك خير",
    "ربي يجزاك خير",
    "شكراً",
    "شكرا",
    "مشكور",
    "مشكورة",
    "تسلم",
    "تسلمي",
    "يسلمو",
    "thanks",
    "thank you",
    "ty",
])
def test_social_thanks(message: str) -> None:
    m = classify_social(message)
    assert m is not None, f"expected SOCIAL match for: {message!r}"
    assert m.category == SOCIAL_THANKS, f"expected thanks, got {m.category} for {message!r}"
    assert m.confidence >= 0.92


@pytest.mark.parametrize("message", [
    "يعطيك العافية",
    "يعطيكم العافية",
    "الله يعافيك",
    "الله يبارك فيك",
    "الله يبارك لك",
    "الله يطول عمرك",
    # NOTE: "بيض الله وجهك" was here historically but now routes to
    # SOCIAL_STRONG_PRAISE (May 2026 #8) so the reciprocal heavy reply
    # is reserved for explicit praise. The dedicated coverage now
    # lives in test_strong_praise_phrasing.py.
    "رحم الله والديك",
    "الله يحفظك",
    "الله يوفقك",
    "الله يسعدك",
])
def test_social_blessing(message: str) -> None:
    m = classify_social(message)
    assert m is not None, f"expected SOCIAL match for: {message!r}"
    assert m.category == SOCIAL_BLESSING


@pytest.mark.parametrize("message", [
    "اللهم صل وسلم على نبينا محمد",
    "صلى الله عليه وسلم",
    "اللهم صل وبارك وسلم على نبينا",
    "إن الله وملائكته يصلون على النبي يا أيها الذين آمنوا صلوا عليه وسلموا تسليما",
    "يا أيها الذين آمنوا صلوا عليه وسلموا تسليما",
])
def test_social_prophet_invocation(message: str) -> None:
    m = classify_social(message)
    assert m is not None, f"expected SOCIAL match for: {message!r}"
    assert m.category == SOCIAL_PROPHET_INVOCATION


@pytest.mark.parametrize("message", [
    "بسم الله",
    "بسم الله الرحمن الرحيم",
])
def test_social_basmala(message: str) -> None:
    m = classify_social(message)
    assert m is not None, f"expected SOCIAL match for: {message!r}"
    assert m.category == SOCIAL_BASMALA


@pytest.mark.parametrize("message", [
    # NOTE: "كفو" / "ما قصرت" / "ماقصرت" used to be in this bucket but
    # were promoted to SOCIAL_STRONG_PRAISE in May 2026 #8 so the
    # template pool routes them to the reciprocal heavy reply. See
    # test_strong_praise_phrasing.py for their coverage.
    #
    # May 2026 #14 — REMOVED "ممتاز" from this bucket. The bare
    # adjective is too ambiguous: customers use it both as a
    # standalone compliment and inside product questions ("هل ممتاز
    # للأطفال؟"). We trust the brain pipeline to read it in context
    # now — see `test_ambiguous_adjective_yields_to_brain` below.
    "والنعم",
    "أحسنت",
    "ما شاء الله",
    "تبارك الله",
])
def test_social_compliment(message: str) -> None:
    m = classify_social(message)
    assert m is not None, f"expected SOCIAL match for: {message!r}"
    assert m.category == SOCIAL_COMPLIMENT


@pytest.mark.parametrize("message", [
    "حياك",
    "حياكم الله",
    "هلا والله",
    "بالعفو",
    "خير إن شاء الله",
    "لا يهمك",
])
def test_social_general_courtesy(message: str) -> None:
    m = classify_social(message)
    assert m is not None, f"expected SOCIAL match for: {message!r}"
    assert m.category == SOCIAL_GENERAL_COURTESY


# ─────────────────────────────────────────────────────────────────────────────
# 2. Social classifier — must NOT fire on commercial messages
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "أبغى عسل سدر",
    "أريد كيلو ضهيان",
    "كم سعر الطلح؟",
    "ابحث عن قرص العسل",
    "وين الرابط؟",
    "أبي أطلب",
    "أرسل لي الباركود",
    # Long mixed message — has thanks but also a real ask. The brain
    # must see this, not the canned social reply.
    "شكراً لك، أبغى عسل سدر للشحن للرياض",
    "تسلم، كم سعر كيلو ضهيان؟",
])
def test_social_does_not_fire_on_commercial(message: str) -> None:
    m = classify_social(message)
    assert m is None, f"expected NO social classification for commercial: {message!r}, got {m}"


# ─────────────────────────────────────────────────────────────────────────────
# 2b. Practical-question disqualifier (May 2026 #11)
# ─────────────────────────────────────────────────────────────────────────────
# Merchant report: "الله يسعدك في طريقة للاستعمال" → bot replied
# "الله يعافيك ويسعدك 🌷 أي وقت" and dropped the real question.
# The fix: when a courtesy phrase is paired with a substantive
# how-to / dosage / suitability ask (or any "؟" / "?"), the social
# classifier must yield to the brain pipeline so the LLM answers
# the practical question. We do NOT want a canned response here.

@pytest.mark.parametrize("message", [
    # The exact merchant reproducer.
    "الله يسعدك في طريقة للاستعمال",
    "الله يسعدك في طريقة الاستخدام",
    # Bare practical questions — should not classify as social.
    "كيف الاستخدام؟",
    "وش طريقة الاستعمال؟",
    "وش الجرعة المناسبة",
    "كم جرعة في اليوم",
    "كم حبة في اليوم؟",
    "كيف استعمله",
    "كيف استخدمه",
    "كيف اشربه",
    "كيف اخذه",
    "متى اشربه",
    "هل ينفع للأطفال",
    "هل يصلح للحامل",
    "ينفع لمشاكل البطن؟",
    # Courtesy + question mark alone — the question mark always wins.
    "تسلم، عندك توصيل بكرة؟",
    "الله يعافيك، متى يصير عندك ضهيان؟",
])
def test_social_yields_on_practical_question(message: str) -> None:
    """Mixed turn: courtesy + how-to / dosage / "?" must NOT classify
    as social. The brain pipeline answers the practical ask."""
    m = classify_social(message)
    assert m is None, (
        f"expected social classifier to YIELD on practical question, "
        f"got {m!r} for message {message!r}"
    )


@pytest.mark.parametrize("message,expected_category", [
    # Pure social phrases must STILL classify — the new disqualifier
    # is conservative: bare "كيف" without a practical anchor doesn't
    # disqualify (we never want "كيف الحال" routed to LLM as a fake
    # how-to question).
    ("الله يسعدك",        SOCIAL_BLESSING),
    ("جزاك الله خير",     SOCIAL_THANKS),
    ("الله يعافيك",       SOCIAL_BLESSING),
    ("شكراً جزيلاً",      SOCIAL_THANKS),
    ("تسلم يا غالي",      SOCIAL_THANKS),
    ("الله يبارك فيك",    SOCIAL_BLESSING),
])
def test_pure_social_still_classifies_after_disqualifier_added(
    message: str, expected_category: str,
) -> None:
    """Belt-and-suspenders: the new practical-question disqualifier
    must NOT shrink the social-classifier's coverage on genuinely
    social messages."""
    m = classify_social(message)
    assert m is not None, f"expected social match for: {message!r}"
    assert m.category == expected_category


# ─────────────────────────────────────────────────────────────────────────────
# May 2026 #14 — Ambiguous descriptive adjectives must go to the brain
# ─────────────────────────────────────────────────────────────────────────────
# Philosophy: classifier short-circuits ONLY on phrases that are
# unambiguously aimed at the merchant. Adjectives like "ممتاز" /
# "زين" / "حلو" doubled as compliments AND as product descriptors,
# and were leaking real product questions into a canned compliment
# reply. Rather than grow an ever-longer "هل + ADJ" disqualifier
# list (which would make the bot more rigid, not smarter), we trust
# the brain pipeline to read these adjectives in context — it has
# the customer profile, product knowledge, and conversational state
# the classifier deliberately doesn't.
@pytest.mark.parametrize("message", [
    # The exact merchant reproducer.
    "هو هل ممتاز لمشاكل البطن والجهاز الهضمي",
    # Sibling shapes that previously canned-replied on "ممتاز".
    "هل ممتاز للأطفال",
    "هل هو ممتاز لمشاكل القولون",
    "العسل ممتاز للقولون؟",
    # Other ambiguous adjectives that left the list — same logic.
    "هل طعمه حلو",
    "هل زين للحامل",
    # Bare adjectives — also yield. The brain composes a short
    # contextual reply (a blessing for a real compliment, a clarifier
    # for an ambiguous one). The classifier no longer pre-judges.
    "ممتاز",
    "حلو",
    "زين",
])
def test_ambiguous_adjective_yields_to_brain(message: str) -> None:
    """Customers use ``ممتاز`` / ``زين`` / ``حلو`` both as a
    standalone compliment AND inside product questions. The
    classifier no longer short-circuits on them; the brain pipeline
    decides what kind of turn this is from context."""
    m = classify_social(message)
    assert m is None, (
        f"expected classifier to YIELD on ambiguous adjective so the "
        f"brain can interpret context, got {m!r} for {message!r}"
    )


def test_disqualifier_helper_is_exposed_for_diagnostics():
    """The helper is module-private but importable. Future regression
    diagnostics (e.g. a brain-pipeline log) may want to call it
    directly to explain why a turn was routed away from
    ACTION_SOCIAL_REPLY."""
    from modules.ai.brain.intent.social_classifier import (
        _has_practical_question_signal,
        _norm,
    )
    assert _has_practical_question_signal(_norm("الله يسعدك في طريقة للاستعمال"))
    assert _has_practical_question_signal(_norm("كم جرعة في اليوم"))
    assert _has_practical_question_signal(_norm("شكراً، متى يصير؟"))
    # Pure social must NOT trigger the helper.
    assert not _has_practical_question_signal(_norm("الله يسعدك"))
    assert not _has_practical_question_signal(_norm("جزاك الله خير"))
    assert not _has_practical_question_signal(_norm("كيف الحال"))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Platform classifier — topic-level
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_topic", [
    ("كم اشتراك نحلة؟",                  PLATFORM_SUBSCRIPTION),
    ("وش باقات نحلة؟",                    PLATFORM_SUBSCRIPTION),
    ("ودي أعرف خطط نحلة",                 PLATFORM_SUBSCRIPTION),
    ("هل في تجربة مجانية؟",                PLATFORM_SUBSCRIPTION),
    ("كيف أربط واتساب الأعمال؟",          PLATFORM_INTEGRATION),
    ("ربط الواتساب مع المنصة",            PLATFORM_INTEGRATION),
    ("waba",                              PLATFORM_INTEGRATION),
    ("WABA",                              PLATFORM_INTEGRATION),
    ("360dialog كيف يشتغل؟",             PLATFORM_INTEGRATION),
    ("عندكم API؟",                        PLATFORM_API),
    ("API documentation",                 PLATFORM_API),
    ("webhook callback",                  PLATFORM_API),
    ("Embedded Signup",                   PLATFORM_META_CONNECTION),
    ("كيف أربط مع ميتا؟",                 PLATFORM_META_CONNECTION),
    ("لوحة التحكم",                       PLATFORM_DASHBOARD),
    ("dashboard",                         PLATFORM_DASHBOARD),
    ("منصة نحلة",                         PLATFORM_GENERAL),
])
def test_platform_topics(message: str, expected_topic: str) -> None:
    m = classify_platform(message)
    assert m is not None, f"expected PLATFORM match for: {message!r}"
    assert m.topic == expected_topic, (
        f"expected {expected_topic}, got {m.topic} for {message!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Platform classifier — weak-token co-occurrence
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "ودي أعرف الباقات والاشتراك في نحلة",
    "الذكاء الاصطناعي في نحلة كيف يشتغل؟",
    "كيف الحملات في منصة نحلة؟",
    "ربط نحلة بالواتساب",
])
def test_platform_weak_cooccurrence(message: str) -> None:
    m = classify_platform(message)
    assert m is not None, f"expected PLATFORM match (weak cooccur) for: {message!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Platform classifier — must NOT fire on honey/product context
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "عندكم عسل نحلة سدر؟",                 # "نحلة" alone + honey context
    "كم سعر كيلو عسل السدر؟",
    "أبغى نصف كيلو من الطلح",
    "وين الرابط للطلب؟",
    "وصلتني الطلبية، شكراً",
    "كيف أطلب من المتجر؟",                 # "المتجر" is merchant store, not platform
    "شموع العسل عندكم؟",
])
def test_platform_does_not_fire_on_honey_context(message: str) -> None:
    m = classify_platform(message)
    assert m is None, f"expected NO platform classification for: {message!r}, got {m}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Full pipeline — rules.match() routes correctly
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_intent", [
    # Social
    ("جزاك الله خير",                     INTENT_SOCIAL),
    ("الله يعافيك",                        INTENT_SOCIAL),
    ("صلى الله عليه وسلم",                 INTENT_SOCIAL),
    ("بسم الله",                           INTENT_SOCIAL),
    ("كفو",                                 INTENT_SOCIAL),
    # Platform
    ("كم اشتراك نحلة؟",                    INTENT_PLATFORM_INQUIRY),
    ("API",                                INTENT_PLATFORM_INQUIRY),
    ("لوحة التحكم",                       INTENT_PLATFORM_INQUIRY),
    ("Embedded Signup",                    INTENT_PLATFORM_INQUIRY),
    # Greeting (must still classify as greeting, NOT social)
    ("السلام عليكم",                       INTENT_GREETING),
    ("مرحبا",                              INTENT_GREETING),
    ("صباح الخير",                         INTENT_GREETING),
    # Commerce (must still classify correctly)
    ("أبغى عسل سدر",                       INTENT_ASK_PRODUCT),
    ("كم سعر كيلو ضهيان؟",                 INTENT_ASK_PRICE),
    ("أطلب الآن",                           INTENT_START_ORDER),
])
def test_rules_match_routing(message: str, expected_intent: str) -> None:
    result = intent_rules.match(message)
    assert result is not None, f"rules.match returned None for: {message!r}"
    assert result.name == expected_intent, (
        f"expected {expected_intent}, got {result.name} (conf={result.confidence}) for {message!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Confidence ordering — social and platform beat commerce
# ─────────────────────────────────────────────────────────────────────────────

def test_social_confidence_beats_commerce() -> None:
    """Social must outrank ASK_PRODUCT / ASK_PRICE / START_ORDER."""
    result = intent_rules.match("شكراً جزيلا")
    # "شكراً جزيلا" has no commercial verb so social fires.
    assert result.name == INTENT_SOCIAL
    assert result.confidence >= 0.92


def test_platform_confidence_beats_commerce() -> None:
    """Platform must outrank generic ASK_PRODUCT regex on 'كيف أربط'."""
    result = intent_rules.match("كيف أربط واتساب الأعمال مع نحلة؟")
    assert result.name == INTENT_PLATFORM_INQUIRY
    assert result.confidence >= 0.92


# ─────────────────────────────────────────────────────────────────────────────
# 8. Template integration — categories render to expected text shapes
# ─────────────────────────────────────────────────────────────────────────────

def test_social_template_renders_for_every_category() -> None:
    from modules.ai.brain.compose import templates as T
    for cat in (
        SOCIAL_THANKS,
        SOCIAL_BLESSING,
        SOCIAL_PROPHET_INVOCATION,
        SOCIAL_BASMALA,
        SOCIAL_COMPLIMENT,
        SOCIAL_GENERAL_COURTESY,
    ):
        text = T.social_reply(category=cat, variant=0, sub_variant=2)
        assert text, f"empty social_reply for category {cat}"
        assert len(text) <= 220, f"social_reply too long for {cat}: {text!r}"
        assert text.count("\n") <= 2, f"social_reply too many lines for {cat}: {text!r}"


def test_platform_kb_excerpt_subscription() -> None:
    from modules.ai.brain.knowledge_platform_slice import (
        PLATFORM_SUBSCRIPTION,
        extract_platform_kb_excerpt,
    )

    kb = (
        "### العسل السدر الكيلو 400 ريال\n"
        "عسل نقي ومجرّب.\n\n"
        "### الاشتراك في نحلة\n"
        "لباقات نحلة: تواصل مع support@nahla.example — الأساسية شهرية "
        "ويمكنك التجربة قبل الدفع من لوحة التحكم.\n\n"
        "### الربط مع واتساب الأعمال\n"
        "افتح لوحة التحكم → الواتساب → واتحدث خطوات الربط."
    )
    ex = extract_platform_kb_excerpt(
        kb,
        PLATFORM_SUBSCRIPTION,
        "وش خطط الأشتراك؟",
        min_score=2.0,
    )
    assert "support@nahla.example" in ex
    assert "السدر" not in ex  # catalogue paragraph should lose to subscription chunk


def test_platform_kb_empty_when_no_signals() -> None:
    from modules.ai.brain.knowledge_platform_slice import (
        PLATFORM_INTEGRATION,
        extract_platform_kb_excerpt,
    )

    kb = "كل شي عن العسل الطبيعي والشحن خلال مدينة الرياض."
    assert extract_platform_kb_excerpt(kb, PLATFORM_INTEGRATION, "") == ""


def test_platform_template_renders_for_every_topic() -> None:
    from modules.ai.brain.compose import templates as T
    for topic in (
        PLATFORM_SUBSCRIPTION,
        PLATFORM_INTEGRATION,
        PLATFORM_API,
        PLATFORM_AI_CAPABILITIES,
        PLATFORM_CAMPAIGNS,
        PLATFORM_DASHBOARD,
        PLATFORM_META_CONNECTION,
        PLATFORM_GENERAL,
    ):
        text = T.platform_reply(topic=topic, variant=0)
        assert text, f"empty platform_reply for topic {topic}"
        # Must NOT contain product / catalog hooks — the whole point is
        # to NOT redirect to merchandise.
        assert "كم سعر" not in text, f"platform_reply leaks price ask: {text!r}"
        assert "أرشّح" not in text, f"platform_reply leaks recommendation funnel: {text!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 9. First-contact welcome gate
# ─────────────────────────────────────────────────────────────────────────────
#
# Customers regularly open a conversation with a salaam plus an actionable
# question in the same breath ("السلام عليكم أبي سعر العسل"). The legacy
# behaviour returned a long welcome card and ignored the question. The fix
# routes the salaam → actionable secondary intent with
# ``embedded_greeting=True`` so the composer can prepend a tiny salaam and
# answer the real question. These tests lock the routing.

@pytest.mark.parametrize("message,expected_intent", [
    ("السلام عليكم أبي سعر العسل",                INTENT_ASK_PRICE),
    ("وعليكم السلام، كم سعر كيلو ضهيان؟",         INTENT_ASK_PRICE),
    ("السلام عليكم أبغى عسل سدر",                 INTENT_ASK_PRODUCT),
    (
        "وعليكم السلام مساء الخيرات، أبي تفاصيل عن العسل الصح وكم سعره",
        INTENT_ASK_PRICE,
    ),
    ("السلام عليكم أبي أربط واتساب الأعمال",       INTENT_PLATFORM_INQUIRY),
    ("مرحبا، كم اشتراك نحلة؟",                     INTENT_PLATFORM_INQUIRY),
])
def test_welcome_gate_routes_to_actionable(message: str, expected_intent: str) -> None:
    result = intent_rules.match(message)
    assert result is not None, f"rules.match returned None for: {message!r}"
    assert result.name == expected_intent, (
        f"expected {expected_intent}, got {result.name} (conf={result.confidence}) for {message!r}"
    )
    assert (result.slots or {}).get("embedded_greeting") is True, (
        f"expected embedded_greeting=True slot on {message!r}, got slots={result.slots}"
    )


@pytest.mark.parametrize("message", [
    "السلام عليكم",
    "مرحبا",
    "صباح الخير",
    "هلا والله",
    "أهلاً وسهلاً",
])
def test_welcome_gate_keeps_pure_greeting(message: str) -> None:
    """Plain greetings (no embedded ask) must NOT be demoted."""
    result = intent_rules.match(message)
    assert result is not None
    assert result.name == INTENT_GREETING
    assert (result.slots or {}).get("embedded_greeting") is None


def test_welcome_gate_prepend_helper() -> None:
    """Pipeline helper prepends a salaam line and skips when one already exists."""
    from modules.ai.brain.pipeline import _prepend_first_contact_salaam

    class _Ctx:
        message = "السلام عليكم أبي سعر العسل"

    out = _prepend_first_contact_salaam("الكيلو ٣٥٠ ريال.", _Ctx())
    assert "\n" in out, f"prefix must be a separate line, got: {out!r}"
    head = out.splitlines()[0]
    assert any(tok in head for tok in ("وعليكم السلام", "أهلاً", "أهلا", "هلا"))

    # If the reply already opens with salaam, don't double up.
    already = "وعليكم السلام، الكيلو ٣٥٠ ريال."
    assert _prepend_first_contact_salaam(already, _Ctx()) == already


def test_welcome_gate_embedded_slot_present_for_platform() -> None:
    result = intent_rules.match("السلام عليكم أبي أربط واتساب الأعمال")
    assert result is not None
    assert result.name == INTENT_PLATFORM_INQUIRY
    assert (result.slots or {}).get("platform_topic")
    assert (result.slots or {}).get("embedded_greeting") is True


# ─────────────────────────────────────────────────────────────────────────────
# 10. Conversation-context inheritance (May 2026 — UX follow-up)
# ─────────────────────────────────────────────────────────────────────────────
#
# A real merchant reported: "السلام عليكم أبي سعر العسل" produced no
# outbound at all in production. The fix has two halves:
#
#   * intent.match() routes the message to ASK_PRICE with
#     ``embedded_greeting=True`` — verified above by
#     ``test_welcome_gate_routes_to_actionable``.
#   * the decision engine's branch 0z resolves a bare follow-up
#     "نعم" / "أرسل" against ``state.last_platform_topic`` so the
#     conversation feels continuous instead of restart-from-zero.
#
# These tests lock the second half: the engine must inherit the
# previous platform topic when the customer only sends a short
# confirmation.

def _build_decision_inputs(
    *,
    message: str,
    last_platform_topic: str = "",
    current_product_focus=None,
    has_products: bool = True,
):
    """Build the minimal BrainContext + Decision-engine inputs.

    Pulled out as a helper because constructing the full BrainContext
    pulls a chunk of brain machinery; tests should stay short and
    declarative.
    """
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_GENERAL,
    )

    state = MerchantConversationState()
    state.last_platform_topic = last_platform_topic
    state.current_product_focus = current_product_focus
    facts = CommerceFacts(
        has_products=has_products,
        product_count=3 if has_products else 0,
        orderable=has_products,
        store_name="متجر الاختبار",
    )
    intent = Intent(
        name=INTENT_GENERAL,
        confidence=0.5,
        slots={},
        raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=message,
        intent=intent,
        state=state,
        facts=facts,
    )
    return ctx, state, intent, facts


def test_context_inherit_bare_yes_after_platform_topic() -> None:
    """`state.last_platform_topic` set + customer says "نعم" → re-emit
    ACTION_PLATFORM_REPLY for that topic, not a generic greet."""
    from modules.ai.brain.decision.actions import ACTION_PLATFORM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    ctx, _state, _intent, _facts = _build_decision_inputs(
        message="نعم",
        last_platform_topic="meta_connection",
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_PLATFORM_REPLY
    assert decision.args.get("platform_topic") == "meta_connection"
    assert decision.args.get("inherited_from_context") is True


@pytest.mark.parametrize("message", ["طيب", "أرسل", "ابعث", "اوكي", "ok"])
def test_context_inherit_various_confirmation_words(message: str) -> None:
    from modules.ai.brain.decision.actions import ACTION_PLATFORM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    ctx, *_ = _build_decision_inputs(
        message=message,
        last_platform_topic="subscription",
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_PLATFORM_REPLY
    assert decision.args.get("platform_topic") == "subscription"


def test_context_inherit_does_not_fire_without_topic() -> None:
    """No `last_platform_topic` → branch 0z must stay out of the way."""
    from modules.ai.brain.decision.actions import ACTION_PLATFORM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    ctx, *_ = _build_decision_inputs(message="نعم", last_platform_topic="")
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action != ACTION_PLATFORM_REPLY


def test_context_inherit_does_not_fire_with_product_focus() -> None:
    """A bare "نعم" while a product is in focus is an order
    confirmation, NOT a platform-context inheritance signal."""
    from modules.ai.brain.decision.actions import ACTION_PLATFORM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    ctx, *_ = _build_decision_inputs(
        message="نعم",
        last_platform_topic="api",
        current_product_focus={
            "id": "p1", "title": "عسل سدر", "price": 350,
        },
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action != ACTION_PLATFORM_REPLY


def test_context_inherit_skips_long_messages() -> None:
    """Long messages carry their own intent and must NOT inherit."""
    from modules.ai.brain.decision.actions import ACTION_PLATFORM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    ctx, *_ = _build_decision_inputs(
        message="نعم وكمان أبي أعرف الأسعار",
        last_platform_topic="subscription",
    )
    decision = DefaultDecisionEngine().decide(ctx)
    # The classifier may have produced INTENT_GENERAL here; engine must
    # decide based on the real signal, not inherit on a 5-word message.
    assert decision.action != ACTION_PLATFORM_REPLY


# ─────────────────────────────────────────────────────────────────────────────
# 11. Direct production-bug regression: "السلام عليكم أبي سعر العسل"
# ─────────────────────────────────────────────────────────────────────────────
#
# This is the EXACT customer message that produced silence in production
# (see merchant screenshot, May 2026). The test asserts:
#   * rules.match() returns INTENT_ASK_PRICE (not GREETING-only).
#   * slots include ``embedded_greeting=True`` so the pipeline prepends
#     a short salaam.
#   * extraction_method is the welcome-gate variant.

def test_production_bug_salaam_with_price_ask() -> None:
    message = "السلام عليكم أبي سعر العسل"
    result = intent_rules.match(message)
    assert result is not None, "rules.match returned None — silent reply regression"
    assert result.name == INTENT_ASK_PRICE, (
        f"expected ASK_PRICE, got {result.name}. The customer asked about price "
        f"and the welcome gate must NOT short-circuit into a greeting card."
    )
    assert (result.slots or {}).get("embedded_greeting") is True, (
        "expected embedded_greeting slot so the composer can prepend a salaam "
        "line on the actionable answer."
    )
    assert "welcome_gate" in (result.extraction_method or ""), (
        f"expected welcome-gate decoration in extraction_method, got "
        f"{result.extraction_method!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 12. MerchantConversationState — new context-memory fields
# ─────────────────────────────────────────────────────────────────────────────
#
# The pipeline persists three new fields in state after each turn:
#   * last_platform_topic   — set when ACTION_PLATFORM_REPLY fires
#   * pending_confirmation  — tag describing what a bare "نعم" would do
#   * last_link_sent /
#     last_link_sent_turn   — repetition-guard for outbound CTAs
# These tests validate the round-trip so the new fields survive a
# serialise→deserialise cycle into Conversation.extra_metadata.

def test_state_round_trip_preserves_context_fields() -> None:
    from modules.ai.brain.types import MerchantConversationState

    s = MerchantConversationState(
        last_platform_topic="meta_connection",
        pending_confirmation="send_platform_link",
        last_link_sent="https://nahla.example/onboarding",
        last_link_sent_turn=3,
    )
    payload = s.to_dict()
    assert payload["last_platform_topic"] == "meta_connection"
    assert payload["pending_confirmation"] == "send_platform_link"
    assert payload["last_link_sent"] == "https://nahla.example/onboarding"
    assert payload["last_link_sent_turn"] == 3

    restored = MerchantConversationState.from_dict(payload)
    assert restored.last_platform_topic == "meta_connection"
    assert restored.pending_confirmation == "send_platform_link"
    assert restored.last_link_sent == "https://nahla.example/onboarding"
    assert restored.last_link_sent_turn == 3


def test_state_round_trip_defaults_when_legacy_blob() -> None:
    """Older brain_state dicts (pre-May-2026) must still deserialise
    without raising — defaults fill in the new fields."""
    from modules.ai.brain.types import MerchantConversationState

    legacy = {"stage": "discovery", "greeted": False}
    restored = MerchantConversationState.from_dict(legacy)
    assert restored.last_platform_topic == ""
    assert restored.pending_confirmation == ""
    assert restored.last_link_sent == ""
    assert restored.last_link_sent_turn == 0


# ─────────────────────────────────────────────────────────────────────────────
# 13. conversation_mode.detect_identity_topic — welcome-gate yield
# ─────────────────────────────────────────────────────────────────────────────
#
# Direct production-bug reproducer: "السلام عليكم أبي سعر العسل" was
# matching the greeting prefix in conversation_mode's _GREETING_PATTERNS
# and triggering MODE_IDENTITY_REPLY → canned identity card. That
# short-circuited the brain entirely so the welcome-gate fix in
# intent/rules.py never ran. The fix yields to the brain whenever a
# greeting prefix is followed by an actionable signal.

@pytest.mark.parametrize("text,expected", [
    # Pure greetings → canned identity card (legacy behaviour preserved).
    ("السلام عليكم",                          "greeting"),
    ("وعليكم السلام",                          "greeting"),
    ("مرحبا",                                  "greeting"),
    ("هلا والله",                              "greeting"),
    ("صباح الخير",                             "greeting"),
    ("أهلاً وسهلاً",                            "greeting"),
    ("hello",                                  "greeting"),
    # Greeting + actionable commerce ask → yield to brain (empty string).
    ("السلام عليكم أبي سعر العسل",             ""),
    ("وعليكم السلام، كم سعر كيلو ضهيان؟",      ""),
    ("السلام عليكم أبغى عسل سدر",              ""),
    ("مرحبا، كم سعر العسل؟",                   ""),
    ("السلام عليكم وش أسعاركم",                ""),
    ("هلا، عندكم عسل سدر؟",                    ""),
    ("صباح الخير أبي تفاصيل عن العسل",         ""),
    # Greeting + platform inquiry → yield to brain.
    ("السلام عليكم أبي أربط واتساب الأعمال",    ""),
    ("مرحبا، كم اشتراك نحلة؟",                  ""),
    # Greeting + payment info / shipping → yield to brain.
    ("السلام عليكم، أبي رابط الدفع",            ""),
    ("مرحبا، كم الشحن للرياض؟",                 ""),
    # Greeting + identity question with greeting FIRST → stays on the
    # canned greeting card path (legacy behaviour preserved). The
    # identity sub-question gets handled by the brain on the next turn
    # since the greeting card asks "what would you like to know?".
    ("السلام عليكم، من أنت؟",                   "greeting"),
    # Pure identity asks → identity card.
    ("من أنت؟",                                "identity"),
    ("هل أنت AI؟",                             "identity"),
])
def test_detect_identity_topic_welcome_gate_yield(
    text: str, expected: str,
) -> None:
    from modules.ai.routing.conversation_mode import detect_identity_topic
    assert detect_identity_topic(text) == expected, (
        f"detect_identity_topic({text!r}) returned the wrong topic — the "
        f"welcome-gate yield is broken and this exact message will be "
        f"swallowed by MODE_IDENTITY_REPLY in production."
    )


def test_actionable_after_greeting_helper_strips_long_salaam() -> None:
    """Long salaam variants (with ورحمة الله وبركاته) must also strip
    cleanly so a price ask after them still routes through the brain."""
    from modules.ai.routing.conversation_mode import (
        _message_has_actionable_after_greeting,
    )
    assert _message_has_actionable_after_greeting(
        "السلام عليكم ورحمة الله وبركاته، أبي سعر العسل"
    )
    assert _message_has_actionable_after_greeting(
        "وعليكم السلام ورحمة الله، كم سعر السدر؟"
    )
    # Greeting + non-actionable courtesy → NOT actionable.
    assert not _message_has_actionable_after_greeting(
        "السلام عليكم ورحمة الله"
    )
    assert not _message_has_actionable_after_greeting(
        "صباح الخير، كيف حالك؟"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
