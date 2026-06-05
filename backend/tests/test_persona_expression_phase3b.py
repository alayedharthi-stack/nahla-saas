"""Persona Expression Phase 3B — residual commerce leakage removal."""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.types import BrainReplyState
from modules.ai.prompts.high_priority_layer import build_high_priority_block
from modules.ai.prompts.nahla_persona import (
    NAHLA_PERSONA,
    NAHLA_PERSONA_SOCIAL_EXPRESSION,
    nahla_persona_system_prompt,
)

_PERSONA_LEAK_MARKERS = (
    "وش أقدر أخدمك فيه؟",
    "الخطوة التجارية التالية",
    "recommended_next_step",
)


def _persona_state(
    topic: str = "persona_social",
    kind: str = "affection",
) -> BrainReplyState:
    return BrainReplyState(
        store_name="test",
        persona_expression_mode=True,
        persona_topic=topic,
        persona_kind=kind,
        response_goal=f"persona — {topic}",
        non_commerce_block_mode=True,
        recommended_next_step="offer_checkout",
        selected_product={"title": "طلح"},
    )


@pytest.mark.parametrize(
    "topic,kind",
    [
        ("persona_identity", ""),
        ("persona_social", "affection"),
        ("persona_social", "tease"),
        ("persona_social", "greeting"),
    ],
)
def test_persona_prompt_excludes_commerce_leakage_markers(
    topic: str, kind: str
) -> None:
    prompt = build_brain_reply_prompt(_persona_state(topic, kind))
    for marker in _PERSONA_LEAK_MARKERS:
        assert marker not in prompt, f"persona prompt must not contain {marker!r}"


def test_persona_prompt_uses_social_persona_block() -> None:
    prompt = build_brain_reply_prompt(_persona_state())
    assert "هذه جولة **شخصية/اجتماعية**" in prompt
    assert "مهمتك الأساسية مساعدة العملاء" not in prompt


def test_persona_high_priority_omits_service_greeting_example() -> None:
    block = build_high_priority_block(
        {},
        omit_sales_behavior=True,
        persona_expression_mode=True,
    )
    assert "وش أقدر أخدمك فيه؟" not in block
    assert "relational_frame" not in block
    assert "جولة persona" in block


def test_commerce_high_priority_keeps_service_greeting_example() -> None:
    block = build_high_priority_block(
        {},
        omit_sales_behavior=False,
        persona_expression_mode=False,
    )
    assert "وش أقدر أخدمك فيه؟" in block
    assert "relational_frame" in block


def test_persona_json_footer_is_continuity_only() -> None:
    prompt = build_brain_reply_prompt(_persona_state())
    assert "استمرارية المحادثة" in prompt
    assert "الخطوة التجارية التالية" not in prompt


def test_persona_brain_state_json_omits_commerce_keys() -> None:
    prompt = build_brain_reply_prompt(_persona_state())
    json_start = prompt.index("BrainStateJSON:\n") + len("BrainStateJSON:\n")
    json_end = prompt.index("\n\n", json_start)
    payload = json.loads(prompt[json_start:json_end])
    assert "recommended_next_step" not in payload
    assert "selected_product" not in payload


def test_commerce_prompt_retains_a1_and_commerce_footer() -> None:
    state = BrainReplyState(
        store_name="test",
        persona_expression_mode=False,
        response_goal="price inquiry",
        recommended_next_step="quote_price",
    )
    prompt = build_brain_reply_prompt(state)
    assert "SALESPERSON BEHAVIOR" in prompt
    assert "الخطوة التجارية التالية" in prompt
    assert "recommended_next_step" in prompt


def test_nahla_persona_switches_block_by_mode() -> None:
    commerce = nahla_persona_system_prompt(persona_expression_mode=False)
    social = nahla_persona_system_prompt(persona_expression_mode=True)
    assert NAHLA_PERSONA.splitlines()[0] in commerce
    assert NAHLA_PERSONA_SOCIAL_EXPRESSION.splitlines()[0] in social
