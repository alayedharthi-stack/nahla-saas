"""
tests/test_shipping_intent_dominance.py
───────────────────────────────────────
Tests for the shipping intent regex coverage + current-turn-dominance
fallback policy (May 2026 #17).

Bug class
─────────
Customer asked: "وشلون طريقة توصيل الطلبات عندكم"
Brain didn't crash, but the visible reply was the four-topic
clarification "(عن المنتج / السعر / التوصيل / الدفع)" — strictly
worse than answering directly from store knowledge.

Two distinct fixes are tested here:

  1. **Regex coverage** — broadened ``INTENT_ASK_SHIPPING`` patterns
     in ``modules.ai.brain.intent.rules`` now match every variant
     phrasing the merchant listed (وشلون / كيف / طريقة (singular) /
     توصلون / تشحنون / carrier names / city destinations).

  2. **Current-turn dominance** — new
     ``services.fallback_policy.choose_intent_aware_fallback``
     consults the rule classifier BEFORE returning a generic
     soft-retry: if the current turn has a confident
     ``INTENT_ASK_SHIPPING`` match, we return a deterministic
     shipping answer (or a focused one-question prompt when the
     merchant hasn't configured shipping info) — NEVER the
     four-topic clarification.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.intent import rules as _rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_SHIPPING,
)
from services.fallback_policy import (  # noqa: E402
    FALLBACK_KIND_INTENT_DETERMINISTIC,
    FALLBACK_KIND_SOFT_RETRY,
    FALLBACK_REASON_BRAIN_EXCEPTION,
    FALLBACK_REASON_NO_API_KEY,
    INTENT_AWARE_MIN_CONFIDENCE,
    choose_intent_aware_fallback,
    choose_safe_fallback,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Regex coverage — every variant in the merchant's list classifies
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    # The actual production incident message.
    "وشلون طريقة توصيل الطلبات عندكم",
    # Variants the merchant explicitly listed.
    "وشلون التوصيل؟",
    "كيف توصلون الطلبات",
    "طريقة التوصيل عندكم؟",
    "توصيلكم عن طريق مين؟",
    "هل تشحنون",
    "كم مدة التوصيل",
    "هل التوصيل سمسا",
    "هل عندكم مندوب",
    "الشحن للرياض",
    "الشحن لجدة",
    "التوصيل للدمام",
    # Other natural phrasings that should also be caught.
    "كيف طريقة الشحن",
    "كيفية التوصيل",
    "شلون الشحن",
    "هل عندكم توصيل",
    "تشحنون لجدة؟",
    "تشحنون مع اراميكس؟",
    "do you ship to riyadh",
    "how do you deliver",
    "shipping methods please",
])
def test_shipping_intent_matches_every_merchant_listed_variant(text):
    """Pin every phrasing the merchant cited as a regression guard.
    A future regex tightening that breaks any of these MUST be
    caught here."""
    intent = _rules.match(text)
    assert intent is not None, f"no intent matched for {text!r}"
    assert intent.name == INTENT_ASK_SHIPPING, (
        f"{text!r} classified as {intent.name!r}, expected {INTENT_ASK_SHIPPING!r}"
    )
    assert intent.confidence >= INTENT_AWARE_MIN_CONFIDENCE, (
        f"{text!r} confidence {intent.confidence} < threshold {INTENT_AWARE_MIN_CONFIDENCE}"
    )


def test_production_incident_confidence_beats_ask_product():
    """Specific pin on the production incident: the broad
    ``INTENT_ASK_PRODUCT`` pattern fires on the substring "طلب"
    inside "الطلبات" at conf 0.82. Our new shipping rule MUST win
    via its higher 0.90 confidence — that's the whole reason we
    bumped the score."""
    intent = _rules.match("وشلون طريقة توصيل الطلبات عندكم")
    assert intent is not None
    assert intent.name == INTENT_ASK_SHIPPING
    assert intent.confidence >= 0.90


def test_personal_shipment_status_still_routes_to_track_order():
    """Regression guard for the OPPOSITE failure mode: the original
    rule was tightened because "وين شحنتي" / "وصلت الشحنة" were
    mis-classifying as ASK_SHIPPING. Make sure our broadening
    didn't reintroduce that bug."""
    for personal in ["وين شحنتي", "وصلت الشحنة", "هل وصل الطلب", "طلبيتي وين"]:
        intent = _rules.match(personal)
        # Either TRACK_ORDER (preferred) or any non-ASK_SHIPPING
        # intent is acceptable here — what matters is we don't
        # collapse personal tracking onto generic policy FAQ.
        assert intent is None or intent.name != INTENT_ASK_SHIPPING, (
            f"personal shipment query {personal!r} mis-classified as ASK_SHIPPING"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2.  match_top_k — observability
# ─────────────────────────────────────────────────────────────────────────────


def test_match_top_k_returns_sorted_candidates():
    """For the production incident, top-k should show
    INTENT_ASK_SHIPPING first; any spurious siblings come after."""
    top = _rules.match_top_k("وشلون طريقة توصيل الطلبات عندكم", k=3)
    assert len(top) >= 1
    confs = [c for c, _ in top]
    assert confs == sorted(confs, reverse=True), "top_k not sorted desc"
    assert top[0][1].name == INTENT_ASK_SHIPPING


def test_match_top_k_empty_when_nothing_matches():
    """A meaningless ping ("hello") may still match INTENT_GREETING
    via the social classifier, but a complete non-message like ""
    yields nothing. Test isolates the empty case."""
    assert _rules.match_top_k("") == []


def test_match_top_k_clamps_k_to_at_least_one():
    """k=0 or negative should still return at least one candidate
    when there IS a match (defensive — callers shouldn't pass 0
    but we should not blow up)."""
    top = _rules.match_top_k("وشلون التوصيل", k=0)
    assert len(top) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Current-turn dominance — choose_intent_aware_fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_intent_aware_fallback_avoids_soft_retry_for_shipping_question():
    """The exact production-incident inbound, when routed through
    the intent-aware fallback, must NEVER return the four-topic
    soft-retry text. Instead it returns a deterministic shipping
    answer (or an honest one-question prompt when no shipping
    info is configured)."""
    decision = choose_intent_aware_fallback(
        "وشلون طريقة توصيل الطلبات عندكم",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        shipping_info={},   # empty — merchant hasn't configured shipping yet
    )
    assert decision.kind == FALLBACK_KIND_INTENT_DETERMINISTIC, (
        f"expected deterministic-intent answer, got {decision.kind!r}"
    )
    # The forbidden four-topic substring must not appear.
    assert "(عن المنتج / السعر / التوصيل / الدفع)" not in decision.text
    # And it must be on-topic: the reply addresses delivery, not
    # the generic "give me more detail" copy.
    assert "التوصيل" in decision.text or "الشحن" in decision.text


def test_intent_aware_fallback_uses_configured_shipping_info():
    """When the merchant HAS shipping info configured, the
    deterministic answer surfaces those concrete facts instead of
    asking the customer for a city."""
    info = {
        "shipping_methods": ["سمسا", "اراميكس"],
        "shipping_policy":  "توصيل خلال 2-5 أيام عمل.",
        "delivery_areas":   ["الرياض", "جدة", "الدمام"],
    }
    decision = choose_intent_aware_fallback(
        "كيف طريقة الشحن",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        shipping_info=info,
    )
    assert decision.kind == FALLBACK_KIND_INTENT_DETERMINISTIC
    # Concrete facts surface in the reply.
    assert "سمسا" in decision.text
    assert "الرياض" in decision.text or "جدة" in decision.text
    assert "2-5 أيام" in decision.text


def test_intent_aware_fallback_delegates_when_no_intent_matches():
    """A non-question / non-shipping turn ("شكراً جزيلاً") with no
    confident intent should delegate to the standard policy.
    Result kind must be the regular soft/neutral retry, NOT the
    deterministic-intent kind."""
    decision = choose_intent_aware_fallback(
        "شكراً جزيلاً",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
    )
    assert decision.kind != FALLBACK_KIND_INTENT_DETERMINISTIC


def test_intent_aware_fallback_respects_min_confidence_threshold():
    """If we crank the threshold above what the rules give us, the
    deterministic path must NOT fire even for a clear shipping
    question — proves the gate is real, not always-on."""
    decision = choose_intent_aware_fallback(
        "كيف طريقة الشحن",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        min_confidence=0.99,    # impossible threshold
    )
    assert decision.kind != FALLBACK_KIND_INTENT_DETERMINISTIC


def test_intent_aware_fallback_no_api_key_path_unchanged():
    """The no-API-key path must keep its existing semantics — it's
    the only honest answer when the AI is fully off, and a
    deterministic shipping answer would be misleading because we
    can't follow it up."""
    decision = choose_intent_aware_fallback(
        "وشلون طريقة توصيل الطلبات عندكم",
        reason=FALLBACK_REASON_NO_API_KEY,
    )
    assert decision.kind != FALLBACK_KIND_INTENT_DETERMINISTIC


def test_intent_aware_fallback_handoff_request_still_routes_to_handoff():
    """Explicit human-handoff requests are not intent-aware
    answerable — the customer asked for a person, not for
    shipping facts. Must still route through the handoff
    path."""
    decision = choose_intent_aware_fallback(
        "أبي أتكلم مع موظف",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
    )
    assert decision.kind != FALLBACK_KIND_INTENT_DETERMINISTIC


# ─────────────────────────────────────────────────────────────────────────────
# 4.  The behavioural rule from the merchant's spec
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("inbound", [
    "وشلون التوصيل؟",
    "كيف توصلون الطلبات",
    "طريقة التوصيل عندكم؟",
    "هل تشحنون لجدة؟",
    "هل التوصيل سمسا",
])
def test_short_clear_shipping_questions_dont_get_four_topic_clarification(inbound):
    """The merchant's rule (May 2026 #17):

        "إذا كان current turn confidence أعلى من threshold،
         فامنع clarification fallback حتى لو كانت المحادثة
         السابقة noisy."

    For short, clear, standalone-understandable SHIPPING
    questions the fallback must NEVER return the four-topic
    soft retry — regardless of what prior context was. This
    test enforces the rule for the shipping family which has
    a deterministic handler today.

    Follow-up scope (not enforced here):
      ``ASK_PRICE`` / ``ASK_PAYMENT_INFO`` / ``ASK_STORE_INFO``
    will get the same dominance guarantee when their
    deterministic responders land — each needs its own
    facts plumb (price → product info, payment → payment
    methods, store_info → store name + location). Until
    those exist, those intents fall through to the standard
    soft-retry policy, which is honest but not yet
    suppressed.
    """
    decision = choose_intent_aware_fallback(
        inbound,
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        shipping_info={"shipping_methods": ["سمسا"]},
    )
    # The forbidden substring must NEVER appear.
    assert "(عن المنتج / السعر / التوصيل / الدفع)" not in decision.text
    assert decision.kind == FALLBACK_KIND_INTENT_DETERMINISTIC


def test_ambiguous_inbound_still_gets_soft_retry():
    """The classifier should NOT over-fire. An ambiguous inbound
    ("شكراً سؤال" — nonsensical) still needs the generic retry —
    we don't want to pretend the customer asked about shipping
    just to avoid the soft-retry text."""
    decision = choose_intent_aware_fallback(
        "شكراً سؤال",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
    )
    # Allow either soft_retry or neutral_retry — the point is we
    # didn't fake an intent answer for an unclear message.
    assert decision.kind != FALLBACK_KIND_INTENT_DETERMINISTIC


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Backward compatibility — choose_safe_fallback is unchanged
# ─────────────────────────────────────────────────────────────────────────────


def test_choose_safe_fallback_still_returns_soft_retry_for_shipping_ask():
    """The lower-level ``choose_safe_fallback`` (no intent awareness)
    still routes shipping questions to SOFT_RETRY — that's its job.
    Only the higher-level ``choose_intent_aware_fallback`` does the
    deterministic upgrade. This keeps the contract layered cleanly."""
    decision = choose_safe_fallback(
        "وشلون التوصيل",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
    )
    assert decision.kind == FALLBACK_KIND_SOFT_RETRY
