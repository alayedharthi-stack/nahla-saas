"""
backend/tests/test_wa_abandoned_order_draft.py
──────────────────────────────────────────────
PR-5 — WhatsApp abandoned draft-order reminders.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    AutomationEvent,
    Base,
    Conversation,
    Customer,
    GovernorSendLog,
    Order,
    SmartAutomation,
    Tenant,
)
from core import automation_emitters  # noqa: E402
from core.automation_triggers import AutomationTrigger  # noqa: E402
from core.send_governor import check as gov_check, record_sent  # noqa: E402
from core.wa_abandoned_order_draft import (  # noqa: E402
    REMINDER_ADDRESS,
    REMINDER_COMPLETE_ORDER,
    REMINDER_PAYMENT,
    resolve_wa_abandoned_draft_reminder,
)


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db) -> Tenant:
    t = Tenant(name="T", is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_customer(db, tenant_id: int, phone: str = "+966500000001") -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=phone.lstrip("+"),
        name="Cust",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _wa_order(
    db,
    tenant_id: int,
    *,
    status: str,
    line_items=None,
    meta=None,
    is_abandoned: bool = False,
    external_id: str = "nahla-wa-1-99",
    source: str = "whatsapp",
) -> Order:
    created = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    extra = {
        "created_via": "nahla_order_bridge",
        "origin": "whatsapp_ai",
        "created_at": created,
        **(meta or {}),
    }
    order = Order(
        tenant_id=tenant_id,
        external_id=external_id,
        external_order_number="NHL-100",
        status=status,
        source=source,
        is_abandoned=is_abandoned,
        line_items=line_items or [],
        customer_info={"phone": "+966500000001"},
        extra_metadata=extra,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _seed_automation(db, tenant_id: int) -> None:
    db.add(SmartAutomation(
        tenant_id=tenant_id,
        automation_type="abandoned_order_draft",
        trigger_event=AutomationTrigger.WA_ORDER_DRAFT_REMINDER_DUE.value,
        name="WA draft reminders",
        enabled=True,
        config={"delay_minutes": 60, "language": "ar"},
    ))
    db.commit()


class TestReminderEligibility:
    def test_draft_with_line_items_eligible(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        order = _wa_order(
            db, t.id, status="draft",
            line_items=[{"title": "عسل", "quantity": 1}],
        )
        plan = resolve_wa_abandoned_draft_reminder(order)
        assert plan is not None
        assert plan.reminder_kind == REMINDER_COMPLETE_ORDER

    def test_draft_without_products_not_eligible(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        order = _wa_order(db, t.id, status="draft", line_items=[])
        assert resolve_wa_abandoned_draft_reminder(order) is None

    def test_pending_customer_info_address_only(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        order = _wa_order(db, t.id, status="pending_customer_info")
        plan = resolve_wa_abandoned_draft_reminder(order)
        assert plan is not None
        assert plan.reminder_kind == REMINDER_ADDRESS

    def test_pending_payment_with_address(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        order = _wa_order(
            db, t.id, status="pending_payment",
            meta={"google_maps_url": "https://maps.google.com/?q=24.7,46.6"},
        )
        plan = resolve_wa_abandoned_draft_reminder(order)
        assert plan is not None
        assert plan.reminder_kind == REMINDER_PAYMENT

    @pytest.mark.parametrize("status", [
        "payment_submitted", "paid", "cancelled", "completed", "processing",
    ])
    def test_terminal_or_submitted_statuses_never_remind(self, status: str) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        order = _wa_order(
            db, t.id, status=status,
            line_items=[{"title": "x", "quantity": 1}],
            meta={"google_maps_url": "https://maps.google.com/?q=1,2"},
        )
        assert resolve_wa_abandoned_draft_reminder(order) is None

    def test_pending_payment_without_address_not_eligible(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        order = _wa_order(db, t.id, status="pending_payment")
        assert resolve_wa_abandoned_draft_reminder(order) is None


class TestEmitterScan:
    def test_scan_emits_for_eligible_draft(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id)
        _seed_automation(db, t.id)
        _wa_order(
            db, t.id, status="draft",
            line_items=[{"title": "طلح", "quantity": 1}],
        )
        count = automation_emitters.scan_abandoned_order_drafts(db, t.id)
        assert count == 1
        events = db.query(AutomationEvent).all()
        assert len(events) == 1
        assert events[0].event_type == AutomationTrigger.WA_ORDER_DRAFT_REMINDER_DUE.value
        assert events[0].payload["reminder_kind"] == REMINDER_COMPLETE_ORDER

    def test_no_emit_on_fresh_order_before_delay(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id)
        _seed_automation(db, t.id)
        _wa_order(
            db, t.id, status="draft",
            line_items=[{"title": "x", "quantity": 1}],
            meta={"created_at": datetime.now(timezone.utc).isoformat()},
        )
        assert automation_emitters.scan_abandoned_order_drafts(db, t.id) == 0

    def test_salla_abandoned_cart_unaffected(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id)
        _seed_automation(db, t.id)
        db.add(Order(
            tenant_id=t.id,
            external_id="cart-12345",
            status="draft",
            source="salla",
            is_abandoned=True,
            line_items=[{"title": "x"}],
            customer_info={"phone": "+966500000001"},
            extra_metadata={"created_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()},
        ))
        db.commit()
        assert automation_emitters.scan_abandoned_order_drafts(db, t.id) == 0

    def test_human_supervision_blocks_emit(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = _seed_customer(db, t.id)
        _seed_automation(db, t.id)
        _wa_order(
            db, t.id, status="pending_customer_info",
            line_items=[{"title": "x", "quantity": 1}],
        )
        db.add(Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="human",
            ai_paused=True,
            extra_metadata={"customer_phone": cust.phone},
        ))
        db.commit()
        assert automation_emitters.scan_abandoned_order_drafts(db, t.id) == 0

    def test_idempotent_per_reminder_kind(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id)
        _seed_automation(db, t.id)
        _wa_order(
            db, t.id, status="draft",
            line_items=[{"title": "x", "quantity": 1}],
        )
        assert automation_emitters.scan_abandoned_order_drafts(db, t.id) == 1
        assert automation_emitters.scan_abandoned_order_drafts(db, t.id) == 0


class TestGovernorAndUnpaidIsolation:
    def test_governor_blocks_second_send_within_cooldown(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = _seed_customer(db, t.id)
        record_sent(db, t.id, cust.id, "abandoned_order_draft")
        db.commit()
        second = gov_check(db, t.id, cust.id, "abandoned_order_draft", order_id=1)
        assert not second.allowed
        assert second.reason_code == "blocked_by_cooldown"

    def test_unpaid_scan_skips_nahla_wa_orders(self) -> None:
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id)
        db.add(SmartAutomation(
            tenant_id=t.id,
            automation_type="unpaid_order_reminder",
            trigger_event=AutomationTrigger.ORDER_PAYMENT_PENDING.value,
            name="unpaid",
            enabled=True,
            config={"steps": [{"delay_minutes": 0}]},
        ))
        _wa_order(
            db, t.id, status="pending_payment",
            meta={"google_maps_url": "https://maps.google.com/?q=1,2"},
        )
        db.commit()
        with patch.object(automation_emitters, "emit_automation_event") as mock_emit:
            count = automation_emitters.scan_unpaid_orders(db, t.id)
        assert count == 0
        mock_emit.assert_not_called()
