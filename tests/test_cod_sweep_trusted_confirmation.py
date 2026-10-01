"""The COD sweep reminds and cancels only while the customer's answer is owed.

Production, tenant 1, September 2026 (read-only trace): the sweep selected
orders by status alone, and ``under_review`` was in its set. That status is
what the confirmation flow writes when the customer confirms — so four
confirmed cash-on-delivery orders were cancelled for "no customer response"
about 24 hours after creation, three of them after the customer had also been
sent the confirmation reminders again — and it is what a store shows for a
bank transfer under review, so eighteen orders Nahla had never asked anything
were reminded and cancelled too. Every cancellation was local; the store still
showed the order under review.

The sweep now reads the flow's own state (``services.cod_confirmation.
cod_awaits_customer_decision``): Nahla asked — its checkout's waiting state, or
the confirmation send stamped on the order — and no decision is recorded on the
order or in its ``order.cod.*`` events. What is proved here:

* a confirmed order is never reminded or cancelled, however often the sweep
  runs, whichever record of the confirmation an older row kept;
* an unconfirmed order keeps the timeout policy: reminders, then one
  cancellation;
* an order Nahla never asked is neither reminded nor cancelled;
* customer cancellations and store-side cancellations are unchanged;
* tenants are kept apart, and incomplete old rows are read safely.

Generic store data only; behaviour and state are asserted, never wording.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core import automation_emitters
from core.automation_triggers import AutomationTrigger
from models import AutomationEvent, Base, Customer, Order, SmartAutomation, SystemEvent, Tenant
from services.cod_confirmation import cod_awaits_customer_decision

NOW = datetime(2026, 9, 28, 12, 0, 0)
CANCEL_AFTER_MINUTES = 1440


def _db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    return sessionmaker(bind=engine)()


class Store:
    """A merchant with the COD confirmation automation, and one customer."""

    _stores = 0

    def __init__(self, db: Any = None, *, enabled: bool = True) -> None:
        self.db = db or _db()
        Store._stores += 1
        self.tenant = Tenant(name=f"متجر تجريبي عام {Store._stores}", is_active=True)
        self.db.add(self.tenant)
        self.db.flush()
        self.customer = Customer(tenant_id=self.tenant.id, name="نورة عبدالله", phone="+966500000002")
        self.db.add(self.customer)
        self.db.add(SmartAutomation(
            tenant_id=self.tenant.id, automation_type="cod_confirmation", engine="recovery",
            trigger_event=AutomationTrigger.ORDER_COD_PENDING.value, name="COD", enabled=enabled,
            config={"cancel_after_minutes": CANCEL_AFTER_MINUTES,
                    "steps": [{"delay_minutes": 120}, {"delay_minutes": 360}, {"delay_minutes": 720}]}))
        self.db.commit()
        self._n = 0

    def order(self, status: str, *, meta: Any = "default", source: str = "salla") -> Order:
        self._n += 1
        if meta == "default":
            meta = {}
        if isinstance(meta, dict):
            meta = {"created_at": NOW.isoformat(), **meta}
        row = Order(tenant_id=self.tenant.id, customer_id=self.customer.id,
                    external_id=f"gen-{self.tenant.id}-{self._n}", external_order_number=f"GEN-{self._n}",
                    status=status, total="99", source=source, customer_info={"phone": "+966500000002"},
                    line_items=[{"name": "قميص قطني أزرق", "quantity": 1}], extra_metadata=meta)
        self.db.add(row)
        self.db.commit()
        return row

    def event(self, order: Order, event_type: str, *, tenant_id: Any = None) -> None:
        self.db.add(SystemEvent(tenant_id=tenant_id or self.tenant.id, category="order", event_type=event_type,
                                severity="info", summary="", payload={}, reference_id=str(order.id)))
        self.db.commit()

    def sweep(self, *, hours: float) -> int:
        """Run the sweep ``hours`` after the orders were created."""
        return automation_emitters.scan_cod_confirmations(self.db, self.tenant.id,
                                                          now=NOW + timedelta(hours=hours))

    def state(self, order: Order) -> tuple:
        self.db.refresh(order)
        meta = order.extra_metadata or {}
        cancels = self.db.query(SystemEvent).filter(SystemEvent.reference_id == str(order.id),
                                                    SystemEvent.event_type == "order.cod.auto_cancelled").count()
        reminders = [e for e in self.db.query(AutomationEvent).all()
                     if (e.payload or {}).get("order_internal_id") == order.id]
        return order.status, bool(meta.get("cod_auto_cancelled_at")), len(meta.get("cod_reminders") or []), \
            cancels, len(reminders)


ASKED = {"payment_method": "cod", "nahla_cod_confirmation_sent": True}
UNTOUCHED = (False, 0, 0, 0)          # no auto-cancel stamp, reminders, cancel events or reminder events


# ── A confirmed order ────────────────────────────────────────────────────────


def test_a_confirmed_order_is_never_reminded_or_cancelled_however_often_the_sweep_runs():
    """The observed shape: a store COD order, asked, confirmed minutes after it
    was placed; the flow wrote ``under_review`` and its records."""
    store = Store()
    confirmed = store.order("under_review", meta={
        **ASKED, "cod_previous_status": "in_progress", "cod_confirm_requested_at": NOW.isoformat(),
        "cod_confirmed_at": NOW.isoformat(), "cod_pushed_external_id": "gen-x"})
    store.event(confirmed, "order.cod.confirmed")
    for hours in (2.5, 7, 13, 24.5, 25, 25, 30, 48, 72):
        assert store.sweep(hours=hours) == 0
    assert store.state(confirmed) == ("under_review", *UNTOUCHED)


@pytest.mark.parametrize("record", [
    {"cod_confirmed_at": "2026-09-28T12:05:00"},
    {"cod_confirm_requested_at": "2026-09-28T12:05:00"},      # confirmed; the store update failed
    {"cod_pushed_external_id": "gen-x"},
    {"cod_confirmation_bypassed": True},                      # pushed without asking
    "event_only",                                             # an older row: the event, no metadata key
])
@pytest.mark.parametrize("status", ["under_review", "pending_confirmation", "بإنتظار المراجعة"])
def test_any_record_of_the_customers_decision_ends_the_question(record: Any, status: str):
    store = Store()
    order = store.order(status, meta={**ASKED, **({} if record == "event_only" else record)})
    if record == "event_only":
        store.event(order, "order.cod.confirmed")
    assert store.sweep(hours=25) == 0 and store.sweep(hours=49) == 0
    assert store.state(order) == (status, *UNTOUCHED)


# ── An unconfirmed order ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status,meta", [
    ("pending_confirmation", ASKED),                           # Nahla's checkout waiting state
    ("pending_confirmation", {}),                              # an older row: no stamp, no method
    ("under_review", ASKED),                                   # a store order Nahla asked
    ("بإنتظار المراجعة", ASKED),
])
def test_an_unconfirmed_order_keeps_the_timeout_policy(status: str, meta: dict):
    store = Store()
    order = store.order(status, meta=meta)
    assert store.sweep(hours=1) == 0                           # nothing due yet
    assert store.sweep(hours=7) == 2                           # the 2 h and 6 h reminders
    assert store.state(order) == (status, False, 2, 0, 2)
    assert store.sweep(hours=25) == 1                          # one cancellation, past the window
    status_now, auto_cancelled, reminders, cancels, _ = store.state(order)
    assert (status_now, auto_cancelled, cancels) == ("cancelled", True, 1)
    assert (order.extra_metadata or {}).get("cod_auto_cancel_reason") == "no_customer_response"
    assert store.sweep(hours=26) == 0 and store.state(order)[3] == 1


# ── An order nobody asked ────────────────────────────────────────────────────


@pytest.mark.parametrize("status,meta", [
    ("under_review", {"payment_method": "bank"}),              # bank transfer under merchant review
    ("under_review", {"payment_method": "cod"}),               # COD, but no confirmation was ever sent
    ("in_review", {}),                                         # an older row: no method, no stamp
    ("قيد المراجعة", {"payment_method": "credit_card"}),
])
def test_an_order_nahla_never_asked_is_neither_reminded_nor_cancelled(status: str, meta: dict):
    store = Store()
    order = store.order(status, meta=meta)
    for hours in (2.5, 7, 13, 25, 48):
        assert store.sweep(hours=hours) == 0
    assert store.state(order) == (status, *UNTOUCHED)


# ── Cancellations and rejections are unchanged ───────────────────────────────


def test_a_customer_cancellation_stays_as_the_flow_wrote_it():
    store = Store()
    order = store.order("cancelled", meta={**ASKED, "cod_cancelled_at": NOW.isoformat()})
    store.event(order, "order.cod.cancelled")
    assert store.sweep(hours=25) == 0
    assert store.state(order) == ("cancelled", *UNTOUCHED)


@pytest.mark.parametrize("record", ["metadata", "event_only"])
def test_a_customer_cancellation_is_kept_after_sync_restores_a_review_status(record: str):
    """The customer cancelled; a later store snapshot put the order back under
    review. The recorded decision still ends the question."""
    store = Store()
    meta = {**ASKED, **({"cod_cancelled_at": NOW.isoformat()} if record == "metadata" else {})}
    order = store.order("under_review", meta=meta)
    if record == "event_only":
        store.event(order, "order.cod.cancelled")
    for hours in (7, 25, 49):
        assert store.sweep(hours=hours) == 0
    assert store.state(order) == ("under_review", *UNTOUCHED)


def test_an_order_is_timed_out_once_even_when_sync_restores_the_store_status():
    """Asked, never answered, cancelled at the timeout; store sync then put the
    store's status back. The sweep does not cancel or remind it again (in
    production the loop ran up to ~190 times on one order)."""
    store = Store()
    order = store.order("under_review", meta=ASKED)
    assert store.sweep(hours=7) == 2
    assert store.sweep(hours=25) == 1
    order.status = "under_review"                     # what store sync wrote back
    store.db.commit()
    for hours in (25.1, 26, 30, 48):
        assert store.sweep(hours=hours) == 0
    status, auto_cancelled, reminders, cancels, reminder_events = store.state(order)
    assert (status, auto_cancelled, reminders, cancels, reminder_events) == ("under_review", True, 2, 1, 2)


def test_a_cancellation_the_store_did_not_take_still_times_out_as_before():
    """The customer asked to cancel and the store update failed: no decision
    is recorded on the order, so the timeout cancels it, as it did before."""
    store = Store()
    order = store.order("pending_confirmation",
                        meta={**ASKED, "cod_cancel_store_update_failed_at": NOW.isoformat()})
    assert store.sweep(hours=25) == 1
    assert store.state(order)[:2] == ("cancelled", True)


def test_a_store_side_cancellation_or_rejection_is_not_touched():
    store = Store()
    rejected = store.order("canceled", meta=ASKED)
    refunded = store.order("refunded", meta={"payment_method": "cod"})
    assert store.sweep(hours=25) == 0
    assert store.state(rejected) == ("canceled", *UNTOUCHED)
    assert store.state(refunded) == ("refunded", *UNTOUCHED)


# ── Tenants ──────────────────────────────────────────────────────────────────


def test_another_tenants_records_never_decide_an_order_or_get_swept():
    first = Store()
    second = Store(first.db)
    disabled = Store(first.db, enabled=False)
    waiting = first.order("pending_confirmation", meta=ASKED)
    # An event in another tenant naming the same reference decides nothing here.
    second.event(waiting, "order.cod.confirmed")
    theirs_confirmed = second.order("under_review", meta={**ASKED, "cod_confirmed_at": NOW.isoformat()})
    theirs_waiting = second.order("pending_confirmation", meta=ASKED)
    unswept = disabled.order("pending_confirmation", meta=ASKED)

    assert first.sweep(hours=25) == 1
    assert first.state(waiting)[:2] == ("cancelled", True)
    assert second.state(theirs_waiting) == ("pending_confirmation", *UNTOUCHED)       # not swept by the first
    assert second.sweep(hours=25) == 1
    assert second.state(theirs_confirmed) == ("under_review", *UNTOUCHED)
    assert second.state(theirs_waiting)[:2] == ("cancelled", True)
    assert disabled.sweep(hours=25) == 0
    assert disabled.state(unswept) == ("pending_confirmation", *UNTOUCHED)


# ── Incomplete old rows ──────────────────────────────────────────────────────


def test_incomplete_old_rows_are_read_safely():
    store = Store()
    no_meta = store.order("pending_confirmation", meta=None)                   # no created_at: skipped
    empty_decision = store.order("under_review", meta={**ASKED, "cod_confirmed_at": None,
                                                       "cod_confirm_requested_at": ""})
    assert store.sweep(hours=25) == 1
    assert store.state(no_meta) == ("pending_confirmation", *UNTOUCHED)
    assert store.state(empty_decision)[:2] == ("cancelled", True)              # an empty value is no decision


@pytest.mark.parametrize("meta", [None, [], "not-a-dict"])
def test_metadata_that_is_not_a_mapping_is_read_as_empty(meta: Any):
    pending = SimpleNamespace(id=7, status="pending_confirmation", extra_metadata=meta)
    reviewed = SimpleNamespace(id=8, status="under_review", extra_metadata=meta)
    assert cod_awaits_customer_decision(pending) is True
    assert cod_awaits_customer_decision(reviewed) is False
    assert cod_awaits_customer_decision(pending, decided_refs={"7"}) is False
