"""Commerce cheap-route enforcement and availability reply warm-up."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: E402
    commerce_prompt_max_chars,
    explain_commerce_prompt_slim_gate,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.cost.model_router import (  # noqa: E402
    detect_compose_standard_signals,
    is_routine_daily_commerce_compose,
    resolve_compose_model_route,
)
from modules.ai.brain.cost.model_router_audit import TIER_CHEAP, TIER_STANDARD  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    build_friendly_availability_conflict_reply,
)
from modules.ai.brain.commerce_reply_humanizer import (  # noqa: E402
    apply_commerce_reply_humanizer,
    detect_product_category,
    pick_category_emojis_for_reply,
    variant_followup_for_product,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    Intent,
    MerchantConversationState,
)
from modules.ai.orchestrator.llm_cost_audit import resolve_model_for_provider  # noqa: E402

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_DRY_AVAILABILITY_REPLY = "متوفر عسل طلح بعدة أحجام، أي حجم يناسبك؟"


@pytest.fixture(autouse=True)
def _router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP", "gpt-4o-mini")
    monkeypatch.setenv("NAHLA_MODEL_STANDARD", "claude-sonnet-4-6")
    monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "true")


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        orderable=True,
        store_name="متجر تجريبي",
    )


def _ctx(
    *,
    intent_name: str,
    message: str,
    reply_state: BrainReplyState | None = None,
) -> BrainContext:
    rs = reply_state or BrainReplyState(
        store_name="متجر تجريبي",
        intent_name=intent_name,
        primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
    )
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=message,
        intent=Intent(name=intent_name, confidence=0.95, raw_message=message),
        state=MerchantConversationState(),
        facts=_facts(),
        reply_state=rs,
    )


class TestRoutineCommerceCheapRoute:
    @pytest.mark.parametrize(
        ("message", "intent"),
        [
            ("هل عندكم عسل طلح؟", INTENT_SOLUTION_SEEKING_COMMERCE),
            ("هل عندكم سدر؟", INTENT_ASK_PRODUCT),
            ("هل عندكم فساتين؟", INTENT_ASK_PRODUCT),
            ("عندكم جوالات؟", INTENT_ASK_PRODUCT),
            ("عندكم أقلام؟", INTENT_ASK_PRODUCT),
            ("متوفر الشاحن؟", INTENT_ASK_PRODUCT),
            ("بكم العباية؟", INTENT_ASK_PRICE),
            ("بكم عسل الطلح؟", INTENT_ASK_PRICE),
            ("متوفر السدر؟", INTENT_ASK_PRODUCT),
        ],
    )
    def test_resolve_route_cheap(
        self,
        message: str,
        intent: str,
    ) -> None:
        route = resolve_compose_model_route(
            intent_name=intent,
            reply_state=BrainReplyState(
                store_name="x",
                intent_name=intent,
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
                policy_reason="service_availability_not_handoff",
                intent_priority_focus="product_availability — أجيبي على توفر المنتج",
            ),
        )
        assert route.tier == TIER_CHEAP
        assert route.provider == "openai_compatible"
        assert route.model == "gpt-4o-mini"
        assert route.provider_hint == "openai_compatible"
        assert "sonnet" not in route.model.lower()

    def test_no_anthropic_hint_for_cheap_intents(self) -> None:
        for intent in (
            INTENT_ASK_PRODUCT,
            INTENT_ASK_PRICE,
            "product_availability",
            "product_reference",
            INTENT_SOLUTION_SEEKING_COMMERCE,
        ):
            route = resolve_compose_model_route(intent_name=intent)
            assert route.provider_hint != "anthropic"
            assert route.provider == "openai_compatible"

    def test_routine_commerce_not_upgraded_by_soft_policy(self) -> None:
        rs = BrainReplyState(
            store_name="x",
            intent_name=INTENT_ASK_PRODUCT,
            policy_reason="service_availability_not_handoff",
        )
        needs, reason = detect_compose_standard_signals(
            intent_name=INTENT_ASK_PRODUCT,
            reply_state=rs,
        )
        assert needs is False
        assert reason == ""

    def test_llm_compose_uses_openai_compatible(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx(
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            message="هل عندكم عسل طلح؟",
        )
        result = ActionResult(success=True, data={})
        captured: dict = {}

        def _fake_generate(**kwargs):
            captured.update(kwargs)
            payload = MagicMock()
            payload.reply_text = "رد"
            payload.provider_used = "openai_compatible"
            payload.metadata = {"model": "gpt-4o-mini"}
            return payload

        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            side_effect=_fake_generate,
        ):
            asyncio.run(composer._llm_compose(ctx, result))

        assert captured["provider_hint"] == "openai_compatible"
        router = captured["prompt_overrides"]["__model_router"]
        assert router["model"] == "gpt-4o-mini"
        assert router["tier"] == TIER_CHEAP


class TestCommerceSlimOnTopicShift:
    def test_stale_variant_checkout_allows_slim_on_topic_shift(self) -> None:
        state = BrainReplyState(
            store_name="x",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            stage="exploring",
            known_facts={
                "checkout_preparation": {"awaiting_variant_choice": True},
                "state_relevance_verdict": {
                    "detected_topic_shift": True,
                    "payment_state_relevant": False,
                    "fulfillment_state_relevant": True,
                    "active_workflows": ["awaiting_variant_choice"],
                },
            },
        )
        eligible, reason, meta = explain_commerce_prompt_slim_gate(state)
        assert eligible is True, (reason, meta)
        assert meta.get("state_topic_shift") is True

    def test_slim_prompt_under_max_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_MAX_CHARS", "25000")
        state = BrainReplyState(
            store_name="متجر",
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            stage="discovery",
            merchant_context={"tenant_id": 7, "ai_settings": {}},
        )
        prompt = build_brain_reply_prompt(state)
        assert len(prompt) <= commerce_prompt_max_chars()


class TestProviderFallbackModel:
    def test_anthropic_fallback_ignores_gpt_override(self) -> None:
        model = resolve_model_for_provider(
            {"model_override": "gpt-4o-mini", "model_tier": TIER_CHEAP},
            provider="anthropic",
            default="claude-haiku-4-5",
        )
        assert model.startswith("claude")


class TestAvailabilityWarmReply:
    def test_friendly_conflict_reply_not_dry_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            lambda *args, **kwargs: "عسل طلح",
        )
        reply = build_friendly_availability_conflict_reply(
            MagicMock(),
            inbound_text="هل عندكم عسل طلح؟",
        )
        assert reply != _DRY_AVAILABILITY_REPLY
        assert "أبشر" in reply
        assert _EMOJI_RE.search(reply)
        assert len(_EMOJI_RE.findall(reply)) <= 2
        assert "وش الحجم" in reply

    def test_humanizer_rejects_legacy_dry_phrase(self) -> None:
        out = apply_commerce_reply_humanizer(
            _DRY_AVAILABILITY_REPLY,
            inbound_text="هل عندكم عسل طلح؟",
            intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="llm",
            product_title="عسل طلح",
        ).reply
        assert out != _DRY_AVAILABILITY_REPLY
        assert _EMOJI_RE.search(out)
        assert len(_EMOJI_RE.findall(out)) <= 2

    def test_no_sonnet_in_cheap_route(self) -> None:
        route = resolve_compose_model_route(intent_name=INTENT_ASK_PRODUCT)
        assert "sonnet" not in route.model.lower()
        assert route.tier != TIER_STANDARD


class TestGeneralityNoProductHardcoding:
    """Warm replies vary by extracted label/category — not one honey template."""

    @pytest.mark.parametrize(
        ("label", "inbound", "expected_category", "expected_emoji_chars", "followup_fragment"),
        [
            ("عسل طلح", "هل عندكم عسل طلح؟", "honey", ("🍯", "🌿"), "وش الحجم"),
            ("السدر", "هل عندكم سدر؟", "honey", ("🍯", "🌿"), "وش الحجم"),
            ("فساتين", "هل عندكم فساتين؟", "dress", ("👗",), "المقاس"),
            ("جوالات", "عندكم جوالات؟", "mobile", ("📱",), "الموديل"),
            ("أقلام", "عندكم أقلام؟", "stationery", ("✏️", "📚"), "الأنواع"),
            ("الشاحن", "متوفر الشاحن؟", "electronics", ("🔌", "📱"), "الموديل"),
            ("", "عندكم منتجات؟", "general", ("🛒", "✨"), "الخيار"),
        ],
    )
    def test_category_emojis_and_followup_vary(
        self,
        label: str,
        inbound: str,
        expected_category: str,
        expected_emoji_chars: tuple[str, ...],
        followup_fragment: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert detect_product_category(f"{label} {inbound}", product_title=label) == expected_category
        emojis = pick_category_emojis_for_reply(product_title=label, inbound_text=inbound)
        assert any(ch in emojis for ch in expected_emoji_chars)
        assert "🍯" not in emojis or expected_category == "honey"
        followup = variant_followup_for_product(product_title=label, inbound_text=inbound)
        assert followup_fragment in followup

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            lambda *args, **kwargs: label or "المنتج",
        )
        guard_reply = build_friendly_availability_conflict_reply(
            MagicMock(),
            inbound_text=inbound,
        )
        assert guard_reply != _DRY_AVAILABILITY_REPLY
        assert "أبشر" in guard_reply
        assert followup_fragment in guard_reply
        if label:
            assert label in guard_reply
        emoji_text = "".join(_EMOJI_RE.findall(guard_reply))
        if expected_category == "honey":
            assert "🍯" in emoji_text
        elif expected_category != "general":
            assert "🍯" not in emoji_text

    def test_different_products_do_not_share_identical_guard_reply(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cases = [
            ("عسل طلح", "هل عندكم عسل طلح؟"),
            ("فساتين", "هل عندكم فساتين؟"),
            ("جوالات", "عندكم جوالات؟"),
        ]
        replies: list[str] = []
        for label, inbound in cases:

            def _label_fn(*args, _label=label, **kwargs):
                return _label

            monkeypatch.setattr(
                "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
                _label_fn,
            )
            replies.append(
                build_friendly_availability_conflict_reply(
                    MagicMock(),
                    inbound_text=inbound,
                )
            )
        assert len(set(replies)) == len(cases)
        assert all("🍯" in r for r in replies[:1])
        assert all("🍯" not in r for r in replies[1:])
