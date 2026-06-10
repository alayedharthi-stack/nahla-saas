"""P1-F — style architecture: relaxed emoji/length pressure, not exact wording."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.greeting_etiquette import salam_return_text, SALAM_BASIC  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.persona_expression import compose_social_persona_goal  # noqa: E402
from modules.ai.prompts.high_priority_layer import (  # noqa: E402
    BASELINE_POLICY_RULES,
    SALES_BEHAVIOR_EXAMPLES,
    build_high_priority_block,
)
from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt  # noqa: E402

_DECORATIVE_EMOJI = ("🌷", "🍯", "💪", "🚚")


def _collect_prompt_examples(*parts: str) -> str:
    return "\n".join(parts)


class TestSocialPersonaGoalArchitecture:
    def test_does_not_force_one_line_acknowledgement(self) -> None:
        goal = compose_social_persona_goal("blessing")
        lowered = goal.lower()
        assert "one short" not in lowered
        assert "one short acknowledgement" not in lowered
        assert "1-line ack" not in lowered
        assert "not a forced one-line" in lowered
        assert "compressing every turn into one or two words" in lowered

    def test_emoji_optional_not_required(self) -> None:
        goal = compose_social_persona_goal("thanks")
        assert "emoji is optional" in goal.lower()
        assert "🌷" not in goal


class TestPersonaPromptArchitecture:
    def test_nahla_persona_does_not_teach_flower_signature(self) -> None:
        prompt = nahla_persona_system_prompt(store_name="متجر")
        assert "🌷 للتحية" not in prompt
        assert "3–4 إيموجيات" not in prompt
        assert "قصيرة (1–3 أسطر)" not in prompt
        assert "الإيموجي اختياري" in prompt

    def test_nahla_tone_examples_without_decorative_emoji(self) -> None:
        prompt = nahla_persona_system_prompt(store_name="متجر")
        examples_block = prompt.split("## أمثلة سريعة على النبرة", 1)[-1]
        for ch in _DECORATIVE_EMOJI:
            assert ch not in examples_block, f"decorative {ch} in tone examples"


class TestHighPriorityLayerArchitecture:
    def test_sales_examples_do_not_model_honey_emoji(self) -> None:
        blob = _collect_prompt_examples(
            *(ex[1] + ex[2] for ex in SALES_BEHAVIOR_EXAMPLES),
        )
        assert "🍯" not in blob
        assert "🌷" not in blob
        assert "💪" not in blob

    def test_baseline_policy_examples_without_flower_signature(self) -> None:
        blob = "\n".join(BASELINE_POLICY_RULES)
        assert "🌷" not in blob

    def test_style_block_emoji_optional_not_quota(self) -> None:
        block = build_high_priority_block({}, store_name="متجر")
        assert "3–4 إيموجيات" not in block
        assert "لا تكرري نفس الرمز" in block or "لا تكرري نفس الرمز دائماً" in block


class TestOperationalCopyNoDecorativeEmoji:
    def test_salam_return_has_no_flower(self) -> None:
        assert "🌷" not in salam_return_text(SALAM_BASIC)

    @pytest.mark.parametrize(
        "message",
        ("ممكن صوره له", "ابي اشوف صورته"),
    )
    def test_product_visual_clarify_question_has_no_flower(
        self, message: str,
    ) -> None:
        from modules.ai.brain.types import (  # noqa: PLC0415
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
        )

        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000000",
            message=message,
            intent=Intent(name="ask_product_visual", confidence=0.9, raw_message=message),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, product_count=3, orderable=True),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        question = (decision.args or {}).get("question") or ""
        if question:
            assert "🌷" not in question
