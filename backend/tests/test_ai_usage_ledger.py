"""Tests for AI usage cost ledger — pricing, persistence, aggregation."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.ai.orchestrator.ai_usage_ledger import (
    TOKEN_SOURCE_ACTUAL,
    TOKEN_SOURCE_ESTIMATED,
    _aggregate_events,
    extract_anthropic_usage,
    record_ai_usage_event,
    record_ai_usage_from_anthropic,
)
from modules.ai.orchestrator.ai_usage_pricing import (
    PRICING_VERSION,
    compute_usage_cost_usd,
    lookup_model_pricing_v2,
    pricing_tier_for_model,
)


class TestPricingV2:
    def test_sonnet_haiku_opus_costs_differ(self):
        sonnet = compute_usage_cost_usd(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        haiku = compute_usage_cost_usd(
            provider="anthropic",
            model="claude-haiku-4-5",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        opus = compute_usage_cost_usd(
            provider="anthropic",
            model="claude-opus-4-6",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        assert sonnet["total_cost_usd"] == Decimal("3")
        assert haiku["total_cost_usd"] == Decimal("0.80")
        assert opus["total_cost_usd"] == Decimal("15")
        assert sonnet["total_cost_usd"] > haiku["total_cost_usd"]
        assert opus["total_cost_usd"] > sonnet["total_cost_usd"]

    def test_input_output_tokens_compute_separate_costs(self):
        costs = compute_usage_cost_usd(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert costs["input_cost_usd"] == Decimal("3")
        assert costs["output_cost_usd"] == Decimal("15")
        assert costs["total_cost_usd"] == Decimal("18")
        assert costs["pricing_version"] == PRICING_VERSION

    def test_display_rounding_separate_from_storage(self):
        from modules.ai.orchestrator.ai_usage_ledger import _round_display_usd

        stored = Decimal("0.123456789")
        displayed = _round_display_usd(stored)
        assert displayed == 0.123457
        assert stored == Decimal("0.123456789")


class TestTokenSource:
    def test_actual_usage_tags_actual(self):
        response = SimpleNamespace(
            id="msg_123",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=10,
                cache_creation_input_tokens=5,
            ),
        )
        usage, has_actual = extract_anthropic_usage(response=response)
        assert has_actual is True
        assert usage == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
            "cache_write_tokens": 5,
        }

        db = MagicMock()
        record_ai_usage_from_anthropic(
            audit_extra={"tenant_id": 1, "reason": "test.actual"},
            model="claude-sonnet-4-6",
            response=response,
            reply_text="hello",
            total_prompt_chars=400,
            db=db,
        )
        row = db.add.call_args[0][0]
        assert row.token_source == TOKEN_SOURCE_ACTUAL
        assert row.input_tokens == 100
        assert row.output_tokens == 50

    def test_estimated_fallback_tags_estimated(self):
        db = MagicMock()
        record_ai_usage_from_anthropic(
            audit_extra={"tenant_id": 2, "reason": "test.estimated"},
            model="claude-haiku-4-5",
            response=SimpleNamespace(id="msg_x", usage=None),
            reply_text="world",
            total_prompt_chars=800,
            db=db,
        )
        row = db.add.call_args[0][0]
        assert row.token_source == TOKEN_SOURCE_ESTIMATED
        assert row.input_tokens is None
        assert row.estimated_input_tokens == 200
        assert row.estimated_output_tokens == 1


class TestAttribution:
    def test_missing_tenant_is_unattributed_in_aggregation(self):
        event_attributed = SimpleNamespace(
            tenant_id=5,
            token_source=TOKEN_SOURCE_ACTUAL,
            total_cost_usd=Decimal("0.10"),
            input_tokens=100,
            output_tokens=20,
            estimated_input_tokens=None,
            estimated_output_tokens=None,
            model="claude-sonnet-4-6",
            provider="anthropic",
            reason="brain.compose",
        )
        event_unattributed = SimpleNamespace(
            tenant_id=None,
            token_source=TOKEN_SOURCE_ESTIMATED,
            total_cost_usd=Decimal("0.05"),
            input_tokens=None,
            output_tokens=None,
            estimated_input_tokens=50,
            estimated_output_tokens=10,
            model="claude-haiku-4-5",
            provider="anthropic",
            reason="brain.intent.slot_extractor",
        )
        agg = _aggregate_events([event_attributed, event_unattributed])
        assert agg["actual_total_cost_usd"] == 0.10
        assert agg["estimated_total_cost_usd"] == 0.0
        assert agg["unattributed_total_cost_usd"] == 0.05


class TestAggregation:
    def test_group_by_tenant_provider_model_reason(self):
        events = [
            SimpleNamespace(
                tenant_id=1,
                token_source=TOKEN_SOURCE_ACTUAL,
                total_cost_usd=Decimal("0.01"),
                input_tokens=10,
                output_tokens=5,
                estimated_input_tokens=10,
                estimated_output_tokens=5,
                model="claude-sonnet-4-6",
                provider="anthropic",
                reason="brain.compose",
            ),
            SimpleNamespace(
                tenant_id=1,
                token_source=TOKEN_SOURCE_ESTIMATED,
                total_cost_usd=Decimal("0.002"),
                input_tokens=None,
                output_tokens=None,
                estimated_input_tokens=20,
                estimated_output_tokens=4,
                model="claude-haiku-4-5",
                provider="anthropic",
                reason="brain.memory.updater._summarise",
            ),
        ]
        agg = _aggregate_events(events)
        assert agg["calls_total"] == 2
        assert {item["model"] for item in agg["models"]} == {
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        }
        assert agg["providers"] == [{"provider": "anthropic", "count": 2}]
        reasons = {item["reason"]: item["count"] for item in agg["reasons"]}
        assert reasons["brain.compose"] == 1
        assert reasons["brain.memory.updater._summarise"] == 1


class TestNoMessageContent:
    def test_ledger_row_has_no_content_fields(self):
        from database.models import AIUsageEvent

        columns = {col.name for col in AIUsageEvent.__table__.columns}
        forbidden = {"message", "prompt", "content", "body", "response_text"}
        assert forbidden.isdisjoint(columns)


class TestFailSafe:
    def test_ledger_write_failure_does_not_raise(self):
        db = MagicMock()
        db.add.side_effect = RuntimeError("db down")
        record_ai_usage_event(
            tenant_id=1,
            provider="anthropic",
            model="claude-sonnet-4-6",
            reason="test.fail",
            estimated_input_tokens=10,
            estimated_output_tokens=5,
            token_source=TOKEN_SOURCE_ESTIMATED,
            db=db,
        )

    def test_record_from_anthropic_failure_does_not_raise(self):
        with patch(
            "modules.ai.orchestrator.ai_usage_ledger.record_ai_usage_event",
            side_effect=RuntimeError("boom"),
        ):
            record_ai_usage_from_anthropic(
                model="claude-sonnet-4-6",
                response=SimpleNamespace(
                    id="x",
                    usage=SimpleNamespace(
                        input_tokens=1,
                        output_tokens=1,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                    ),
                ),
            )


class TestAnthropicProviderIntegration:
    def test_provider_call_still_returns_on_ledger_failure(self):
        from modules.ai.orchestrator.providers import anthropic_provider

        provider = anthropic_provider.AnthropicProvider()
        fake_response = SimpleNamespace(
            id="msg_ok",
            content=[SimpleNamespace(type="text", text="مرحبا")],
            usage=SimpleNamespace(
                input_tokens=12,
                output_tokens=3,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

        class _AuthError(Exception):
            pass

        class _ConnError(Exception):
            pass

        mock_sdk = MagicMock()
        mock_sdk.Anthropic.return_value.messages.create.return_value = fake_response
        mock_sdk.AuthenticationError = _AuthError
        mock_sdk.APIConnectionError = _ConnError

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            with patch.object(anthropic_provider, "_SDK_AVAILABLE", True):
                with patch.object(anthropic_provider, "_anthropic_sdk", mock_sdk):
                    with patch(
                        "modules.ai.orchestrator.ai_usage_ledger.record_ai_usage_event",
                        side_effect=RuntimeError("ledger boom"),
                    ):
                        result = provider.call("hello", "system prompt")
        assert result["reply_text"] == "مرحبا"
        assert result["status"] == "ok"


def test_pricing_tier_labels():
    assert pricing_tier_for_model("claude-opus-4-6") == "opus"
    assert pricing_tier_for_model("claude-sonnet-4-6") == "sonnet"
    assert pricing_tier_for_model("claude-haiku-4-5") == "haiku"
    assert lookup_model_pricing_v2("anthropic", "claude-opus-4-6").input_per_1m == Decimal("15")
