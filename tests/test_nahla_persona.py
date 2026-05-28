"""
test_nahla_persona.py
─────────────────────
Lock the contract of the canonical Nahla persona prompt builder.

The persona is the single source of truth for the assistant's voice
across both the legacy WhatsApp AI path and the Merchant Brain LLM
fallback. These tests pin down the public signals that downstream
prompt layers depend on:

  - bee 🐝 + flower 🌷 emoji hooks are present
  - explicit emoji-rule mapping is included
  - store name is interpolated when supplied
  - merchant store context is appended as a clearly-fenced block
  - no stale references to «نظام» / «روبوت» language
"""
from modules.ai.prompts.nahla_persona import (
    NAHLA_PERSONA,
    nahla_persona_system_prompt,
)


class TestPersonaConstants:
    def test_persona_uses_bee_and_flower_hooks(self):
        # Bee belongs to the Nahla identity; flower to greetings.
        assert "🐝" in NAHLA_PERSONA
        assert "🌷" in NAHLA_PERSONA

    def test_persona_lists_contextual_emoji_map(self):
        # The five context-bound emojis the user requested.
        for emoji in ("🌷", "👍", "🛍️", "🎁", "🚚"):
            assert emoji in NAHLA_PERSONA, f"missing {emoji} in persona"

    def test_persona_caps_emoji_usage(self):
        assert "1–2" in NAHLA_PERSONA or "1-2" in NAHLA_PERSONA or "واحد" in NAHLA_PERSONA

    def test_persona_includes_saudi_dialect_guidance(self):
        assert "اللهجة السعودية" in NAHLA_PERSONA
        assert "حياك الله" in NAHLA_PERSONA
        assert "يطري" in NAHLA_PERSONA  # forbidden leakage called out

    def test_persona_forbids_robot_self_reference(self):
        # Hard rule: the assistant never refers to itself as a system /
        # AI / robot in the body of replies.
        assert "لا تذكري أنك برنامج أو روبوت" in NAHLA_PERSONA
        assert "نظام" in NAHLA_PERSONA  # the *forbidden* term is mentioned
        assert "ذكاء اصطناعي" in NAHLA_PERSONA  # so the model knows to avoid it


class TestPersonaPromptBuilder:
    def test_default_returns_persona_only(self):
        prompt = nahla_persona_system_prompt()
        assert prompt.startswith("أنتِ «نحلة 🐝»")
        # No store-context fence when no context was supplied.
        assert "## معلومات المتجر المتاحة" not in prompt

    def test_store_name_replaces_generic_label(self):
        prompt = nahla_persona_system_prompt(store_name="متجر النور")
        assert "لمتجر «متجر النور»" in prompt
        # The original generic line should NOT appear.
        assert "أنتِ «نحلة 🐝»، المساعدة الذكية للمتجر." not in prompt

    def test_store_context_is_appended_as_fenced_block(self):
        ctx = "المنتج: قميص قطني\nالسعر: 99 ريال"
        prompt = nahla_persona_system_prompt(store_context_text=ctx)
        assert "## معلومات المتجر المتاحة" in prompt
        assert "لا تخترعي" in prompt
        assert "قميص قطني" in prompt

    def test_empty_or_blank_store_context_is_ignored(self):
        for ctx in ("", "   ", "\n\n"):
            prompt = nahla_persona_system_prompt(store_context_text=ctx)
            assert "## معلومات المتجر المتاحة" not in prompt

    def test_full_layering(self):
        prompt = nahla_persona_system_prompt(
            store_name="متجر الواحة",
            store_context_text="المنتج: عسل سدر\nالسعر: 150",
        )
        # Order matters: persona first, then merchant ground-truth context.
        idx_persona = prompt.find("أنتِ «نحلة 🐝»")
        idx_context = prompt.find("## معلومات المتجر المتاحة")
        assert 0 == idx_persona
        assert idx_persona < idx_context
        assert "متجر الواحة" in prompt
        assert "عسل سدر" in prompt
