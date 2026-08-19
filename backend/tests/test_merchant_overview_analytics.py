"""Merchant Overview analytics — period accuracy, isolation, AI rate."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

from core.merchant_overview_analytics import (  # noqa: E402
    compute_overview_kpis,
    order_created_at,
    resolve_period_bounds,
)
from models import (  # noqa: E402
    Base,
    ConversationLog,
    Customer,
    MessageEvent,
    Order,
    Tenant,
    TenantSettings,
)

NOW = datetime(2026, 8, 20, 15, 0, 0, tzinfo=timezone.utc)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)()


@pytest.fixture
def db():
    session = _make_db()
    try:
        yield session
    finally:
        session.close()


def _tenant(db, name: str, tz: str = "Asia/Riyadh") -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.flush()
    db.add(TenantSettings(tenant_id=tenant.id, store_settings={"timezone": tz}))
    db.flush()
    return tenant


def _order(db, tenant_id, *, created, status="paid", total="100.00", source="salla", abandoned=False, meta=None):
    extra = {"created_at": created.isoformat()}
    if meta:
        extra.update(meta)
    row = Order(
        tenant_id=tenant_id,
        status=status,
        total=total,
        source=source,
        is_abandoned=abandoned,
        extra_metadata=extra,
    )
    db.add(row)
    db.flush()
    return row


def _customer(db, tenant_id, *, first_seen, phone):
    row = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=phone,
        first_seen_at=first_seen,
    )
    db.add(row)
    db.flush()
    return row


def _inbound(db, tenant_id, created, event_type="whatsapp_message"):
    db.add(MessageEvent(
        tenant_id=tenant_id,
        direction="inbound",
        event_type=event_type,
        body="hello",
        created_at=created.replace(tzinfo=None) if created.tzinfo else created,
    ))


def _ai_out(db, tenant_id, created):
    db.add(MessageEvent(
        tenant_id=tenant_id,
        direction="outbound",
        event_type="whatsapp_message",
        body="reply",
        created_at=created.replace(tzinfo=None) if created.tzinfo else created,
        extra_metadata={"is_ai": True},
    ))


def _human_out(db, tenant_id, created):
    db.add(MessageEvent(
        tenant_id=tenant_id,
        direction="outbound",
        event_type="whatsapp_message",
        body="staff",
        created_at=created.replace(tzinfo=None) if created.tzinfo else created,
        extra_metadata={"is_ai": False},
    ))


def _conv_log(db, tenant_id, started):
    db.add(ConversationLog(
        tenant_id=tenant_id,
        customer_phone="+966500000000",
        conversation_started_at=started.replace(tzinfo=None) if started.tzinfo else started,
        source="inbound",
        category="service",
    ))


def _seed_controlled(db) -> tuple[Tenant, Tenant]:
    a = _tenant(db, "analytics-merchant-a")
    b = _tenant(db, "analytics-merchant-b")
    today = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    in_7 = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    in_month = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    for _ in range(2):
        _order(db, a.id, created=today, total="100.00")
    _order(db, a.id, created=today, status="cancelled", total="40.00")
    _order(db, a.id, created=today, status="draft", total="9.00")
    _order(db, a.id, created=today, status="paid", total="70.00", abandoned=True)
    _order(db, a.id, created=today, status="paid", total="55.00", meta={"created_at": None})
    undated = Order(tenant_id=a.id, status="paid", total="55.00", source="salla", extra_metadata={})
    db.add(undated)

    _order(db, a.id, created=in_7, total="50.00")
    for _ in range(4):
        _order(db, a.id, created=in_month, total="25.00")

    for i in range(3):
        _customer(db, a.id, first_seen=today, phone=f"+96650000000{i}")
    for i in range(2):
        _customer(db, a.id, first_seen=in_7, phone=f"+96650000001{i}")
    for i in range(5):
        _customer(db, a.id, first_seen=in_month, phone=f"+96650000002{i}")
    _customer(db, a.id, first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc), phone="+966500000099")

    for _ in range(10):
        _inbound(db, a.id, today)
    for _ in range(8):
        _ai_out(db, a.id, today)
    for _ in range(2):
        _human_out(db, a.id, today)
    _inbound(db, a.id, today, event_type="coexistence_history")

    for _ in range(4):
        _inbound(db, a.id, in_7)
    for _ in range(2):
        _ai_out(db, a.id, in_7)

    for _ in range(5):
        _inbound(db, a.id, in_month)
    _ai_out(db, a.id, in_month)

    for _ in range(2):
        _conv_log(db, a.id, today)
    for _ in range(3):
        _conv_log(db, a.id, in_7)
    _conv_log(db, a.id, in_month)

    for _ in range(40):
        _order(db, b.id, created=today, total="999.00")
        _customer(db, b.id, first_seen=today, phone=f"+9665999{_:04d}")
        _inbound(db, b.id, today)
        _ai_out(db, b.id, today)
        _conv_log(db, b.id, today)

    db.commit()
    return a, b


def test_undated_order_is_excluded_not_dated_as_now(db):
    tenant = _tenant(db, "undated")
    row = Order(tenant_id=tenant.id, status="paid", total="10", extra_metadata={})
    db.add(row)
    db.commit()
    assert order_created_at(row) is None
    kpis = compute_overview_kpis(db, tenant.id, "today", now=NOW)
    assert kpis["orders"] == 0


def test_period_bounds_riyadh_today_is_calendar_not_rolling_24h(db):
    tenant = _tenant(db, "tz-today")
    bounds = resolve_period_bounds(db, tenant.id, "today", now=NOW)
    assert bounds["start_utc"] == datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc)
    last7 = resolve_period_bounds(db, tenant.id, "last_7_days", now=NOW)
    assert last7["start_utc"] == datetime(2026, 8, 13, 21, 0, tzinfo=timezone.utc)
    month = resolve_period_bounds(db, tenant.id, "this_month", now=NOW)
    assert month["start_utc"] == datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)


def test_utc_vs_merchant_midnight_boundary(db):
    tenant = _tenant(db, "boundary")
    just_before = datetime(2026, 8, 19, 20, 59, tzinfo=timezone.utc)
    just_after = datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc)
    _order(db, tenant.id, created=just_before, total="10.00")
    _order(db, tenant.id, created=just_after, total="20.00")
    db.commit()
    today = compute_overview_kpis(db, tenant.id, "today", now=NOW)
    assert today["orders"] == 1
    assert today["revenue"] == 20.0


def test_controlled_dataset_today_last7_month(db):
    a, _b = _seed_controlled(db)
    today = compute_overview_kpis(db, a.id, "today", now=NOW)
    last7 = compute_overview_kpis(db, a.id, "last_7_days", now=NOW)
    month = compute_overview_kpis(db, a.id, "this_month", now=NOW)

    assert today["orders"] == 3
    assert today["revenue"] == 200.0
    assert today["new_customers"] == 3
    assert today["conversations"] == 2
    assert today["ai_rate"] == 80.0
    assert today["ai_rate_numerator"] == 8
    assert today["ai_rate_denominator"] == 10
    assert today["card_chart_reconciled"] is True

    assert last7["orders"] == 4
    assert last7["revenue"] == 250.0
    assert last7["new_customers"] == 5
    assert last7["conversations"] == 5
    assert last7["ai_rate"] == 71.4
    assert last7["card_chart_reconciled"] is True

    assert month["orders"] == 8
    assert month["revenue"] == 350.0
    assert month["new_customers"] == 10
    assert month["conversations"] == 6
    assert month["ai_rate"] == 57.9
    assert month["card_chart_reconciled"] is True

    assert today["orders"] != last7["orders"] != month["orders"]
    assert today["new_customers"] != month["new_customers"]
    assert today["ai_rate"] != month["ai_rate"]


def test_tenant_isolation(db):
    a, b = _seed_controlled(db)
    ka = compute_overview_kpis(db, a.id, "today", now=NOW)
    kb = compute_overview_kpis(db, b.id, "today", now=NOW)
    assert ka["orders"] == 3
    assert kb["orders"] == 40
    assert kb["new_customers"] == 40
    assert ka["revenue"] != kb["revenue"]


def test_last_7_days_is_calendar_not_rolling_24h(db):
    tenant = _tenant(db, "calendar-7")
    # Inside rolling 7*24h from NOW, but before Riyadh Aug 14 00:00.
    in_rolling_only = datetime(2026, 8, 13, 16, 0, tzinfo=timezone.utc)
    _order(db, tenant.id, created=in_rolling_only, total="15.00")
    db.commit()
    last7 = compute_overview_kpis(db, tenant.id, "last_7_days", now=NOW)
    assert last7["orders"] == 0


def test_zero_denominator_is_null_not_100(db):
    tenant = _tenant(db, "zero-den")
    kpis = compute_overview_kpis(db, tenant.id, "today", now=NOW)
    assert kpis["ai_rate"] is None
    assert kpis["ai_rate_denominator"] == 0
    assert kpis["orders"] == 0
    assert kpis["new_customers"] == 0


def test_existing_customer_not_counted_as_new(db):
    tenant = _tenant(db, "existing-cust")
    _customer(db, tenant.id, first_seen=datetime(2025, 12, 1, tzinfo=timezone.utc), phone="+966511111111")
    db.commit()
    kpis = compute_overview_kpis(db, tenant.id, "this_month", now=NOW)
    assert kpis["new_customers"] == 0


def test_ai_rate_follows_numerator_over_denominator(db):
    tenant = _tenant(db, "uncapped-rate")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    for _ in range(2):
        _inbound(db, tenant.id, t)
    for _ in range(5):
        _ai_out(db, tenant.id, t)
    db.commit()
    kpis = compute_overview_kpis(db, tenant.id, "today", now=NOW)
    assert kpis["ai_rate_numerator"] == 5
    assert kpis["ai_rate_denominator"] == 2
    assert kpis["ai_rate"] == 250.0


def test_human_outbound_does_not_inflate_ai_rate(db):
    tenant = _tenant(db, "human-takeover")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    _inbound(db, tenant.id, t)
    _human_out(db, tenant.id, t)
    db.commit()
    kpis = compute_overview_kpis(db, tenant.id, "today", now=NOW)
    assert kpis["ai_rate_denominator"] == 1
    assert kpis["ai_rate_numerator"] == 0
    assert kpis["ai_rate"] == 0.0
