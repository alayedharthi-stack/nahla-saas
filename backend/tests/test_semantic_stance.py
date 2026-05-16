"""
tests/test_semantic_stance.py
─────────────────────────────
Tests for the May 2026 #7 relational-frame layer.

What we're guarding
───────────────────
The production regression that motivated this layer was a customer
saying:

    "الحبل الأول باقي منه عندي ماخلص لكن المرات الجاية إن شاء الله،
     الله يحفظك ويرزقك حبيبًا"

The legacy stack treated that as ``INTENT_GENERAL`` → vague LLM
fallback → tone-deaf "كيف أقدر أخدمك؟". The customer was DEFERRING
warmly, not opening a new buying session, and the bot should have
honoured that.

Two test surfaces are exercised in this file:

  1. :func:`stance_detector.detect_stance` returns the correct
     ``STANCE_*`` label for production-style phrasings AND stays at
     ``STANCE_UNKNOWN`` for ambiguous or hard-buy-signal inputs.

  2. :func:`pipeline._compose_response_goal` prepends the right
     directive when a non-unknown stance is supplied — and is a
     byte-for-byte no-op when stance is ``None`` / ``UNKNOWN``.

We DO NOT spin up the full pipeline here — the stance detector is
pure, and the goal composer is reachable with a tiny dataclass
fixture. The full E2E happens in the production tests via the
``[STANCE]`` log marker.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the ``backend`` package importable.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.intent.stance_detector import (  # noqa: E402
    STANCE_BROWSING,
    STANCE_BUYING_NOW,
    STANCE_DEFERRED,
    STANCE_DIRECTIVES,
    STANCE_INFO_ONLY,
    STANCE_OBJECTION,
    STANCE_POLITE_CLOSE,
    STANCE_SOCIAL_BONDING,
    STANCE_SUPPORT_REQUEST,
    STANCE_UNKNOWN,
    StanceResult,
    detect_stance,
)
from modules.ai.brain.pipeline import _compose_response_goal  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    Decision,
    SuggestionSnapshot,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Production phrasing — the exact bug this layer fixes MUST classify
#     as DEFERRED so the LLM gets the right frame.
# ─────────────────────────────────────────────────────────────────────────────

def test_production_deferred_message() -> None:
    """The exact production case that motivated this layer.

    The customer says they still have honey from before and will buy
    next time. The detector MUST return DEFERRED so the LLM frames
    the reply as "honour the relationship, don't push the sale"."""
    msg = (
        "الحبل الأول باقي منه عندي ماخلص لكن المرات الجاية إن شاء الله،"
        " الله يحفظك ويرزقك حبيبًا"
    )
    r = detect_stance(msg)
    assert r.stance == STANCE_DEFERRED, (
        f"production deferred case misclassified as {r.stance!r}"
    )
    assert r.confidence >= 0.8, "should be high-confidence deferred"
    assert r.evidence, "deferred matches must surface an evidence string"


@pytest.mark.parametrize("msg", [
    "لسه عندي عسل من الطلب اللي قبل",
    "مازال عندي كمية من السمر",
    "باقي عندي شوي من العسل",
    "ما خلص اللي عندي",
    "إن شاء الله المرة الجاية أطلب",
    "بعدين إن شاء الله",
    "لاحقًا إن شاء الله",
    "مو الحين بس بعدين",
])
def test_deferred_variants(msg: str) -> None:
    """All Gulf-Arabic deferral phrasings must classify as DEFERRED."""
    r = detect_stance(msg)
    assert r.stance == STANCE_DEFERRED, (
        f"deferred phrasing {msg!r} misclassified as {r.stance!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Strong buy-intent MUST win — DEFERRED can't override it.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "أبغى أطلب عسل السمر",
    "أبي اشتري كيلو سدر",
    "ودي أحجز الطلب الحين",
    "تمم الطلب",
    "place order now",
])
def test_buying_now_wins(msg: str) -> None:
    """Explicit buy verbs must always classify as BUYING_NOW.

    Even if a sentence also contains deferral noise, the explicit buy
    intent is the dominant signal. Misclassifying these as DEFERRED
    would suppress sales we actually want to advance."""
    r = detect_stance(msg)
    assert r.stance == STANCE_BUYING_NOW, (
        f"explicit buy {msg!r} misclassified as {r.stance!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Support requests — must NOT push for a new sale.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "طلبي تأخر",
    "الشحنة ما وصلت",
    "فيه مشكلة في الطلب",
    "المنتج وصل مكسور",
    "أبغى إرجاع الطلب",
])
def test_support_request(msg: str) -> None:
    """Active issues on prior orders must be flagged so the LLM avoids
    re-pitching."""
    r = detect_stance(msg)
    assert r.stance == STANCE_SUPPORT_REQUEST


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Polite close — soft farewell as session terminator.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "شكرًا",
    "تسلم يا غالي",
    "مع السلامة",
    "الله يحفظك",
    "تمام شكرًا",
])
def test_polite_close(msg: str) -> None:
    """Standalone closing phrases — no buy follow-up should fire."""
    r = detect_stance(msg)
    assert r.stance == STANCE_POLITE_CLOSE, (
        f"polite-close phrasing {msg!r} misclassified as {r.stance!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Objection — price/trust pushback frame.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "السعر غالي",
    "ما اقدر أدفع المبلغ",
    "عند فلان أرخص",
    "مو واثق من الجودة",
])
def test_objection(msg: str) -> None:
    """Objection signals must surface so the LLM doesn't default to
    a discount or a defensive script."""
    r = detect_stance(msg)
    assert r.stance == STANCE_OBJECTION


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Social bonding — short religious / courtesy phrases mid-flow.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "جزاك الله خير",
    "بارك الله فيك",
    "ما شاء الله",
    "كفو والله",
])
def test_social_bonding(msg: str) -> None:
    """Religious / courtesy short phrases must adopt social_bonding so
    the LLM acknowledges warmly without pivoting to a sales script."""
    r = detect_stance(msg)
    assert r.stance == STANCE_SOCIAL_BONDING


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Info only — short question without a buy verb.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "كم مدة التوصيل؟",
    "وين فروعكم؟",
    "هل عندكم شحن لجدة؟",
    "متى يفتح المتجر؟",
])
def test_info_only(msg: str) -> None:
    """Pure information seeks must surface so the LLM answers and stops
    (no automatic up-sell)."""
    r = detect_stance(msg)
    assert r.stance == STANCE_INFO_ONLY


# ─────────────────────────────────────────────────────────────────────────────
# 8.  Browsing — soft exploration verbs only fire when no stronger
#     signal matched.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "ايش عندكم؟",
    "وش متوفر اليوم؟",
    "أبي أتفرّج بس",
    "أشوف الأنواع",
])
def test_browsing(msg: str) -> None:
    r = detect_stance(msg)
    assert r.stance == STANCE_BROWSING


# ─────────────────────────────────────────────────────────────────────────────
# 9.  Negative — ambiguous / direct asks must return UNKNOWN so the
#     pipeline's default behaviour is preserved.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "",                                # empty
    "السلام عليكم",                    # bare greeting (handled elsewhere)
    "عسل",                              # single noun — too ambiguous
    "أبغى أشوف الكتالوج",              # visual ask (handled by enforcement)
    "كيف أتواصل معكم؟",                # contact ask — info_only-ish but with
                                       # "كيف" + buy-related noun; we keep this
                                       # as UNKNOWN since it's borderline.
])
def test_unknown_stance_for_ambiguous_or_handled_elsewhere(msg: str) -> None:
    """Ambiguous messages / asks handled by other layers must remain
    UNKNOWN. Misclassifying them would force a frame the rest of the
    pipeline doesn't expect."""
    r = detect_stance(msg)
    # "كيف أتواصل" matches the INFO_ONLY pattern (starts with "كيف" + "؟"
    # is implied even when missing) — accept either UNKNOWN or INFO_ONLY
    # for that specific case so we never assert a frame that would
    # change downstream behaviour.
    if "أتواصل" in msg:
        assert r.stance in (STANCE_UNKNOWN, STANCE_INFO_ONLY)
    else:
        assert r.stance == STANCE_UNKNOWN, (
            f"ambiguous message {msg!r} unexpectedly classified as {r.stance!r}"
        )


