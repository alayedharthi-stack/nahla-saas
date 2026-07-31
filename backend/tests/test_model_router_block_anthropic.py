"""Block Anthropic full-prompt fallback for routine daily commerce compose."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.prompt_state_serializer import commerce_prompt_max_chars  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.cost.model_router import (  # noqa: E402
    resolve_compose_model_route,
    should_block_anthropic_compose_result,
)
from modules.ai.brain.cost.model_router_audit import TIER_CHEAP  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    Intent,
    MerchantConversationState,
)
from modules.ai.orchestrator.engine import AIOrchestratorEngine  # noqa: E402
from modules.ai.orchestrator.provider_router import ProviderChainConfig  # noqa: E402
from modules.ai.orchestrator.types import AIContext, AIOrchestrationRequest  # noqa: E402


@pytest.fixture(autouse=True)
def _router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP", "gpt-5.6-luna")
    monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "true")


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("هل عندكم عسل طلح؟", INTENT_SOLUTION_SEEKING_COMMERCE),
        ("هل عندكم فساتين؟", INTENT_ASK_PRODUCT),
        ("بكم الطلح؟", INTENT_ASK_PRICE),
        ("متوفر الشاحن؟", INTENT_ASK_PRODUCT),
    ],
)
def test_routine_commerce_route_blocks_anthropic_chain(
    message: str,
    intent: str,
) -> None:
    route = resolve_compose_model_route(
        intent_name=intent,
        reply_state=BrainReplyState(
            store_name="x",
            intent_name=intent,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
        ),
    )
    assert route.tier == TIER_CHEAP
    assert route.block_anthropic_fallback is True
    assert route.provider_chain_override is not None
    assert "anthropic" not in route.provider_chain_override
    assert route.provider == "openai_compatible"
    assert route.model == "gpt-5.6-luna"


def test_should_block_anthropic_compose_result() -> None:
    route = resolve_compose_model_route(
        intent_name=INTENT_ASK_PRODUCT,
        reply_state=BrainReplyState(
            store_name="x",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
        ),
    )
    assert should_block_anthropic_compose_result(route=route, provider_used="anthropic")
    assert not should_block_anthropic_compose_result(
        route=route, provider_used="openai_compatible"
    )


def test_engine_skips_hard_anthropic_fallback_when_blocked() -> None:
    engine = AIOrchestratorEngine()
    request = AIOrchestrationRequest(
        context=AIContext(channel="whatsapp", tenant_id=7),
        message="هل عندكم عسل طلح؟",
        prompt_overrides={
            "__model_router": {
                "tier": "cheap",
                "block_anthropic_fallback": True,
                "provider_chain_override": ["openai_compatible", "gemini"],
            },
        },
    )
    chain = ProviderChainConfig(providers=["openai_compatible", "gemini"])

    empty_provider = MagicMock()
    empty_provider.provider_name = "openai_compatible"
    empty_provider.is_configured.return_value = True
    empty_provider.call.return_value = {"reply_text": "", "provider": "openai_compatible"}

    gemini_provider = MagicMock()
    gemini_provider.provider_name = "gemini"
    gemini_provider.is_configured.return_value = True
    gemini_provider.call.return_value = {"reply_text": "", "provider": "gemini"}

    anthropic_provider = MagicMock()
    anthropic_provider.provider_name = "anthropic"
    anthropic_provider.call.return_value = {
        "reply_text": "blocked should not run",
        "provider": "anthropic",
    }

    def _get_provider(name: str):
        return {
            "openai_compatible": empty_provider,
            "gemini": gemini_provider,
            "anthropic": anthropic_provider,
        }.get(name)

    with patch("modules.ai.orchestrator.engine.get_provider", side_effect=_get_provider):
        with patch("modules.ai.orchestrator.engine.call_with_resilience") as mock_resilience:
            mock_resilience.side_effect = lambda _name, fn, timeout=30: fn()
            result = engine._call_with_chain(request, "prompt", chain)

    assert result.get("reply_text") == ""
    assert result.get("anthropic_fallback_blocked") is True
    anthropic_provider.call.assert_not_called()


def test_llm_compose_rejects_anthropic_for_routine_commerce() -> None:
    import asyncio  # noqa: PLC0415

    async def _run() -> str:
        composer = DefaultComposer()
        ctx = BrainContext(
            tenant_id=7,
            customer_phone="+966555555555",
            message="هل عندكم عسل طلح؟",
            intent=Intent(
                name=INTENT_SOLUTION_SEEKING_COMMERCE,
                confidence=0.95,
                raw_message="هل عندكم عسل طلح؟",
            ),
            state=MerchantConversationState(),
            facts=CommerceFacts(
                has_products=True,
                product_count=5,
                orderable=True,
                store_name="متجر",
            ),
            reply_state=BrainReplyState(
                store_name="متجر",
                intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            ),
        )
        result = ActionResult(success=True, data={})

        anthropic_payload = MagicMock()
        anthropic_payload.reply_text = "رد من anthropic"
        anthropic_payload.provider_used = "anthropic"
        anthropic_payload.metadata = {"model": "claude-haiku-4-5"}

        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=anthropic_payload,
        ):
            with patch.object(
                composer,
                "_thin_compose_retry",
                return_value="رد آمن محلي",
            ) as mock_thin:
                reply = await composer._llm_compose(ctx, result)

        assert reply == "رد آمن محلي"
        mock_thin.assert_called_once()
        return reply

    asyncio.run(_run())


def test_routine_commerce_prompt_within_limit_when_slim_enabled() -> None:
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
    from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: E402
        explain_commerce_prompt_slim_gate,
    )

    rs = BrainReplyState(
        store_name="متجر",
        intent_name=INTENT_ASK_PRODUCT,
        primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
    )
    slim_applied, _, _ = explain_commerce_prompt_slim_gate(rs)
    assert slim_applied is True
    prompt = build_brain_reply_prompt(rs)
    assert len(prompt) <= commerce_prompt_max_chars()
