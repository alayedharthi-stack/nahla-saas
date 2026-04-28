"""
tests/test_pending_payment_queues.py
──────────────────────────────────────
Comprehensive tests for the pending-payment and COD-confirmation queue
engines, covering:

  1. Queue isolation   — the same order must not appear in more than one queue.
  2. Emitter conflict  — pending_payment orders must stay out of abandoned_cart
                         logic; COD orders must stay out of pending_payment.
  3. Paid/completed/cancelled orders must not receive reminders.
  4. Idempotency       — the emitter must not re-emit an already-emitted step.
  5. Governor blocking — a message already sent blocks a second send within
                         the cooldown / 6h window.
  6. Stage calculation — current_stage in the queue response reflects the
                         correct number of steps emitted.
  7. Status transition guard — if the order is paid before sending, the
                                emitter skips it on the next sweep.

Architecture under test
───────────────────────
  core.automation_emitters.scan_unpaid_orders
  core.automation_emitters.scan_cod_confirmations
  core.send_governor.check / record_sent

All tests use an in-memory SQLite DB with JSONB→JSON remapping (same
pattern used by conftest / other automation tests).
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
    GovernorSendLog,
    Order,
    SmartAutomation,
    Tenant,
)
from core import automation_emitters  # noqa: E402
from core.automation_triggers import AutomationTrigger  # noqa: E402
from core.send_governor import check as gov_check, record_sent  # noqa: E402


# ── DB harness ─────────────────────────────────────────────────────────────────

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


# ── Seed helpers ────────────────────────────────────────────────────────────────

def _tenant(db) -> Tenant:
    t = Tenant(name="TestMerchant", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _customer(db, tenant_id: int, phone: str = "+966500111222") -> Customer:
    c = Customer(tenant_id=tenant_id, phone=phone, name="Khalid")
    db.add(c); db.commit(); db.refresh(c)
    return c


def _unpaid_automation(db, tenant_id: int, enabled: bool = True) -> SmartAutomation:
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="unpaid_order_reminder",
        engine="recovery",
        trigger_event=AutomationTrigger.ORDER_PAYMENT_PENDING.value,
        name="Unpaid Reminder",
        enabled=enabled,
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


def _cod_automation(db, tenant_id: int, enabled: bool = True) -> SmartAutomation:
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="cod_confirmation",
        engine="recovery",
        trigger_event=AutomationTrigger.ORDER_COD_PENDING.value,
        name="COD Confirmation",
        enabled=enabled,
        config={
            "reminder_after_minutes": 360,
            "cancel_after_minutes":   1440,
            "steps": [
                {"delay_minutes": 360, "message_type": "reminder"},
            ],
        },
    )
    db.add(a); db.commit()
    return a


def _order(
    db,
    *,
    tenant_id: int,
    status: str,
    age: timedelta,
    phone: str = "+966500111222",
    is_abandoned: bool = False,
    external_id: str = "ORD-001",
) -> Order:
    created = (datetime.now(timezone.utc) - age).replace(tzinfo=None)
    o = Order(
        tenant_id=tenant_id,
        external_id=external_id,
        status=status,
        total="150.00",
        is_abandoned=is_abandoned,
        customer_info={"phone": phone},
        line_items=[],
        extra_metadata={"created_at": created.isoformat()},
    )
    db.add(o); db.commit(); db.refresh(o)
    return o


# ═════════════════════════════════════════════════════════════════════════════
# 1. Queue isolation — same order must NOT appear in more than one queue
# ═════════════════════════════════════════════════════════════════════════════

def test_abandoned_cart_excluded_from_pending_payment_emitter() -> None:
    """
    An Order with is_abandoned=True and a `pending` status must NOT trigger
    the unpaid-order-reminder sweeper.  If it did, the customer would receive
    both an abandoned-cart nudge AND an unpaid-order nudge for the same item.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        # An abandoned cart row with a status that would otherwise match
        # the _PENDING_PAYMENT_STATUSES set.
        _order(db, tenant_id=t.id, status="pending", age=timedelta(hours=3),
               is_abandoned=True)

        emitted = automation_emitters.scan_unpaid_orders(db, t.id)
        assert emitted == 0, (
            "scan_unpaid_orders must skip orders where is_abandoned=True"
        )
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


