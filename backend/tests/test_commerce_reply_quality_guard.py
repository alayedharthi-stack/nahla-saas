"""Tests for commerce reply output quality guard (post-compose)."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in (_backend, os.path.join(_backend, "..")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.compose.prompt_state_serializer import (
    _COMMERCE_SLIM_RESIDUAL_RULES,
)
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (
    apply_commerce_reply_quality_guard,
    inbound_is_arabic,
)
from modules.ai.brain.types import BrainReplyState, INTENT_SOLUTION_SEEKING_COMMERCE


def _guard(
    reply: str,
    *,
    inbound: str = "هل عندكم عسل طلح؟",
    intent: str = INTENT_SOLUTION_SEEKING_COMMERCE,
    goal: str = GOAL_PRODUCT_AVAILABILITY,
) -> str:
    return apply_commerce_reply_quality_guard(
        reply,
        inbound_text=inbound,
        intent_name=intent,
        primary_customer_goal=goal,
        tenant_id=7,
    ).reply


class TestForbiddenResidue:
    def test_arabic_inbound_never_returns_powered_by_nahla(self) -> None:
        raw = "🐝 Powered by Nahla"
        out = _guard(raw, inbound="عسل")
        assert "powered by nahla" not in out.lower()
        assert "Powered by Nahla" not in out
        assert out

    def test_asil_only_branding_gets_arabic_fallback(self) -> None:
        out = _guard("Powered by Nahla", inbound="عسل")
        assert out == "حدّد المنتج أو المقاس المطلوب."

    def test_arabic_inbound_never_returns_let_me_verify(self) -> None:
        raw = (
            "سأتحقق من توفر المنتج لك. ✅ "
            "Let me verify the current availability for you. ✅"
        )
        out = _guard(raw)
        assert "let me verify" not in out.lower()
        assert "current availability" not in out.lower()
        assert inbound_is_arabic("هل عندكم عسل طلح؟")
        assert "Let me" not in out

    def test_empty_after_stripping_uses_availability_fallback(self) -> None:
        out = _guard(
            "Let me verify the current availability for you.",
            inbound="\u0647\u0644 \u0639\u0646\u062f\u0643\u0645 \u0639\u0633\u0644 \u0637\u0644\u062d\u061f",
        )
        assert out == "التوفر قيد التحقق."


class TestCommerceFriendlyArabic:
    def test_availability_reply_stays_arabic(self) -> None:
        raw = "متوفر عسل طلح بعدة أحجام، أي حجم يناسبك؟"
        out = _guard(raw, inbound="هل عندكم عسل طلح؟")
        assert out == raw
        assert "Let me" not in out

    def test_delivery_reply_stays_arabic(self) -> None:
        raw = (
            "سأتحقق من إمكانية التوصيل السريع في منطقتك 🚚. "
            "Let me verify same-day delivery availability for your area."
        )
        out = _guard(raw, inbound="\u0645\u0648\u0642\u0639\u064a \u0627\u0644\u0631\u064a\u0627\u0636 \u062d\u064a \u0627\u0644\u0646\u0631\u062c\u0633")
        assert "Let me" not in out
        assert "same-day" not in out.lower()
        assert "توصيل" in out


class TestPromptSlimContract:
    @pytest.fixture
    def slim_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "true")

    def test_slim_prompt_includes_arabic_response_contract(
        self,
        slim_enabled: None,
    ) -> None:
        state = BrainReplyState(
            store_name="متجر",
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            need_based_advice_mode=True,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            stage="discovery",
            merchant_context={"tenant_id": 7, "ai_settings": {"reply_tone": "friendly"}},
        )
        prompt = build_brain_reply_prompt(state)
        assert "Powered by Nahla" in _COMMERCE_SLIM_RESIDUAL_RULES
        assert "لا تكتبي Powered by Nahla" in prompt
        assert "لا تكتبي أي جملة إنجليزية" in prompt

    def test_slim_enabled_guard_still_protects_mixed_output(
        self,
        slim_enabled: None,
    ) -> None:
        out = _guard(
            "Powered by Nahla\nLet me verify the current availability for you.",
            inbound="عسل",
        )
        assert "Powered by Nahla" not in out
        assert "Let me" not in out
        assert len(out) >= 10


class TestModelRouterUntouched:
    def test_guard_does_not_import_model_router(self) -> None:
        import modules.ai.brain.postprocess.commerce_reply_quality_guard as mod

        source = open(mod.__file__, encoding="utf-8").read()
        assert "model_router" not in source

    def test_resolve_compose_still_cheap_for_commerce(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.cost.model_router import resolve_compose_model_route

        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        route = resolve_compose_model_route(
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            reply_state=BrainReplyState(
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            ),
        )
        assert route.model == "gpt-4o-mini"
