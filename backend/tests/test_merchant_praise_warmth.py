"""Regression tests for Saudi merchant praise warmth (May 2026).

Production failure: ``ما شاء الله … شغل مرتب`` routed to the
deterministic compliment template pool and returned the literary line
``دوم إحساسك`` — bypassing persona guidance entirely.

Fix: route ``social_category=compliment`` to generative compose with a
strict ``merchant_praise_ack`` response goal; prune literary lines from
the compliment fallback pool.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_LITERARY_FORBIDDEN = (
    "دوم إحساسك",
    "دمت بود",
    "يسعد مساك على شعورك",
    "الله يبحث عنك بحسن ظنك",
    "والله الثناء منك وسام",
)


def _build_social_decision_ctx(*, message: str, social_category: str):
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_SOCIAL,
    )

    state = MerchantConversationState()
    facts = CommerceFacts(
        has_products=True,
        product_count=3,
        orderable=True,
        store_name="متجر الاختبار",
    )
    intent = Intent(
        name=INTENT_SOCIAL,
        confidence=0.95,
        slots={"social_category": social_category},
        raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=message,
        intent=intent,
        state=state,
        facts=facts,
    )
    return ctx


def test_compliment_routes_to_generative_praise_ack() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SOCIAL_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    msg = "ما شاء الله يا أبو هشام شغل مرتب 👍🏻🌹"
    ctx = _build_social_decision_ctx(message=msg, social_category="compliment")
    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == "merchant_praise_ack"
    assert decision.action != ACTION_SOCIAL_REPLY


def test_thanks_routes_to_social_persona_llm() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.persona_expression import PERSONA_TOPIC_SOCIAL_PERSONA_ACK

    ctx = _build_social_decision_ctx(message="جزاك الله خير", social_category="thanks")
    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL_PERSONA_ACK
    assert decision.args.get("social_category") == "thanks"


def test_strong_praise_routes_to_social_persona_llm() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.persona_expression import PERSONA_TOPIC_SOCIAL_PERSONA_ACK

    ctx = _build_social_decision_ctx(message="كفو", social_category="strong_praise")
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL_PERSONA_ACK


def test_merchant_praise_ack_response_goal() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.decision.engine import Decision
    from modules.ai.brain.pipeline import _compose_base_response_goal
    from modules.ai.brain.types import SuggestionSnapshot

    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={"topic": "merchant_praise_ack"},
        reason="merchant praise — generative warmth ack (compliment)",
        confidence=0.95,
    )
    goal = _compose_base_response_goal(decision, SuggestionSnapshot())
    assert "merchant_praise_ack" in goal
    assert "دوم إحساسك" in goal
    assert "Do NOT pitch" in goal or "ممنوع" in goal


def test_persona_includes_merchant_praise_guidance() -> None:
    from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt

    prompt = nahla_persona_system_prompt(store_name="آل عايد")
    assert "دوم إحساسك" in prompt
    assert "تاجر سعودي" in prompt


def test_high_priority_includes_merchant_praise_guard() -> None:
    from modules.ai.prompts.high_priority_layer import build_high_priority_block

    block = build_high_priority_block({}, store_name="آل عايد")
    assert "دوم إحساسك" in block
    assert "pitch بيعي" in block


def test_compliment_template_pool_has_no_literary_lines() -> None:
    from modules.ai.brain.compose import templates

    pool = templates._SOCIAL_COMPLIMENT_VARIANTS
    joined = "\n".join(pool)
    for phrase in _LITERARY_FORBIDDEN:
        assert phrase not in joined, f"literary phrase still in compliment pool: {phrase!r}"