def test_real_pending_order_reaches_unpaid_emitter() -> None:
    """
    A normal (non-abandoned) pending order past the grace period must be
    picked up by scan_unpaid_orders.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        _order(db, tenant_id=t.id, status="pending", age=timedelta(hours=2),
               is_abandoned=False)

        emitted = automation_emitters.scan_unpaid_orders(db, t.id)
        assert emitted == 1
        evs = db.query(AutomationEvent).all()
        assert len(evs) == 1
        assert evs[0].event_type == AutomationTrigger.ORDER_PAYMENT_PENDING.value
    finally:
        db.close(); engine.dispose()


def test_cod_order_excluded_from_pending_payment_emitter() -> None:
    """
    A COD order in `pending_confirmation` must be invisible to
    scan_unpaid_orders — its status is not in _PENDING_PAYMENT_STATUSES.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        _order(db, tenant_id=t.id, status="pending_confirmation",
               age=timedelta(hours=3), is_abandoned=False)

        emitted = automation_emitters.scan_unpaid_orders(db, t.id)
        assert emitted == 0
    finally:
        db.close(); engine.dispose()


def test_pending_payment_order_excluded_from_cod_emitter() -> None:
    """
    An online-payment order (`pending`) must be invisible to
    scan_cod_confirmations — only `pending_confirmation` is in scope.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _cod_automation(db, t.id)
        _order(db, tenant_id=t.id, status="pending",
               age=timedelta(hours=7), is_abandoned=False)

        emitted = automation_emitters.scan_cod_confirmations(db, t.id)
        assert emitted == 0
    finally:
        db.close(); engine.dispose()


def test_abandoned_cart_excluded_from_cod_emitter() -> None:
    """
    An Order with is_abandoned=True must not be processed by
    scan_cod_confirmations even if it somehow carries `pending_confirmation`.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _cod_automation(db, t.id)
        _order(db, tenant_id=t.id, status="pending_confirmation",
               age=timedelta(hours=8), is_abandoned=True)

        emitted = automation_emitters.scan_cod_confirmations(db, t.id)
        assert emitted == 0
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 2. Paid / completed / cancelled orders must not receive reminders
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status", ["paid", "completed", "delivered", "shipped"])
def test_paid_order_ignored_by_unpaid_emitter(status: str) -> None:
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        _order(db, tenant_id=t.id, status=status, age=timedelta(hours=3))

        emitted = automation_emitters.scan_unpaid_orders(db, t.id)
        assert emitted == 0, f"status={status!r} must not trigger unpaid reminder"
    finally:
        db.close(); engine.dispose()


@pytest.mark.parametrize("status", ["cancelled", "refunded", "returned"])
def test_cancelled_order_ignored_by_unpaid_emitter(status: str) -> None:
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        _order(db, tenant_id=t.id, status=status, age=timedelta(hours=3))

        emitted = automation_emitters.scan_unpaid_orders(db, t.id)
        assert emitted == 0
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 3. Idempotency — same step must not be emitted twice
# ═════════════════════════════════════════════════════════════════════════════

def test_unpaid_emitter_idempotent_across_sweeps() -> None:
    """
    Running scan_unpaid_orders twice for the same order must emit step-1
    only once.  The second sweep detects the step_idx in
    Order.extra_metadata.unpaid_reminders and skips it.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        _order(db, tenant_id=t.id, status="pending", age=timedelta(hours=2))

        first = automation_emitters.scan_unpaid_orders(db, t.id)
        second = automation_emitters.scan_unpaid_orders(db, t.id)

        assert first == 1, "first sweep must emit one event"
        assert second == 0, "second sweep must be a no-op (idempotent)"
        assert db.query(AutomationEvent).count() == 1
    finally:
        db.close(); engine.dispose()


def test_unpaid_emitter_progresses_through_stages() -> None:
    """
    Each step is emitted only once, but later steps fire as delays elapse.
    Step 1 at T+60m, step 2 at T+360m, step 3 at T+1440m.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        o = _order(db, tenant_id=t.id, status="pending",
                   age=timedelta(hours=2))

        # Only step 1 delay (60m) has elapsed.
        n1 = automation_emitters.scan_unpaid_orders(db, t.id)
        assert n1 == 1

        # Simulate 7 hours elapsed — step 2 (360m) should fire.
        o2 = db.query(Order).filter(Order.id == o.id).first()
        meta = dict(o2.extra_metadata or {})
        # Backdate created_at by an extra 5h to simulate elapsed time.
        import datetime as _dt
        created = _dt.datetime.fromisoformat(meta["created_at"])
        meta["created_at"] = (created - timedelta(hours=5)).isoformat()
        o2.extra_metadata = meta
        db.commit()

        n2 = automation_emitters.scan_unpaid_orders(db, t.id)
        assert n2 == 1  # step 2 now fires
        assert db.query(AutomationEvent).count() == 2
    finally:
        db.close(); engine.dispose()


