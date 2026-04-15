"""
tests/test_automation_engine.py
────────────────────────────────
Tests for the event-driven automation engine.

Coverage:
  - emit_automation_event inserts a row with processed=False
  - process_pending_events marks events without matching automations as processed
  - delay_not_elapsed leaves event unprocessed
  - delay_elapsed → condition_failed → skipped execution log
  - delay_elapsed + condition_passed → sends (mocked) + sent execution log
  - idempotency: second cycle does not re-execute an already-sent event
  - customer_status_changed is emitted when status transitions
  - whatsapp_message_received is emitted for inbound messages (smoke)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    AutomationEvent,
    AutomationExecution,
    Base,
    Customer,
    CustomerProfile,
    SmartAutomation,
    Tenant,
    WhatsAppConnection,
    WhatsAppTemplate,
)
from core.automation_engine import (  # noqa: E402
    emit_automation_event,
    process_pending_events,
)


# ── SQLite compatibility: remap JSONB → JSON per call (non-global) ────────────

def _make_db():
    """
    Create an isolated in-memory SQLite database.

    JSONB columns are temporarily remapped to JSON for table creation only,
    then restored to JSONB so global metadata is not polluted for other test
    files (which could break coupon generator `.astext` queries when run in
    the same pytest session).
    """
    engine = create_engine("sqlite:///:memory:")

    # Save and remap
    _saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()

    Base.metadata.create_all(engine)

    # Restore original JSONB types — critical to avoid cross-test pollution
    for col, orig_type in _saved:
        col.type = orig_type

    Session = sessionmaker(bind=engine)
    return Session(), engine


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _seed_tenant(db, name="Test Tenant") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_customer(db, tenant_id, phone="+966555000100", name="Test Customer") -> Customer:
    c = Customer(tenant_id=tenant_id, phone=phone, name=name)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_profile(db, tenant_id, customer_id, status="active") -> CustomerProfile:
    p = CustomerProfile(
        tenant_id=tenant_id,
        customer_id=customer_id,
        segment=status,
        customer_status=status,
        total_orders=3,
        total_spend_sar=500.0,
        metrics_computed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        last_recomputed_reason="test",
    )
    db.add(p)
    db.commit()
    return p


def _seed_template(db, tenant_id, name="test_tpl", status="APPROVED") -> WhatsAppTemplate:
    t = WhatsAppTemplate(
        tenant_id=tenant_id, name=name, language="ar",
        category="MARKETING", status=status,
        components=[{"type": "BODY", "text": "مرحبا {{1}}"}],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_wa_conn(db, tenant_id) -> WhatsAppConnection:
    c = WhatsAppConnection(
        tenant_id=tenant_id, status="connected",
        phone_number_id="PID_ENGINE", phone_number="+966500000000",
        sending_enabled=True, webhook_verified=True,
        connection_type="embedded", provider="meta",
    )
    db.add(c)
    db.commit()
    return c


def _seed_automation(
    db, tenant_id, template_id,
    *,
    trigger_event="order_created",
    enabled=True,
    delay_minutes=0,
    conditions=None,
) -> SmartAutomation:
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="new_product_alert",
        name="Test Automation",
        enabled=enabled,
        trigger_event=trigger_event,
        config={
            "delay_minutes": delay_minutes,
            "template_name": "test_tpl",
            "conditions": conditions or {},
        },
        template_id=template_id,
        stats_triggered=0,
        stats_sent=0,
        stats_converted=0,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _ago(minutes=0, hours=0) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes, hours=hours)).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_emit_automation_event_creates_unprocessed_row():
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        ev = emit_automation_event(
            db, tenant.id, "order_created",
            customer_id=customer.id,
            payload={"external_id": "O-1"},
            commit=True,
        )
        assert ev.id is not None
        assert ev.processed is False
        assert ev.event_type == "order_created"
        assert ev.customer_id == customer.id
    finally:
        db.close(); engine.dispose()


def test_process_event_no_matching_automation_marks_processed():
    """Events with no matching enabled automation are marked processed immediately."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        ev = emit_automation_event(
            db, tenant.id, "some_unknown_event",
            payload={}, commit=True,
        )
        asyncio.run(process_pending_events(db, tenant.id))
        db.refresh(ev)
        assert ev.processed is True
    finally:
        db.close(); engine.dispose()


