"""
Post-delivery review request automation — emitter, guards, idempotency, fake send.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from commerce_scenario_fixtures import (
    DEFAULT_PHONE_E164,
    enable_tenant_autopilot,
    make_scenario_db,
    persona_delivered_order,
    persona_new_customer,
    persona_paused_conversation,
    persona_returning_with_address,
    persona_store_ai_off,
    seed_order,
    seed_post_delivery_review_automation,
    seed_review_request_template,
)
from commerce_scenario_runner import AIScenarioRunner
from core import automation_emitters
from core.automation_triggers import AutomationTrigger
from core.post_delivery_review_request import POST_DELIVERY_REVIEW_DELAY_HOURS
from models import AutomationEvent, Order


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_eligible_delivered(db, world, *, hours_ago: int = 25) -> Order:
    delivered_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ext_id = f"nahla-wa-{world.tenant.id}-{world.conversation.id}"
    return seed_order(
        db,
        world.tenant.id,
        status="delivered",
        external_id=ext_id,
        external_order_number="NHL-8800",
        customer_info={"phone": world.phone_e164, "name": "Scenario Customer"},
        line_items=[{"title": "Sample Product", "quantity": 1, "unit_price": "99"}],
        extra_metadata={
            "delivered_at": delivered_at.isoformat(),
            "review_request_sent": False,
        },
    )


def _prepare_automation_stack(db, tenant_id: int) -> None:
    seed_post_delivery_review_automation(db, tenant_id)
    seed_review_request_template(db, tenant_id)
    enable_tenant_autopilot(db, tenant_id)


async def _scan_and_process(db, tenant_id: int, runner: AIScenarioRunner, *, now=None) -> int:
    with patch("core.billing.has_billing_access", return_value=True):
        with runner.fake_sender.patch():
            emitted = automation_emitters.scan_post_delivery_review_requests(
                db, tenant_id, now=now or _now_naive(),
            )
            from core.automation_engine import process_pending_events  # noqa: PLC0415

            await process_pending_events(db, tenant_id, skip_autopilot_check=False)
            return emitted


class TestPostDeliveryReviewEligible:
    def test_eligible_delivered_order_sends_once_and_stamps_flag(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        _prepare_automation_stack(db, world.tenant.id)
        order = _seed_eligible_delivered(db, world, hours_ago=25)
        runner = AIScenarioRunner(world)

        emitted = asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        db.refresh(order)

        assert emitted == 1
        assert len(runner.fake_sender.sent) == 1
        assert runner.fake_sender.sent[0].type == "template"
        assert "review_request" in runner.fake_sender.sent[0].body
        meta = dict(order.extra_metadata or {})
        assert meta.get("review_request_sent") is True
        assert meta.get("review_requested_at")

    def test_idempotency_second_scan_sends_zero(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        _prepare_automation_stack(db, world.tenant.id)
        _seed_eligible_delivered(db, world, hours_ago=25)
        runner = AIScenarioRunner(world)
        now = _now_naive()

        first = asyncio.run(_scan_and_process(db, world.tenant.id, runner, now=now))
        second_emit = automation_emitters.scan_post_delivery_review_requests(
            db, world.tenant.id, now=now,
        )

        assert first == 1
        assert second_emit == 0
        assert len(runner.fake_sender.sent) == 1


class TestPostDeliveryReviewIneligible:
    def test_too_recent_delivery_not_sent(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        _prepare_automation_stack(db, world.tenant.id)
        _seed_eligible_delivered(db, world, hours_ago=1)
        runner = AIScenarioRunner(world)

        emitted = asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        assert emitted == 0
        assert runner.fake_sender.sent == []

    @pytest.mark.parametrize("status", ["shipped", "confirmed", "pending_payment"])
    def test_not_delivered_status_not_sent(self, status: str) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        _prepare_automation_stack(db, world.tenant.id)
        delivered_at = datetime.now(timezone.utc) - timedelta(hours=25)
        seed_order(
            db,
            world.tenant.id,
            status=status,
            customer_info={"phone": world.phone_e164},
            extra_metadata={
                "delivered_at": delivered_at.isoformat(),
                "review_request_sent": False,
            },
        )
        runner = AIScenarioRunner(world)
        emitted = asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        assert emitted == 0
        assert runner.fake_sender.sent == []

    def test_store_ai_off_not_sent(self) -> None:
        db, _ = make_scenario_db()
        world = persona_store_ai_off(db)
        _prepare_automation_stack(db, world.tenant.id)
        _seed_eligible_delivered(db, world, hours_ago=25)
        runner = AIScenarioRunner(world)
        emitted = asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        assert emitted == 0
        assert runner.fake_sender.sent == []

    def test_conversation_paused_not_sent(self) -> None:
        db, _ = make_scenario_db()
        world = persona_paused_conversation(db)
        _prepare_automation_stack(db, world.tenant.id)
        _seed_eligible_delivered(db, world, hours_ago=25)
        runner = AIScenarioRunner(world)
        emitted = asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        assert emitted == 0
        assert runner.fake_sender.sent == []

    def test_handoff_active_not_sent(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        world.conversation.handoff_active = True
        db.add(world.conversation)
        db.commit()
        _prepare_automation_stack(db, world.tenant.id)
        _seed_eligible_delivered(db, world, hours_ago=25)
        runner = AIScenarioRunner(world)
        emitted = asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        assert emitted == 0
        assert runner.fake_sender.sent == []

    @pytest.mark.parametrize("status", ["cancelled", "refunded"])
    def test_cancelled_or_refunded_not_sent(self, status: str) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        _prepare_automation_stack(db, world.tenant.id)
        delivered_at = datetime.now(timezone.utc) - timedelta(hours=25)
        seed_order(
            db,
            world.tenant.id,
            status=status,
            customer_info={"phone": world.phone_e164},
            extra_metadata={
                "delivered_at": delivered_at.isoformat(),
                "review_request_sent": False,
            },
        )
        runner = AIScenarioRunner(world)
        emitted = asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        assert emitted == 0
        assert runner.fake_sender.sent == []


class TestPostDeliveryReviewSafety:
    def test_fake_sender_captures_outbound_no_real_provider(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        _prepare_automation_stack(db, world.tenant.id)
        _seed_eligible_delivered(db, world, hours_ago=25)
        runner = AIScenarioRunner(world)
        asyncio.run(_scan_and_process(db, world.tenant.id, runner))
        assert runner.fake_sender.real_send_attempted is True
        assert all(r.path for r in runner.fake_sender.sent)

    def test_emitter_records_automation_event(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        _prepare_automation_stack(db, world.tenant.id)
        _seed_eligible_delivered(db, world, hours_ago=25)
        count = automation_emitters.scan_post_delivery_review_requests(db, world.tenant.id)
        events = db.query(AutomationEvent).all()
        assert count == 1
        assert len(events) == 1
        assert events[0].event_type == AutomationTrigger.POST_DELIVERY_REVIEW_REQUEST_DUE.value
        assert events[0].payload.get("nahla_source_key") == "review_request"

    def test_generality_uses_generic_product_fixture(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        _prepare_automation_stack(db, world.tenant.id)
        order = seed_order(
            db,
            world.tenant.id,
            status="delivered",
            customer_info={"phone": world.phone_e164, "name": "Buyer"},
            line_items=[{"title": "Generic Widget", "quantity": 1}],
            extra_metadata={
                "delivered_at": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(),
                "review_request_sent": False,
            },
        )
        count = automation_emitters.scan_post_delivery_review_requests(db, world.tenant.id)
        db.refresh(order)
        assert count == 1
        assert dict(order.extra_metadata or {}).get("review_request_sent") is True
        events = db.query(AutomationEvent).all()
        assert events[0].payload.get("product_name") == "Generic Widget"

    def test_default_delay_constant_is_24_hours(self) -> None:
        assert POST_DELIVERY_REVIEW_DELAY_HOURS == 24

    def test_persona_delivered_order_seed_compatible(self) -> None:
        db, _ = make_scenario_db()
        world = persona_delivered_order(db)
        _prepare_automation_stack(db, world.tenant.id)
        count = automation_emitters.scan_post_delivery_review_requests(db, world.tenant.id)
        assert count == 1
        meta = dict(world.order.extra_metadata or {})
        assert meta.get("review_request_sent") is True