def test_cod_emitter_idempotent() -> None:
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _cod_automation(db, t.id)
        _order(db, tenant_id=t.id, status="pending_confirmation",
               age=timedelta(hours=7))

        first = automation_emitters.scan_cod_confirmations(db, t.id)
        second = automation_emitters.scan_cod_confirmations(db, t.id)

        assert first == 1
        assert second == 0
        assert db.query(AutomationEvent).count() == 1
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 4. Governor blocking — same customer, different automation types
# ═════════════════════════════════════════════════════════════════════════════

def test_governor_blocks_second_message_within_6h() -> None:
    """
    After recording a send for `unpaid_order_reminder`, asking the governor
    for the same customer/type within 6 hours must be SOFT_BLOCKED
    (blocked_by_cooldown or blocked_by_6h_limit).
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        c = _customer(db, t.id)

        # Record a send.
        record_sent(db, t.id, c.id, "unpaid_order_reminder")
        db.commit()

        decision = gov_check(db, t.id, c.id, "unpaid_order_reminder")
        assert not decision.allowed, "governor must block a second send within cooldown"
        assert decision.reason_code in {
            "blocked_by_cooldown", "blocked_by_6h_limit",
        }
    finally:
        db.close(); engine.dispose()


def test_governor_cod_has_higher_priority_than_marketing() -> None:
    """
    When a COD (HIGH priority) event is pending for a customer, a LOW
    priority automation (e.g. salary_payday_offer) must be SOFT_BLOCKED
    with blocked_by_priority.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        c = _customer(db, t.id)

        # Simulate a pending COD AutomationEvent for this customer.
        from models import SmartAutomation as _SA  # noqa: PLC0415
        from core.automation_engine import emit_automation_event  # noqa: PLC0415

        cod_auto = _cod_automation(db, t.id)
        emit_automation_event(
            db,
            tenant_id=t.id,
            event_type=AutomationTrigger.ORDER_COD_PENDING.value,
            customer_id=c.id,
            payload={"step_idx": 0},
            commit=True,
        )

        decision = gov_check(db, t.id, c.id, "salary_payday_offer")
        assert not decision.allowed
        assert decision.reason_code == "blocked_by_priority"
        assert decision.blocked_by_type == "cod_confirmation"
    finally:
        db.close(); engine.dispose()


def test_governor_allows_cod_even_with_pending_low_priority() -> None:
    """
    A HIGH-priority send (cod_confirmation) is never blocked by pending
    LOW-priority events — the governor only applies the priority block to
    lower-priority callers.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        c = _customer(db, t.id)

        decision = gov_check(db, t.id, c.id, "cod_confirmation")
        assert decision.allowed, "COD (HIGH) must never be priority-blocked"
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 5. Stage calculation — current_stage reflects steps emitted
# ═════════════════════════════════════════════════════════════════════════════

def test_stage_zero_before_any_reminder() -> None:
    """
    An order that is only 30 minutes old (inside the 60m step-1 grace)
    must not yet have any unpaid_reminders, so current_stage is 0.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        o = _order(db, tenant_id=t.id, status="pending",
                   age=timedelta(minutes=30))

        n = automation_emitters.scan_unpaid_orders(db, t.id)
        assert n == 0  # step 1 delay not yet elapsed

        db.refresh(o)
        meta = o.extra_metadata or {}
        progress = meta.get("unpaid_reminders") or []
        assert len(progress) == 0, "no step should have been recorded yet"
    finally:
        db.close(); engine.dispose()


def test_stage_one_after_first_reminder_emitted() -> None:
    """
    After one step is emitted, unpaid_reminders has one entry with step_idx=0.
    The queue endpoint would derive current_stage = 0 + 1 = 1.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        o = _order(db, tenant_id=t.id, status="pending",
                   age=timedelta(hours=2))

        automation_emitters.scan_unpaid_orders(db, t.id)

        db.refresh(o)
        meta = o.extra_metadata or {}
        progress = meta.get("unpaid_reminders") or []
        assert len(progress) == 1
        assert progress[0]["step_idx"] == 0
        # Derive current_stage as the endpoint does.
        last_step = max(int(r.get("step_idx", -1)) for r in progress)
        assert last_step + 1 == 1   # current_stage = 1
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 6. Status transition guard — order paid between sweeps is ignored
# ═════════════════════════════════════════════════════════════════════════════

def test_order_paid_between_sweeps_is_not_re_emitted() -> None:
    """
    If an order transitions from `pending` to `paid` between two sweeps,
    the second sweep must not emit any event (status no longer in
    _PENDING_PAYMENT_STATUSES).
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        o = _order(db, tenant_id=t.id, status="pending",
                   age=timedelta(hours=2))

        n1 = automation_emitters.scan_unpaid_orders(db, t.id)
        assert n1 == 1

        # Simulate payment.
        o2 = db.query(Order).filter(Order.id == o.id).first()
        o2.status = "paid"
        db.commit()

        n2 = automation_emitters.scan_unpaid_orders(db, t.id)
        assert n2 == 0, "paid order must not be re-emitted"
        assert db.query(AutomationEvent).count() == 1  # only the first event
    finally:
        db.close(); engine.dispose()


