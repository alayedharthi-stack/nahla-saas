"""Store Employee Role Contract — prompt-level regression locks.

Locks MEANING in persona prompts (not customer reply text).
Role Contract SoT = System Persona only; identity bucket stays name-only.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.persona_expression import compose_persona_identity_goal  # noqa: E402
from modules.ai.prompts.nahla_persona import (  # noqa: E402
    NAHLA_PERSONA,
    NAHLA_PERSONA_SOCIAL_EXPRESSION,
    nahla_persona_system_prompt,
)
from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split  # noqa: E402

_GENERIC_CHAT_DRIFT = (
    "المساعدة الذكية للمتجر",
    "شخصية ودودة على واتساب",
    "مساعدة ذكية للمتجر",
    "مهمتك الأساسية مساعدة العملاء",
)

_ROLE_ESSAY = (
    "مستشارة مبيعات ذكية. في أول رسالة عرّفي بنفسك. "
    "[COUPON:TEST] كيف أقدر أخدمك؟"
)


class TestCommercePersonaRoleContract:
    def test_commerce_persona_framed_as_digital_store_employee(self):
        assert "موظفة رقمية" in NAHLA_PERSONA
        assert "تمثّلين المتجر" in NAHLA_PERSONA
        for marker in _GENERIC_CHAT_DRIFT:
            assert marker not in NAHLA_PERSONA, f"drift marker {marker!r} in commerce persona"

    def test_commerce_persona_no_salesperson_banned_phrase(self):
        assert "موظف/ة مبيعات" not in NAHLA_PERSONA

    def test_commerce_persona_no_canned_identity_reply(self):
        assert "أنا نحلة 🐝 مساعدة ذكية للمتجر" not in NAHLA_PERSONA

    def test_commerce_store_name_interpolation(self):
        prompt = nahla_persona_system_prompt(store_name="متجر النور")
        assert "تمثّلين متجر «متجر النور»" in prompt
        assert "تمثّلين المتجر الحالي" not in prompt


class TestSocialPersonaRoleContract:
    def test_social_persona_framed_as_digital_store_employee(self):
        assert "موظفة رقمية" in NAHLA_PERSONA_SOCIAL_EXPRESSION
        assert "تمثّلين المتجر" in NAHLA_PERSONA_SOCIAL_EXPRESSION
        for marker in _GENERIC_CHAT_DRIFT:
            assert marker not in NAHLA_PERSONA_SOCIAL_EXPRESSION, (
                f"drift marker {marker!r} in social persona"
            )

    def test_social_persona_still_marks_non_cs_turn(self):
        assert "هذه جولة **شخصية/اجتماعية**" in NAHLA_PERSONA_SOCIAL_EXPRESSION
        assert "ليست خدمة عملاء ولا مبيعات" in NAHLA_PERSONA_SOCIAL_EXPRESSION


class TestPersonaIdentityGoalRoleContract:
    def test_identity_goal_framed_as_store_digital_employee(self):
        goal = compose_persona_identity_goal()
        assert "digital employee" in goal
        assert "representing the merchant" in goal
        assert "not a generic chat assistant" in goal

    def test_identity_goal_rejects_generic_chat_companion_framing(self):
        goal = compose_persona_identity_goal()
        assert "generic chat companion" in goal
        assert "هنا للدردشة" in goal
        # Role is defined positively; do not phrase-ban «مساعدة ذكية».
        assert "مساعدة ذكية" not in goal


class TestMultiTenantNameSoT:
    def test_custom_name_and_store_not_hardcoded(self):
        prompt_a = nahla_persona_system_prompt(
            store_name="متجر النور",
            assistant_name="وردة",
            persona_expression_mode=True,
        )
        prompt_b = nahla_persona_system_prompt(
            store_name="متجر الأمل",
            assistant_name="سارة",
            persona_expression_mode=False,
        )
        assert "أنتِ «وردة» من متجر «متجر النور»" in prompt_a
        assert "أنتِ «سارة»، موظفة رقمية تمثّلين متجر «متجر الأمل»" in prompt_b
        assert "سارة" not in prompt_a
        assert "وردة" not in prompt_b
        assert "متجر الأمل" not in prompt_a
        assert "متجر النور" not in prompt_b

    def test_default_name_keeps_platform_self_label(self):
        prompt = nahla_persona_system_prompt(assistant_name="")
        assert prompt.startswith("أنتِ «نحلة 🐝»")


class TestIdentityBucketUnchanged:
    def test_identity_bucket_name_only(self):
        buckets = build_tenant_overlay_split({
            "assistant_name": "نحلة",
            "assistant_role": _ROLE_ESSAY,
        })
        assert "اسمك: نحلة" in buckets["identity"]
        assert "دورك" not in buckets["identity"]
