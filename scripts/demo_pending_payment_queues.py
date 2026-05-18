"""
scripts/demo_pending_payment_queues.py
──────────────────────────────────────
Demonstration / smoke-proof for the "بانتظار الدفع" + "بانتظار التأكيد"
operational queues. Run this BEFORE the Salla demo to confirm both flows
behave end-to-end (classifier + emitter + AutomationEvent row).

What it does
────────────
Uses an in-memory SQLite database (the same harness the test suite uses)
so it never touches the real DB. Seeds:

  • Tenant #21 ("متجر تجريبي 21")
  • A customer with a Saudi mobile
  • An ``unpaid_order_reminder`` SmartAutomation (3 steps: 60m / 6h / 24h)
  • A ``cod_confirmation`` SmartAutomation (T+6h reminder, T+24h cancel)
  • Five orders covering every status path:
        1. status="في انتظار الدفع"        (Arabic alias)        → pending_payment
        2. status="PAYMENT_PENDING"        (mixed-case)         → pending_payment
        3. status="pending_confirmation"   (COD slug)           → pending_confirmation
        4. status="بإنتظار المراجعة"        (COD Arabic alias)   → pending_confirmation
        5. status="paid"                                          → no queue

Then runs ``scan_unpaid_orders`` + ``scan_cod_confirmations`` and prints:

  • The per-order classification table (what the new
    /autopilot/queues/debug endpoint returns).
  • The AutomationEvent rows actually emitted by the sweepers.
  • A one-line PASS/FAIL summary.

Run::

    railway run python scripts/demo_pending_payment_queues.py
    # or locally:
    python scripts/demo_pending_payment_queues.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from models import (  # noqa: E402
    AutomationEvent, Base, Customer, Order, SmartAutomation, Tenant,
)
from core import automation_emitters  # noqa: E402
from core.automation_triggers import AutomationTrigger  # noqa: E402
from core.order_queue_classifier import classify_order_queue  # noqa: E402

TENANT_ID = 21
PHONE = "+966500111222"


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, t in saved:
        col.type = t
    return sessionmaker(bind=engine)()


def _seed(db):
    t = Tenant(id=TENANT_ID, name="متجر تجريبي 21", is_active=True)
    db.add(t); db.commit()
    c = Customer(tenant_id=t.id, phone=PHONE, name="خالد التجريبي")
    db.add(c); db.commit()
    db.add(SmartAutomation(
        tenant_id=t.id, automation_type="unpaid_order_reminder",
        engine="recovery", trigger_event="order_payment_pending",
        name="تذكير الدفع", enabled=True,
        config={"steps": [
            {"delay_minutes": 60,   "message_type": "reminder"},
            {"delay_minutes": 360,  "message_type": "reminder"},
            {"delay_minutes": 1440, "message_type": "final"},
        ]},
    ))
    db.add(SmartAutomation(
        tenant_id=t.id, automation_type="cod_confirmation",
        engine="recovery", trigger_event="order_cod_pending",
        name="تأكيد الدفع عند الاستلام", enabled=True,
        config={"reminder_after_minutes": 360, "cancel_after_minutes": 1440,
                "steps": [{"delay_minutes": 360, "message_type": "reminder"}]},
    ))
    db.commit()

    fixtures = [
        ("ORD-AR-PAY",  "في انتظار الدفع",     timedelta(hours=3)),
        ("ORD-EN-PAY",  "PAYMENT_PENDING",      timedelta(hours=3)),
        ("ORD-COD-EN",  "pending_confirmation", timedelta(hours=7)),
        ("ORD-COD-AR",  "بإنتظار المراجعة",     timedelta(hours=7)),
        ("ORD-PAID",    "paid",                  timedelta(hours=3)),
    ]
    for ext, status, age in fixtures:
        created = (datetime.now(timezone.utc) - age).replace(tzinfo=None)
        db.add(Order(
            tenant_id=t.id, external_id=ext, external_order_number=ext,
            status=status, total="150.00", is_abandoned=False,
            customer_info={"phone": PHONE, "name": "خالد التجريبي"},
            line_items=[], extra_metadata={"created_at": created.isoformat()},
        ))
    db.commit()


def _print_classification(db):
    print()
    print("─" * 88)
    print("Per-order classification (mirrors GET /autopilot/queues/debug)")
    print("─" * 88)
    print(f"{'external_id':<14} {'raw_status':<24} {'queue_detected':<22} {'eligible'}")
    print("─" * 88)
    for o in db.query(Order).order_by(Order.id.asc()).all():
        q = classify_order_queue(o.status) or "(none)"
        print(f"{o.external_id:<14} {(o.status or ''):<24} {q:<22} —")


def _run_sweepers(db):
    print()
    print("─" * 88)
    print("Running emitters (scan_unpaid_orders + scan_cod_confirmations)")
    print("─" * 88)
    unpaid = automation_emitters.scan_unpaid_orders(db, TENANT_ID)
    cod    = automation_emitters.scan_cod_confirmations(db, TENANT_ID)
    print(f"  scan_unpaid_orders        emitted={unpaid}")
    print(f"  scan_cod_confirmations    emitted={cod}")
    return unpaid, cod


def _print_events(db):
    print()
    print("─" * 88)
    print("AutomationEvent rows created (these are what triggers the WhatsApp send)")
    print("─" * 88)
    for ev in db.query(AutomationEvent).order_by(AutomationEvent.id.asc()).all():
        payload = ev.payload or {}
        print(
            f"  event#{ev.id:<3} type={ev.event_type:<25} "
            f"order={payload.get('order_id') or payload.get('order_internal_id')} "
            f"step_idx={payload.get('step_idx')} "
            f"message_type={payload.get('message_type')}"
        )


def main() -> int:
    db = _make_db()
    _seed(db)
    _print_classification(db)
    unpaid, cod = _run_sweepers(db)
    _print_events(db)

    print()
    print("─" * 88)
    expected_unpaid = 2   # Arabic + mixed-case payment_pending orders
    expected_cod    = 2   # English + Arabic confirmation orders
    ok = (unpaid == expected_unpaid) and (cod == expected_cod)
    status_line = "PASS" if ok else "FAIL"
    print(f"Result: {status_line}  "
          f"(unpaid emitted={unpaid}/{expected_unpaid}, "
          f"cod emitted={cod}/{expected_cod})")
    print("─" * 88)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
