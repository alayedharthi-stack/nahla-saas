"""
tests/test_coupon_source_filters.py
───────────────────────────────────
Follow-up to Phase 1: manual/imported source taxonomy, filter counts,
and coupon-only Salla sync endpoint.
"""
from __future__ import annotations

import asyncio
import sys
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

from database.models import Base, Coupon, Tenant  # noqa: E402
from services.coupon_sync_visibility import (  # noqa: E402
    compute_source_type_counts,
    derive_coupon_sync_visibility,
    resolve_coupon_source_type,
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
    tenant = Tenant(name="Source Filter Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


class _FakeCouponAdapter:
    def __init__(self, coupons):
        self._coupons = coupons

    async def get_coupons(self):
        return self._coupons


SALLA_COUPON = {
    "id": 44221,
    "code": "SALLA10",
    "type": "percentage",
    "amount": 10,
    "description": "خصم سلة",
    "status": "active",
}


def test_resolve_manual_when_metadata_source_dashboard_overrides_system_column():
    resolved = resolve_coupon_source_type(
        column_source_type="system",
        meta={"source": "dashboard"},
        origin="automation",
    )
    assert resolved == "manual"


def test_manual_coupon_sync_badge_is_not_pushed():
    fields = derive_coupon_sync_visibility(
        source_type="manual",
        meta={"source": "dashboard"},
    )
    assert fields["source_label"] == "يدوي"
    assert fields["sync_badge"] == "not_pushed"
    assert fields["sync_badge_label"] == "لم يُرسل إلى سلة"


def test_source_filter_counts_match_normalized_source_types():
    coupons = [
        {"source_type": "system"},
        {"source_type": "system"},
        {"source_type": "manual"},
        {"source_type": "imported"},
    ]
    counts = compute_source_type_counts(coupons)
    assert counts == {"all": 4, "system": 2, "manual": 1, "imported": 1}


def test_create_coupon_sets_manual_source_type(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import CouponCreateIn, create_coupon  # noqa: E402

    db, tenant_id = _make_db()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)

    result = asyncio.run(create_coupon(
        CouponCreateIn(code="MANUAL99", type="percentage", value="15"),
        request=None,
        db=db,
    ))
    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="MANUAL99").one()

    assert row.source_type == "manual"
    assert row.extra_metadata["source"] == "dashboard"
    assert result["source_type"] == "manual"
    assert result["sync_badge"] == "not_pushed"


def test_list_coupons_includes_manual_in_source_counts(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import list_coupons  # noqa: E402
    from database.models import TenantSettings  # noqa: E402

    db, tenant_id = _make_db()
    db.add(TenantSettings(tenant_id=tenant_id, ai_settings={}))
    db.add_all([
        Coupon(
            tenant_id=tenant_id,
            code="POOL01",
            description="pool",
            discount_type="percentage",
            discount_value="5",
            source_type="system",
            extra_metadata={"source": "pool", "salla_synced": True},
        ),
        Coupon(
            tenant_id=tenant_id,
            code="MANUAL01",
            description="manual",
            discount_type="percentage",
            discount_value="10",
            source_type="manual",
            extra_metadata={"source": "dashboard"},
        ),
    ])
    db.commit()

    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)

    payload = asyncio.run(list_coupons(request=None, db=db))
    assert payload["source_counts"]["manual"] == 1
    assert payload["source_counts"]["system"] == 1
    manual_rows = [c for c in payload["coupons"] if c["source_type"] == "manual"]
    assert len(manual_rows) == 1
    assert manual_rows[0]["code"] == "MANUAL01"
    assert manual_rows[0]["sync_badge"] == "not_pushed"


def test_sync_salla_endpoint_imports_coupon(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import sync_salla_coupons  # noqa: E402
    import services.store_sync as store_sync_mod  # noqa: E402

    db, tenant_id = _make_db()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)

    inner = StoreSyncService(db, tenant_id)
    inner._adapter = _FakeCouponAdapter([SALLA_COUPON])

    class _FakeSvc:
        def __init__(self, db, tid):
            self._inner = inner

        def _get_adapter(self):
            return self._inner._adapter

        async def sync_coupons(self):
            return await self._inner.sync_coupons()

    monkeypatch.setattr(store_sync_mod, "StoreSyncService", _FakeSvc)

    result = asyncio.run(sync_salla_coupons(request=None, db=db))
    assert result["synced"] == 1

    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="SALLA10").one()
    assert row.source_type == "imported"
    assert row.extra_metadata["source"] == "salla"
    assert row.extra_metadata["salla_synced"] is True


