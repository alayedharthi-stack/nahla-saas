"""PR0 — model router audit scaffold (read-only, no behavior change)."""
from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from modules.ai.brain.cost.model_router_audit import (
    TIER_CHEAP,
    TIER_NONE,
    TIER_PREMIUM,
    TIER_STANDARD,
    TIER_TINY,
    audit_gpt4o_mini_pricing_v2,
    emit_model_router_audit,
    is_model_router_audit_enabled,
    is_premium_model_allowed,
    maybe_audit_model_router,
    suggest_model_tier,
)


class TestTierSuggestions:
    def test_greeting_suggests_none(self):
        s = suggest_model_tier(call_site="brain.compose._llm_compose", intent_name="greeting")
        assert s.tier == TIER_NONE

    def test_social_thanks_suggests_none(self):
        s = suggest_model_tier(
            call_site="brain.compose._llm_compose",
            intent_name="social",
            social_category="thanks",
        )
        assert s.tier == TIER_NONE

    def test_slot_extractor_suggests_tiny(self):
        s = suggest_model_tier(call_site="brain.intent.slot_extractor")
        assert s.tier == TIER_TINY
        assert s.suggested_model == "gpt-4o-mini"

    def test_memory_summarise_suggests_tiny(self):
        s = suggest_model_tier(call_site="brain.memory.updater._summarise")
        assert s.tier == TIER_TINY

    def test_commerce_suggests_cheap(self):
        s = suggest_model_tier(
            call_site="brain.compose._llm_compose",
            intent_name="solution_seeking_commerce",
        )
        assert s.tier == TIER_CHEAP
        assert s.suggested_provider == "openai_compatible"

    def test_escalation_suggests_standard(self):
        s = suggest_model_tier(
            call_site="brain.compose._llm_compose",
            intent_name="talk_to_human",
        )
        assert s.tier == TIER_STANDARD
        assert "claude-sonnet" in (s.suggested_model or "")

    def test_premium_disabled_by_default(self):
        assert is_premium_model_allowed() is False
        s = suggest_model_tier(
            call_site="brain.compose._llm_compose",
            intent_name="premium_explicit",
        )
        assert s.tier != TIER_PREMIUM


class TestGpt4oMiniPricingAudit:
    def test_pricing_matches_reference(self):
        check = audit_gpt4o_mini_pricing_v2()
        assert check["model"] == "gpt-4o-mini"
        assert check["pricing_ok"] is True
        assert Decimal(check["input_per_1m_usd"]) == Decimal("0.15")
        assert Decimal(check["output_per_1m_usd"]) == Decimal("0.60")


class TestAuditLogging:
    def test_disabled_by_default_no_log(self, caplog):
        caplog.set_level(logging.INFO, logger="nahla.ai.brain.cost.model_router")
        emit_model_router_audit(call_site="test", tier=TIER_CHEAP)
        assert not any("[MODEL_ROUTER_AUDIT]" in r.message for r in caplog.records)

    def test_enabled_emits_audit_with_pricing_check(self, monkeypatch, caplog):
        monkeypatch.setenv("NAHLA_MODEL_ROUTER_AUDIT_ENABLED", "true")
        caplog.set_level(logging.INFO, logger="nahla.ai.brain.cost.model_router")
        emit_model_router_audit(call_site="brain.compose._llm_compose", tier=TIER_CHEAP)
        lines = [r.message for r in caplog.records if "[MODEL_ROUTER_AUDIT]" in r.message]
        assert len(lines) == 1
        assert "gpt4o_mini_pricing_check" in lines[0]
        assert "behavior_change" not in lines[0] or "false" in lines[0].lower()

    def test_maybe_audit_never_raises_when_disabled(self):
        maybe_audit_model_router(call_site="brain.intent.slot_extractor")
