"""Greeting + commerce inquiry must not collapse to greeting-only social route."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.dedup_order_state_gate import inbound_is_commerce_inquiry_turn  # noqa: E402
from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: E402
    classify_commerce_turn_kind,
    CommerceTurnKind,
    extract_inquiry_subject,
    has_embedded_commerce_inquiry_beyond_greeting,
    is_commerce_inquiry_turn,
)
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.solution_seeking import _is_bare_availability_inquiry  # noqa: E402
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.intent.non_commerce_classifier import classify_non_commerce  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_GREETING,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
)


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        has_active_integration=True,
        orderable=True,
    )


def _ctx(message: str, *, intent: Intent | None = None) -> BrainContext:
    resolved = intent or intent_rules.match(message) or Intent(
        name="general",
        confidence=0.55,
        raw_message=message,
    )
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        message=message,
        intent=resolved,
        state=MerchantConversationState(greeted=True),
        facts=_facts(),
    )


class TestGreetingCommerceInquiryBreaker:
    def test_pure_morning_greeting_stays_greeting(self) -> None:
        message = "صباح الخير"
        intent = intent_rules.match(message)
        assert intent is not None
        assert intent.name == INTENT_GREETING
        assert not has_embedded_commerce_inquiry_beyond_greeting(message)
        assert classify_non_commerce(message) is None

        verdict = resolve_current_turn_social_non_commerce(message, intent=intent)
        assert verdict.matched is True
        assert verdict.category == "greeting"

        decision = DefaultDecisionEngine().decide(_ctx(message, intent=intent))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") != "social_persona_ack" or (
            decision.args.get("persona_kind") == "greeting"
        )

    @pytest.mark.parametrize(
        "message",
        [
            "صباح الخير\nفيه عندك طرود نحل؟",
            "صباح الخير\nفي عندك طرود نحل ؟",
        ],
    )
    def test_greeting_plus_availability_not_greeting_only(self, message: str) -> None:
        assert has_embedded_commerce_inquiry_beyond_greeting(message)
        assert is_commerce_inquiry_turn(message)
        assert classify_commerce_turn_kind(message) == CommerceTurnKind.AVAILABILITY
        assert extract_inquiry_subject(message) == "طرود نحل"
        assert classify_non_commerce(message) is None

        intent = intent_rules.match(message)
        assert intent is not None
        assert intent.name != INTENT_SOCIAL
        assert intent.slots.get("social_category") != "morning_greeting"

        verdict = resolve_current_turn_social_non_commerce(message, intent=intent)
        assert verdict.matched is False

        decision = DefaultDecisionEngine().decide(_ctx(message, intent=intent))
        assert decision.args.get("topic") != "social_persona_ack"
        assert "social courtesy ack" not in (decision.reason or "")
        assert _is_bare_availability_inquiry(message)

    def test_salaam_plus_product_availability_not_greeting_only(self) -> None:
        message = "السلام عليكم\nهل عسل السمر متوفر؟"
        assert has_embedded_commerce_inquiry_beyond_greeting(message)

        intent = intent_rules.match(message)
        assert intent is not None
        assert intent.name != INTENT_SOCIAL
        assert intent.name != INTENT_GREETING

        verdict = resolve_current_turn_social_non_commerce(message, intent=intent)
        assert verdict.matched is False

        decision = DefaultDecisionEngine().decide(_ctx(message, intent=intent))
        assert decision.args.get("topic") != "social_persona_ack"

    def test_greeting_plus_price_not_greeting_only(self) -> None:
        message = "هلا\nكم سعر عسل الطلح؟"
        assert has_embedded_commerce_inquiry_beyond_greeting(message)

        intent = intent_rules.match(message)
        assert intent is not None
        assert intent.name != INTENT_SOCIAL

        decision = DefaultDecisionEngine().decide(_ctx(message, intent=intent))
        assert decision.args.get("topic") != "social_persona_ack"

    def test_greeting_plus_browse_types_not_greeting_only(self) -> None:
        message = "صباح الخير\nوش الأنواع المتوفرة؟"
        assert has_embedded_commerce_inquiry_beyond_greeting(message)
        assert classify_commerce_turn_kind(message) == CommerceTurnKind.BROWSE

        intent = intent_rules.match(message)
        assert intent is not None
        assert intent.name != INTENT_SOCIAL

        decision = DefaultDecisionEngine().decide(_ctx(message, intent=intent))
        assert decision.args.get("topic") != "social_persona_ack"

    def test_kb_route_eligible_after_social_break(self) -> None:
        """Availability/KB path must remain reachable (no db → None, not social)."""
        message = "صباح الخير\nفيه عندك طرود نحل؟"
        ctx = _ctx(message)
        assert try_non_catalog_availability_kb_decision(ctx) is None
        assert extract_inquiry_subject(message) == "طرود نحل"

    def test_live_regression_message_route(self) -> None:
        message = "صباح الخير\nفيه عندك طرود نحل؟"
        intent = intent_rules.match(message)
        decision = DefaultDecisionEngine().decide(_ctx(message, intent=intent))

        assert intent.name != INTENT_SOCIAL
        assert decision.args.get("topic") != "social_persona_ack"
        assert "morning_greeting" not in (decision.reason or "")


class TestGreetingCommerceRegressions:
    def test_social_only_thanks_stays_social(self) -> None:
        message = "جزاك الله خير"
        intent = intent_rules.match(message)
        assert intent is not None
        assert intent.name == INTENT_SOCIAL

        verdict = resolve_current_turn_social_non_commerce(message, intent=intent)
        assert verdict.matched is True

    def test_stale_fulfillment_still_breaks_on_greeting_availability(self) -> None:
        message = "صباح الخير\nفي عندك طرود نحل ؟"
        assert inbound_is_commerce_inquiry_turn(message)

    def test_general_image_without_caption_stays_d6d(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (  # noqa: PLC0415
            build_safe_general_image_facts,
        )

        vision = "[وصف الصورة المرسلة] لقطة شاشة من منشور اجتماعي قصير"
        facts = build_safe_general_image_facts(
            message=vision,
            inbound_metadata={"vision_text": "لقطة شاشة من منشور اجتماعي"},
        )
        assert facts.get("scene_type") == "social_media_screenshot"
        assert not has_embedded_commerce_inquiry_beyond_greeting(vision)