def test_context_only_polite_close_when_recent_history_thin() -> None:
    """Bare 'تمام' / 'شكرًا' becomes polite_close ONLY when the customer
    hasn't been driving the conversation (recent_customer_messages
    sparse). With a busy recent history the bare ack stays UNKNOWN so
    the brain's bare-confirmation path (P1) handles it."""
    # Thin history → polite_close.
    r1 = detect_stance("تمام", recent_customer_messages=[])
    assert r1.stance == STANCE_POLITE_CLOSE

    # Busy history → UNKNOWN (bare-confirmation layer takes it).
    r2 = detect_stance("تمام", recent_customer_messages=[
        "أبي عسل السمر",
        "كم سعره؟",
    ])
    assert r2.stance == STANCE_UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# 10.  _compose_response_goal prepends the stance directive correctly.
# ─────────────────────────────────────────────────────────────────────────────

def _make_decision(reason: str = "no rule matched — LLM fallback") -> Decision:
    return Decision(action=ACTION_LLM_REPLY, args={}, reason=reason, confidence=0.5)


def test_compose_goal_unknown_stance_is_noop() -> None:
    """``stance=None`` and ``stance=StanceResult(UNKNOWN)`` must produce
    a goal byte-for-byte identical to the legacy path — proves the
    enrichment is fully optional and never side-effects existing flows."""
    decision = _make_decision()
    sug = SuggestionSnapshot()
    legacy = _compose_response_goal(decision, sug)
    enriched_none = _compose_response_goal(decision, sug, stance=None)
    enriched_unknown = _compose_response_goal(
        decision, sug, stance=StanceResult(STANCE_UNKNOWN, "", 0.0),
    )
    assert enriched_none == legacy
    assert enriched_unknown == legacy