def test_order_cancelled_before_reminder_is_skipped() -> None:
    """
    An order that gets cancelled before the first reminder delay elapses
    must receive zero reminders.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id)
        o = _order(db, tenant_id=t.id, status="cancelled",
                   age=timedelta(hours=2))

        n = automation_emitters.scan_unpaid_orders(db, t.id)
        assert n == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 7. COD reminder tracking — reminders_sent reflects cod_reminders metadata
# ═════════════════════════════════════════════════════════════════════════════

def test_cod_reminder_recorded_in_metadata() -> None:
    """
    After scan_cod_confirmations emits a reminder, the order's
    cod_reminders metadata must have one entry (step_idx=0).
    This is the source the queue endpoint uses for reminders_sent.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _cod_automation(db, t.id)
        o = _order(db, tenant_id=t.id, status="pending_confirmation",
                   age=timedelta(hours=7))

        automation_emitters.scan_cod_confirmations(db, t.id)

        db.refresh(o)
        meta = o.extra_metadata or {}
        cod_reminders = meta.get("cod_reminders") or []
        assert len(cod_reminders) == 1
        assert cod_reminders[0]["step_idx"] == 0
        assert "emitted_at" in cod_reminders[0]
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 8. Multi-order / multi-tenant isolation
# ═════════════════════════════════════════════════════════════════════════════

def test_two_pending_orders_both_get_reminders() -> None:
    """
    Two distinct orders for two different customers in the same tenant
    must each get one reminder event.
    """
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id, phone="+966500000001")
        _customer(db, t.id, phone="+966500000002")
        _unpaid_automation(db, t.id)
        _order(db, tenant_id=t.id, status="pending",
               age=timedelta(hours=2), phone="+966500000001",
               external_id="ORD-A")
        _order(db, tenant_id=t.id, status="pending",
               age=timedelta(hours=3), phone="+966500000002",
               external_id="ORD-B")

        emitted = automation_emitters.scan_unpaid_orders(db, t.id)
        assert emitted == 2
        assert db.query(AutomationEvent).count() == 2
    finally:
        db.close(); engine.dispose()


def test_tenant_isolation_in_emitter() -> None:
    """
    scan_unpaid_orders for tenant A must not emit events for tenant B's orders.
    """
    db, engine = _make_db()
    try:
        tA = _tenant(db)
        tB = Tenant(name="TenantB", is_active=True)
        db.add(tB); db.commit(); db.refresh(tB)

        _customer(db, tA.id, phone="+966500000001")
        _customer(db, tB.id, phone="+966500000002")
        _unpaid_automation(db, tA.id)
        _unpaid_automation(db, tB.id)

        _order(db, tenant_id=tA.id, status="pending",
               age=timedelta(hours=2), phone="+966500000001",
               external_id="ORD-A")
        _order(db, tenant_id=tB.id, status="pending",
               age=timedelta(hours=2), phone="+966500000002",
               external_id="ORD-B")

        emitted_A = automation_emitters.scan_unpaid_orders(db, tA.id)
        # Only tenant A's events should have been written.
        events_A = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tA.id,
        ).count()
        events_B = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tB.id,
        ).count()

        assert emitted_A == 1
        assert events_A == 1
        assert events_B == 0, "tenant B events must not be created by tenant A scan"
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 9. Disabled automation is a no-op
# ═════════════════════════════════════════════════════════════════════════════

def test_disabled_unpaid_automation_is_noop() -> None:
    db, engine = _make_db()
    try:
        t = _tenant(db)
        _customer(db, t.id)
        _unpaid_automation(db, t.id, enabled=False)
        _order(db, tenant_id=t.id, status="pending", age=timedelta(hours=3))

        emitted = automation_emitters.scan_unpaid_orders(db, t.id)
        assert emitted == 0
    finally:
        db.close(); engine.dispose()
