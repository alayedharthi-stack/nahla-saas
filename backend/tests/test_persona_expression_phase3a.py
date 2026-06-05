"""Persona Expression Phase 3A — prompt profile + stance bypass + kind goals."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent import rules
from modules.ai.brain.intent.stance_detector import detect_stance
from modules.ai.brain.pipeline import _compose_response_goal
from modules.ai.brain.types import (
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)
from modules.ai.prompts.high_priority_layer import build_high_priority_block


def _ctx(msg: str, intent: Intent) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=MerchantConversationState(),
        facts=CommerceFacts(store_name="متجر الاختبار", assistant_name="نحلة"),
    )


def _decision(topic: str, persona_kind: str = "") -> Decision:
    args: dict = {"topic": topic, "block_commerce_escalation": True}
    if persona_kind:
        args["persona_kind"] = persona_kind
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason=f"persona — {topic}",
        confidence=0.94,
    )


@pytest.mark.parametrize(
    "topic,kind",
    [
        ("persona_identity", ""),
        ("persona_social", "tease"),
    ],
)
def test_high_priority_omits_a1_on_persona(topic: str, kind: str) -> None:
    block = build_high_priority_block({}, omit_sales_behavior=True)
    assert "SALESPERSON BEHAVIOR" not in block
    assert "Progressive Selling" not in block
    assert "HIGH PRIORITY" in block


def test_high_priority_keeps_a1_on_commerce() -> None:
    block = build_high_priority_block({}, omit_sales_behavior=False)
    assert "SALESPERSON BEHAVIOR" in block
    assert "Progressive Selling" in block


@pytest.mark.parametrize(
    "topic,kind",
    [
        ("persona_identity", ""),
        ("persona_social", "affection"),
        ("persona_social", "greeting"),
    ],
)
def test_prompt_builder_suppresses_a1_for_persona_state(
    topic: str, kind: str
) -> None:
    state = BrainReplyState(
        store_name="test",
        persona_expression_mode=True,
        persona_topic=topic,
        persona_kind=kind,
        response_goal="persona test",
        non_commerce_block_mode=True,
    )
    prompt = build_brain_reply_prompt(state)
    assert "SALESPERSON BEHAVIOR" not in prompt
    assert "HIGH PRIORITY" in prompt


def test_commerce_prompt_still_has_a1() -> None:
    state = BrainReplyState(
        store_name="test",
        persona_expression_mode=False,
        response_goal="price inquiry",
    )
    prompt = build_brain_reply_prompt(state)
    assert "SALESPERSON BEHAVIOR" in prompt


def test_stance_bypass_on_persona_identity() -> None:
    msg = "هل تنامين؟"
    intent = rules.match(msg)
    assert intent is not None
    decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
    stance = detect_stance(msg)
    assert stance.stance == "info_only"
    goal = _compose_response_goal(decision, SuggestionSnapshot(), stance=stance)
    assert "relational_frame=info_only" not in goal
    assert "persona_identity" in goal
    assert "answer playfully that you are a" not in goal
    assert "avoid «digital assistant always available» boilerplate" in goal


@pytest.mark.parametrize(
    "msg,expected_kind,needle",
    [
        ("انتي حلوة؟", "appearance", "Modest friendly acknowledgment"),
        ("اشتقت لك", "affection", "warm reciprocal"),
        ("فاشلة", "tease", "playful pushback"),
        ("زعلان منك", "upset", "gentle light repair"),
    ],
)
def test_persona_kind_goal_guidance(
    msg: str, expected_kind: str, needle: str
) -> None:
    intent = rules.match(msg)
    assert intent is not None
    decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
    assert decision.args.get("topic") == "persona_social"
    assert decision.args.get("persona_kind") == expected_kind
    goal = _compose_response_goal(
        decision, SuggestionSnapshot(), stance=detect_stance(msg)
    )
    assert f"persona_kind={expected_kind}" in goal
    assert needle.lower() in goal.lower()
    assert "relational_frame=" not in goal


@pytest.mark.parametrize(
    "msg",
    ["كم سعر الطلح؟", "عندكم سدر؟", "ابي اطلب"],
)
def test_commerce_stance_still_prepended(msg: str) -> None:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(
            name=INTENT_GENERAL, confidence=0.5, slots={}, raw_message=msg
        )
    decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
    stance = detect_stance(msg)
    goal = _compose_response_goal(decision, SuggestionSnapshot(), stance=stance)
    if stance.stance and stance.stance != "unknown":
        assert "relational_frame=" in goal
    assert "SALESPERSON BEHAVIOR" in build_brain_reply_prompt(
        BrainReplyState(store_name="test", response_goal=goal)
    )


def test_persona_routing_unchanged_phase3a() -> None:
    for msg in ["هل تنامين؟", "فاشلة", "خدمة فاشلة"]:
        intent = rules.match(msg)
        if intent is None:
            intent = Intent(
                name=INTENT_GENERAL, confidence=0.5, slots={}, raw_message=msg
            )
        decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
        if msg == "خدمة فاشلة":
            assert decision.args.get("topic") != "persona_social"
        elif msg == "هل تنامين؟":
            assert decision.args.get("topic") == "persona_identity"
        else:
            assert decision.args.get("topic") == "persona_social"
