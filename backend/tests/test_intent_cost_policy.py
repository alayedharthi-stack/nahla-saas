"""PR2B — intent cost policy and no-LLM routine social paths."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.cost.intent_cost_policy import (  # noqa: E402
    emit_llm_avoidable_call,
    get_intent_cost_policy,
    should_avoid_llm_for_intent,
    should_use_template_for_pure_greeting,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_GREET,
    ACTION_LLM_REPLY,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    INTENT_ASK_PRODUCT,
    INTENT_GREETING,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
)


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        orderable=True,
        store_name="متجر تجريبي",
    )


def _greeting_ctx(
    msg: str,
    *,
    greeted: bool = False,
    slots: dict | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=msg,
        intent=Intent(
            name=INTENT_GREETING,
            confidence=0.95,
            slots=dict(slots or {}),
            raw_message=msg,
        ),
        state=MerchantConversationState(greeted=greeted),
        facts=_facts(),
    )


def _social_ctx(message: str, category: str = "thanks") -> BrainContext:
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=message,
        intent=Intent(
            name=INTENT_SOCIAL,
            confidence=0.95,
            slots={"social_category": category},
            raw_message=message,
        ),
        state=MerchantConversationState(),
        facts=_facts(),
    )


class TestIntentCostPolicyModule:
    def test_routine_intents_avoid_llm_by_default(self) -> None:
        for name in ("greeting", "social", "thanks", "farewell"):
            policy = get_intent_cost_policy(name)
            assert policy.llm_mode == "avoid"
            assert policy.allow_kb is False
            assert policy.allow_catalog is False
            assert policy.allow_tools is False

    def test_commerce_intent_allows_llm(self) -> None:
        policy = get_intent_cost_policy(INTENT_ASK_PRODUCT)
        assert policy.llm_mode == "allow"

    def test_no_tenant_specific_logic_in_policy(self) -> None:
        assert should_avoid_llm_for_intent("greeting") is True
        policy_t7 = get_intent_cost_policy("greeting")
        policy_t33 = get_intent_cost_policy("greeting")
        assert policy_t7 == policy_t33

    def test_kill_switch_disables_avoid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "false")
        assert should_avoid_llm_for_intent("greeting") is False
        assert should_use_template_for_pure_greeting(
            intent_name=INTENT_GREETING,
            embedded_greeting=False,
            has_actionable_substance=False,
        ) is False


class TestPureGreetingRouting:
    @pytest.mark.parametrize("msg", ["هلا", "مرحبا", "السلام عليكم"])
    def test_pure_greeting_decision_is_action_greet_not_llm(self, msg: str) -> None:
        decision = DefaultDecisionEngine().decide(_greeting_ctx(msg, greeted=False))
        assert decision.action == ACTION_GREET
        assert decision.action != ACTION_LLM_REPLY

    def test_established_pure_greeting_uses_re_greet_template(self) -> None:
        decision = DefaultDecisionEngine().decide(_greeting_ctx("هلا", greeted=True))
        assert decision.action == ACTION_GREET
        assert decision.args.get("re_greet") is True

    def test_pure_greeting_compose_does_not_call_llm(self) -> None:
        composer = DefaultComposer()
        ctx = _greeting_ctx("هلا", greeted=False)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_GREET
        result = ActionResult(success=True, data={})

        async def _run() -> None:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value="يجب ألا يُستدعى"),
            ) as mock_llm:
                reply = await composer.compose(decision, result, ctx)
            mock_llm.assert_not_called()
            assert reply.strip()

        asyncio.run(_run())

    def test_pure_greeting_pre_commerce_shortcut_skips_catalog(self) -> None:
        intent = Intent(
            name=INTENT_GREETING,
            confidence=0.95,
            slots={},
            raw_message="هلا",
        )
        assert should_pre_commerce_shortcut(
            intent,
            None,
            message="هلا",
            state=MerchantConversationState(),
        )

    def test_embedded_greeting_with_product_question_not_pure_greet(self) -> None:
        msg = "هلا عندكم طلح؟"
        decision = DefaultDecisionEngine().decide(
            _greeting_ctx(
                msg,
                greeted=False,
                slots={"embedded_greeting": True},
            )
        )
        assert decision.action != ACTION_GREET

    def test_daf_bypass_first_turn_with_substance_not_action_greet(self) -> None:
        msg = "السلام عليكم كم سعر العسل؟"
        decision = DefaultDecisionEngine().decide(_greeting_ctx(msg, greeted=False))
        assert decision.action != ACTION_GREET


class TestSocialThanksRouting:
    @pytest.mark.parametrize(
        "message, category",
        [
            ("جزاك الله خير", "thanks"),
            ("الله يسعدك", "blessing"),
            ("تسلم", "thanks"),
        ],
    )
    def test_social_thanks_routes_to_persona_compose_not_template(
        self, message: str, category: str,
    ) -> None:
        decision = DefaultDecisionEngine().decide(
            _social_ctx(message, category),
        )
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") in {
            "social_persona_ack",
            "persona_social",
        }

    def test_social_compose_uses_llm_for_thanks(self) -> None:
        composer = DefaultComposer()
        ctx = _social_ctx("جزاك الله خير", "thanks")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        result = ActionResult(success=True, data={})

        async def _run() -> None:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value="وإياك، الله يبارك فيك"),
            ) as mock_llm:
                reply = await composer.compose(decision, result, ctx)
            mock_llm.assert_called_once()
            assert reply.strip()

        asyncio.run(_run())


class TestAvoidableCallWarning:
    def test_llm_avoidable_call_emits_metadata_only(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="nahla.ai.brain.cost")
        secret = "محتوى_سر_للعميل"
        emit_llm_avoidable_call(
            tenant_id=7,
            intent="greeting",
            action=ACTION_LLM_REPLY,
            estimated_input_tokens=5000,
            system_chars=20000,
            reason="policy_bypass_test",
        )
        joined = " ".join(r.message for r in caplog.records)
        assert "[LLM_AVOIDABLE_CALL]" in joined
        assert secret not in joined

    def test_llm_compose_logs_avoidable_for_greeting_intent(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="nahla.ai.brain.cost")
        composer = DefaultComposer()
        ctx = BrainContext(
            tenant_id=7,
            customer_phone="+966555555555",
            message="هلا",
            intent=Intent(name=INTENT_GREETING, confidence=0.95, slots={}),
            state=MerchantConversationState(),
            facts=_facts(),
            reply_state=BrainReplyState(
                store_name="متجر",
                intent_name=INTENT_GREETING,
                stage="exploring",
                response_goal="test",
            ),
        )
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"persona_kind": "greeting"},
            reason="forced llm for test",
        )
        result = ActionResult(success=True, data={})

        async def _run() -> None:
            with patch(
                "modules.ai.orchestrator.adapter.generate_ai_reply",
                new=AsyncMock(return_value="رد"),
            ):
                await composer._llm_compose(ctx, result, decision=decision)

        asyncio.run(_run())
        joined = " ".join(r.message for r in caplog.records)
        assert "[LLM_AVOIDABLE_CALL]" in joined
        assert "هلا" not in joined


class TestPersonaWarmCompose:
    def test_pure_greeting_reply_is_warm_not_cold_stub(self) -> None:
        from modules.ai.brain.compose.persona_template_engine import (  # noqa: PLC0415
            persona_reply_is_warm_greeting,
        )

        composer = DefaultComposer()
        ctx = _greeting_ctx("هلا", greeted=False)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_GREET
        result = ActionResult(success=True, data={})

        async def _run() -> str:
            return await composer.compose(decision, result, ctx)

        reply = asyncio.run(_run())
        assert persona_reply_is_warm_greeting(reply)
        assert reply not in {"يا هلا", "حياك الله 💛", "أهلاً"}


class TestPureGreetingNoKbPrompt:
    def test_greeting_avoid_policy_blocks_kb_in_prompt_when_built(self) -> None:
        state = BrainReplyState(
            store_name="متجر",
            intent_name=INTENT_GREETING,
            stage="exploring",
            response_goal="رد",
            merchant_context={
                "structured_facts_block": "حقائق المتجر.\n" * 500,
                "products": [{"id": 1, "title": "عسل"}],
                "resolver_overlay": "[PRODUCT:1]",
            },
        )
        prompt = build_brain_reply_prompt(state)
        assert "حقائق المتجر" not in prompt
        assert "[PRODUCT:1]" not in prompt