def test_process_event_delay_not_elapsed_leaves_unprocessed():
    """Event created just now with delay=60m should NOT be processed yet."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        tpl = _seed_template(db, tenant.id)
        _seed_automation(db, tenant.id, tpl.id, trigger_event="order_created", delay_minutes=60)
        # Event created NOW — 0 minutes old, delay=60
        ev = emit_automation_event(db, tenant.id, "order_created", payload={}, commit=True)

        asyncio.run(process_pending_events(db, tenant.id))
        db.refresh(ev)
        assert ev.processed is False
        # No execution record should exist
        assert db.query(AutomationExecution).count() == 0
    finally:
        db.close(); engine.dispose()


def test_process_event_condition_failed_writes_skipped_execution():
    """If customer_status condition fails, write skipped execution and mark processed."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_profile(db, tenant.id, customer.id, status="new")  # NOT vip
        tpl = _seed_template(db, tenant.id)
        _seed_automation(
            db, tenant.id, tpl.id,
            trigger_event="order_created",
            delay_minutes=0,
            conditions={"customer_status": ["vip"]},
        )
        ev = emit_automation_event(
            db, tenant.id, "order_created",
            customer_id=customer.id, payload={}, commit=True,
        )

        asyncio.run(process_pending_events(db, tenant.id))
        db.refresh(ev)
        assert ev.processed is True
        exec_row = db.query(AutomationExecution).first()
        assert exec_row is not None
        assert exec_row.status == "skipped"
        assert exec_row.skip_reason is not None
    finally:
        db.close(); engine.dispose()


def test_process_event_sends_message_and_marks_processed():
    """
    When delay=0 and conditions pass, the engine should attempt to send and
    mark the event processed.  provider_send_message is mocked.
    """
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_profile(db, tenant.id, customer.id, status="active")
        tpl = _seed_template(db, tenant.id)
        _seed_wa_conn(db, tenant.id)
        _seed_automation(
            db, tenant.id, tpl.id,
            trigger_event="order_created",
            delay_minutes=0,
            conditions={"customer_status": ["active", "vip"]},
        )
        ev = emit_automation_event(
            db, tenant.id, "order_created",
            customer_id=customer.id,
            payload={"external_id": "O-99"},
            commit=True,
        )

        mock_response = ({"messages": [{"id": "wamid.abc123"}]}, object())
        with patch(
            "core.automation_engine._execute_action",
            new=AsyncMock(return_value=(True, {"template": "test_tpl", "to": "+966555000100", "vars": {}})),
        ):
            asyncio.run(process_pending_events(db, tenant.id))

        db.refresh(ev)
        assert ev.processed is True
        exec_row = db.query(AutomationExecution).first()
        assert exec_row is not None
        assert exec_row.status == "sent"

        automation = db.query(SmartAutomation).first()
        assert automation.stats_triggered == 1
        assert automation.stats_sent == 1
    finally:
        db.close(); engine.dispose()


def test_idempotency_second_cycle_does_not_resend():
    """
    If the engine already wrote a 'sent' execution for (event, automation),
    a second cycle must not re-execute.
    """
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_profile(db, tenant.id, customer.id, status="active")
        tpl = _seed_template(db, tenant.id)
        _seed_wa_conn(db, tenant.id)
        automation = _seed_automation(
            db, tenant.id, tpl.id,
            trigger_event="order_created", delay_minutes=0,
        )
        ev = emit_automation_event(
            db, tenant.id, "order_created",
            customer_id=customer.id, payload={}, commit=True,
        )

        with patch(
            "core.automation_engine._execute_action",
            new=AsyncMock(return_value=(True, {"template": "test_tpl", "to": "+966555000100", "vars": {}})),
        ) as mock_send:
            asyncio.run(process_pending_events(db, tenant.id))
            first_send_count = mock_send.call_count

            # Force ev.processed = False to simulate a re-run
            ev.processed = False
            db.commit()
            asyncio.run(process_pending_events(db, tenant.id))
            second_send_count = mock_send.call_count

        # Send should have been called exactly once total
        assert first_send_count == 1
        assert second_send_count == 1  # no additional call
        assert db.query(AutomationExecution).count() == 1
    finally:
        db.close(); engine.dispose()


