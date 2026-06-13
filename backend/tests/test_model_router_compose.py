"""PR1 — commerce compose cheap-first model router enforcement."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.cost.model_router import (  # noqa: E402
    compose_route_skips_llm,
    is_model_router_enabled,
    resolve_compose_model_route,
)
from modules.ai.brain.cost.model_router_audit import (  # noqa: E402
    TIER_CHEAP,
    TIER_PREMIUM,
    TIER_STANDARD,
)
from modules.ai.brain.decision.actions import ACTION_GREET, ACTION_HANDOFF  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    INTENT_ASK_PRODUCT,
    INTENT_GREETING,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    INTENT_SOCIAL,
    INTENT_TALK_HUMAN,
    Intent,
    MerchantConversationState,
)
from modules.ai.orchestrator.pipeline import AIOrchestrationPipeline  # noqa: E402
from modules.ai.orchestrator.provider_router import DEFAULT_PROVIDER_CHAIN  # noqa: E402
from modules.ai.orchestrator.types import AIContext, AIOrchestrationRequest  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_routine_avoid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "true")


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        orderable=True,
        store_name="متجر تجريبي",
    )


def _ctx(
    *,
    intent_name: str,
    message: str = "test",
    reply_state: BrainReplyState | None = None,
    human_priority: bool = False,
) -> BrainContext:
    rs = reply_state or BrainReplyState(
        store_name="متجر تجريبي",
        intent_name=intent_name,
    )
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=message,
        intent=Intent(name=intent_name, confidence=0.95, raw_message=message),
        state=MerchantConversationState(),
        facts=_facts(),
        reply_state=rs,
        human_priority=human_priority,
    )


class TestResolveComposeModelRoute:
    def test_router_disabled_keeps_legacy_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NAHLA_MODEL_ROUTER_ENABLED", raising=False)
        route = resolve_compose_model_route(intent_name=INTENT_ASK_PRODUCT)
        assert route.enforced is False
        assert route.provider_hint == "anthropic"
        assert route.provider_chain_override is None

    def test_router_enabled_commerce_cheap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        for intent in (INTENT_ASK_PRODUCT, INTENT_SOLUTION_SEEKING_COMMERCE):
            route = resolve_compose_model_route(intent_name=intent)
            assert route.enforced is True
            assert route.tier == TIER_CHEAP
            assert route.provider == "openai_compatible"
            assert route.model == "gpt-4o-mini"
            assert route.provider_hint == "openai_compatible"
            assert route.provider_chain_override[0] == "openai_compatible"

    def test_router_enabled_escalation_standard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        monkeypatch.setenv("NAHLA_MODEL_STANDARD", "claude-sonnet-4-6")
        route = resolve_compose_model_route(
            intent_name=INTENT_TALK_HUMAN,
            decision_action=ACTION_HANDOFF,
        )
        assert route.enforced is True
        assert route.tier == TIER_STANDARD
        assert route.provider == "anthropic"
        assert route.model == "claude-sonnet-4-6"
        assert route.provider_hint == "anthropic"

    def test_premium_opus_only_when_explicitly_enabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        disabled = resolve_compose_model_route(intent_name="premium_explicit")
        assert disabled.tier != TIER_PREMIUM

        monkeypatch.setenv("ALLOW_PREMIUM_MODEL", "true")
        enabled = resolve_compose_model_route(intent_name="premium_explicit")
        assert enabled.tier == TIER_PREMIUM
        assert "opus" in enabled.model.lower()


class TestLlmComposeIntegration:
    async def _run_llm_compose(
        self,
        ctx: BrainContext,
        *,
        decision: Decision | None = None,
    ) -> dict:
        composer = DefaultComposer()
        result = ActionResult(success=True, data={})
        captured: dict = {}

        def _fake_generate(**kwargs):
            captured.update(kwargs)
            payload = MagicMock()
            payload.reply_text = "رد تجريبي"
            payload.provider_used = kwargs.get("provider_hint") or "anthropic"
            payload.metadata = {"model": "mock-model"}
            return payload

        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            side_effect=_fake_generate,
        ):
            await composer._llm_compose(ctx, result, decision=decision)
        return captured

    def test_llm_compose_disabled_router_uses_anthropic(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NAHLA_MODEL_ROUTER_ENABLED", raising=False)
        ctx = _ctx(intent_name=INTENT_ASK_PRODUCT, message="هل عندكم عسل؟")
        captured = asyncio.run(self._run_llm_compose(ctx))
        assert captured["provider_hint"] == "anthropic"
        assert "__model_router" not in captured["prompt_overrides"]

    def test_llm_compose_enabled_commerce_uses_cheap(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        ctx = _ctx(intent_name=INTENT_ASK_PRODUCT, message="هل عندكم عسل طلح؟")
        captured = asyncio.run(self._run_llm_compose(ctx))
        assert captured["provider_hint"] == "openai_compatible"
        router = captured["prompt_overrides"]["__model_router"]
        assert router["tier"] == TIER_CHEAP
        assert router["provider"] == "openai_compatible"
        assert router["model"] == "gpt-4o-mini"
        audit = captured["prompt_overrides"]["__llm_cost_audit"]
        assert audit["model_tier"] == TIER_CHEAP
        assert audit["model_override"] == "gpt-4o-mini"

    def test_llm_compose_enabled_escalation_uses_standard(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        monkeypatch.setenv("NAHLA_MODEL_STANDARD", "claude-sonnet-4-6")
        ctx = _ctx(intent_name=INTENT_TALK_HUMAN, message="أبي أتكلم مع موظف")
        captured = asyncio.run(
            self._run_llm_compose(
                ctx,
                decision=Decision(action=ACTION_HANDOFF, reason="handoff"),
            ),
        )
        assert captured["provider_hint"] == "anthropic"
        router = captured["prompt_overrides"]["__model_router"]
        assert router["tier"] == TIER_STANDARD
        assert router["model"] == "claude-sonnet-4-6"


class TestNoLlmSocialPaths:
    @pytest.mark.parametrize(
        ("message", "intent_name", "category"),
        [
            ("هلا", INTENT_GREETING, ""),
            ("شكرا", INTENT_SOCIAL, "thanks"),
            ("جزاك الله خير", INTENT_SOCIAL, "thanks"),
        ],
    )
    def test_routine_paths_skip_llm(
        self,
        message: str,
        intent_name: str,
        category: str,
    ) -> None:
        assert compose_route_skips_llm(
            intent_name=intent_name,
            social_category=category,
        )

    def test_greet_compose_does_not_call_llm(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx(intent_name=INTENT_GREETING, message="هلا")
        ctx.intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="هلا")
        decision = Decision(action=ACTION_GREET, reason="test")
        result = ActionResult(success=True, data={})

        async def _run() -> None:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value="must not call"),
            ) as mock_llm:
                await composer.compose(decision, result, ctx)
            mock_llm.assert_not_called()

        asyncio.run(_run())


class TestProviderChainGlobalUnchanged:
    def test_default_provider_chain_unchanged(self) -> None:
        assert DEFAULT_PROVIDER_CHAIN == [
            "anthropic",
            "openai_compatible",
            "gemini",
            "mock",
        ]

    def test_per_request_override_does_not_mutate_default(self) -> None:
        pipeline = AIOrchestrationPipeline()
        request = AIOrchestrationRequest(
            context=AIContext(tenant_id=7),
            prompt_overrides={
                "__model_router": {
                    "provider_chain_override": ["openai_compatible", "anthropic", "gemini"],
                },
            },
            provider_hint="openai_compatible",
        )
        chain = pipeline.resolve_provider_chain(request)
        assert chain.providers[0] == "openai_compatible"
        assert DEFAULT_PROVIDER_CHAIN[0] == "anthropic"


class TestLedgerRouting:
    def test_openai_provider_records_router_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modules.ai.orchestrator.providers.openai_compatible_provider as openai_mod  # noqa: PLC0415

        monkeypatch.setattr(openai_mod, "_API_KEY", "test-key")
        provider = openai_mod.OpenAICompatibleProvider()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "رد"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        fake_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client, patch(
            "modules.ai.orchestrator.providers.openai_compatible_provider.record_ai_usage_from_openai_compatible",
        ) as mock_ledger:
            mock_client.return_value.__enter__.return_value.post.return_value = fake_resp
            provider.call(
                "هل عندكم عسل؟",
                "system prompt",
                audit_context={
                    "tenant_id": 7,
                    "model_override": "gpt-4o-mini",
                    "model_tier": TIER_CHEAP,
                    "reason": "brain.compose._llm_compose",
                },
            )
        mock_ledger.assert_called_once()
        assert mock_ledger.call_args.kwargs["model"] == "gpt-4o-mini"


class TestAuditLoggingSafety:
    def test_router_audit_never_logs_customer_message(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from modules.ai.brain.cost.model_router_audit import maybe_audit_model_router  # noqa: PLC0415

        secret = "هل عندكم عسل طلح السري؟"
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_AUDIT_ENABLED", "true")
        caplog.set_level(logging.INFO, logger="nahla.ai.brain.cost.model_router")
        maybe_audit_model_router(
            call_site="brain.compose._llm_compose",
            intent_name=INTENT_ASK_PRODUCT,
            extra={"model_tier": TIER_CHEAP, "behavior_change": True},
        )
        joined = "\n".join(r.message for r in caplog.records)
        assert secret not in joined
        assert "[MODEL_ROUTER_AUDIT]" in joined


class TestRouterFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NAHLA_MODEL_ROUTER_ENABLED", raising=False)
        assert is_model_router_enabled() is False

    def test_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        assert is_model_router_enabled() is True
