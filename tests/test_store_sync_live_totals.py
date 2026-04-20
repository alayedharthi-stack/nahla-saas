"""
tests/test_store_sync_live_totals.py
────────────────────────────────────
Regression tests for the data-consistency bug where the Connection /
Knowledge widget displayed misleading "0 طلبات / 0 كوبونات" while the
dedicated Orders and Coupons pages still listed real, previously-synced
data.

Root cause: ``StoreKnowledgeSnapshot.{order,coupon,product,category}_count``
columns are written by ``_rebuild_snapshot`` from the **delta** of the most
recent sync run (``created + updated``), not the live totals. When a sync
returns an empty delta (e.g. Salla 401, empty incremental window), those
columns were silently zeroed even though the real ``orders``/``coupons``
tables still held the correct rows. The widget read the snapshot columns
directly and showed zeros.

Fix: ``StoreSyncService._compute_live_totals`` now exposes counts straight
from the source-of-truth tables (same tables ``GET /orders`` and
``GET /coupons`` read from), and ``get_status`` / ``/store-sync/knowledge``
report those instead of the snapshot columns. The snapshot columns are
still surfaced under ``snapshot_*`` prefixes for diagnostics.
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
    Base, Coupon, Customer, Order, Product, StoreKnowledgeSnapshot, Tenant,
)
from services.store_sync import StoreSyncService  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Live Totals Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


def _seed_data(db, tenant_id):
    """Seed 3 products (across 2 categories), 7 orders, 5 coupons."""
    db.add_all([
        Product(
            tenant_id=tenant_id, external_id=f"p-{i}",
            title=f"منتج {i}", price=100.0 + i, in_stock=True,
            extra_metadata={"category": "فساتين" if i < 2 else "أحذية"},
        )
        for i in range(3)
    ])
    db.add_all([
        Order(
            tenant_id=tenant_id, external_id=f"o-{i}",
            status="completed", total="150.00",
        )
        for i in range(7)
    ])
    now = datetime.now(timezone.utc)
    db.add_all([
        Coupon(
            tenant_id=tenant_id, code=f"C{i}",
            description="x", discount_type="percent",
            discount_value="10",
            expires_at=(now + timedelta(days=10) if i < 3 else now - timedelta(days=1)),
        )
        for i in range(5)
    ])
    db.add_all([
        Customer(tenant_id=tenant_id, phone=f"+96650000{i:04d}", name=f"عميل {i}")
        for i in range(4)
    ])
    db.commit()


# ── _compute_live_totals: source-of-truth counts ─────────────────────────────


def test_live_totals_reflect_actual_db_rows():
    db, tenant_id = _make_db()
    _seed_data(db, tenant_id)

    svc = StoreSyncService(db, tenant_id)
    live = svc._compute_live_totals()

    assert live["product_count"]  == 3
    assert live["order_count"]    == 7
    assert live["customer_count"] == 4
    # 5 coupons total, 3 active (expires in future), 2 expired.
    assert live["coupon_total"]   == 5
    assert live["coupon_count"]   == 3
    # 2 distinct categories from product metadata.
    assert live["category_count"] == 2


def test_live_totals_honour_active_metadata_override():
    """A coupon flagged inactive via extra_metadata MUST be excluded from
    coupon_count even if its expires_at is in the future — same rule as
    GET /coupons."""
    db, tenant_id = _make_db()
    now = datetime.now(timezone.utc)
    db.add_all([
        Coupon(
            tenant_id=tenant_id, code="ACTIVE",
            description="-", discount_type="percent", discount_value="5",
            expires_at=now + timedelta(days=30),
        ),
        Coupon(
            tenant_id=tenant_id, code="MUTED",
            description="-", discount_type="percent", discount_value="5",
            expires_at=now + timedelta(days=30),
            extra_metadata={"active": False},
        ),
    ])
    db.commit()

    live = StoreSyncService(db, tenant_id)._compute_live_totals()
    assert live["coupon_total"] == 2
    assert live["coupon_count"] == 1   # MUTED is excluded


# ── get_status: regression for the Connection screen showing zeros ───────────


def test_get_status_reports_live_counts_when_snapshot_is_stale():
    """
    THE production bug, in test form:
      • DB has 7 orders + 5 coupons (carried over from earlier syncs)
      • The most recent sync returned 0 rows (Salla 401), so the snapshot
        was rebuilt with order_count=0 / coupon_count=0
      • get_status() MUST still report 7 orders / live coupon counts so
        the widget no longer lies to the merchant.
    """
    db, tenant_id = _make_db()
    _seed_data(db, tenant_id)

    # Simulate a stale-zero snapshot left over from a failed sync.
    snap = StoreKnowledgeSnapshot(
        tenant_id=tenant_id,
        product_count=0, order_count=0, coupon_count=0,
        category_count=0, customer_count=0,
        sync_version=1,
    )
    db.add(snap)
    db.commit()

    status = StoreSyncService(db, tenant_id).get_status()

    # Headline counts come from live tables — no longer the stale zeros.
    assert status["product_count"]  == 3
    assert status["order_count"]    == 7
    assert status["coupon_count"]   == 3
    assert status["coupon_total"]   == 5
    assert status["category_count"] == 2
    assert status["customer_count"] == 4

    # Snapshot deltas are still surfaced (under prefixed keys) for
    # diagnostics — they MUST NOT bleed into the headline counters.
    assert status["snapshot_product_count"]  == 0
    assert status["snapshot_order_count"]    == 0
    assert status["snapshot_coupon_count"]   == 0
    assert status["snapshot_category_count"] == 0


def test_get_status_works_when_no_snapshot_row_exists():
    """First-time merchant: live counts must still appear without a snapshot."""
    db, tenant_id = _make_db()
    _seed_data(db, tenant_id)

    status = StoreSyncService(db, tenant_id).get_status()
    assert status["has_snapshot"] is False
    assert status["product_count"] == 3
    assert status["order_count"]   == 7
    # Snapshot deltas all default to 0 when no snapshot row exists.
    assert status["snapshot_order_count"] == 0


def test_get_status_returns_zeros_for_empty_tenant():
    db, tenant_id = _make_db()
    status = StoreSyncService(db, tenant_id).get_status()
    assert status["product_count"] == 0
    assert status["order_count"]   == 0
    assert status["coupon_count"]  == 0
    assert status["category_count"] == 0
