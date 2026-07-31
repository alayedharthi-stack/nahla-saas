"""OpenAI-only customer chat model migration — routing, escalation, telemetry."""
from __future__ import annotations

import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.cost.model_router import resolve_compose_model_route  # noqa: E402
from modules.ai.brain.cost.model_router_audit import (  # noqa: E402
    TIER_CHEAP,
    TIER_PREMIUM,
    TIER_STANDARD,
    audit_luna_pricing_v2,
    is_premium_model_allowed,
)
from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: E402
from modules.ai.brain.types import INTENT_ASK_PRODUCT, INTENT_TALK_HUMAN  # noqa: E402
from modules.ai.orchestrator.ai_usage_pricing import (  # noqa: E402
    lookup_model_pricing_v2,
    pricing_tier_for_model,
)
from modules.ai.orchestrator.customer_chat_models import (  # noqa: E402
    MODEL_LUNA,
    MODEL_SOL,
    MODEL_TERRA,
    openai_only_provider_chain,
    resolve_default_customer_chat_model,
    technical_escalation_models,
)
from modules.ai.orchestrator.engine import AIOrchestratorEngine  # noqa: E402
from modules.ai.orchestrator.provider_router import DEFAULT_PROVIDER_CHAIN  # noqa: E402
from modules.ai.orchestrator.provider_router import ProviderChainConfig  # noqa: E402
from modules.ai.orchestrator.types import AIContext, AIOrchestrationRequest  # noqa: E402


class TestModelSlugDefaults:
    def test_default_route_luna(self) -> None:
        assert resolve_default_customer_chat_model() == MODEL_LUNA

    def test_openai_only_provider_chain(self) -> None:
        assert openai_only_provider_chain() == ("openai_compatible",)
        assert "anthropic" not in DEFAULT_PROVIDER_CHAIN
        assert "gemini" not in DEFAULT_PROVIDER_CHAIN
        assert DEFAULT_PROVIDER_CHAIN[0] == "openai_compatible"


class TestSemanticEscalation:
    def test_standard_escalation_terra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        route = resolve_compose_model_route(
            intent_name=INTENT_TALK_HUMAN,
            decision_action=ACTION_HANDOFF,
        )
        assert route.tier == TIER_STANDARD
        assert route.model == MODEL_TERRA
        assert route.provider == "openai_compatible"

    def test_premium_allowed_sol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        monkeypatch.setenv("ALLOW_PREMIUM_MODEL", "true")
        route = resolve_compose_model_route(intent_name="premium_explicit")
        assert route.tier == TIER_PREMIUM
        assert route.model == MODEL_SOL

    def test_premium_blocked_no_sol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        monkeypatch.delenv("ALLOW_PREMIUM_MODEL", raising=False)
        route = resolve_compose_model_route(intent_name="premium_explicit")
        assert route.tier != TIER_PREMIUM

    def test_cheap_route_luna(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        route = resolve_compose_model_route(intent_name=INTENT_ASK_PRODUCT)
        assert route.tier == TIER_CHEAP
        assert route.model == MODEL_LUNA


class TestTechnicalEscalation:
    def test_luna_failure_escalates_terra(self) -> None:
        chain = technical_escalation_models(MODEL_LUNA)
        assert chain == [MODEL_TERRA]

    def test_terra_failure_no_sol_when_premium_blocked(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ALLOW_PREMIUM_MODEL", raising=False)
        assert technical_escalation_models(MODEL_TERRA) == []

    def test_terra_failure_sol_when_premium_allowed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALLOW_PREMIUM_MODEL", "true")
        assert technical_escalation_models(MODEL_TERRA) == [MODEL_SOL]


class TestEngineNoAnthropicGemini:
    def test_chain_skips_anthropic_and_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        engine = AIOrchestratorEngine()
        request = AIOrchestrationRequest(
            context=AIContext(tenant_id=1),
            message="مرحبا",
            prompt_overrides={
                "__model_router": {
                    "model": MODEL_LUNA,
                    "tier": TIER_CHEAP,
                    "block_anthropic_fallback": True,
                },
            },
        )
        chain = ProviderChainConfig(
            providers=["anthropic", "gemini", "openai_compatible"],
        )
        with patch.object(engine, "_invoke_provider_call", return_value=None) as mock_invoke:
            with patch(
                "modules.ai.orchestrator.engine.call_with_resilience",
                return_value=None,
            ):
                with patch.object(
                    engine, "_try_technical_model_escalation", return_value={"reply_text": ""},
                ):
                    result = engine._call_with_chain(request, "system", chain)
        assert mock_invoke.call_count == 0
        assert result.get("status") in {"openai_chain_exhausted", "blocked_anthropic_fallback"}
        assert result.get("reply_text") == ""

    def test_technical_escalation_luna_to_terra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        engine = AIOrchestratorEngine()
        request = AIOrchestrationRequest(context=AIContext(tenant_id=1), message="test")
        observer = MagicMock()
        with patch.object(
            engine._provider,
            "is_configured",
            return_value=True,
        ):
            with patch.object(
                engine,
                "_invoke_provider_call",
                return_value={"reply_text": "ok", "model": MODEL_TERRA, "provider": "openai_compatible"},
            ):
                result = engine._try_technical_model_escalation(
                    request_obj=request,
                    prompt="system",
                    observer=observer,
                    audit_context={"tenant_id": 1},
                    requested_model=MODEL_LUNA,
                )
        assert result.get("reply_text") == "ok"
        assert result.get("model_escalation") is True


class TestCostAuditPricing:
    def test_luna_terra_sol_pricing_recognized(self) -> None:
        for model in (MODEL_LUNA, MODEL_TERRA, MODEL_SOL):
            pricing = lookup_model_pricing_v2("openai_compatible", model)
            assert pricing.input_per_1m > 0
            assert pricing.output_per_1m > 0
            assert pricing_tier_for_model(model) in {"luna", "terra", "sol"}

    def test_audit_luna_pricing_ok(self) -> None:
        assert audit_luna_pricing_v2()["pricing_ok"] is True


class TestTelemetry:
    def test_customer_chat_model_telemetry_no_content(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from modules.ai.orchestrator.customer_chat_models import (  # noqa: PLC0415
            emit_customer_chat_model_telemetry,
        )

        secret = "رسالة سرية للعميل"
        caplog.set_level(logging.INFO, logger="nahla.ai.customer_chat_models")
        emit_customer_chat_model_telemetry(
            provider="openai_compatible",
            requested_model=MODEL_LUNA,
            actual_model=MODEL_TERRA,
            escalation_reason="technical_failure",
        )
        joined = "\n".join(r.message for r in caplog.records)
        assert secret not in joined
        assert "[CUSTOMER_CHAT_MODEL]" in joined
        payload = json.loads(joined.split("[CUSTOMER_CHAT_MODEL] ", 1)[1])
        assert payload["requested_model"] == MODEL_LUNA
        assert payload["actual_model"] == MODEL_TERRA
