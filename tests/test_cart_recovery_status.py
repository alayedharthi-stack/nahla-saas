"""
tests/test_cart_recovery_status.py
──────────────────────────────────
Pin the contract of ``services.cart_recovery_status.summarise_for_orders``
and ``timeline_for_order``: the data the merchant dashboard surfaces in
the abandoned-carts queue MUST answer five questions correctly:

  1. "Who got reminder #1, #2, #3?"
  2. "Whose reminder is pending right now?"
  3. "Whose reminder failed and why?"
  4. "Whose recovery was cancelled because they bought (and when)?"
  5. "Which carts have no recovery event linked at all?"

These are the exact regressions a future refactor of the engine /
emitter / cancel service could re-introduce silently. A test failure
here is the canary for the merchant losing visibility into the recovery
funnel.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    AutomationEvent,
    AutomationExecution,
    Base,
    Customer,
    Order,
    SmartAutomation,
    Tenant,
)
from services.cart_recovery_status import (  # noqa: E402
    RECOVERY_STATUS_COMPLETED,
    RECOVERY_STATUS_CONVERTED,
    RECOVERY_STATUS_FAILED,
    RECOVERY_STATUS_IN_PROGRESS,
    RECOVERY_STATUS_NO_RECOVERY,
    RECOVERY_STATUS_PENDING,
    summarise_for_orders,
    timeline_for_order,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    t = Tenant(name="recovery status test", is_active=True)
    db.add(t)
    db.commit()
    return db, t.id


def _customer(db, tid):
    c = Customer(tenant_id=tid, name="عميل", phone="+966500111222")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _automation(db, tid, *, num_steps=3):
    """Create a cart recovery automation with ``num_steps`` stages."""
    delays = [30, 360, 1425, 4320]  # 30m, 6h, ~24h, 3d
    steps = [{"delay_minutes": d, "enabled": True} for d in delays[:num_steps]]
    a = SmartAutomation(
        tenant_id=tid,
        automation_type="abandoned_cart",
        name="Cart recovery",
        enabled=True,
        engine="advanced",
        config={"steps": steps},
        trigger_event="cart_abandoned",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _order(db, tid, *, recovery_event_id=None, **meta_extra):
    meta = {"created_at": "2026-04-20T10:00:00+00:00"}
    if recovery_event_id is not None:
        meta["recovery_event_id"] = recovery_event_id
    meta.update(meta_extra)
    o = Order(
        tenant_id=tid,
        external_id="cart-test",
        status="abandoned",
        customer_name="عميل",
        total=100.0,
        is_abandoned=True,
        extra_metadata=meta,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _event(db, tid, customer_id, *, payload=None, created_at=None, processed=False, automation_id=None):
    ev = AutomationEvent(
        tenant_id=tid,
        event_type="cart_abandoned",
        customer_id=customer_id,
        payload=payload or {},
        processed=processed,
        automation_id=automation_id,
    )
    if created_at is not None:
        ev.created_at = created_at
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def _execution(db, tid, *, event_id, automation_id, customer_id, status, executed_at=None,
               error_message=None, action_taken=None):
    ex = AutomationExecution(
        tenant_id=tid,
        automation_id=automation_id,
        event_id=event_id,
        customer_id=customer_id,
        status=status,
        error_message=error_message,
        action_taken=action_taken or {},
    )
    if executed_at is not None:
        ex.executed_at = executed_at
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


# ── 1. Carts without any recovery event ──────────────────────────────────────
def test_summarise_marks_carts_without_recovery_event_as_no_recovery():
    db, tid = _db()
    o = _order(db, tid)  # no recovery_event_id

    out = summarise_for_orders(db, tid, [o])
    assert out[o.id]["status"] == RECOVERY_STATUS_NO_RECOVERY
    assert out[o.id]["steps_sent"] == 0
    assert out[o.id]["recovery_event_id"] is None


# ── 2. Pending: event emitted but engine has not run yet ─────────────────────
def test_summarise_marks_event_without_execution_as_pending():
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid)
    ev = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    o = _order(db, tid, recovery_event_id=ev.id)

    out = summarise_for_orders(db, tid, [o])
    s = out[o.id]
    assert s["status"] == RECOVERY_STATUS_PENDING
    assert s["steps_sent"] == 0
    assert s["next_pending_at"] is not None
    assert s["recovery_event_id"] == ev.id


# ── 3. In-progress: stage 1 sent, stage 2 still pending ─────────────────────
def test_summarise_in_progress_when_some_sent_some_pending():
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    sent_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    _execution(
        db, tid, event_id=root.id, automation_id=auto.id, customer_id=cust.id,
        status="sent", executed_at=sent_at,
        action_taken={"template_name": "abandoned_cart_recovery_ar", "wa_message_id": "wamid.X"},
    )

    follow = _event(
        db, tid, cust.id, automation_id=auto.id,
        payload={"step_idx": 1, "parent_event_id": root.id},
        created_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
    )
    o = _order(db, tid, recovery_event_id=root.id)

    out = summarise_for_orders(db, tid, [o])
    s = out[o.id]
    assert s["status"] == RECOVERY_STATUS_IN_PROGRESS
    assert s["steps_sent"] == 1
    assert s["last_sent_at"] is not None
    assert s["next_pending_at"] is not None
    assert "2026-04-20T18:00:00" in s["next_pending_at"]
    assert s["recovery_event_id"] == root.id


# ── 4. Completed: ALL configured stages sent, no pending ────────────────────
def test_summarise_completed_when_all_stages_sent():
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=3)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    s2 = _event(db, tid, cust.id, automation_id=auto.id,
                payload={"step_idx": 1, "parent_event_id": root.id})
    s3 = _event(db, tid, cust.id, automation_id=auto.id,
                payload={"step_idx": 2, "parent_event_id": root.id})

    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc))
    _execution(db, tid, event_id=s2.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 20, 16, 0, tzinfo=timezone.utc))
    _execution(db, tid, event_id=s3.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 21, 9, 45, tzinfo=timezone.utc))

    o = _order(db, tid, recovery_event_id=root.id)
    out = summarise_for_orders(db, tid, [o])
    assert out[o.id]["status"] == RECOVERY_STATUS_COMPLETED
    assert out[o.id]["steps_sent"] == 3
    assert out[o.id]["total_stages"] == 3


# ── 5. Failed: latest execution failed with a real error message ────────────
def test_summarise_failed_when_real_error_and_no_sent_stage():
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid)
    ev = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    _execution(
        db, tid, event_id=ev.id, automation_id=auto.id, customer_id=cust.id,
        status="failed", error_message="WhatsApp template not approved",
        executed_at=datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc),
    )
    o = _order(db, tid, recovery_event_id=ev.id)

    s = summarise_for_orders(db, tid, [o])[o.id]
    assert s["status"] == RECOVERY_STATUS_FAILED
    assert s["steps_failed"] == 1
    assert s["last_error"] == "WhatsApp template not approved"


# ── 6. Converted (cancel-on-purchase) overrides any other state ─────────────
def test_summarise_converted_when_recovery_converted_at_set():
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid)
    converted_at = "2026-04-20T15:30:00+00:00"
    ev = _event(
        db, tid, cust.id, automation_id=auto.id,
        payload={
            "step_idx": 0,
            "recovery_converted_at": converted_at,
            "recovery_cancel_reason": "customer_purchased",
        },
    )
    _execution(
        db, tid, event_id=ev.id, automation_id=auto.id, customer_id=cust.id,
        status="sent",
        executed_at=datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc),
    )
    o = _order(db, tid, recovery_event_id=ev.id)

    s = summarise_for_orders(db, tid, [o])[o.id]
    assert s["status"] == RECOVERY_STATUS_CONVERTED
    assert s["converted_at"] == converted_at
    assert s["cancel_reason"] == "customer_purchased"


# ── 7. Cancel service: failed status with skip_reason → treated as skipped ──
def test_step_recorded_as_skipped_when_failed_status_carries_metrics_skip_reason():
    """Stage-1 skip_reason is surfaced on the real step; later configured
    stages appear as ``upcoming`` in the full recovery roadmap."""
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid)
    ev = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    _execution(
        db, tid, event_id=ev.id, automation_id=auto.id, customer_id=cust.id,
        status="failed", error_message=None,
        action_taken={"metrics": {"skip_reason": "customer_purchased",
                                   "converted": True}},
    )
    o = _order(db, tid, recovery_event_id=ev.id)

    tl = timeline_for_order(db, tid, o)
    assert len(tl["steps"]) == 3
    stage1 = next(s for s in tl["steps"] if s["step_idx"] == 1)
    assert stage1["status"] == "skipped"
    assert stage1["skip_reason"] == "customer_purchased"
    upcoming = [s for s in tl["steps"] if s["status"] == "upcoming"]
    assert len(upcoming) == 2
    assert [s["step_idx"] for s in upcoming] == [2, 3]


# ── 8. timeline_for_order returns chronological steps with full context ─────
def test_timeline_returns_full_step_chain_in_step_idx_order():
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=3)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    s2 = _event(db, tid, cust.id, automation_id=auto.id,
                payload={"step_idx": 1, "parent_event_id": root.id})
    s3 = _event(db, tid, cust.id, automation_id=auto.id,
                payload={"step_idx": 2, "parent_event_id": root.id})

    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               action_taken={"template_name": "tpl_30min", "wa_message_id": "wamid.1"})
    _execution(db, tid, event_id=s2.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               action_taken={"template_name": "tpl_6h", "wa_message_id": "wamid.2"})
    # s3 has no execution → still pending

    o = _order(db, tid, recovery_event_id=root.id)
    tl = timeline_for_order(db, tid, o)

    assert len(tl["steps"]) == 3
    assert [s["step_idx"] for s in tl["steps"]] == [1, 2, 3]
    assert tl["steps"][0]["status"] == "sent"
    assert tl["steps"][0]["template_name"] == "tpl_30min"
    assert tl["steps"][0]["wa_message_id"] == "wamid.1"
    assert tl["steps"][2]["status"] == "pending"
    assert tl["status"] == RECOVERY_STATUS_IN_PROGRESS


# ── 9. Empty timeline for cart without recovery_event_id ────────────────────
def test_timeline_returns_empty_steps_when_no_recovery_event():
    db, tid = _db()
    o = _order(db, tid)
    tl = timeline_for_order(db, tid, o)
    assert tl["status"] == RECOVERY_STATUS_NO_RECOVERY
    assert tl["steps"] == []


# ── 10. Batch summarise across many orders works without N+1 explosion ──────
def test_summarise_handles_mixed_batch_correctly():
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid)

    # Order A: no recovery
    o_a = Order(tenant_id=tid, external_id="cart-a", status="abandoned",
                customer_name="A", total=10, is_abandoned=True,
                extra_metadata={"created_at": "2026-04-20T10:00:00+00:00"})
    # Order B: pending (event but no execution)
    ev_b = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    o_b = Order(tenant_id=tid, external_id="cart-b", status="abandoned",
                customer_name="B", total=20, is_abandoned=True,
                extra_metadata={"recovery_event_id": ev_b.id,
                                "created_at": "2026-04-20T10:00:00+00:00"})
    # Order C: converted
    ev_c = _event(db, tid, cust.id, automation_id=auto.id,
                  payload={"step_idx": 0,
                           "recovery_converted_at": "2026-04-20T14:00:00+00:00",
                           "recovery_cancel_reason": "order_paid"})
    o_c = Order(tenant_id=tid, external_id="cart-c", status="abandoned",
                customer_name="C", total=30, is_abandoned=True,
                extra_metadata={"recovery_event_id": ev_c.id,
                                "created_at": "2026-04-20T10:00:00+00:00"})

    db.add_all([o_a, o_b, o_c])
    db.commit()
    for o in (o_a, o_b, o_c):
        db.refresh(o)

    out = summarise_for_orders(db, tid, [o_a, o_b, o_c])
    assert out[o_a.id]["status"] == RECOVERY_STATUS_NO_RECOVERY
    assert out[o_b.id]["status"] == RECOVERY_STATUS_PENDING
    assert out[o_c.id]["status"] == RECOVERY_STATUS_CONVERTED


# ═══════════════════════════════════════════════════════════════════════════════
#  REGRESSION: stage 1 only sent → must NOT show "completed"
# ═══════════════════════════════════════════════════════════════════════════════

def test_stage1_only_sent_is_in_progress_not_completed():
    """After Stage 1 is sent but follow-ups haven't been emitted yet,
    the status MUST be in_progress — never completed."""
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=3)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc))
    o = _order(db, tid, recovery_event_id=root.id)

    s = summarise_for_orders(db, tid, [o])[o.id]
    assert s["status"] == RECOVERY_STATUS_IN_PROGRESS, \
        f"Expected in_progress after stage 1 only, got {s['status']}"
    assert s["steps_sent"] == 1
    assert s["total_stages"] == 3


def test_stage1_and_stage2_sent_is_in_progress_not_completed():
    """After 2 of 3 stages are sent, still in_progress."""
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=3)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    s2 = _event(db, tid, cust.id, automation_id=auto.id,
                payload={"step_idx": 1, "parent_event_id": root.id})
    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc))
    _execution(db, tid, event_id=s2.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 20, 16, 0, tzinfo=timezone.utc))
    o = _order(db, tid, recovery_event_id=root.id)

    s = summarise_for_orders(db, tid, [o])[o.id]
    assert s["status"] == RECOVERY_STATUS_IN_PROGRESS
    assert s["steps_sent"] == 2


def test_converted_after_stage1_is_converted_not_completed():
    """If the customer buys after Stage 1, status = converted."""
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=3)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={
        "step_idx": 0,
        "recovery_converted_at": "2026-04-20T11:00:00+00:00",
        "recovery_cancel_reason": "customer_purchased",
    })
    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc))
    o = _order(db, tid, recovery_event_id=root.id)

    s = summarise_for_orders(db, tid, [o])[o.id]
    assert s["status"] == RECOVERY_STATUS_CONVERTED


def test_step_idx_conversion_0_based_to_1_based():
    """Payload step_idx is 0-based; displayed step_idx must be 1-based."""
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=3)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    s2 = _event(db, tid, cust.id, automation_id=auto.id,
                payload={"step_idx": 1, "parent_event_id": root.id})
    s3 = _event(db, tid, cust.id, automation_id=auto.id,
                payload={"step_idx": 2, "parent_event_id": root.id})
    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent")
    o = _order(db, tid, recovery_event_id=root.id)

    tl = timeline_for_order(db, tid, o)
    assert [s["step_idx"] for s in tl["steps"]] == [1, 2, 3]
    assert tl["steps"][0]["status"] == "sent"
    assert tl["steps"][1]["status"] == "pending"
    assert tl["steps"][2]["status"] == "pending"


def test_timeline_step_sent_shows_correct_status():
    """A step with an execution status='sent' must report status='sent',
    not 'pending' or 'no_recovery' (the bug that showed 'لم يبدأ')."""
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=3)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent",
               executed_at=datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc),
               action_taken={"template_name": "cart_reminder", "wa_message_id": "wamid.abc"})
    o = _order(db, tid, recovery_event_id=root.id)

    tl = timeline_for_order(db, tid, o)
    step1 = tl["steps"][0]
    assert step1["status"] == "sent"
    assert step1["sent_at"] is not None
    assert step1["template_name"] == "cart_reminder"


def test_total_stages_derived_from_automation_config():
    """total_stages in the response must match the automation's config."""
    db, tid = _db()
    cust = _customer(db, tid)
    auto = _automation(db, tid, num_steps=4)

    root = _event(db, tid, cust.id, automation_id=auto.id, payload={"step_idx": 0})
    _execution(db, tid, event_id=root.id, automation_id=auto.id,
               customer_id=cust.id, status="sent")
    o = _order(db, tid, recovery_event_id=root.id)

    s = summarise_for_orders(db, tid, [o])[o.id]
    assert s["total_stages"] == 4
    assert s["status"] == RECOVERY_STATUS_IN_PROGRESS
