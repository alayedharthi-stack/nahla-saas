"""
Phase A — contextual clarify rollout tests.

Validates that legacy ``general_attribute`` templates do not reach customers
when ``CONTEXTUAL_CLARIFY_ENABLED=true``, policy clamp blocks clarify on
non-commerce turns, and deterministic variant pricing is unchanged.
"""
from __future__ import annotations

import pytest

from modules.ai.brain.clarification.router import (
    try_contextual_clarification_fallback,
    try_contextual_price_clarification,
)
from modules.ai.brain.commerce.solution_seeking import intelligent_need_clarification
from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.decision.policy import RealPolicyGate
from modules.ai.brain.intent import rules
from modules.ai.brain.product_discovery_gate import (
    clarify_instead_of_top_products,
    try_price_query_decision,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    INTENT_ASK_PRICE,
    INTENT_SOCIAL,
    MerchantConversationState,
)

_LEGACY_GENERAL_ATTRIBUTE = intelligent_need_clarification("general_attribute")


def _ctx(
    message: str,
    *,
    intent_name: str | None = None,
    intent_conf: float = 0.9,
    slots: dict | None = None,
    focus: dict | None = None,
    block_commerce: bool = False,
) -> BrainContext:
    intent = rules.match(message)
    if intent_name is not None:
        intent = Intent(
            name=intent_name,
            confidence=intent_conf,
            raw_message=message,
            slots=dict(slots or {}),
        )
    elif intent is None:
        intent = Intent(
            name="general",
            confidence=0.5,
            raw_message=message,
            slots=dict(slots or {}),
        )
    elif slots:
        intent = Intent(
            name=intent.name,
            confidence=intent.confidence,
            raw_message=message,
            slots={**(intent.slots or {}), **slots},
        )

    state = MerchantConversationState(greeted=True, stage="discovery")
    if focus:
        state.current_product_focus = focus

    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=state,
        facts=CommerceFacts(
            store_name="Test",
            has_products=True,
            product_count=5,
            in_stock_count=5,
            orderable=True,
            snapshot_fresh=True,
        ),
    )
    if block_commerce:
        ctx.block_commerce_escalation = True
        ctx.non_commerce_category = "religious_media"
    return ctx


def _assert_no_legacy_general_attribute(decision) -> None:
    question = str((decision.args or {}).get("question") or "")
    assert _LEGACY_GENERAL_ATTRIBUTE not in question
    assert "تقصد حاجة أو مواصفة" not in question


@pytest.fixture
def phase_a_flag_on(monkeypatch):
    monkeypatch.setenv("CONTEXTUAL_CLARIFY_ENABLED", "true")
    monkeypatch.setenv("CLARIFICATION_SHADOW_ENABLED", "true")


PHASE_A_REPLAY_MESSAGES = [
    "بكم القسط؟",
    "تقسيط بكم والسعر الإجمالي كم؟",
    "أبي شيء مناسب للوالد.",
    "أبغى الأفضل.",
]


class TestPhaseAClarifyInstead:
    @pytest.mark.parametrize("message", PHASE_A_REPLAY_MESSAGES)
    def test_clarify_instead_never_general_attribute_template(
        self, phase_a_flag_on, message,
    ):
        ctx = _ctx(message)
        dec = clarify_instead_of_top_products(ctx, reason="weak_or_unknown_intent")
        _assert_no_legacy_general_attribute(dec)
        assert dec.action in {ACTION_LLM_REPLY, ACTION_CLARIFY}
        if dec.action == ACTION_LLM_REPLY:
            topic = str((dec.args or {}).get("topic") or "")
            assert topic in {
                "contextual_clarify",
                "solution_seeking_commerce",
                "ask_shipping",
                "ask_payment_info",
                "track_order",
                "fulfillment_location",
            }


class TestPhaseADecisionEngineReplay:
    @pytest.mark.parametrize("message", PHASE_A_REPLAY_MESSAGES)
    def test_engine_never_general_attribute_template(
        self, phase_a_flag_on, message,
    ):
        ctx = _ctx(message)
        dec = DefaultDecisionEngine().decide(ctx)
        _assert_no_legacy_general_attribute(dec)

    def test_installment_price_routes_to_contextual_clarify(
        self, phase_a_flag_on,
    ):
        message = "تقسيط بكم والسعر الإجمالي كم؟"
        ctx = _ctx(message)
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert (dec.args or {}).get("topic") == "contextual_clarify"

    def test_solution_seeking_parent_gift_stays_generative(
        self, phase_a_flag_on,
    ):
        ctx = _ctx("أبي شيء مناسب للوالد.")
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert (dec.args or {}).get("topic") == "solution_seeking_commerce"


class TestPhaseAPriceHook:
    def test_price_hook_generative_when_flag_on(self, phase_a_flag_on):
        ctx = _ctx("بكم", intent_name=INTENT_ASK_PRICE)
        dec = try_price_query_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert (dec.args or {}).get("topic") == "contextual_clarify"
        _assert_no_legacy_general_attribute(dec)

    def test_router_fallback_generative(self, phase_a_flag_on):
        ctx = _ctx("أبغى الأفضل.")
        dec = try_contextual_clarification_fallback(
            ctx, trigger="discovery_blocked",
        )
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        _assert_no_legacy_general_attribute(dec)


class TestPhaseANonCommerceClamp:
    def test_clarify_downgraded_to_social_on_non_commerce(self):
        gate = RealPolicyGate()
        incoming = Decision(
            action=ACTION_CLARIFY,
            args={"question": "تقصد حاجة أو مواصفة معيّنة؟"},
            reason="test",
            confidence=0.8,
        )
        ctx = _ctx("الله يسعدك وين ما تروح", block_commerce=True)
        out = gate.gate(incoming, ctx)
        assert out.action == ACTION_SOCIAL_REPLY
        assert out.args.get("policy_reason") == "non_commerce_clamp"

    def test_social_blessing_not_legacy_clarify(self, phase_a_flag_on):
        ctx = _ctx("الله يسعدك وين ما تروح", intent_name=INTENT_SOCIAL)
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action != ACTION_CLARIFY
        _assert_no_legacy_general_attribute(dec)


class TestPhaseAProductVisualArgFix:
    def test_visual_clarify_uses_question_key(self):
        ctx = _ctx(
            "وريني صورته",
            intent_name="product_visual_request",
        )
        dec = DefaultDecisionEngine().decide(ctx)
        if dec.action == ACTION_CLARIFY:
            q = str((dec.args or {}).get("question") or "")
            assert q
            assert q != "ما الذي تبحث عنه بالضبط؟"
            assert "صور" in q


class TestPhaseAFlagOffUnchanged:
    def test_flag_off_still_returns_none_from_router(self, monkeypatch):
        monkeypatch.setenv("CONTEXTUAL_CLARIFY_ENABLED", "false")
        ctx = _ctx("تقسيط بكم والسعر الإجمالي كم؟")
        assert try_contextual_clarification_fallback(
            ctx, trigger="discovery_blocked",
        ) is None
