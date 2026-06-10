from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def test_persona_uses_flexible_visual_marketing_guidance() -> None:
    from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt

    prompt = nahla_persona_system_prompt(store_name="آل عايد")

    assert "الذوق البصري والتسويقي" in prompt
    assert "CTA" in prompt
    assert "3–4 إيموجيات" not in prompt
    assert "قالب محفوظ" in prompt
    assert "🌷 للتحية" not in prompt
    assert "ضعي الإيموجي في بداية الجملة أو نهايتها فقط" not in prompt
    assert "الإيموجي اختياري" in prompt


def test_high_priority_style_allows_contextual_emoji_variation() -> None:
    from modules.ai.prompts.high_priority_layer import build_high_priority_block

    block = build_high_priority_block({}, store_name="آل عايد")

    assert "الإيموجي والتنسيق البصري" in block
    assert "3–4 إيموجيات" not in block
    assert "لا تكرري نفس الرمز" in block
    assert "إيموجي بحد أقصى 1-2" not in block
