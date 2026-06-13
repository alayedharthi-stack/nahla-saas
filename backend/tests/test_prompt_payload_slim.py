"""Tests for routine Brain prompt size reduction."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict

import pytest

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.compose.prompt_payload_slim import (
    cap_kb_for_prompt,
    is_routine_social_turn,
    max_kb_prompt_chars,
    resolve_kb_block_for_prompt,
    strip_state_dict_for_prompt,
)
from modules.ai.brain.types import BrainReplyState, INTENT_ASK_PRODUCT
from modules.ai.orchestrator.llm_cost_audit import emit_llm_cost_audit


_LARGE_KB = "حقائق المتجر.\n" * 4000
_LARGE_MANUAL = "نص معرفة يدوي.\n" * 3000


def _state(**overrides) -> BrainReplyState:
    base = dict(
        store_name="متجر",
        intent_name="greeting",
        stage="exploring",
        response_goal="رد ودي",
        merchant_context={
            "tenant_id": 1,
            "structured_facts_block": _LARGE_KB,
            "products": [{"id": 1, "title": "عسل"}],
            "resolver_overlay": "[PRODUCT:1]",
            "ai_settings": {"manual_knowledge_base": _LARGE_MANUAL},
        },
    )
    base.update(overrides)
    return BrainReplyState(**base)


def test_greeting_does_not_inject_kb_or_catalog_in_prompt():
    state = _state(intent_name="greeting")
    prompt = build_brain_reply_prompt(state)
    assert "حقائق المتجر" not in prompt
    assert "[PRODUCT:1]" not in prompt
    assert len(prompt) < 40_000


def test_manual_knowledge_not_in_brain_state_json_when_kb_block_present():
    state = _state(intent_name=INTENT_ASK_PRODUCT)
    state_dict = asdict(state)
    state_dict.pop("tenant_overlay", None)
    kb_block = resolve_kb_block_for_prompt(
        state,
        structured_kb=str(state.merchant_context["structured_facts_block"]),
        overlay_facts="",
    )
    assert kb_block
    slim = strip_state_dict_for_prompt(
        state_dict, state, kb_in_prompt_block=True,
    )
    mc = slim.get("merchant_context") or {}
    assert "structured_facts_block" not in mc
    ai = mc.get("ai_settings") or {}
    assert "manual_knowledge_base" not in ai
    dumped = json.dumps(slim, ensure_ascii=False)
    assert "نص معرفة يدوي" not in dumped


def test_structured_facts_not_duplicated_in_json_and_kb_block():
    state = _state(intent_name=INTENT_ASK_PRODUCT)
    state_dict = asdict(state)
    state_dict.pop("tenant_overlay", None)
    kb_block = resolve_kb_block_for_prompt(
        state,
        structured_kb="KB_BLOCK_ONLY",
        overlay_facts="",
    )
    slim = strip_state_dict_for_prompt(
        state_dict, state, kb_in_prompt_block=bool(kb_block),
    )
    assert "KB_BLOCK_ONLY" in kb_block
    assert "structured_facts_block" not in (slim.get("merchant_context") or {})


def test_ask_product_prompt_below_threshold_with_large_kb(monkeypatch):
    monkeypatch.setenv("NAHLA_MAX_KB_PROMPT_CHARS", "12000")
    state = _state(intent_name=INTENT_ASK_PRODUCT)
    prompt = build_brain_reply_prompt(state)
    assert len(prompt) < 80_000
    assert len(cap_kb_for_prompt(_LARGE_KB)) <= max_kb_prompt_chars() + 100


def test_prompt_size_warnings_do_not_log_customer_content(caplog):
    caplog.set_level(logging.WARNING, logger="nahla.ai.llm_cost_audit")
    secret = "رسالة_سرية_للعميل_12345"
    emit_llm_cost_audit(
        tenant_id=1,
        intent="greeting",
        model="claude-sonnet-4-6",
        kb_chars=500,
        catalog_chars=100,
        estimated_input_tokens=100,
        total_prompt_chars=400,
    )
    joined = " ".join(r.message for r in caplog.records)
    assert "[LLM_PROMPT_ROUTINE_BLOAT_WARN]" in joined
    assert secret not in joined


def test_model_selection_patch_still_defaults_not_opus(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    from modules.ai.orchestrator.llm_cost_audit import resolve_anthropic_model

    assert "opus" not in resolve_anthropic_model().lower()


def test_routine_social_turn_detects_greeting_and_thanks():
    assert is_routine_social_turn(_state(intent_name="greeting"))
    assert is_routine_social_turn(_state(intent_name="thanks"))
    assert not is_routine_social_turn(_state(intent_name=INTENT_ASK_PRODUCT))
