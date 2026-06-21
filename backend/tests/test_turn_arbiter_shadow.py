"""
tests/test_turn_arbiter_shadow.py
──────────────────────────────────
Phase 1 — Turn Understanding + Turn Arbiter shadow layer tests.

Validates shadow semantics only; no production routing changes.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_HANDOFF,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.intent_priority.types import (  # noqa: E402
    GOAL_PRICE_INQUIRY,
    IntentPriorityVerdict,
)
from modules.ai.brain.state.state_relevance import StateRelevanceVerdict  # noqa: E402
from modules.ai.brain.turn.arbiter import arbitrate_turn  # noqa: E402
from modules.ai.brain.turn.contract import (  # noqa: E402
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_PERSONA_SOCIAL,
    OWNER_POST_PURCHASE,
    OWNER_STAFF_ESCALATION,
    OWNER_SUPPORT,
)
from modules.ai.brain.turn.mismatch import (  # noqa: E402
    MISMATCH_CHECKOUT_VS_DISCOVERY,
    MISMATCH_CHECKOUT_VS_SUPPORT,
    MISMATCH_NONE,
    MISMATCH_STAFF_VS_PERSONA,
    classify_owner_mismatch,
)
from modules.ai.brain.turn.shadow import (  # noqa: E402
    complete_turn_shadow_telemetry,
    run_turn_shadow_before_decide,
)
from modules.ai.brain.turn.telemetry import build_shadow_telemetry  # noqa: E402
from modules.ai.brain.turn.understanding import synthesize_turn_understanding  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_COMPLAINT_REFUND,
    INTENT_SOCIAL,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(
    msg: str,
    *,
    intent_name: str = "general",
    intent_confidence: float = 0.9,
    state: MerchantConversationState | None = None,
    state_relevance: StateRelevanceVerdict | None = None,
    intent_priority: IntentPriorityVerdict | None = None,
    social_human_context=None,
    commerce_bundle: dict | None = None,
) -> BrainContext:
    st = state or MerchantConversationState(turn=3)
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=msg,
        raw_message=msg,
        intent=Intent(name=intent_name, confidence=intent_confidence, slots={}),
        state=st,
        facts=CommerceFacts(has_products=True),
        history=[],
        state_relevance=state_relevance,
        intent_priority=intent_priority,
        social_human_context=social_human_context,
        commerce_bundle=commerce_bundle or {},
    )


def _stale_checkout_state() -> MerchantConversationState:
    st = MerchantConversationState(turn=5, stage="checkout")
    st.order_prep = OrderPreparationState(
        product_id="p1",
        missing_fields=["city"],
    )
    st.last_question_asked = "ما المدينة التي سيصلها الطلب؟"
    st.last_question_answered = False
    return st


def _state_rel(
    *,
    topic_shift: bool = True,
    safe_to_resume: bool = False,
) -> StateRelevanceVerdict:
    return StateRelevanceVerdict(
        payment_state_relevant=False,
        fulfillment_state_relevant=False,
        product_replay_relevant=False,
        safe_to_resume_state=safe_to_resume,
        detected_topic_shift=topic_shift,
        active_workflows=("active_fulfillment",),
        relevance_confidence=0.85,
    )


class _PureSocialShc:
    is_pure_social_turn = True
    social_category = "gratitude"


def test_complaint_with_stale_checkout_routes_support_not_checkout():
    msg = "العسل خفيف ومو مثل أول"
    ctx = _ctx(
        msg,
        intent_name=INTENT_COMPLAINT_REFUND,
        intent_confidence=0.92,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)

    assert understanding.current_intent == "complaint_refund"
    assert understanding.should_suspend_stale_state is True
    assert understanding.active_objective_candidate is not None
    assert arbitration.turn_owner in {OWNER_SUPPORT, OWNER_POST_PURCHASE}
    assert arbitration.turn_owner != OWNER_CHECKOUT
    assert arbitration.slot_replay_approved is False
    assert arbitration.owner_brief.owner in {OWNER_SUPPORT, OWNER_POST_PURCHASE}
    assert "checkout" in arbitration.owner_brief.forbidden_objectives


def test_discount_question_routes_discovery_not_checkout_continuation():
    msg = "ما عندكم كود خصم"
    ctx = _ctx(
        msg,
        intent_name=INTENT_ASK_PRICE,
        intent_confidence=0.88,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)

    assert understanding.current_intent == "product_inquiry"
    assert understanding.should_suspend_stale_state is True
    assert arbitration.turn_owner == OWNER_DISCOVERY
    assert arbitration.turn_owner != OWNER_CHECKOUT
    assert arbitration.slot_replay_approved is False
    assert "answer_discount_or_product_question_first" in arbitration.owner_brief.reply_goal


def test_gratitude_routes_persona_not_staff_escalation():
    msg = "وصل والله يبيض وجهك"
    ctx = _ctx(
        msg,
        intent_name=INTENT_SOCIAL,
        intent_confidence=0.91,
        social_human_context=_PureSocialShc(),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)

    assert understanding.current_intent in {"social_interaction", "social_gratitude"}
    assert arbitration.turn_owner in {OWNER_PERSONA_SOCIAL, OWNER_POST_PURCHASE}
    assert arbitration.turn_owner != OWNER_STAFF_ESCALATION


def test_unrelated_message_with_checkout_state_sets_suspend_stale():
    msg = "ما عندكم كود خصم"
    ctx = _ctx(
        msg,
        intent_name=INTENT_ASK_PRICE,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(topic_shift=True, safe_to_resume=False),
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )
    understanding = synthesize_turn_understanding(ctx)

    assert understanding.should_suspend_stale_state is True
    assert len(understanding.conflicts_with_state) >= 1
    assert "order_prep" in understanding.suspend_scope


def test_shadow_flag_defaults_enabled():
    from modules.ai.brain.turn.flags import is_turn_arbiter_shadow_enabled

    assert is_turn_arbiter_shadow_enabled() is True


def test_shadow_flag_disabled_skips_all_work(monkeypatch):
    monkeypatch.setenv("TURN_ARBITER_SHADOW_ENABLED", "false")

    from modules.ai.brain.turn.flags import is_turn_arbiter_shadow_enabled

    assert is_turn_arbiter_shadow_enabled() is False

    ctx = _ctx("test message", intent_name=INTENT_SOCIAL)
    result = run_turn_shadow_before_decide(ctx)
    assert result is None
    assert getattr(ctx, "turn_understanding_shadow", None) is None

    decision = Decision(action=ACTION_SOCIAL_REPLY, reason="test")
    telemetry = complete_turn_shadow_telemetry(ctx, decision)
    assert telemetry is None


def test_mismatch_type_checkout_vs_support_on_complaint():
    msg = "العسل خفيف ومو مثل أول"
    ctx = _ctx(
        msg,
        intent_name=INTENT_COMPLAINT_REFUND,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")

    telemetry = build_shadow_telemetry(understanding, arbitration, legacy)

    assert telemetry.mismatch_type == MISMATCH_CHECKOUT_VS_SUPPORT
    assert telemetry.owner_mismatch is True
    assert telemetry.proposed_owner in {OWNER_SUPPORT, OWNER_POST_PURCHASE}
    assert telemetry.legacy_owner == OWNER_CHECKOUT


def test_mismatch_type_checkout_vs_discovery_on_discount():
    msg = "ما عندكم كود خصم"
    ctx = _ctx(
        msg,
        intent_name=INTENT_ASK_PRICE,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="slot_fill")

    telemetry = build_shadow_telemetry(understanding, arbitration, legacy)

    assert telemetry.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
    assert telemetry.proposed_owner == OWNER_DISCOVERY
    assert telemetry.legacy_owner == OWNER_CHECKOUT


def test_mismatch_type_staff_vs_persona_on_gratitude():
    msg = "وصل والله يبيض وجهك"
    ctx = _ctx(
        msg,
        intent_name=INTENT_SOCIAL,
        social_human_context=_PureSocialShc(),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)
    legacy = Decision(action=ACTION_HANDOFF, reason="keyword_staff")

    telemetry = build_shadow_telemetry(understanding, arbitration, legacy)

    assert telemetry.mismatch_type == MISMATCH_STAFF_VS_PERSONA
    assert telemetry.proposed_owner == OWNER_PERSONA_SOCIAL
    assert telemetry.legacy_owner == OWNER_STAFF_ESCALATION


def test_mismatch_type_none_when_owners_match():
    assert classify_owner_mismatch(OWNER_DISCOVERY, OWNER_DISCOVERY) == MISMATCH_NONE

    ctx = _ctx(
        "ما عندكم كود خصم",
        intent_name=INTENT_ASK_PRICE,
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)
    legacy = Decision(action=ACTION_SEARCH_PRODUCTS, reason="catalog")

    telemetry = build_shadow_telemetry(understanding, arbitration, legacy)

    assert telemetry.mismatch_type == MISMATCH_NONE
    assert telemetry.owner_mismatch is False
    assert telemetry.proposed_owner == OWNER_DISCOVERY
    assert telemetry.legacy_owner == OWNER_DISCOVERY


def test_telemetry_to_dict_includes_flat_fields():
    msg = "ما عندكم كود خصم"
    ctx = _ctx(
        msg,
        intent_name=INTENT_ASK_PRICE,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )
    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
    telemetry = build_shadow_telemetry(understanding, arbitration, legacy)
    payload = telemetry.to_dict()

    assert payload["current_intent"] == understanding.current_intent
    assert payload["mismatch_type"] == MISMATCH_CHECKOUT_VS_DISCOVERY
    assert payload["conflicts_with_state_count"] >= 1
    assert payload["slot_replay_approved"] is False
    assert payload["shadow"] is True
    assert payload["reply_goal"]
    assert payload["compose_mode"] == "persona"
    assert "checkout_resume" in payload["forbidden_objectives"]
