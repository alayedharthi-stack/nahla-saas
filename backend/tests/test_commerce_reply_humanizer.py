"""Tests for deterministic commerce reply humanizer (cheap path warm-up)."""
from __future__ import annotations

import os
import re
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in (_backend, os.path.join(_backend, "..")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce_reply_humanizer import (  # noqa: E402
    FAST_DELIVERY_EMOJI,
    apply_commerce_reply_humanizer,
    detect_product_category,
)
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: E402
    _COMMERCE_SLIM_RESIDUAL_RULES,
)
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainReplyState,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    INTENT_TALK_HUMAN,
)


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _humanize(
    reply: str,
    *,
    inbound: str = "هل عندكم عسل طلح؟",
    intent: str = INTENT_SOLUTION_SEEKING_COMMERCE,
    goal: str = GOAL_PRODUCT_AVAILABILITY,
    chosen_path: str = "llm",
    human_priority: bool = False,
    product_title: str = "",
) -> str:
    return apply_commerce_reply_humanizer(
        reply,
        inbound_text=inbound,
        intent_name=intent,
        primary_customer_goal=goal,
        chosen_path=chosen_path,
        human_priority=human_priority,
        product_title=product_title,
        tenant_id=7,
    ).reply


def _emoji_count(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


class TestWarmCommerceTone:
    def test_ask_product_dry_reply_gets_warm_tone_and_emojis(self) -> None:
        raw = "نعم، عندنا عسل الطلح، تبي أي حجم أو نوع معين؟"
        out = _humanize(raw, inbound="هل عندكم عسل طلح؟")
        assert "أبشر" in out or "نعم متوفر" in out
        assert "وش الحجم" in out
        assert _emoji_count(out) <= 2
        assert _emoji_count(out) >= 1
        assert "🍯" in out or "🌿" in out or "✨" in out

    def test_availability_reply_not_cold_formal(self) -> None:
        raw = "لدينا عدة أحجام، أي حجم تريد؟"
        out = _humanize(raw, inbound="هل متوفر عسل سدر؟")
        assert "يرجى" not in out
        assert "هل ترغب" not in out
        assert "وش الحجم" in out
        assert _emoji_count(out) <= 2

    def test_delivery_location_reply_warms_up(self) -> None:
        raw = "ما هو موقعك للتوصيل؟"
        out = _humanize(
            raw,
            inbound="موقعي الرياض حي النرجس",
            intent="ask_shipping",
            goal="shipping_inquiry",
        )
        assert "أبشر" in out
        assert "🚚" in out
        assert "أرسل" in out

    def test_general_product_uses_shopping_emoji(self) -> None:
        raw = "نعم، عندنا المنتج، تبي أي حجم؟"
        out = _humanize(
            raw,
            inbound="هل عندكم المنتج؟",
            product_title="",
        )
        assert _emoji_count(out) <= 2
        assert "🛒" in out or "✨" in out


class TestProductCategoryEmojis:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("عسل الطلح", "honey"),
            ("فساتين سهرة", "dress"),
            ("ملابس رجالية", "clothes"),
            ("جوالات آيفون", "mobile"),
            ("قرطاسية وأقلام", "stationery"),
            ("منتج عام", "general"),
        ],
    )
    def test_detect_product_category(self, text: str, expected: str) -> None:
        assert detect_product_category(text) == expected

    def test_dress_reply_allows_dress_emoji(self) -> None:
        raw = "نعم، عندنا فساتين بعدة موديلات، أي مقاس تريدين؟"
        out = _humanize(
            raw,
            inbound="هل عندكم فساتين؟",
            product_title="فستان سهرة",
        )
        assert "👗" in out or "✨" in out
        assert _emoji_count(out) <= 2

    def test_electronics_reply_allows_tech_emoji(self) -> None:
        raw = "نعم، عندنا خيارات، أي موديل تريد؟"
        out = _humanize(
            raw,
            inbound="هل عندكم شواحن؟",
            product_title="شاحن إلكتروني",
        )
        assert any(e in out for e in ("🔌", "📱", "💻", "✨", "🛒"))
        assert _emoji_count(out) <= 2


class TestResidueAndSafety:
    def test_no_english_after_guard_and_humanizer(self) -> None:
        raw = "Let me verify the current availability for you."
        guarded = apply_commerce_reply_quality_guard(
            raw,
            inbound_text="هل عندكم عسل طلح؟",
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
        ).reply
        out = _humanize(guarded, inbound="هل عندكم عسل طلح؟")
        assert "Let me" not in out
        assert "Powered by Nahla" not in out

    def test_complaint_does_not_get_playful_emojis(self) -> None:
        raw = "نعم، عندنا عسل الطلح، تبي أي حجم؟"
        out = _humanize(
            raw,
            inbound="أنا زعلان من خدمتكم وما راضي",
            intent=INTENT_ASK_PRODUCT,
        )
        assert out == raw

    def test_escalation_intent_skips_humanizer(self) -> None:
        raw = "سأحولك للدعم الآن."
        out = _humanize(
            raw,
            inbound="أبي موظف",
            intent=INTENT_TALK_HUMAN,
            human_priority=True,
        )
        assert out == raw

    def test_no_fire_emoji_in_normal_availability(self) -> None:
        raw = "نعم، عندنا عسل الطلح، تبي أي حجم؟"
        out = _humanize(raw)
        assert "🔥" not in out


