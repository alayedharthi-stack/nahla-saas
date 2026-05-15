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
    "بيض الله وجهك",
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
    "كفو",
    "والنعم",
    "ما قصرت",
    "ماقصرت",
    "أحسنت",
    "ما شاء الله",
    "تبارك الله",
    "ممتاز",
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
        text = T.social_reply(category=cat, variant=0)
        assert text, f"empty social_reply for category {cat}"
        # Must be short (one-liner) — Gulf-Arabic ack, not a paragraph.
        assert len(text) <= 80, f"social_reply too long for {cat}: {text!r}"


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
