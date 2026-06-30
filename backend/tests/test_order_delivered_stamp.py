"""
Tests for operational delivered_at stamping on order status transitions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from commerce_scenario_fixtures import (
    enable_tenant_autopilot,
    make_scenario_db,
    persona_returning_with_address,
    seed_order,
    seed_post_delivery_review_automation,
    seed_review_request_template,
)
from commerce_scenario_runner import AIScenarioRunner
from core import automation_emitters
from core.order_delivered_stamp import apply_order_status, stamp_order_delivered_at_if_needed
from core.post_delivery_review_request import read_delivered_at


def _parse_iso(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


class TestDeliveredAtStamping:
    def test_transition_to_delivered_stamps_delivered_at(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        order = seed_order(
            db,
            world.tenant.id,
            status="shipped",
            customer_info={"phone": world.phone_e164},
            extra_metadata={},
        )
        fixed = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        stamped = apply_order_status(order, "delivered", now=fixed)
        db.commit()
        db.refresh(order)

        assert stamped is True
        raw = dict(order.extra_metadata or {}).get("delivered_at")
        assert raw
        parsed = _parse_iso(str(raw))
        assert parsed.tzinfo is not None
        assert parsed == fixed

    def test_existing_delivered_at_is_preserved(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        old_ts = "2026-05-01T08:00:00+00:00"
        order = seed_order(
            db,
            world.tenant.id,
            status="delivered",
            customer_info={"phone": world.phone_e164},
            extra_metadata={"delivered_at": old_ts},
        )
        newer = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)

        stamped = apply_order_status(order, "delivered", now=newer)
        db.commit()
        db.refresh(order)

        assert stamped is False
        assert dict(order.extra_metadata or {}).get("delivered_at") == old_ts

    @pytest.mark.parametrize(
        "status",
        ["pending_payment", "confirmed", "ready_to_ship", "shipped", "cancelled"],
    )
    def test_non_delivered_status_does_not_stamp(self, status: str) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        order = seed_order(
            db,
            world.tenant.id,
            status="shipped",
            customer_info={"phone": world.phone_e164},
            extra_metadata={"review_request_sent": False},
        )

        apply_order_status(order, status, now=datetime.now(timezone.utc))
        db.commit()
        db.refresh(order)

        assert "delivered_at" not in (order.extra_metadata or {})

    def test_metadata_merge_preserves_existing_keys(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        order = seed_order(
            db,
            world.tenant.id,
            status="shipped",
            customer_info={"phone": world.phone_e164},
            extra_metadata={
                "review_request_sent": False,
                "some_existing_key": "value",
            },
        )

        apply_order_status(order, "delivered", now=datetime.now(timezone.utc))
        db.commit()
        db.refresh(order)

        meta = dict(order.extra_metadata or {})
        assert meta.get("some_existing_key") == "value"
        assert meta.get("review_request_sent") is False
        assert meta.get("delivered_at")

    def test_store_sync_webhook_path_stamps_on_delivered(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        order = seed_order(
            db,
            world.tenant.id,
            status="shipped",
            external_id="salla-order-9001",
            source="salla",
            customer_info={"phone": world.phone_e164},
            extra_metadata={"created_at": datetime.now(timezone.utc).isoformat()},
        )
        prev = order.status
        order.status = "delivered"
        order.extra_metadata = {
            **(order.extra_metadata or {}),
            "created_at": (order.extra_metadata or {}).get("created_at"),
        }
        stamp_order_delivered_at_if_needed(order, previous_status=prev)
        db.commit()
        db.refresh(order)
        assert read_delivered_at(order) is not None


class TestReviewAutomationIntegrationSmoke:
    def test_delivered_transition_enables_review_scan(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        seed_post_delivery_review_automation(db, world.tenant.id)
        seed_review_request_template(db, world.tenant.id)
        enable_tenant_autopilot(db, world.tenant.id)

        delivered_at = datetime.now(timezone.utc) - timedelta(hours=25)
        order = seed_order(
            db,
            world.tenant.id,
            status="shipped",
            customer_info={"phone": world.phone_e164, "name": "Buyer"},
            extra_metadata={"review_request_sent": False},
        )
        apply_order_status(order, "delivered", now=delivered_at)
        db.commit()

        runner = AIScenarioRunner(world)
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

        async def _run() -> int:
            with patch("core.billing.has_billing_access", return_value=True):
                with runner.fake_sender.patch():
                    emitted = automation_emitters.scan_post_delivery_review_requests(
                        db, world.tenant.id, now=now_naive,
                    )
                    from core.automation_engine import process_pending_events  # noqa: PLC0415

                    await process_pending_events(db, world.tenant.id)
                    return emitted

        emitted = asyncio.run(_run())
        db.refresh(order)

        assert emitted == 1
        assert len(runner.fake_sender.sent) == 1
        assert "review_request" in runner.fake_sender.sent[0].body
        assert dict(order.extra_metadata or {}).get("review_request_sent") is True