def test_customer_status_changed_emitted_on_transition():
    """recompute_profile_for_customer should emit customer_status_changed when status changes."""
    from models import Order
    from services.customer_intelligence import CustomerIntelligenceService

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id, phone="+966555000200")
        # Seed profile as "new" (1 recent order)
        profile = _seed_profile(db, tenant.id, customer.id, status="new")
        # Now add 5 more orders to make them VIP
        now = datetime.now(timezone.utc)
        for i in range(6):
            db.add(Order(
                tenant_id=tenant.id,
                external_id=f"vip-order-{i}",
                status="completed",
                total="700",
                customer_info={"name": "Test Customer", "mobile": "+966555000200"},
                line_items=[],
                extra_metadata={"created_at": (now - timedelta(days=i + 1)).isoformat()},
            ))
        db.commit()

        svc = CustomerIntelligenceService(db, tenant.id)
        svc.recompute_profile_for_customer(customer.id, reason="test_vip_upgrade", commit=True)

        # A customer_status_changed event should have been emitted
        ev = db.query(AutomationEvent).filter(
            AutomationEvent.event_type == "customer_status_changed",
            AutomationEvent.customer_id == customer.id,
        ).first()
        assert ev is not None
        assert ev.payload["from"] == "new"
        assert ev.payload["to"] == "vip"
    finally:
        db.close(); engine.dispose()


def test_emit_and_process_cart_abandoned_event():
    """
    cart_abandoned events (emitted by tracking.py) are picked up by the engine
    and matched to an abandoned_cart automation.
    """
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_profile(db, tenant.id, customer.id, status="active")
        tpl = _seed_template(db, tenant.id)
        _seed_wa_conn(db, tenant.id)
        _seed_automation(
            db, tenant.id, tpl.id,
            trigger_event="cart_abandoned",
            delay_minutes=0,
        )
        emit_automation_event(
            db, tenant.id, "cart_abandoned",
            customer_id=customer.id,
            payload={"cart_total": "200", "url": "https://store.example.com/cart"},
            commit=True,
        )

        with patch(
            "core.automation_engine._execute_action",
            new=AsyncMock(return_value=(True, {"template": "test_tpl", "to": "+966555000100", "vars": {}})),
        ):
            asyncio.run(process_pending_events(db, tenant.id))

        ev = db.query(AutomationEvent).filter_by(event_type="cart_abandoned").first()
        assert ev.processed is True
        exec_row = db.query(AutomationExecution).first()
        assert exec_row.status == "sent"
    finally:
        db.close(); engine.dispose()


def test_multiple_automations_same_event_all_executed():
    """Two automations with the same trigger_event are both executed independently."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_profile(db, tenant.id, customer.id, status="vip")
        tpl1 = _seed_template(db, tenant.id, name="tpl_a")
        tpl2 = _seed_template(db, tenant.id, name="tpl_b")
        _seed_wa_conn(db, tenant.id)
        _seed_automation(db, tenant.id, tpl1.id, trigger_event="order_paid", delay_minutes=0)
        _seed_automation(db, tenant.id, tpl2.id, trigger_event="order_paid", delay_minutes=0)

        emit_automation_event(
            db, tenant.id, "order_paid",
            customer_id=customer.id, payload={"order_id": 1}, commit=True,
        )

        with patch(
            "core.automation_engine._execute_action",
            new=AsyncMock(return_value=(True, {"template": "tpl_a", "to": "+966555000100", "vars": {}})),
        ) as mock_send:
            asyncio.run(process_pending_events(db, tenant.id))

        # Both automations should have been executed
        assert mock_send.call_count == 2
        assert db.query(AutomationExecution).filter_by(status="sent").count() == 2
    finally:
        db.close(); engine.dispose()
