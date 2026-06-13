"""P1-F social routing — template-first under PR2B intent cost policy."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.decision.policy import RealPolicyGate  # noqa: E402
from modules.ai.brain.intent.social_classifier import classify_social  # noqa: E402
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_TOPIC_SOCIAL_PERSONA_ACK,
    compose_social_persona_goal,
)
from modules.ai.brain.pipeline import _compose_base_response_goal  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)


def _social_ctx(*, message: str, social_category: str) -> BrainContext:
    intent = Intent(
        name=INTENT_SOCIAL,
        confidence=0.95,
        slots={"social_category": social_category},
        raw_message=message,
    )
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=message,
        intent=intent,
        state=MerchantConversationState(),
        facts=CommerceFacts(
            has_products=True,
            product_count=3,
            orderable=True,
            store_name="متجر الاختبار",
        ),
    )


def _assert_template_social(decision: Decision) -> None:
    assert decision.action == ACTION_SOCIAL_REPLY
    assert decision.action != ACTION_LLM_REPLY
    assert decision.args.get("social_category")


def _assert_llm_social_persona(decision: Decision) -> None:
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action != ACTION_SOCIAL_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL_PERSONA_ACK


class TestSocialPersonaRouting:
    @pytest.mark.parametrize(
        "message, category",
        [
            ("جزاك الله خير", "thanks"),
            ("الله يسعدك", "blessing"),
            ("الله يسلمك", "blessing"),
            ("ربي يحفظك", "blessing"),
            ("هلا وسهلا", "general_courtesy"),
            ("كفو", "strong_praise"),
        ],
    )
    def test_social_categories_route_to_template_not_llm(
        self, message: str, category: str,
    ) -> None:
        decision = DefaultDecisionEngine().decide(
            _social_ctx(message=message, social_category=category),
        )
        _assert_template_social(decision)
        assert decision.args.get("social_category") == category

    def test_social_categories_route_to_llm_when_avoid_disabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "false")
        decision = DefaultDecisionEngine().decide(
            _social_ctx(message="جزاك الله خير", social_category="thanks"),
        )
        _assert_llm_social_persona(decision)

    def test_classified_teslam_routes_to_template_not_llm(self) -> None:
        match = classify_social("تسلم")
        assert match is not None
        ctx = _social_ctx(message="تسلم", social_category=match.category)
        decision = DefaultDecisionEngine().decide(ctx)
        _assert_template_social(decision)

    def test_classified_rabi_yahfazk_routes_to_template_not_llm(self) -> None:
        match = classify_social("ربي يحفظك")
        assert match is not None
        ctx = _social_ctx(message="ربي يحفظك", social_category=match.category)
        decision = DefaultDecisionEngine().decide(ctx)
        _assert_template_social(decision)

    def test_classified_allah_yeslamk_routes_to_template_not_llm(self) -> None:
        match = classify_social("الله يسلمك")
        assert match is not None
        assert match.category == "blessing"
        ctx = _social_ctx(message="الله يسلمك", social_category=match.category)
        decision = DefaultDecisionEngine().decide(ctx)
        _assert_template_social(decision)

    def test_allah_yeslamk_end_to_end_from_rules_match(self) -> None:
        from modules.ai.brain.intent import rules as intent_rules  # noqa: PLC0415

        matched = intent_rules.match("الله يسلمك")
        assert matched is not None
        assert matched.name == INTENT_SOCIAL
        assert (matched.slots or {}).get("social_category") == "blessing"
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000000",
            message="الله يسلمك",
            intent=matched,
            state=MerchantConversationState(),
            facts=CommerceFacts(
                has_products=True,
                product_count=3,
                orderable=True,
                store_name="متجر الاختبار",
            ),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        _assert_template_social(decision)


class TestSocialPersonaGoal:
    def test_goal_is_principle_based_not_phrase_prescriptive(self) -> None:
        goal = compose_social_persona_goal("thanks")
        assert "social_persona_ack" in goal
        assert "Principles:" in goal or "Principles" in goal
        assert "يطري" not in goal
        assert "دوم بخير" not in goal

    def test_goal_forbids_follow_up_and_status_questions(self) -> None:
        goal = compose_social_persona_goal("blessing")
        assert "follow-up question" in goal.lower()
        assert "status" in goal.lower()
        assert "customer-care" in goal.lower() or "customer care" in goal.lower()
        assert "آمين" not in goal
        assert "كيف أمورك" not in goal

    def test_goal_does_not_compress_to_one_line_ack(self) -> None:
        goal = compose_social_persona_goal("blessing")
        assert "one short" not in goal.lower()
        assert "1-line" not in goal.lower()
        assert "not a forced one-line" in goal.lower()

    def test_pipeline_wires_social_persona_goal(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": PERSONA_TOPIC_SOCIAL_PERSONA_ACK,
                "social_category": "blessing",
            },
            reason="test",
            confidence=0.9,
        )
        goal = _compose_base_response_goal(decision, SuggestionSnapshot())
        assert "social_persona_ack" in goal


class TestSocialPersonaComposePath:
    def test_healthy_llm_path_does_not_call_social_reply_pool(self) -> None:
        composer = DefaultComposer()
        ctx = _social_ctx(message="تسلم", social_category="thanks")
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": PERSONA_TOPIC_SOCIAL_PERSONA_ACK,
                "social_category": "thanks",
            },
            reason="test",
        )
        result = ActionResult(success=True, data={})

        async def _run() -> None:
            with patch.object(T, "social_reply") as mock_pool:
                with patch.object(
                    composer,
                    "_llm_compose",
                    new=AsyncMock(return_value="رد تجريبي"),
                ):
                    reply = await composer.compose(decision, result, ctx)
                mock_pool.assert_not_called()
            assert reply == "رد تجريبي"
            assert result.data.get("chosen_path") != "social_mirror_emergency_fallback"

        asyncio.run(_run())

    def test_mirror_fallback_only_when_llm_empty(self) -> None:
        composer = DefaultComposer()
        ctx = _social_ctx(message="تسلم", social_category="thanks")
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": PERSONA_TOPIC_SOCIAL_PERSONA_ACK,
                "social_category": "thanks",
            },
            reason="test",
        )
        result = ActionResult(success=True, data={"chosen_path": "llm_fallback_failed"})

        async def _run() -> str:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value=""),
            ):
                return await composer.compose(decision, result, ctx)

        reply = asyncio.run(_run())
        assert result.data.get("chosen_path") == "social_mirror_emergency_fallback"
        assert reply
        assert "social_reply" not in str(result.data.get("chosen_path", ""))

    def test_mirror_not_used_when_llm_succeeds(self) -> None:
        composer = DefaultComposer()
        ctx = _social_ctx(message="تسلم", social_category="thanks")
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": PERSONA_TOPIC_SOCIAL_PERSONA_ACK,
                "social_category": "thanks",
            },
            reason="test",
        )
        result = ActionResult(success=True, data={})

        async def _run() -> None:
            with patch.object(T, "social_mirror_fallback_reply") as mock_mirror:
                with patch.object(
                    composer,
                    "_llm_compose",
                    new=AsyncMock(return_value="رد من النموذج"),
                ):
                    await composer.compose(decision, result, ctx)
                mock_mirror.assert_not_called()

        asyncio.run(_run())


class TestTemplateOnlyCategories:
    def test_condolence_stays_deterministic_template(self) -> None:
        decision = DefaultDecisionEngine().decide(
            _social_ctx(message="الله يرحمه", social_category="condolence"),
        )
        assert decision.action == ACTION_SOCIAL_REPLY
        assert decision.args.get("social_category") == "condolence"

    def test_policy_clamp_religious_media_routes_to_social_template(self) -> None:
        from modules.ai.brain.decision.actions import ACTION_CLARIFY

        gate = RealPolicyGate()
        incoming = Decision(
            action=ACTION_CLARIFY,
            args={"question": "تقصد حاجة؟"},
            reason="test",
            confidence=0.8,
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="صورة دعاء",
            intent=Intent(name="general", confidence=0.5, slots={}),
            state=MerchantConversationState(),
            facts=CommerceFacts(store_name="Test"),
        )
        ctx.block_commerce_escalation = True
        ctx.non_commerce_category = "religious_media"
        out = gate.gate(incoming, ctx)
        assert out.action == ACTION_SOCIAL_REPLY
        assert out.args.get("social_category") == "religious_media"
