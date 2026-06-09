"""
ARCH-KB-001 Phase 1a — platform persona constitution regression locks.

Evidence-driven cleanup: no auto identity, no role essay in identity_block,
owner_instructions filtered on persona turns, phatic rollback templates.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.tenant import DEFAULT_AI, merge_ai_defaults  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.types import BrainReplyState  # noqa: E402
from modules.ai.prompts.high_priority_layer import (  # noqa: E402
    build_high_priority_block,
    filter_owner_instructions_for_persona,
)
from modules.ai.prompts.nahla_persona import NAHLA_PERSONA  # noqa: E402
from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split  # noqa: E402


_T33_ROLE_ESSAY = (
    "مستشارة مبيعات ذكية. في أول رسالة عرّفي بنفسك. "
    "[COUPON:TEST] كيف أقدر أخدمك؟"
)
_T33_OWNER = (
    "- في أول رسالة عرّفي بنفسك كمستشارة مبيعات.\n"
    "- ردودك قصيرة.\n"
    "- لا تبالغي في الوصف."
)


class TestDefaultAiConstitution:
    def test_default_assistant_role_has_no_sales_title(self):
        role = DEFAULT_AI["assistant_role"]
        assert "مستشارة" not in role
        assert "موظف" not in role

    def test_default_owner_instructions_no_mandatory_intro(self):
        owner = DEFAULT_AI["owner_instructions"]
        assert "أول رسالة" not in owner
        assert "عرّف" not in owner
        assert "مستشارة" not in owner

    def test_merge_ai_defaults_empty_role_falls_back(self):
        merged = merge_ai_defaults({"assistant_role": ""})
        assert merged["assistant_role"] == DEFAULT_AI["assistant_role"]


class TestNahlaPersonaConstitution:
    def test_no_salesperson_framing(self):
        assert "موظف/ة مبيعات" not in NAHLA_PERSONA

    def test_no_first_greeting_intro_example(self):
        assert "أول تحية" not in NAHLA_PERSONA
        assert "أنا نحلة 🐝 مساعدة المتجر، تحت أمرك" not in NAHLA_PERSONA

    def test_bot_answer_not_customer_service_script(self):
        assert "خدمة العملاء والطلبات" not in NAHLA_PERSONA


class TestTenantOverlayIdentity:
    def test_identity_bucket_name_only(self):
        buckets = build_tenant_overlay_split({
            "assistant_name": "نحلة",
            "assistant_role": _T33_ROLE_ESSAY,
        })
        assert "اسمك: نحلة" in buckets["identity"]
        assert "دورك" not in buckets["identity"]
        assert "مستشارة" not in buckets["identity"]


class TestOwnerInstructionsFilter:
    def test_strips_intro_and_sales_lines(self):
        filtered = filter_owner_instructions_for_persona(_T33_OWNER)
        assert "أول رسالة" not in filtered
        assert "مستشارة" not in filtered
        assert "قصيرة" in filtered

    def test_persona_high_priority_omits_stripped_owner(self):
        block = build_high_priority_block(
            {"owner_instructions": _T33_OWNER},
            persona_expression_mode=True,
        )
        assert "أول رسالة" not in block
        assert "مستشارة" not in block
        assert "قصيرة" in block

    def test_commerce_high_priority_keeps_owner(self):
        block = build_high_priority_block(
            {"owner_instructions": _T33_OWNER},
            persona_expression_mode=False,
        )
        assert "أول رسالة" in block


class TestPromptBuilderIdentityGating:
    def _state(self, **kwargs) -> BrainReplyState:
        base = {
            "store_name": "متجر الاختبار",
            "merchant_context": {
                "ai_settings": {
                    "assistant_name": "نحلة مستشارة المبيعات",
                    "assistant_role": _T33_ROLE_ESSAY,
                    "owner_instructions": _T33_OWNER,
                },
            },
        }
        base.update(kwargs)
        return BrainReplyState(**base)

    def test_persona_social_prompt_has_no_identity_block(self):
        prompt = build_brain_reply_prompt(
            self._state(
                persona_expression_mode=True,
                persona_topic="persona_social",
                persona_kind="greeting",
                response_goal="persona_social test",
                non_commerce_block_mode=True,
            )
        )
        assert "هوية المساعد" not in prompt
        assert "دورك" not in prompt
        assert "ممنوع" in prompt and "التعريف" in prompt

    def test_persona_social_prompt_filters_owner_intro(self):
        prompt = build_brain_reply_prompt(
            self._state(
                persona_expression_mode=True,
                persona_topic="persona_social",
                persona_kind="greeting",
                response_goal="persona_social test",
                non_commerce_block_mode=True,
            )
        )
        assert "أول رسالة" not in prompt
        assert "مستشارة" not in prompt

    def test_commerce_prompt_keeps_name_only_identity(self):
        prompt = build_brain_reply_prompt(
            self._state(
                persona_expression_mode=False,
                response_goal="price inquiry",
            )
        )
        assert "اسمك:" in prompt
        assert "دورك" not in prompt

    def test_persona_identity_allows_short_intro_guidance(self):
        prompt = build_brain_reply_prompt(
            self._state(
                persona_expression_mode=True,
                persona_topic="persona_identity",
                response_goal="persona_identity test",
                non_commerce_block_mode=True,
            )
        )
        assert "persona_identity" in prompt
        assert "مسموح تعريف قصير" in prompt


class TestRollbackTemplates:
    @pytest.mark.parametrize("variant", (0, 1, 2))
    def test_greeting_templates_phatic_only(self, variant: int):
        reply = T.greeting(
            store_name="متجر العسل",
            assistant_name="نحلة",
            variant=variant,
        )
        assert "أنا " not in reply
        assert "•" not in reply
        assert "كيف أقدر" not in reply
        assert "بماذا أخدمك" not in reply

    @pytest.mark.parametrize("variant", (0, 1, 2))
    def test_re_greeting_templates_phatic_only(self, variant: int):
        reply = T.re_greeting(
            store_name="متجر العسل",
            assistant_name="نحلة",
            variant=variant,
        )
        assert "نحلة" not in reply
        assert "وش أقدر" not in reply
        assert "تحت أمرك" not in reply
        assert reply.count("\n") <= 1