def test_sync_salla_reimport_does_not_duplicate():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([SALLA_COUPON])
    asyncio.run(svc.sync_coupons())
    asyncio.run(svc.sync_coupons())
    assert db.query(Coupon).filter_by(tenant_id=tenant_id, code="SALLA10").count() == 1


def test_existing_nahla_system_coupon_stays_system_on_salla_match():
    db, tenant_id = _make_db()
    db.add(Coupon(
        tenant_id=tenant_id,
        code="NHPOOL02",
        description="pool",
        discount_type="percentage",
        discount_value="5",
        source_type="system",
        extra_metadata={"source": "pool", "salla_synced": True},
    ))
    db.commit()

    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([{**SALLA_COUPON, "code": "NHPOOL02", "id": 991}])
    asyncio.run(svc.sync_coupons())

    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="NHPOOL02").one()
    assert row.source_type == "system"
    assert row.extra_metadata["source"] == "pool"


def test_existing_manual_coupon_stays_manual_on_salla_match():
    db, tenant_id = _make_db()
    db.add(Coupon(
        tenant_id=tenant_id,
        code="MANUALSAME",
        description="manual",
        discount_type="percentage",
        discount_value="10",
        source_type="manual",
        extra_metadata={"source": "dashboard"},
    ))
    db.commit()

    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeCouponAdapter([{**SALLA_COUPON, "code": "MANUALSAME", "id": 881}])
    asyncio.run(svc.sync_coupons())

    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="MANUALSAME").one()
    assert row.source_type == "manual"
    assert row.extra_metadata["source"] == "dashboard"
    assert row.extra_metadata["salla_synced"] is True
    assert row.extra_metadata["salla_coupon_id"] == "881"


def test_sync_salla_endpoint_does_not_call_full_store_sync(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import sync_salla_coupons  # noqa: E402
    import services.store_sync as store_sync_mod  # noqa: E402

    db, tenant_id = _make_db()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)

    inner = StoreSyncService(db, tenant_id)
    inner._adapter = _FakeCouponAdapter([SALLA_COUPON])
    full_sync_called = {"value": False}

    class _FakeSvc:
        def __init__(self, db, tid):
            self._inner = inner

        def _get_adapter(self):
            return self._inner._adapter

        async def sync_coupons(self):
            return await self._inner.sync_coupons()

        async def full_sync(self, *args, **kwargs):
            full_sync_called["value"] = True
            return None

    monkeypatch.setattr(store_sync_mod, "StoreSyncService", _FakeSvc)
    asyncio.run(sync_salla_coupons(request=None, db=db))
    assert full_sync_called["value"] is False


def test_cross_tenant_isolation_for_manual_coupon():
    db, tenant_a = _make_db()
    tenant_b = Tenant(name="Other", is_active=True)
    db.add(tenant_b)
    db.commit()

    db.add(Coupon(
        tenant_id=tenant_a,
        code="SHARED",
        description="manual",
        discount_type="percentage",
        discount_value="5",
        source_type="manual",
        extra_metadata={"source": "dashboard"},
    ))
    db.commit()

    svc_b = StoreSyncService(db, tenant_b.id)
    svc_b._adapter = _FakeCouponAdapter([{**SALLA_COUPON, "code": "SHARED"}])
    asyncio.run(svc_b.sync_coupons())

    row_a = db.query(Coupon).filter_by(tenant_id=tenant_a, code="SHARED").one()
    row_b = db.query(Coupon).filter_by(tenant_id=tenant_b.id, code="SHARED").one()
    assert row_a.source_type == "manual"
    assert row_b.source_type == "imported"
    assert row_a.id != row_b.id
