"""Persona identity name SoT — assistant_name from ai_settings grounds Block1 + overlay."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.persona_expression import compose_persona_identity_goal  # noqa: E402
from modules.ai.brain.types import BrainReplyState  # noqa: E402
from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt  # noqa: E402


def _persona_identity_state(
    assistant_name: str | None,
    *,
    store_name: str = "متجر تجريبي عام",
) -> BrainReplyState:
    ai_settings: dict[str, str] = {}
    if assistant_name is not None:
        ai_settings["assistant_name"] = assistant_name
    return BrainReplyState(
        store_name=store_name,
        merchant_context={"ai_settings": ai_settings},
        persona_expression_mode=True,
        persona_topic="persona_identity",
        response_goal="persona_identity test",
        non_commerce_block_mode=True,
    )


class TestAssistantNameSourceOfTruth:
    def test_custom_name_in_identity_block_and_block1(self):
        prompt = build_brain_reply_prompt(_persona_identity_state("وردة"))
        assert "اسمك: وردة" in prompt
        assert "أنتِ «وردة»" in prompt
        assert "أنتِ «نحلة 🐝»" not in prompt

    def test_second_tenant_custom_name_isolated(self):
        prompt_a = build_brain_reply_prompt(_persona_identity_state("وردة"))
        prompt_b = build_brain_reply_prompt(_persona_identity_state("سارة"))
        assert "اسمك: وردة" in prompt_a
        assert "اسمك: سارة" in prompt_b
        assert "أنتِ «وردة»" in prompt_a
        assert "أنتِ «سارة»" in prompt_b
        assert "سارة" not in prompt_a
        assert "وردة" not in prompt_b

    def test_empty_assistant_name_keeps_platform_default(self):
        prompt = build_brain_reply_prompt(_persona_identity_state(""))
        assert "أنتِ «نحلة 🐝»" in prompt
        assert "هوية المساعد" not in prompt

    def test_missing_assistant_name_keeps_platform_default(self):
        prompt = build_brain_reply_prompt(_persona_identity_state(None))
        assert "أنتِ «نحلة 🐝»" in prompt

    def test_default_ai_bare_nahla_keeps_platform_self_label(self):
        """DEFAULT_AI uses assistant_name='نحلة' without emoji — Block1 keeps bee."""
        prompt = build_brain_reply_prompt(_persona_identity_state("نحلة"))
        assert "أنتِ «نحلة 🐝»" in prompt
        assert "اسمك: نحلة" in prompt
        assert "أنتِ «نحلة»" not in prompt.replace("أنتِ «نحلة 🐝»", "")

    def test_persona_expression_mode_does_not_drop_name_fact(self):
        prompt = build_brain_reply_prompt(_persona_identity_state("وردة"))
        assert "هوية المساعد" in prompt
        assert "اسمك: وردة" in prompt

    def test_platform_nahla_saas_references_preserved_with_custom_name(self):
        block = nahla_persona_system_prompt(
            persona_expression_mode=False,
            assistant_name="وردة",
        )
        assert "منصّة نحلة" in block
        assert "أنتِ «وردة»" in block.splitlines()[0]

    def test_nahla_persona_block1_custom_name_with_store(self):
        block = nahla_persona_system_prompt(
            store_name="متجر تجريبي عام",
            persona_expression_mode=True,
            assistant_name="وردة",
        )
        assert "أنتِ «وردة» من متجر «متجر تجريبي عام»" in block
        assert "نحلة 🐝" not in block.splitlines()[0]

    def test_nahla_persona_block1_default_without_custom_name(self):
        block = nahla_persona_system_prompt(
            persona_expression_mode=True,
            assistant_name="",
        )
        assert block.startswith("أنتِ «نحلة 🐝»")


class TestPersonaIdentityGoalWording:
    def test_goal_does_not_force_nahla_name(self):
        goal = compose_persona_identity_goal()
        assert "Nahla" not in goal
        assert "assistant name from identity facts" in goal
