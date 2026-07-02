"""
tests/test_coupon_sync_visibility.py
────────────────────────────────────
Phase 1 regression tests for Salla coupon import taxonomy, list API sync
visibility fields, pool usage display normalization, and tenant isolation.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, create_engine, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Coupon, Tenant  # noqa: E402
from services.coupon_sync_visibility import (  # noqa: E402
    derive_coupon_sync_visibility,
    normalize_coupon_usage_display,
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
    tenant = Tenant(name="Coupon Sync Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


class _FakeCouponAdapter:
    def __init__(self, coupons):
        self._coupons = coupons

    async def get_coupons(self):
        return self._coupons


SALLA_COUPON = {
    "id": 99123,
    "code": "SAVE10",
    "type": "percentage",
    "amount": 10,
    "description": "خصم عام",
    "status": "active",
    "expire_date": "2026-12-31T23:59:59Z",
}


def test_salla_import_sets_source_type_imported():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([SALLA_COUPON])

    count = asyncio.run(svc.sync_coupons())
    assert count == 1

    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="SAVE10").one()
    assert row.source_type == "imported"


def test_salla_import_sets_metadata_source_salla():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([SALLA_COUPON])

    asyncio.run(svc.sync_coupons())
    meta = db.query(Coupon).filter_by(tenant_id=tenant_id, code="SAVE10").one().extra_metadata
    assert meta["source"] == "salla"


def test_salla_import_sets_salla_synced_true():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([SALLA_COUPON])

    asyncio.run(svc.sync_coupons())
    meta = db.query(Coupon).filter_by(tenant_id=tenant_id, code="SAVE10").one().extra_metadata
    assert meta["salla_synced"] is True
    assert meta["sync_status"] == "synced"
    assert meta["sync_direction"] == "salla_to_nahla"
    assert meta["salla_coupon_id"] == "99123"
    assert meta["external_id"] == "99123"
    assert meta["last_synced_at"]


def test_salla_reimport_does_not_duplicate_tenant_code():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([SALLA_COUPON])

    asyncio.run(svc.sync_coupons())
    asyncio.run(svc.sync_coupons())

    rows = db.query(Coupon).filter_by(tenant_id=tenant_id, code="SAVE10").all()
    assert len(rows) == 1


def test_salla_reimport_preserves_nahla_system_pool_source_type():
    db, tenant_id = _make_db()
    db.add(Coupon(
        tenant_id=tenant_id,
        code="NHPOOL01",
        description="pool",
        discount_type="percentage",
        discount_value="5",
        source_type="system",
        extra_metadata={"source": "pool", "salla_synced": True},
    ))
    db.commit()

    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([{
        **SALLA_COUPON,
        "id": 555,
        "code": "NHPOOL01",
    }])
    asyncio.run(svc.sync_coupons())

    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="NHPOOL01").one()
    assert row.source_type == "system"
    assert row.extra_metadata["source"] == "pool"
    assert row.extra_metadata["salla_synced"] is True
    assert row.extra_metadata["sync_direction"] == "nahla_to_salla"
    assert row.extra_metadata["salla_coupon_id"] == "555"
    sync = derive_coupon_sync_visibility(source_type=row.source_type, meta=row.extra_metadata)
    assert sync["sync_badge"] == "synced"
    assert sync["source_label"] == "نظام"


def test_list_api_exposes_sync_fields():
    meta = {
        "source": "salla",
        "salla_synced": True,
        "sync_status": "synced",
        "sync_direction": "salla_to_nahla",
        "last_synced_at": "2026-07-02T10:00:00+00:00",
        "salla_coupon_id": "99123",
    }
    fields = derive_coupon_sync_visibility(source_type="imported", meta=meta)
    assert fields["source_label"] == "مستورد من سلة"
    assert fields["salla_synced"] is True
    assert fields["sync_status"] == "synced"
    assert fields["sync_direction"] == "salla_to_nahla"
    assert fields["salla_coupon_id"] == "99123"
    assert fields["sync_badge"] == "imported"
    assert fields["sync_badge_label"] == "مستورد من سلة"


def test_manual_coupon_without_salla_metadata_is_not_pushed():
    fields = derive_coupon_sync_visibility(
        source_type="manual",
        meta={"source": "dashboard"},
    )
    assert fields["salla_synced"] is False
    assert fields["sync_badge"] == "not_pushed"
    assert fields["sync_badge_label"] == "لم يُرسل إلى سلة"
    assert fields["source_label"] == "يدوي"


def test_failed_sync_badge_exposes_error():
    fields = derive_coupon_sync_visibility(
        source_type="manual",
        meta={"sync_status": "failed", "sync_error": "Salla 422"},
    )
    assert fields["sync_badge"] == "failed"
    assert fields["sync_error"] == "Salla 422"


def test_pool_coupon_used_true_displays_one_of_one():
    usages, limit = normalize_coupon_usage_display({"used": True})
    assert usages == 1
    assert limit == 1


def test_pool_coupon_used_false_displays_zero_of_one():
    usages, limit = normalize_coupon_usage_display({"used": False})
    assert usages == 0
    assert limit == 1


def test_pool_coupon_with_numeric_usage_count_keeps_usage_count():
    usages, limit = normalize_coupon_usage_display({
        "used": True,
        "usage_count": 3,
        "usage_limit": 10,
    })
    assert usages == 3
    assert limit == 10


def test_cross_tenant_isolation_on_import():
    db, tenant_a = _make_db()
    tenant_b = Tenant(name="Other Tenant", is_active=True)
    db.add(tenant_b)
    db.commit()

    svc_a = StoreSyncService(db, tenant_a)
    svc_a._adapter = _FakeCouponAdapter([SALLA_COUPON])
    asyncio.run(svc_a.sync_coupons())

    svc_b = StoreSyncService(db, tenant_b.id)
    svc_b._adapter = _FakeCouponAdapter([{
        **SALLA_COUPON,
        "id": 777,
        "code": "SAVE10",
    }])
    asyncio.run(svc_b.sync_coupons())

    assert db.query(func.count(Coupon.id)).filter_by(tenant_id=tenant_a).scalar() == 1
    assert db.query(func.count(Coupon.id)).filter_by(tenant_id=tenant_b.id).scalar() == 1
    rows_a = db.query(Coupon).filter_by(tenant_id=tenant_a, code="SAVE10").all()
    rows_b = db.query(Coupon).filter_by(tenant_id=tenant_b.id, code="SAVE10").all()
    assert len(rows_a) == 1
    assert len(rows_b) == 1
    assert rows_a[0].id != rows_b[0].id


def test_manual_dashboard_row_serializes_not_pushed_badge():
    """Manual dashboard coupon without Salla metadata → not_pushed."""
    db, tenant_id = _make_db()
    db.add(Coupon(
        tenant_id=tenant_id,
        code="MANUAL1",
        description="يدوي",
        discount_type="percentage",
        discount_value="15",
        source_type="manual",
        extra_metadata={"source": "dashboard"},
    ))
    db.commit()

    row = db.query(Coupon).filter_by(tenant_id=tenant_id).one()
    sync = derive_coupon_sync_visibility(source_type=row.source_type, meta=row.extra_metadata)
    usages, limit = normalize_coupon_usage_display(row.extra_metadata)
    assert sync["sync_badge"] == "not_pushed"
    assert usages == 0
    assert limit == 0
