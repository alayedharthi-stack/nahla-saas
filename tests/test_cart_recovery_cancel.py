"""
tests/test_cart_recovery_cancel.py
──────────────────────────────────
Pin the P0 invariant: once a customer pays, no more abandoned-cart
reminders ever go out — regardless of whether they tapped a button on
WhatsApp or paid directly on the storefront.

The invariant is enforced in three layers (all tested here):

  1. Event-driven cancellation
     ``services.cart_recovery_cancel.cancel_recovery_for_customer``
     runs from ``StoreSyncService.handle_order_webhook`` the moment a
     real purchase webhook lands. It walks every unprocessed
     cart_abandoned event for the customer, marks queued follow-ups as
     processed, flattens recovery_followups on parent events, stamps
     ``converted=True`` on AutomationExecution rows, and flips the
     matching cart's ``is_abandoned=False`` so the dashboard hides it.

  2. Pre-send fast-path guard
     ``automation_engine._detect_recovery_already_converted`` reads the
     ``recovery_converted_at`` payload stamp BEFORE hitting the
     conversion-layer DB lookup. Catches anything the cancel hook
     stamps even if the Order rows haven't fully synced.

  3. Sweeper guard (existing) and conversion-layer DB guard (existing).
     We don't re-test those here — they already have coverage in
     tests/test_abandoned_cart_recovery.py and tests/test_conversion_layer.py.

Each test below maps to one of the explicit requirements in the
"stop reminders after purchase" spec.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import (  # noqa: E402
    Base,
    AutomationEvent,
    AutomationExecution,
    Customer,
    Order,
    SmartAutomation,
    Tenant,
)
from services.cart_recovery_cancel import (  # noqa: E402
    cancel_recovery_for_customer,
    order_is_a_purchase,
)


# SQLite needs JSONB → JSON remap (same trick the sibling suites use).
@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Cancel Test Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


def _make_customer(db, tenant_id, *, phone="+966500111222") -> Customer:
    cust = Customer(tenant_id=tenant_id, name="عميل تجريبي", phone=phone)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


def _make_automation(db, tenant_id) -> SmartAutomation:
    auto = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="abandoned_cart",
        name="Cart Recovery",
        enabled=True,
        engine="recovery",
        config={"steps": [{}, {}, {}, {}]},
        trigger_event="cart_abandoned",
    )
    db.add(auto)
    db.commit()
    db.refresh(auto)
    return auto


def _make_parent_event(db, tenant_id, customer_id, *, cart_id="999") -> AutomationEvent:
    """Create a stage-1 cart_abandoned event with one recovery_followup
    already emitted (mirrors the state right after stage 2 fired)."""
    ev = AutomationEvent(
        tenant_id=tenant_id,
        event_type="cart_abandoned",
        customer_id=customer_id,
        payload={
            "cart_id":       cart_id,
            "checkout_url":  f"https://store.example/cart/{cart_id}",
            "step_idx":      0,
            "recovery_followups": [
                {"step_idx": 1, "emitted_at": datetime.utcnow().isoformat()},
            ],
        },
        processed=True,
        created_at=datetime.utcnow() - timedelta(hours=2),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def _make_pending_followup(db, tenant_id, customer_id, *, parent_id, step_idx=2,
                           fire_in_minutes=60) -> AutomationEvent:
    """Insert a future-dated cart_abandoned follow-up (e.g. one queued
    by the postpone path or by the sweeper)."""
    fire_at = datetime.utcnow() + timedelta(minutes=fire_in_minutes)
    ev = AutomationEvent(
        tenant_id=tenant_id,
        event_type="cart_abandoned",
        customer_id=customer_id,
        payload={
            "cart_id":         "999",
            "step_idx":        step_idx,
            "parent_event_id": parent_id,
        },
        processed=False,
        created_at=fire_at,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def _make_execution(db, tenant_id, automation_id, event_id, customer_id) -> AutomationExecution:
    ex = AutomationExecution(
        tenant_id=tenant_id,
        automation_id=automation_id,
        event_id=event_id,
        customer_id=customer_id,
        status="sent",
        action_taken={"metrics": {"clicked": 0}},
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


def _make_abandoned_cart_row(db, tenant_id, *, phone="+966500111222",
                             external_id="cart-999") -> Order:
    cart = Order(
        tenant_id=tenant_id,
        external_id=external_id,
        external_order_number=external_id,
        status="abandoned",
        total="248.00",
        is_abandoned=True,
        source="salla",
        customer_info={"name": "عميل تجريبي", "phone": phone},
        checkout_url="https://store.example/cart/999",
    )
    db.add(cart)
    db.commit()
    db.refresh(cart)
    return cart


# ── 1. Webhook before next scheduled step → step skipped ─────────────────────
#
# This is the worst-case real-world scenario the user explicitly called
# out: a future-dated reminder is already queued, the customer pays
# directly on the website, and the order webhook arrives before the
# engine wakes up to fire the reminder. The cancel hook MUST mark the
# queued event as processed so the engine never picks it up.

def test_pending_followup_event_is_marked_processed_after_purchase():
    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)
    auto = _make_automation(db, tenant_id)
    parent = _make_parent_event(db, tenant_id, customer.id)
    pending = _make_pending_followup(db, tenant_id, customer.id, parent_id=parent.id)
    _make_execution(db, tenant_id, auto.id, parent.id, customer.id)

    counters = cancel_recovery_for_customer(
        db,
        tenant_id=tenant_id,
        customer_id=customer.id,
        reason="customer_purchased",
        order_id=42,
        order_external_id="ORD-42",
        order_status="completed",
    )

    assert counters["events_cancelled"] == 1, (
        "The future-dated follow-up event must be cancelled BEFORE the "
        "engine has a chance to pick it up."
    )

    db.refresh(pending)
    assert pending.processed is True
    assert (pending.payload or {}).get("recovery_converted_at")
    assert (pending.payload or {}).get("recovery_cancel_reason") == "customer_purchased"


# ── 2. Customer purchases from website directly → automation stops ───────────
#
# Same path as above but verifies the FULL state machine: parent event
# is flattened, execution metrics stamped, cart row flipped. This is
# the end-to-end check that mirrors the production webhook flow.

def test_direct_website_purchase_stops_entire_recovery_flow():
    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)
    auto = _make_automation(db, tenant_id)
    parent = _make_parent_event(db, tenant_id, customer.id)
    _make_pending_followup(db, tenant_id, customer.id, parent_id=parent.id, step_idx=2)
    _make_pending_followup(db, tenant_id, customer.id, parent_id=parent.id, step_idx=3)
    execution = _make_execution(db, tenant_id, auto.id, parent.id, customer.id)
    cart = _make_abandoned_cart_row(db, tenant_id)

    counters = cancel_recovery_for_customer(
        db,
        tenant_id=tenant_id,
        customer_id=customer.id,
        reason="customer_purchased",
        order_id=77,
        order_external_id="ORD-77",
        order_status="paid",
    )

    assert counters["events_cancelled"] == 2
    assert counters["parent_events_marked"] == 1
    assert counters["executions_stamped"] == 1
    assert counters["carts_recovered"] == 1

    db.refresh(parent)
    progress = (parent.payload or {}).get("recovery_followups") or []
    skipped_steps = {p["step_idx"] for p in progress if p.get("skipped")}
    # Stage 1 was already emitted, so it stays. Stages 2..7 should now
    # all be marked skipped with the cancel reason.
    assert skipped_steps >= {2, 3, 4, 5, 6, 7}
    for entry in progress:
        if entry.get("skipped"):
            assert entry["reason"] == "customer_purchased"

    db.refresh(execution)
    metrics = (execution.action_taken or {}).get("metrics") or {}
    assert metrics["converted"] is True
    assert metrics["remaining_steps_skipped"] is True
    assert metrics["skip_reason"] == "customer_purchased"
    assert metrics["converted_order_id"] == 77

    db.refresh(cart)
    assert cart.is_abandoned is False, (
        "Cart must disappear from the merchant's abandoned-carts queue "
        "the moment the customer's order webhook lands."
    )
    assert (cart.extra_metadata or {}).get("recovered_at")
    assert (cart.extra_metadata or {}).get("recovered_via") == "customer_purchased"


# ── 3. Converted cart disappears from the abandoned queue ────────────────────

def test_converted_cart_no_longer_appears_in_dashboard_query():
    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)
    parent = _make_parent_event(db, tenant_id, customer.id)
    cart = _make_abandoned_cart_row(db, tenant_id)

    cancel_recovery_for_customer(
        db,
        tenant_id=tenant_id,
        customer_id=customer.id,
        reason="customer_purchased",
        order_id=1,
        order_external_id="ORD-1",
        order_status="completed",
    )

    rows = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned == True)  # noqa: E712
        .all()
    )
    assert rows == [], (
        "The /autopilot/queues dashboard query is `is_abandoned == True`. "
        "After a real purchase that flag must be cleared so the cart "
        "stops showing in the merchant's abandoned-carts panel."
    )

    db.refresh(cart)
    assert cart.is_abandoned is False


# ── 4. Pre-send fast-path guard (recovery_converted_at on payload) ───────────
#
# Even if the cancel hook ran but the queued event was already inside
# `_execute_action` when the webhook landed (race), the engine's pre-send
# fast-path guard reads `recovery_converted_at` from the parent event
# and bails before hitting WhatsApp.

def test_pre_send_guard_detects_converted_on_parent_event():
    from core.automation_engine import _detect_recovery_already_converted

    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)
    parent = _make_parent_event(db, tenant_id, customer.id)
    follower = _make_pending_followup(db, tenant_id, customer.id, parent_id=parent.id)

    # Stamp the cancel marker on the parent (mirrors what the cancel
    # hook does in production).
    parent_payload = dict(parent.payload or {})
    parent_payload["recovery_converted_at"]  = datetime.utcnow().isoformat()
    parent_payload["recovery_cancel_reason"] = "customer_purchased"
    parent.payload = parent_payload
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(parent, "payload")
    db.commit()

    reason = _detect_recovery_already_converted(db, follower)
    assert reason == "customer_purchased", (
        "Follow-up event must inherit the parent's converted stamp via "
        "parent_event_id so the pre-send guard short-circuits before any "
        "WhatsApp send is attempted."
    )


def test_pre_send_guard_detects_converted_on_event_itself():
    from core.automation_engine import _detect_recovery_already_converted

    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)
    parent = _make_parent_event(db, tenant_id, customer.id)
    follower = _make_pending_followup(db, tenant_id, customer.id, parent_id=parent.id)

    payload = dict(follower.payload or {})
    payload["recovery_converted_at"]  = datetime.utcnow().isoformat()
    payload["recovery_cancel_reason"] = "customer_purchased"
    follower.payload = payload
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(follower, "payload")
    db.commit()

    assert _detect_recovery_already_converted(db, follower) == "customer_purchased"


def test_pre_send_guard_returns_none_when_no_stamp_present():
    from core.automation_engine import _detect_recovery_already_converted

    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)
    parent = _make_parent_event(db, tenant_id, customer.id)
    follower = _make_pending_followup(db, tenant_id, customer.id, parent_id=parent.id)

    assert _detect_recovery_already_converted(db, follower) is None, (
        "Without a cancel stamp the engine must fall through to the "
        "conversion-layer DB check — never default to skipping."
    )


# ── 5. Pending / draft order status does NOT cancel ──────────────────────────
#
# Salla emits webhooks for draft and pending-payment orders too. Those
# are NOT a real purchase — the customer literally just clicked
# "checkout" without paying. We must keep chasing the cart, not silence
# the workflow on a half-finished order.

def test_pending_payment_order_status_does_not_cancel_recovery():
    assert order_is_a_purchase("pending") is False
    assert order_is_a_purchase("pending_payment") is False
    assert order_is_a_purchase("payment_pending") is False
    assert order_is_a_purchase("awaiting_payment") is False
    assert order_is_a_purchase("draft") is False
    assert order_is_a_purchase("new") is False
    assert order_is_a_purchase("cancelled") is False
    assert order_is_a_purchase("refunded") is False
    assert order_is_a_purchase(None) is False
    assert order_is_a_purchase("") is False


def test_paid_or_completed_order_status_triggers_cancellation():
    assert order_is_a_purchase("paid") is True
    assert order_is_a_purchase("completed") is True
    assert order_is_a_purchase("processing") is True
    assert order_is_a_purchase("shipped") is True
    assert order_is_a_purchase("delivered") is True


# ── 6. Idempotency ───────────────────────────────────────────────────────────
#
# The cancel hook may fire twice (e.g. order.created and then
# order.payment.success arrive in close succession). Re-runs must not
# double-stamp metrics or revive previously-cancelled rows.

def test_cancellation_is_idempotent_on_repeat_invocation():
    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)
    auto = _make_automation(db, tenant_id)
    parent = _make_parent_event(db, tenant_id, customer.id)
    pending = _make_pending_followup(db, tenant_id, customer.id, parent_id=parent.id)
    _make_execution(db, tenant_id, auto.id, parent.id, customer.id)
    _make_abandoned_cart_row(db, tenant_id)

    first = cancel_recovery_for_customer(
        db, tenant_id=tenant_id, customer_id=customer.id,
        reason="customer_purchased", order_id=1, order_external_id="ORD-1",
        order_status="paid",
    )
    second = cancel_recovery_for_customer(
        db, tenant_id=tenant_id, customer_id=customer.id,
        reason="customer_purchased", order_id=1, order_external_id="ORD-1",
        order_status="paid",
    )

    assert first["events_cancelled"] == 1
    assert second["events_cancelled"] == 0, "Already-cancelled events must not re-stamp."

    # Cart row stays cleared, doesn't bounce back.
    db.refresh(pending)
    assert pending.processed is True
    rows = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned == True)  # noqa: E712
        .all()
    )
    assert rows == []


# ── 7. Tenant isolation ──────────────────────────────────────────────────────
#
# A purchase from tenant A must NEVER cancel a recovery thread for the
# same customer ID at tenant B (which would be a different person).

def test_cancellation_does_not_cross_tenants():
    db, tenant_id_a = _make_db()
    # Add a second tenant + identically-numbered customer in the same DB.
    tenant_b = Tenant(name="Tenant B", is_active=True)
    db.add(tenant_b)
    db.commit()
    tenant_id_b = tenant_b.id

    customer_a = _make_customer(db, tenant_id_a, phone="+9665000000A")
    customer_b = Customer(tenant_id=tenant_id_b, name="عميل B", phone="+9665000000B")
    db.add(customer_b)
    db.commit()
    db.refresh(customer_b)

    parent_a = _make_parent_event(db, tenant_id_a, customer_a.id)
    parent_b = _make_parent_event(db, tenant_id_b, customer_b.id)
    follow_b = _make_pending_followup(db, tenant_id_b, customer_b.id, parent_id=parent_b.id)

    cancel_recovery_for_customer(
        db, tenant_id=tenant_id_a, customer_id=customer_a.id,
        reason="customer_purchased", order_id=1, order_external_id="ORD-A",
        order_status="paid",
    )

    db.refresh(follow_b)
    assert follow_b.processed is False, (
        "Tenant A's purchase must never touch tenant B's recovery flow "
        "even when customer IDs collide."
    )
    assert "recovery_converted_at" not in (follow_b.payload or {})


# ── 8. No-op when customer has no recovery thread ────────────────────────────

def test_cancel_is_a_noop_when_no_recovery_state_exists():
    db, tenant_id = _make_db()
    customer = _make_customer(db, tenant_id)

    counters = cancel_recovery_for_customer(
        db, tenant_id=tenant_id, customer_id=customer.id,
        reason="customer_purchased", order_id=1, order_external_id="ORD-1",
        order_status="paid",
    )

    assert counters == {
        "events_cancelled":     0,
        "parent_events_marked": 0,
        "executions_stamped":   0,
        "carts_recovered":      0,
    }


# ── 9. Customer ID is required ───────────────────────────────────────────────

def test_cancel_with_falsy_customer_id_is_short_circuited():
    db, tenant_id = _make_db()
    counters = cancel_recovery_for_customer(
        db, tenant_id=tenant_id, customer_id=0,
        reason="customer_purchased", order_id=1, order_external_id="ORD-1",
        order_status="paid",
    )
    assert counters["events_cancelled"] == 0
    assert counters["parent_events_marked"] == 0
