"""
test_ai_commerce_scenario_kb_delivery_fixes.py
──────────────────────────────────────────────
Scenario-runner regression tests for KB availability and delivery
confirmation detection fixes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from commerce_scenario_fixtures import make_scenario_db, persona_kb_inquiry  # noqa: E402
from commerce_scenario_runner import AIScenarioRunner  # noqa: E402
from core.payment_intent import looks_like_delivery_confirmation  # noqa: E402
from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: E402
    CommerceTurnKind,
    classify_commerce_turn_kind,
    extract_inquiry_subject,
    has_explicit_order_select_signal,
)
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    TOPIC_KB_AVAILABILITY_FACTS,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    is_explicit_order_tracking_request,
)
from modules.ai.brain.commerce.solution_seeking import _is_bare_availability_inquiry  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    Intent,
    MerchantConversationState,
)


def _brain_ctx(world, message: str) -> BrainContext:
    ctx = BrainContext(
        tenant_id=world.tenant.id,
        customer_phone=world.phone_e164,
        customer_id=world.customer.id,
        conversation_id=world.conversation.id,
        message=message,
        intent=Intent(name="general", confidence=0.5, raw_message=message),
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            product_count=1,
            orderable=True,
            has_active_integration=True,
            store_name="Scenario Store",
        ),
        history=[],
    )
    ctx._db = world.db  # noqa: SLF001
    return ctx


def _assert_availability_inquiry(message: str) -> None:
    assert classify_commerce_turn_kind(message) == CommerceTurnKind.AVAILABILITY
    assert _is_bare_availability_inquiry(message) is True
    assert extract_inquiry_subject(message)
    intent = rules.match(message)
    assert intent is None or intent.name not in {INTENT_TRACK_ORDER, INTENT_START_ORDER}
    assert is_explicit_order_tracking_request(message) is False
    assert has_explicit_order_select_signal(message) is False


class TestAvailabilityInquiryDetection:
    @pytest.mark.parametrize(
        "message",
        [
            "هل السدر متوفر؟",
            "هل عندكم سدر؟",
            "السدر موجود؟",
        ],
    )
    def test_availability_phrases_recognized(self, message: str) -> None:
        _assert_availability_inquiry(message)

    @pytest.mark.parametrize(
        "message",
        [
            "هل السدر متوفر؟",
            "هل عندكم سدر؟",
            "السدر موجود؟",
        ],
    )
    def test_availability_inquiry_does_not_create_order(self, message: str) -> None:
        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)
        runner = AIScenarioRunner(world)
        before = runner.order_count()
        runner.run_inbound_only(message)
        assert runner.order_count() == before

    def test_sidr_routes_to_kb_decision_when_section_seeded(self) -> None:
        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)
        decision = try_non_catalog_availability_kb_decision(
            _brain_ctx(world, "هل السدر متوفر؟"),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS

    def test_explicit_order_not_bare_availability(self) -> None:
        message = "أبغى سدر نصف كيلo"
        assert _is_bare_availability_inquiry(message) is False
        assert classify_commerce_turn_kind(message) == CommerceTurnKind.ORDER


class TestDeliveryConfirmationDetection:
    @pytest.mark.parametrize(
        "message",
        [
            "وصل الطلب",
            "وصلني الطلب",
            "استلمت الطلب",
            "تم الاستلام",
        ],
    )
    def test_delivery_confirmation_phrases_recognized(self, message: str) -> None:
        assert looks_like_delivery_confirmation(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "وصل الطلب",
            "وصلني الطلب",
            "استلمت الطلب",
            "تم الاستلام",
        ],
    )
    def test_delivery_confirmation_does_not_create_order(self, message: str) -> None:
        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)
        runner = AIScenarioRunner(world)
        before = runner.order_count()
        runner.run_inbound_only(message)
        assert runner.order_count() == before
        intent = rules.match(message)
        assert intent is None or intent.name not in {INTENT_START_ORDER}

    def test_shipping_duration_not_delivery_confirmation(self) -> None:
        message = "متى يوصل الطلب؟"
        assert looks_like_delivery_confirmation(message) is False
        assert is_explicit_order_tracking_request(message) is False

    def test_payment_transfer_not_delivery_confirmation(self) -> None:
        assert looks_like_delivery_confirmation("وصل التحويل") is False
