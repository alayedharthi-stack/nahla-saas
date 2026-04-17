"""
tests/test_cod_confirmation_flow.py
────────────────────────────────────
Coverage for the Cash-on-Delivery confirmation flow.

Architectural contract under test:

    1. New COD order      → status = pending_confirmation
    2. Customer confirms  → status → under_review
    3. No confirmation
       within 6 hours     → reminder template emitted via the engine
    4. Still no answer
       within 24 hours    → order auto-cancelled (state mutation, not a send)

This file pins the second half (timed reminder + auto-cancel) shipped via
`scan_cod_confirmations`. The synchronous half (initial template + reply
classification) is covered by `tests/test_back_in_stock_and_cod.py`.

Conflict prevention
───────────────────
COD and the unpaid-online-payment reminder must never operate on the
same order. We test this explicitly: an order in `pending_confirmation`
must be invisible to `scan_unpaid_orders`, and an order in
`pending`/`payment_pending` must be invisible to `scan_cod_confirmations`.
That is the user-facing promise from the product spec.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Tuple

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
    Base,
    Customer,
    Order,
    SmartAutomation,
    Tenant,
)
from core import automation_emitters  # noqa: E402
from core.automation_triggers import (  # noqa: E402
    AUTOMATION_TYPE_TO_TRIGGER,
    AutomationTrigger,
)
from core.automations_seed import ENGINE_BY_TYPE, SEED_AUTOMATIONS  # noqa: E402
from core.template_library import DEFAULT_AUTOMATION_TEMPLATES  # noqa: E402


# ── DB harness ───────────────────────────────────────────────────────────────

def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    _saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in _saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db) -> Tenant:
    t = Tenant(name="T", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _seed_customer(db, tenant_id: int, phone: str = "+966555000222") -> Customer:
    c = Customer(tenant_id=tenant_id, phone=phone, name="Sara")
    db.add(c); db.commit(); db.refresh(c)
    return c


def _seed_cod_automation(db, tenant_id: int, *, enabled: bool = True) -> SmartAutomation:
    seed = next(
        s for s in SEED_AUTOMATIONS if s["automation_type"] == "cod_confirmation"
    )
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="cod_confirmation",
        engine="recovery",
        trigger_event=AutomationTrigger.ORDER_COD_PENDING.value,
        name="COD",
        enabled=enabled,
        config=seed["config"],
    )
    db.add(a); db.commit()
    return a


def _seed_cod_order(
    db,
    *,
    tenant_id: int,
    age: timedelta,
    status: str = "pending_confirmation",
    phone: str = "+966555000222",
    external_id: str = "O-COD-1",
) -> Order:
    created = (datetime.now(timezone.utc) - age).replace(tzinfo=None)
    order = Order(
        tenant_id=tenant_id,
        external_id=external_id,
        status=status,
        total="180.00",
        customer_info={"phone": phone},
        line_items=[],
        extra_metadata={"created_at": created.isoformat(), "payment_method": "cod"},
    )
    db.add(order); db.commit(); db.refresh(order)
    return order


# ═════════════════════════════════════════════════════════════════════════════
# 1. Trigger + seed registration
# ═════════════════════════════════════════════════════════════════════════════

def test_cod_trigger_is_registered() -> None:
    """The flow needs its own trigger so it can never collide with
    `order_payment_pending` (which drives the unpaid-online sweeper)."""
    assert AutomationTrigger.ORDER_COD_PENDING.value == "order_cod_pending"
    assert (
        AUTOMATION_TYPE_TO_TRIGGER["cod_confirmation"]
        == AutomationTrigger.ORDER_COD_PENDING
    )


def test_cod_seed_exists_with_engine_and_default_off() -> None:
    seed = next(
        (s for s in SEED_AUTOMATIONS if s["automation_type"] == "cod_confirmation"),
        None,
    )
    assert seed is not None, "cod_confirmation must ship in SEED_AUTOMATIONS"
    assert seed["engine"] == "recovery"
    assert ENGINE_BY_TYPE["cod_confirmation"] == "recovery"
    assert seed["trigger_event"] == AutomationTrigger.ORDER_COD_PENDING.value
    # Off by default — same safety contract as every other recovery seed.
    assert seed["enabled"] is False


def test_cod_seed_carries_required_timing_knobs() -> None:
    """The two settings the sweeper reads — reminder_after_minutes and
    cancel_after_minutes — must be present in the seed config."""
    seed = next(
        s for s in SEED_AUTOMATIONS if s["automation_type"] == "cod_confirmation"
    )
    cfg = seed["config"]
    assert int(cfg["reminder_after_minutes"]) == 360   # 6h default
    assert int(cfg["cancel_after_minutes"]) == 1440    # 24h default
    # Cancel must outlive at least one reminder window. Otherwise the
    # sweeper would auto-cancel before any customer ever got nudged.
    assert int(cfg["cancel_after_minutes"]) > int(cfg["reminder_after_minutes"])


def test_cod_reminder_template_ships_in_library() -> None:
    spec = DEFAULT_AUTOMATION_TEMPLATES.get("cod_confirmation_reminder")
    assert spec is not None
    assert spec["category"] == "UTILITY"  # transactional, not marketing
    assert spec["trigger_event"] == AutomationTrigger.ORDER_COD_PENDING.value
    for lang in ("ar", "en"):
        slots = spec["languages"][lang]["slots"]
        assert "customer_name" in slots
        assert "order_id" in slots
        assert "store_name" in slots
        # No discount_code slot — this is a confirmation request, not a
        # promotional nudge. Adding one would let the auto-coupon path
        # silently attach a code to a transactional message.
        assert "discount_code" not in slots


# ═════════════════════════════════════════════════════════════════════════════
# 2. scan_cod_confirmations — reminder
# ═════════════════════════════════════════════════════════════════════════════

def test_no_reminder_inside_six_hour_window() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=1))

        emitted = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert emitted == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


def test_reminder_emitted_after_six_hours() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        order = _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=7))

        emitted = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert emitted == 1

        evs = db.query(AutomationEvent).all()
        assert len(evs) == 1
        ev = evs[0]
        assert ev.event_type == AutomationTrigger.ORDER_COD_PENDING.value
        assert ev.customer_id == customer.id
        payload = ev.payload or {}
        assert payload["step_idx"] == 0
        assert payload["order_internal_id"] == order.id
        assert payload["source"] == "automation_emitters.cod_confirmation"

        # Order metadata must record the reminder so the next sweep skips it.
        db.refresh(order)
        meta = order.extra_metadata or {}
        progress = meta.get("cod_reminders") or []
        assert len(progress) == 1
        assert progress[0]["step_idx"] == 0
    finally:
        db.close(); engine.dispose()


def test_reminder_is_idempotent() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=7))

        first = automation_emitters.scan_cod_confirmations(db, tenant.id)
        second = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert (first, second) == (1, 0)
        assert db.query(AutomationEvent).count() == 1
    finally:
        db.close(); engine.dispose()


def test_reminder_skipped_when_no_customer_resolvable() -> None:
    """No matching Customer row → no event. The sweeper must NOT crash
    or emit an orphan event the engine cannot deliver."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_cod_automation(db, tenant.id)
        # No Customer seeded for this phone.
        _seed_cod_order(
            db,
            tenant_id=tenant.id, age=timedelta(hours=7),
            phone="+966500999999",
        )
        emitted = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert emitted == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


