"""
tests/test_turn_arbiter_observability.py
────────────────────────────────────────
Phase 3 — turn arbiter outcome classification for production review.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import ACTION_ORDER_CONTEXT_UPDATE  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRICE_INQUIRY, IntentPriorityVerdict  # noqa: E402
from modules.ai.brain.state.state_relevance import StateRelevanceVerdict  # noqa: E402
from modules.ai.brain.turn.arbiter import arbitrate_turn  # noqa: E402
from modules.ai.brain.turn.observability import (  # noqa: E402
    GREP_PATTERNS,
    OUTCOME_COMPOSER_TONE_ISSUE,
    OUTCOME_MISSED_MISMATCH,
    OUTCOME_NO_MISMATCH,
    OUTCOME_SUCCESS,
    classify_turn_outcome,
)
from modules.ai.brain.turn.telemetry import build_shadow_telemetry  # noqa: E402
from modules.ai.brain.turn.understanding import synthesize_turn_understanding  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(msg: str, **kwargs) -> BrainContext:
    st = kwargs.pop("state", None) or MerchantConversationState(turn=3)
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=msg,
        raw_message=msg,
        intent=Intent(name=kwargs.get("intent_name", "general"), confidence=0.9, slots={}),
        state=st,
        facts=CommerceFacts(has_products=True),
        history=[],
        **{k: v for k, v in kwargs.items() if k != "intent_name"},
    )


def _stale_checkout_state() -> MerchantConversationState:
    st = MerchantConversationState(turn=5, stage="checkout")
    st.order_prep = OrderPreparationState(product_id="p1", missing_fields=["city"])
    st.last_question_asked = "ما المدينة؟"
    st.last_question_answered = False
    return st


def _state_rel() -> StateRelevanceVerdict:
    return StateRelevanceVerdict(
        safe_to_resume_state=False,
        detected_topic_shift=True,
        active_workflows=("active_fulfillment",),
    )


def _telemetry_for_discount_mismatch():
    ctx = _ctx(
        "ما عندكم كود خصم",
        intent_name=INTENT_ASK_PRICE,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
    return build_shadow_telemetry(understanding, arbitration, legacy)


def test_grep_patterns_defined():
    assert "[TURN_ARBITER_SHADOW]" in GREP_PATTERNS["shadow_all"]
    assert "[TURN_ARBITER_OUTCOME]" in GREP_PATTERNS["outcome_log"]


def test_classify_no_mismatch():
    ctx = _ctx("مرحبا", intent_name="greeting")
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)
    legacy = Decision(action="ACTION_LLM_REPLY", reason="greeting")
    telemetry = build_shadow_telemetry(understanding, arbitration, legacy)
    assert classify_turn_outcome(telemetry, enforced=False) == OUTCOME_NO_MISMATCH


def test_classify_missed_mismatch():
    telemetry = _telemetry_for_discount_mismatch()
    assert telemetry.owner_mismatch is True
    assert classify_turn_outcome(telemetry, enforced=False) == OUTCOME_MISSED_MISMATCH


def test_classify_success_when_enforced_with_brief():
    telemetry = _telemetry_for_discount_mismatch()
    outcome = classify_turn_outcome(
        telemetry,
        enforced=True,
        compose_used_brief=True,
        reply_text="عندنا كود خصم للطلبات الجديدة",
    )
    assert outcome == OUTCOME_SUCCESS


def test_classify_composer_tone_issue_heuristic():
    telemetry = _telemetry_for_discount_mismatch()
    outcome = classify_turn_outcome(
        telemetry,
        enforced=True,
        compose_used_brief=True,
        reply_text="ما المدينة التي سيصلها الطلب؟ يسعدنا خدمتك نحن هنا لمساعدتك",
    )
    assert outcome == OUTCOME_COMPOSER_TONE_ISSUE
