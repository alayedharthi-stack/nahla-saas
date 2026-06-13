"""Ledger integration for OpenAI-compatible and Gemini fallback providers."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.ai.orchestrator.ai_usage_ledger import (
    TOKEN_SOURCE_ACTUAL,
    TOKEN_SOURCE_ESTIMATED,
    extract_gemini_usage,
    extract_openai_usage,
    record_ai_usage_from_gemini,
    record_ai_usage_from_openai_compatible,
)
from modules.ai.orchestrator.ai_usage_pricing import compute_usage_cost_usd
from modules.ai.orchestrator.providers.gemini_provider import GeminiProvider
from modules.ai.orchestrator.providers.openai_compatible_provider import OpenAICompatibleProvider


class TestOpenAICompatibleLedger:
    def test_successful_response_writes_ledger_row(self):
        db = MagicMock()
        data = {
            "id": "chatcmpl-abc",
            "choices": [{"message": {"content": "مرحبا"}}],
            "usage": {"prompt_tokens": 19000, "completion_tokens": 148, "total_tokens": 19148},
        }
        record_ai_usage_from_openai_compatible(
            audit_extra={
                "tenant_id": 33,
                "conversation_id": 100,
                "turn_id": 7,
                "reason": "brain.compose._llm_compose",
            },
            model="gpt-4o-mini",
            httpx_data=data,
            reply_text="مرحبا",
            total_prompt_chars=76000,
            db=db,
        )
        row = db.add.call_args[0][0]
        assert row.provider == "openai_compatible"
        assert row.model == "gpt-4o-mini"
        assert row.tenant_id == 33
        assert row.conversation_id == 100
        assert row.turn_id == 7
        assert row.token_source == TOKEN_SOURCE_ACTUAL
        assert row.input_tokens == 19000
        assert row.output_tokens == 148
        assert row.request_id == "chatcmpl-abc"

    def test_actual_openai_usage_cost(self):
        costs = compute_usage_cost_usd(
            provider="openai_compatible",
            model="gpt-4o-mini",
            input_tokens=19000,
            output_tokens=148,
        )
        expected_input = Decimal("19000") * Decimal("0.15") / Decimal("1000000")
        expected_output = Decimal("148") * Decimal("0.60") / Decimal("1000000")
        assert costs["input_cost_usd"] == expected_input
        assert costs["output_cost_usd"] == expected_output
        assert costs["total_cost_usd"] == expected_input + expected_output

    def test_estimated_fallback_when_usage_missing(self):
        db = MagicMock()
        record_ai_usage_from_openai_compatible(
            audit_extra={
                "tenant_id": 33,
                "estimated_input_tokens": 19148,
                "reason": "brain.compose._llm_compose",
            },
            model="gpt-4o-mini",
            httpx_data={"id": "chatcmpl-x", "choices": [{"message": {"content": "ok"}}]},
            reply_text="ok",
            total_prompt_chars=76592,
            db=db,
        )
        row = db.add.call_args[0][0]
        assert row.token_source == TOKEN_SOURCE_ESTIMATED
        assert row.input_tokens is None
        assert row.estimated_input_tokens == 19148

    def test_provider_call_writes_ledger_without_message_content(self):
        provider = OpenAICompatibleProvider()
        fake_data = {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "رد"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status.return_value = None

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch(
                "modules.ai.orchestrator.providers.openai_compatible_provider._API_KEY",
                "test-key",
            ):
                with patch("httpx.Client") as client_cls:
                    client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
                    with patch(
                        "modules.ai.orchestrator.providers.openai_compatible_provider.record_ai_usage_from_openai_compatible",
                    ) as ledger:
                        result = provider.call(
                            "hello",
                            "system",
                            audit_context={"tenant_id": 1, "reason": "test.openai"},
                        )
        assert result["status"] == "ok"
        ledger.assert_called_once()
        kwargs = ledger.call_args.kwargs
        assert kwargs["audit_extra"]["tenant_id"] == 1
        assert "hello" not in str(kwargs)
        assert "system" not in str(kwargs.get("httpx_data", {}))


class TestGeminiLedger:
    def test_successful_response_writes_ledger_row(self):
        db = MagicMock()
        data = {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 500,
                "candidatesTokenCount": 20,
                "totalTokenCount": 520,
            },
        }
        record_ai_usage_from_gemini(
            audit_extra={"tenant_id": 5, "reason": "gemini.test"},
            model="gemini-1.5-flash",
            httpx_data=data,
            reply_text="ok",
            total_prompt_chars=2000,
            db=db,
        )
        row = db.add.call_args[0][0]
        assert row.provider == "gemini"
        assert row.model == "gemini-1.5-flash"
        assert row.token_source == TOKEN_SOURCE_ACTUAL
        assert row.input_tokens == 500
        assert row.output_tokens == 20

    def test_estimated_fallback_when_usage_missing(self):
        db = MagicMock()
        record_ai_usage_from_gemini(
            audit_extra={"tenant_id": 5, "estimated_input_tokens": 400},
            model="gemini-1.5-flash",
            httpx_data={"candidates": [{"content": {"parts": [{"text": "x"}]}}]},
            reply_text="x",
            total_prompt_chars=1600,
            db=db,
        )
        row = db.add.call_args[0][0]
        assert row.token_source == TOKEN_SOURCE_ESTIMATED

    def test_provider_call_path_ready(self):
        provider = GeminiProvider()
        fake_data = {
            "candidates": [{"content": {"parts": [{"text": "gemini reply"}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status.return_value = None

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False):
            with patch(
                "modules.ai.orchestrator.providers.gemini_provider._API_KEY",
                "test-key",
            ):
                with patch("httpx.Client") as client_cls:
                    client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
                    with patch(
                        "modules.ai.orchestrator.providers.gemini_provider.record_ai_usage_from_gemini",
                    ) as ledger:
                        result = provider.call("hi", "sys", audit_context={"tenant_id": 2})
        assert result["reply_text"] == "gemini reply"
        ledger.assert_called_once()


class TestUsageExtractors:
    def test_extract_openai_usage(self):
        usage, has_actual = extract_openai_usage(
            httpx_data={"usage": {"prompt_tokens": 10, "completion_tokens": 4}},
        )
        assert has_actual is True
        assert usage == {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    def test_extract_gemini_usage(self):
        usage, has_actual = extract_gemini_usage(
            httpx_data={"usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 2}},
        )
        assert has_actual is True
        assert usage["input_tokens"] == 8
        assert usage["output_tokens"] == 2


class TestProviderChainFallback:
    def test_anthropic_failure_openai_success_records_openai_cost(self):
        from modules.ai.orchestrator.engine import AIOrchestratorEngine
        from modules.ai.orchestrator.provider_router import ProviderChainConfig
        from modules.ai.orchestrator.types import AIOrchestrationRequest, AIContext

        class AnthropicStub:
            provider_name = "anthropic"

            def is_configured(self):
                return True

            def call(self, message, prompt, *, history=None, audit_context=None):
                return {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "reply_text": "",
                    "status": "rate_limit",
                }

        class OpenAIStub:
            provider_name = "openai_compatible"
            captured_audit = {}

            def is_configured(self):
                return True

            def call(self, message, prompt, *, history=None, audit_context=None):
                OpenAIStub.captured_audit = dict(audit_context or {})
                return {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "reply_text": "fallback ok",
                    "status": "ok",
                }

        anthropic = AnthropicStub()
        openai = OpenAIStub()

        def _get_provider(name):
            return {"anthropic": anthropic, "openai_compatible": openai}.get(name)

        request = AIOrchestrationRequest(
            message="test",
            context=AIContext(tenant_id=33, store_name="shop"),
            prompt_overrides={
                "__llm_cost_audit": {
                    "conversation_id": 55,
                    "turn_id": 3,
                    "reason": "brain.compose._llm_compose",
                    "estimated_input_tokens": 19148,
                },
            },
        )

        engine = AIOrchestratorEngine()
        chain = ProviderChainConfig(providers=["anthropic", "openai_compatible"], hint="default")

        with patch("modules.ai.orchestrator.engine.get_provider", side_effect=_get_provider):
            with patch(
                "modules.ai.orchestrator.engine.call_with_resilience",
                side_effect=lambda _n, fn, timeout=None: fn(),
            ):
                raw = engine._call_with_chain(request, "system prompt", chain)

        assert raw["provider"] == "openai_compatible"
        assert raw["reply_text"] == "fallback ok"
        assert OpenAIStub.captured_audit["tenant_id"] == 33
        assert OpenAIStub.captured_audit["estimated_input_tokens"] == 19148
        assert OpenAIStub.captured_audit["reason"] == "brain.compose._llm_compose"

    def test_no_message_content_in_ledger_model(self):
        from database.models import AIUsageEvent

        columns = {col.name for col in AIUsageEvent.__table__.columns}
        assert "message" not in columns
        assert "prompt" not in columns
        assert "content" not in columns