def test_disabled_cod_automation_is_noop() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id, enabled=False)
        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=30))
        assert automation_emitters.scan_cod_confirmations(db, tenant.id) == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 3. scan_cod_confirmations — auto-cancel
# ═════════════════════════════════════════════════════════════════════════════

def test_order_auto_cancelled_after_twenty_four_hours() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        order = _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=25))

        mutations = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert mutations >= 1

        db.refresh(order)
        assert order.status == "cancelled"
        meta = order.extra_metadata or {}
        assert "cod_auto_cancelled_at" in meta
        assert meta.get("cod_auto_cancel_reason") == "no_customer_response"
    finally:
        db.close(); engine.dispose()


def test_auto_cancel_takes_priority_over_reminder() -> None:
    """If the order is already past the cancel window, we cancel it
    rather than emitting yet another reminder. Otherwise a customer
    might receive a reminder for an order we are about to kill."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        order = _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=25))

        automation_emitters.scan_cod_confirmations(db, tenant.id)

        # No new automation event should have been written for this order.
        evs = db.query(AutomationEvent).all()
        assert evs == []
        db.refresh(order)
        assert order.status == "cancelled"
    finally:
        db.close(); engine.dispose()


def test_auto_cancel_is_idempotent() -> None:
    """A cancelled order must not be re-cancelled or re-counted on the
    next sweep — it falls out of the `pending_confirmation` filter."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=25))

        first = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert first >= 1
        second = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert second == 0
    finally:
        db.close(); engine.dispose()


