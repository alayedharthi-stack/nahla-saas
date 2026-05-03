"""
tests/test_cod_confirmation_flow.py
────────────────────────────────────
Coverage for the Cash-on-Delivery confirmation flow.

Architectural contract under test:

    1. New COD order      → status = pending_confirmation
    2. Customer confirms  → status → under_review (reminders stop)
    3. No confirmation:
         T+2 h            → reminder #1 emitted via the engine
         T+6 h            → reminder #2 emitted via the engine
         T+12 h           → reminder #3 (final) emitted via the engine
    4. Still no answer:
         T+24 h           → order auto-cancelled (state mutation, not a send)

This file pins the second half (timed reminders + auto-cancel) shipped via
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
    """The COD seed must ship with a 3-stage reminder schedule plus a
    cancel window that comfortably outlives the last reminder."""
    seed = next(
        s for s in SEED_AUTOMATIONS if s["automation_type"] == "cod_confirmation"
    )
    cfg = seed["config"]

    # New canonical shape — three stages at T+2h / T+6h / T+12h.
    steps = cfg.get("steps") or []
    delays = [int(s.get("delay_minutes") or 0) for s in steps]
    assert delays == [120, 360, 720], (
        f"COD reminder schedule drifted: expected [120, 360, 720], got {delays}"
    )
    # Last stage is the "final" notice; everything before it is a regular
    # reminder. The engine reads message_type to pick the right copy.
    assert steps[-1]["message_type"] == "final"

    # Auto-cancel must outlive the last reminder by at least a 6-hour
    # buffer so the customer has time to react to the final nudge.
    assert int(cfg["cancel_after_minutes"]) == 1440
    assert int(cfg["cancel_after_minutes"]) > delays[-1] + 60

    # Legacy knob retained for backward-compat with merchants whose
    # config was customised before the multi-step schedule shipped.
    assert int(cfg["reminder_after_minutes"]) == 120


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

def test_no_reminder_before_first_stage() -> None:
    """The first reminder fires at T+2h. An order seen at T+1h must not
    trigger any event yet."""
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


def test_first_reminder_emitted_at_two_hours() -> None:
    """At T+2h only step 0 is due; steps 1 and 2 must wait."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        order = _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=3))

        emitted = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert emitted == 1

        evs = db.query(AutomationEvent).all()
        assert len(evs) == 1
        ev = evs[0]
        assert ev.event_type == AutomationTrigger.ORDER_COD_PENDING.value
        assert ev.customer_id == customer.id
        payload = ev.payload or {}
        assert payload["step_idx"] == 0
        assert payload["message_type"] == "reminder"
        assert payload["order_internal_id"] == order.id
        assert payload["source"] == "automation_emitters.cod_confirmation"

        db.refresh(order)
        progress = (order.extra_metadata or {}).get("cod_reminders") or []
        assert [p["step_idx"] for p in progress] == [0]
    finally:
        db.close(); engine.dispose()


def test_two_reminders_emitted_after_six_hours() -> None:
    """At T+7h both step 0 (T+2h) and step 1 (T+6h) are due. Step 2
    (T+12h) must wait."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        order = _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=7))

        emitted = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert emitted == 2

        evs = db.query(AutomationEvent).order_by(AutomationEvent.id.asc()).all()
        steps = [(e.payload or {}).get("step_idx") for e in evs]
        msg_types = [(e.payload or {}).get("message_type") for e in evs]
        assert steps == [0, 1]
        assert msg_types == ["reminder", "reminder"]

        db.refresh(order)
        progress = (order.extra_metadata or {}).get("cod_reminders") or []
        assert sorted(p["step_idx"] for p in progress) == [0, 1]
    finally:
        db.close(); engine.dispose()


def test_third_reminder_is_marked_final() -> None:
    """At T+13h all three steps are due. The last one is the 'final'
    nudge so the engine can pick the right copy variant."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=13))

        emitted = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert emitted == 3

        evs = db.query(AutomationEvent).order_by(AutomationEvent.id.asc()).all()
        steps = [(e.payload or {}).get("step_idx") for e in evs]
        msg_types = [(e.payload or {}).get("message_type") for e in evs]
        assert steps == [0, 1, 2]
        assert msg_types == ["reminder", "reminder", "final"]
    finally:
        db.close(); engine.dispose()


def test_reminders_are_idempotent_across_sweeps() -> None:
    """Two consecutive sweeps for an order at T+7h must emit only the
    two due steps once. The second sweep is a no-op."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        _seed_cod_automation(db, tenant.id)
        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=7))

        first = automation_emitters.scan_cod_confirmations(db, tenant.id)
        second = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert (first, second) == (2, 0)
        assert db.query(AutomationEvent).count() == 2
    finally:
        db.close(); engine.dispose()


def test_legacy_single_step_config_still_runs() -> None:
    """Backward-compat: tenants whose config was seeded BEFORE the
    multi-step schedule shipped (one stage at T+6h) keep working."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id)
        a = _seed_cod_automation(db, tenant.id)
        a.config = {
            "reminder_after_minutes": 360,
            "cancel_after_minutes":   1440,
            "steps": [{"delay_minutes": 360, "message_type": "reminder"}],
        }
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(a, "config")
        db.commit()

        _seed_cod_order(db, tenant_id=tenant.id, age=timedelta(hours=7))
        emitted = automation_emitters.scan_cod_confirmations(db, tenant.id)
        assert emitted == 1

        ev = db.query(AutomationEvent).first()
        payload = ev.payload or {}
        assert payload["step_idx"] == 0
        # Single-step schedule → that one step IS the final.
        assert payload["message_type"] == "final"
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
    pending order should see each sweeper act on its own lane only.
    Crucially, neither sweeper may emit an event tied to the other
    sweeper's order."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id, phone="+966555000222")
        _seed_customer(db, tenant.id, phone="+966555000333")
        _seed_unpaid_automation(db, tenant.id)
        _seed_cod_automation(db, tenant.id)

        # COD order at T+7h → due steps 0 (T+2h) and 1 (T+6h) — two emits.
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
        assert cod_mut == 2     # two COD reminders due
        assert unpaid_mut == 1  # one online reminder due

        evs = db.query(AutomationEvent).all()
        types = {e.event_type for e in evs}
        assert types == {
            AutomationTrigger.ORDER_COD_PENDING.value,
            AutomationTrigger.ORDER_PAYMENT_PENDING.value,
        }
        for e in evs:
            payload = e.payload or {}
            order_id = payload.get("order_internal_id")
            if e.event_type == AutomationTrigger.ORDER_COD_PENDING.value:
                assert order_id == cod.id
            else:
                assert order_id == online.id
    finally:
        db.close(); engine.dispose()