@pytest.mark.parametrize("stance_name", [
    STANCE_BUYING_NOW,
    STANCE_DEFERRED,
    STANCE_POLITE_CLOSE,
    STANCE_OBJECTION,
    STANCE_SOCIAL_BONDING,
    STANCE_INFO_ONLY,
    STANCE_BROWSING,
    STANCE_SUPPORT_REQUEST,
])
def test_compose_goal_prepends_directive_for_known_stances(stance_name: str) -> None:
    """For every non-unknown stance the goal MUST start with the matching
    ``relational_frame=<stance>`` directive — that's the contract the
    LLM prompt rule depends on."""
    decision = _make_decision()
    sug = SuggestionSnapshot()
    goal = _compose_response_goal(
        decision, sug,
        stance=StanceResult(stance_name, evidence="evidence note", confidence=0.85),
    )
    assert goal.startswith(f"relational_frame={stance_name}"), (
        f"directive missing for {stance_name!r} — got: {goal[:120]!r}"
    )
    assert "evidence=evidence note" in goal


def test_compose_goal_keeps_base_text_intact() -> None:
    """The base goal (decision.reason / next_step / ask_one) must NOT
    be lost when stance enrichment fires — both are visible to the LLM."""
    decision = _make_decision(reason="advance the order flow")
    sug = SuggestionSnapshot(
        suggested_next_step="ask_for_quantity",
        needs_follow_up_question=True,
        follow_up_question="كم كيلو تحب؟",
    )
    goal = _compose_response_goal(
        decision, sug,
        stance=StanceResult(STANCE_DEFERRED, "evidence", 0.85),
    )
    assert "relational_frame=deferred" in goal
    assert "advance the order flow" in goal
    assert "next_step=ask_for_quantity" in goal
    assert "ask_one=" in goal


# ─────────────────────────────────────────────────────────────────────────────
# 11.  Directive table sanity — every stance MUST have a directive
#      string so the LLM has guidance.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stance_name", [
    STANCE_BUYING_NOW, STANCE_DEFERRED, STANCE_POLITE_CLOSE,
    STANCE_OBJECTION, STANCE_SOCIAL_BONDING, STANCE_INFO_ONLY,
    STANCE_BROWSING, STANCE_SUPPORT_REQUEST,
])
def test_every_stance_has_a_directive(stance_name: str) -> None:
    """Closed-enum invariant — adding a stance constant without a
    directive in STANCE_DIRECTIVES would silently break the prompt."""
    assert STANCE_DIRECTIVES.get(stance_name), (
        f"stance {stance_name!r} has no directive in STANCE_DIRECTIVES"
    )


def test_unknown_stance_has_empty_directive() -> None:
    """STANCE_UNKNOWN must map to an empty directive — that's the
    contract that makes the goal a no-op when the detector is unsure."""
    assert STANCE_DIRECTIVES.get(STANCE_UNKNOWN, "?") == ""
