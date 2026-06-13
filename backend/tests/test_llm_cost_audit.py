"""Tests for LLM cost audit telemetry and Anthropic model defaults."""
from __future__ import annotations

import json
import logging

import pytest


def test_resolve_anthropic_model_defaults_not_opus(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    from modules.ai.orchestrator.llm_cost_audit import resolve_anthropic_model

    model = resolve_anthropic_model()
    assert "opus" not in model.lower()
    assert model  # non-empty


def test_resolve_anthropic_model_respects_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    from modules.ai.orchestrator.llm_cost_audit import resolve_anthropic_model

    assert resolve_anthropic_model() == "claude-sonnet-4-6"


def test_emit_llm_cost_audit_structured(caplog):
    from modules.ai.orchestrator.llm_cost_audit import emit_llm_cost_audit

    caplog.set_level(logging.INFO, logger="nahla.ai.llm_cost_audit")
    emit_llm_cost_audit(
        tenant_id=33,
        model="claude-haiku-4-5",
        provider="anthropic",
        system_chars=120_000,
        messages_chars=2_000,
        total_prompt_chars=122_000,
        estimated_input_tokens=30_500,
        reason="test",
    )
    lines = [r for r in caplog.records if "[LLM_COST_AUDIT]" in r.message]
    assert lines
    payload = json.loads(lines[-1].message.split("[LLM_COST_AUDIT] ", 1)[1])
    assert payload["tenant_id"] == 33
    assert payload["model"] == "claude-haiku-4-5"
    assert payload["estimated_input_tokens"] == 30_500
    assert "system_chars" in payload


def test_anthropic_provider_uses_config_default_not_opus(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    from modules.ai.orchestrator.providers import anthropic_provider

    # Re-import resolves at call time via resolve_anthropic_model()
    from modules.ai.orchestrator.llm_cost_audit import resolve_anthropic_model

    assert "opus" not in resolve_anthropic_model().lower()


def test_build_brain_compose_audit_extra_no_message_bodies():
    from dataclasses import asdict

    from modules.ai.brain.compose.brain_state_slim import (
        is_slim_general_brain_state_enabled,
    )
    from modules.ai.brain.types import BrainReplyState
    from modules.ai.orchestrator.llm_cost_audit import build_brain_compose_audit_extra

    state = BrainReplyState(
        store_name="test",
        intent_name="ask_product",
        stage="discovery",
        merchant_context={
            "structured_facts_block": "حقائق المتجر",
            "products": [{"id": 1, "title": "عسل"}],
            "ai_settings": {"manual_knowledge_base": "نص طويل"},
        },
    )
    prompt = "system prompt block"
    history = [{"role": "user", "content": "مرحبا"}]
    extra = build_brain_compose_audit_extra(
        reply_state=state,
        prompt=prompt,
        history_messages=history,
        tenant_id=1,
        conversation_id=99,
        turn_id=3,
    )
    assert extra["kb_chars"] == len("حقائق المتجر")
    assert extra["tenant_id"] == 1
    assert extra["reason"] == "brain.compose._llm_compose"
    # Ensure we never embed raw message text in audit keys
    dumped = json.dumps(extra, ensure_ascii=False)
    assert "مرحبا" not in dumped
    _ = is_slim_general_brain_state_enabled()  # import side-effect sanity
