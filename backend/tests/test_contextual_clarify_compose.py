"""Compose wiring for contextual clarify — no persona layer changes."""
from __future__ import annotations

from modules.ai.brain.clarification.compose_goal import compose_contextual_clarify_goal
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
from modules.ai.brain.pipeline import _compose_response_goal
from modules.ai.brain.types import (
    BrainReplyState,
    Decision,
    SuggestionSnapshot,
)


def test_response_goal_is_behavioral_not_sales_default():
    dec = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "contextual_clarify",
            "ambiguity_class": "missing_product_ref",
            "clarification_evidence": {"intent_name": "ask_price"},
        },
        reason="test",
        confidence=0.84,
    )
    goal = _compose_response_goal(dec, SuggestionSnapshot())
    assert "contextual_clarify" in goal
    assert "ambiguity_class=missing_product_ref" in goal
    assert "advance the conversation toward the next sales step" not in goal
    assert "Generate" in goal or "Compose" in goal


def test_compose_goal_module_bans_system_voice():
    goal = compose_contextual_clarify_goal(ambiguity_class="missing_product_ref")
    assert "contextual_clarify" in goal
    assert "persona" in goal.lower() or "Nahla" in goal
    assert "template-engine" in goal.lower() or "workflow" in goal.lower()


def test_prompt_includes_evidence_block_not_salesperson():
    state = BrainReplyState(
        store_name="Test Store",
        tone="warm",
        stage="discovery",
        response_goal=compose_contextual_clarify_goal(
            ambiguity_class="missing_product_ref",
        ),
        contextual_clarify_mode=True,
        ambiguity_class="missing_product_ref",
        clarification_evidence={
            "intent_name": "ask_price",
            "catalog_available": True,
            "product_focus_title": "",
        },
        intent_name="ask_price",
    )
    prompt = build_brain_reply_prompt(state)
    assert "contextual_clarify" in prompt
    assert "missing_product_ref" in prompt
    assert "ask_price" in prompt
    assert "SALESPERSON BEHAVIOR" not in prompt