def test_misconfigured_cancel_window_is_clamped() -> None:
    """If a merchant accidentally sets cancel_after_minutes <=
    reminder_after_minutes, the sweeper must still leave room for at
    least one reminder before cancelling. Otherwise an admin typo
    could nuke every COD order before any customer ever got nudged."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        a = _seed_cod_automation(db, tenant.id)
        a.config = dict(a.config)
        # Hostile config: cancel = reminder.
        a.config["reminder_after_minutes"] = 60
        a.config["cancel_after_minutes"]   = 60
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(a, "config")
        db.commit()

        # Order is just past the (broken) cancel threshold but not yet
        # past a sane cancel window. The sweeper must NOT cancel it.
        order = _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(minutes=70))
        automation_emitters.scan_cod_confirmations(db, tenant.id)
        db.refresh(order)
        assert order.status == "pending_confirmation"
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 4. Conflict prevention vs unpaid-online sweeper
# ═════════════════════════════════════════════════════════════════════════════

def _seed_unpaid_automation(db, tenant_id: int) -> SmartAutomation:
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="unpaid_order_reminder",
        engine="recovery",
        trigger_event=AutomationTrigger.ORDER_PAYMENT_PENDING.value,
        name="Unpaid",
        enabled=True,
        config={
            "steps": [
                {"delay_minutes": 60,   "message_type": "reminder"},
                {"delay_minutes": 360,  "message_type": "reminder"},
                {"delay_minutes": 1440, "message_type": "final"},
            ],
        },
    )
    db.add(a); db.commit()
    return a


def test_unpaid_sweeper_ignores_pending_confirmation_orders() -> None:
    """A COD order is in `pending_confirmation`, NOT in any of the
    online-payment statuses. The unpaid-online sweeper must not see it,
    otherwise the customer would get two unrelated reminders."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_unpaid_automation(db, tenant.id)
        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=10))
        emitted = automation_emitters.scan_unpaid_orders(db, tenant.id)
        assert emitted == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


def test_cod_sweeper_ignores_online_pending_orders() -> None:
    """Conversely: an order in plain `pending` (online checkout, not
    COD) must be invisible to the COD sweeper."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        _seed_cod_order(
            db,
            tenant_id=tenant.id, age=timedelta(hours=25),
            status="pending",   # online checkout, not COD
            external_id="O-ONLINE-1",
        )
        mutations = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert mutations == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


def test_sweepers_remain_independent_when_both_orders_exist() -> None:
    """A tenant that has BOTH a stale COD order and a stale online
    pending order should see exactly one COD action (reminder/cancel)
    and one unpaid-online action — never the same order twice."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id, phone="+966555000222")
        _seed_customer(db, tenant.id, phone="+966555000333")
        _seed_unpaid_automation(db, tenant.id)
        _seed_cod_automation(db, tenant.id)

        cod = _seed_cod_order(
            db, tenant_id=tenant.id, age=timedelta(hours=7),
            phone="+966555000222", external_id="O-COD-X",
        )
        online = _seed_cod_order(
            db, tenant_id=tenant.id, age=timedelta(hours=2),
            status="pending",
            phone="+966555000333", external_id="O-ONLINE-X",
        )

        cod_mut    = automation_emitters.scan_cod_confirmations(db, tenant.id)
        unpaid_mut = automation_emitters.scan_unpaid_orders(db, tenant.id)
        assert cod_mut == 1
        assert unpaid_mut == 1

        evs = db.query(AutomationEvent).all()
        types = {e.event_type for e in evs}
        assert types == {
            AutomationTrigger.ORDER_COD_PENDING.value,
            AutomationTrigger.ORDER_PAYMENT_PENDING.value,
        }
        # And neither sweeper crossed lanes.
        for e in evs:
            payload = e.payload or {}
            order_id = payload.get("order_internal_id")
            if e.event_type == AutomationTrigger.ORDER_COD_PENDING.value:
                assert order_id == cod.id
            else:
                assert order_id == online.id
    finally:
        db.close(); engine.dispose()