class TestFactPreservation:
    def test_does_not_flip_unavailable_to_available(self) -> None:
        raw = "عذراً، عسل الطلح غير متوفر حالياً."
        out = _humanize(raw, inbound="هل عندكم عسل طلح؟")
        assert "غير متوفر" in out
        assert "متوفر عندنا" not in out

    def test_does_not_add_price(self) -> None:
        raw = "نعم، عندنا عسل الطلح، تبي أي حجم؟"
        out = _humanize(raw)
        assert "ريال" not in out
        assert "SAR" not in out

    def test_does_not_add_confirmed_delivery_without_evidence(self) -> None:
        raw = "أرسل لي موقعك لأتحقق من التوصيل."
        out = _humanize(
            raw,
            inbound="هل توصلون الرياض؟",
            intent="ask_shipping",
            goal="shipping_inquiry",
        )
        assert "التوصيل متاح" not in out

    def test_verifying_reply_does_not_become_confirmed_available(self) -> None:
        raw = "أبشر، أتحقق لك من التوفر. أي حجم تقصد؟"
        out = _humanize(raw, inbound="عسل")
        assert "أتحقق" in out or "تحقق" in out


class TestFastDeliveryEmoji:
    def test_urgent_inbound_order_may_get_speed_emoji_not_airplane(self) -> None:
        raw = "تمام، أجهز لك الطلب. أرسل لي الاسم والموقع."
        out = _humanize(
            raw,
            inbound="أبغاه سريع الحين",
            intent="start_order",
            goal="",
        )
        assert _emoji_count(out) <= 2
        assert "✈️" not in out
        assert any(e in out for e in ("⚡", "🚀", "🛒", "✅"))

    def test_delivery_inquiry_prefers_truck_not_airplane(self) -> None:
        raw = "أرسل لي موقعك وأتأكد لك من التوصيل."
        out = _humanize(
            raw,
            inbound="هل توصلون الرياض؟",
            intent="ask_shipping",
            goal="shipping_inquiry",
        )
        assert "🚚" in out or "📍" in out
        assert "✈️" not in out

    def test_playful_air_metaphor_allows_airplane_emoji(self) -> None:
        raw = "أبشر، نجهزه لك طيارة. وش الكمية؟"
        out = _humanize(
            raw,
            inbound="أبغاه بسرعة",
            intent="start_order",
        )
        assert "✈️" in out
        assert _emoji_count(out) <= 2

    def test_literal_air_shipping_strips_risky_claim(self) -> None:
        raw = "الشحن الجوي متاح ✈️"
        out = _humanize(
            raw,
            inbound="هل عندكم شحن جوي؟",
            intent="ask_shipping",
            goal="shipping_inquiry",
        )
        assert "الشحن الجوي" not in out
        assert "✈️" not in out

    def test_unconfirmed_minutes_promise_is_removed(self) -> None:
        raw = "نوصله لك خلال دقائق 🚀"
        out = _humanize(
            raw,
            inbound="مستعجل",
            intent="ask_shipping",
            goal="shipping_inquiry",
        )
        assert "خلال دقائق" not in out

    def test_urgent_delivery_softens_wording_without_fixed_template(self) -> None:
        raw = "أرسل لي موقعك وأتأكد لك من التوصيل."
        out = _humanize(
            raw,
            inbound="مستعجل بسرعة",
            intent="ask_shipping",
            goal="shipping_inquiry",
        )
        assert "أسرع توصيل" in out
        assert "خلال دقائق" not in out

    def test_complaint_skips_fast_delivery_emojis(self) -> None:
        raw = "نجهزه لك طيارة بسرعة"
        out = _humanize(
            raw,
            inbound="أنا زعلان وما راضي مستعجل",
            intent="start_order",
        )
        assert out == raw


class TestPromptWarmContract:
    @pytest.fixture
    def slim_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "true")

    def test_slim_prompt_includes_warm_saudi_tone_rules(
        self,
        slim_enabled: None,
    ) -> None:
        assert "واتساب سعودي" in _COMMERCE_SLIM_RESIDUAL_RULES
        state = BrainReplyState(
            store_name="متجر",
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            need_based_advice_mode=True,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            stage="discovery",
            merchant_context={"tenant_id": 7, "ai_settings": {"reply_tone": "friendly"}},
        )
        prompt = build_brain_reply_prompt(state)
        assert "واتساب سعودي" in prompt
        assert "وش الحجم" in prompt


class TestModelRouterUntouched:
    def test_humanizer_does_not_import_model_router(self) -> None:
        import modules.ai.brain.commerce_reply_humanizer as mod

        source = open(mod.__file__, encoding="utf-8").read()
        assert "model_router" not in source

    def test_cheap_intents_still_route_to_gpt4o_mini(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.cost.model_router import resolve_compose_model_route

        monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
        for intent in (
            INTENT_ASK_PRODUCT,
            INTENT_ASK_PRICE,
            "product_availability",
        ):
            route = resolve_compose_model_route(
                intent_name=intent,
                reply_state=BrainReplyState(
                    primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
                ),
            )
            assert route.model == "gpt-4o-mini", intent

    def test_template_path_skips_humanizer(self) -> None:
        raw = "نعم، عندنا عسل الطلح، تبي أي حجم؟"
        out = _humanize(raw, chosen_path="template")
        assert out == raw
