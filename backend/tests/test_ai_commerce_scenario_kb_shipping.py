"""
test_ai_commerce_scenario_kb_shipping.py
────────────────────────────────────────
Extended AI commerce scenario tests — KB/FAQ, pre-order shipping,
shipped tracking, delivered acknowledgement, and review emitter gap.

Deferred TODO (future PR):
- Full end-to-end KB compose with LLM fixtures for every FAQ variant
- Timed post-delivery review request emitter + idempotent send job
- Delivery-confirmation token coverage for bare «وصل الطلب»
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from commerce_scenario_fixtures import (  # noqa: E402
    make_scenario_db,
    persona_delivered_order,
    persona_kb_inquiry,
    persona_new_customer,
    persona_paused_conversation,
    persona_shipped_order,
)
from commerce_scenario_runner import AIScenarioRunner, TextInbound  # noqa: E402
from core.active_order_context import prepare_tracking_follow_up_decision  # noqa: E402
from core.payment_intent import looks_like_delivery_confirmation  # noqa: E402
from modules.ai.brain.commerce.conversation_context_reset import (  # noqa: E402
    maybe_reset_stale_order_context,
)
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    TOPIC_KB_AVAILABILITY_FACTS,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    is_explicit_order_tracking_request,
    is_general_shipping_duration_inquiry,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.intent.link_disambiguation import looks_like_tracking_link_request  # noqa: E402
from modules.ai.brain.postprocess.shipment_evidence import evaluate_shipment_evidence  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_ASK_SHIPPING,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
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
            shipping_policy="التوصيل 2-4 أيام داخل السعودية",
        ),
        history=[],
    )
    ctx._db = world.db  # noqa: SLF001 — brain route owners read private db handle
    return ctx


def _assert_no_order_created(runner: AIScenarioRunner, message: str) -> None:
    before = runner.order_count()
    result = runner.run_inbound_only(message)
    assert runner.order_count() == before
    assert result.errors == []


class TestFAQKnowledgeScenarios:
    @pytest.mark.parametrize(
        "message",
        [
            "هل عسلكم طبيعي؟",
            "من وين عسل الطلح؟",
            "هل السدر متوفر؟",
        ],
    )
    def test_faq_inquiry_does_not_create_order_or_request_address(self, message: str) -> None:
        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)
        runner = AIScenarioRunner(world)
        _assert_no_order_created(runner, message)

    @pytest.mark.parametrize(
        "message",
        [
            "هل عسلكم طبيعي؟",
            "من وين عسل الطلح؟",
            "هل السدر متوفر؟",
        ],
    )
    def test_faq_inquiry_not_routed_to_tracking_or_start_order(self, message: str) -> None:
        intent = rules.match(message)
        assert intent is None or intent.name not in {INTENT_TRACK_ORDER, INTENT_START_ORDER}
        assert is_explicit_order_tracking_request(message) is False

    def test_sidr_availability_kb_route_after_detection_fix(self) -> None:
        from modules.ai.brain.commerce.solution_seeking import _is_bare_availability_inquiry  # noqa: PLC0415

        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)
        message = "هل السدر متوفر؟"
        assert _is_bare_availability_inquiry(message) is True
        decision = try_non_catalog_availability_kb_decision(_brain_ctx(world, message))
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS


class TestPreOrderShippingScenarios:
    @pytest.mark.parametrize(
        "message",
        [
            "كم مدة التوصيل لمكة؟",
            "متى يوصل الطلب؟",
        ],
    )
    def test_pre_order_shipping_stays_policy_not_tracking(self, message: str) -> None:
        intent = rules.match(message)
        assert intent is not None
        assert intent.name == INTENT_ASK_SHIPPING
        assert is_explicit_order_tracking_request(message) is False

    def test_pre_order_shipping_does_not_create_order(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        runner = AIScenarioRunner(world)
        for message in ("كم مدة التوصيل لمكة؟", "متى يوصل الطلب؟"):
            _assert_no_order_created(runner, message)

    def test_bare_delivery_duration_is_policy_without_prior_order(self) -> None:
        assert is_general_shipping_duration_inquiry("متى يوصل الطلب؟") is True
        assert is_explicit_order_tracking_request("متى يوصل الطلب؟") is False


class TestShippedOrderTrackingScenario:
    TRACKING_MESSAGE = "أرسل رقم التتبع"

    def test_tracking_request_detected_with_shipped_seed(self) -> None:
        db, _ = make_scenario_db()
        world = persona_shipped_order(db)
        bundle = AIScenarioRunner(world).commerce_bundle()
        assert is_explicit_order_tracking_request(
            self.TRACKING_MESSAGE,
            commerce_bundle=bundle,
        ) is True
        assert looks_like_tracking_link_request(
            self.TRACKING_MESSAGE,
            commerce_bundle=bundle,
        ) is True

    def test_tracking_follow_up_includes_order_reference_and_tracking(self) -> None:
        db, _ = make_scenario_db()
        world = persona_shipped_order(db)
        runner = AIScenarioRunner(world)
        bundle = runner.commerce_bundle()
        ctx = SimpleNamespace(
            tenant_id=world.tenant.id,
            commerce_bundle=bundle,
            state=MerchantConversationState(greeted=True, stage="complete"),
            history=[],
        )
        args = prepare_tracking_follow_up_decision(ctx)
        assert args.get("order_reference") == "NHL-7788"
        assert args.get("tracking_available") is True
        assert args.get("topic") == "tracking_link_follow_up"

        evidence = evaluate_shipment_evidence(commerce_bundle=bundle)
        assert evidence.evidence_ok is True
        assert evidence.tracking_present is True

        ctx_data = dict(bundle.get("active_order_context") or {})
        assert ctx_data.get("tracking_number") == "TRK123456"
        assert ctx_data.get("shipping_provider") == "smsa"

    def test_tracking_request_does_not_create_new_order(self) -> None:
        db, _ = make_scenario_db()
        world = persona_shipped_order(db)
        runner = AIScenarioRunner(world)
        before = runner.order_count()
        result = runner.run([TextInbound(self.TRACKING_MESSAGE)])
        assert runner.order_count() == before
        assert result.errors == []


class TestDeliveredOrderScenario:
    DELIVERED_MESSAGE = "وصل الطلب"

    def test_delivered_ack_does_not_open_new_order(self) -> None:
        db, _ = make_scenario_db()
        world = persona_delivered_order(db)
        runner = AIScenarioRunner(world)
        before = runner.order_count()
        result = runner.run_inbound_only(self.DELIVERED_MESSAGE)
        assert runner.order_count() == before
        prep = dict((result.conversation.extra_metadata or {}).get("brain_state", {}).get("order_prep") or {})
        assert prep.get("order_status") == "delivered"

    def test_delivered_ack_does_not_start_checkout_on_deterministic_path(self) -> None:
        db, _ = make_scenario_db()
        world = persona_delivered_order(db)
        runner = AIScenarioRunner(world)
        before = runner.order_count()
        runner.run([TextInbound("نعم أكد الطلب")])
        assert runner.order_count() == before

    def test_wasl_al_talb_recognized_as_delivery_confirmation(self) -> None:
        assert looks_like_delivery_confirmation(self.DELIVERED_MESSAGE) is True

    def test_delivered_state_reset_preserves_history_not_new_checkout(self) -> None:
        state = MerchantConversationState(
            stage="discovery",
            conversation_summary="عميل استلم طلب NHL-9900",
        )
        state.order_prep = OrderPreparationState(order_status="delivered")
        reset = maybe_reset_stale_order_context(state, self.DELIVERED_MESSAGE)
        assert reset == "order_delivered"
        assert state.conversation_summary == "عميل استلم طلب NHL-9900"


class TestReviewRequestEmitterGap:
    REVIEW_EMITTER_SCAN = "scan_post_delivery_review_requests"

    def test_review_emitter_scan_implemented(self) -> None:
        from core import automation_emitters  # noqa: PLC0415
        from core.automation_triggers import AutomationTrigger  # noqa: PLC0415

        assert hasattr(automation_emitters, self.REVIEW_EMITTER_SCAN)
        assert callable(getattr(automation_emitters, self.REVIEW_EMITTER_SCAN))
        trigger_values = {member.value for member in AutomationTrigger}
        assert AutomationTrigger.POST_DELIVERY_REVIEW_REQUEST_DUE.value in trigger_values

    def test_delivered_order_seed_ready_for_future_review_job(self) -> None:
        db, _ = make_scenario_db()
        world = persona_delivered_order(db)
        meta = dict(world.order.extra_metadata or {})
        assert meta.get("review_request_sent") is False
        assert meta.get("delivered_at")

    def test_paused_conversation_would_block_automation_send(self) -> None:
        db, _ = make_scenario_db()
        world = persona_paused_conversation(db)
        from core.automation_send_guard import should_block_automation_for_conversation  # noqa: PLC0415

        blocked = should_block_automation_for_conversation(
            db,
            tenant_id=world.tenant.id,
            customer_phone=world.phone,
            conversation=world.conversation,
        )
        assert blocked.block is True
        assert blocked.reason

    def test_fake_sender_can_capture_review_template_outbound(self) -> None:
        """Harness placeholder until post-delivery review emitter lands."""
        db, _ = make_scenario_db()
        world = persona_delivered_order(db)
        runner = AIScenarioRunner(world)
        with runner.fake_sender.patch():
            import asyncio

            asyncio.run(
                runner.fake_sender._capture_send(
                    json={
                        "type": "template",
                        "to": world.phone,
                        "template": {"name": "review_request"},
                    },
                )
            )
        assert len(runner.fake_sender.sent) == 1
        assert runner.fake_sender.sent[0].type == "template"
        assert "review_request" in runner.fake_sender.sent[0].body
        assert runner.fake_sender.real_send_attempted is True
