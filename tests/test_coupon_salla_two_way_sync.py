"""
tests/test_coupon_salla_two_way_sync.py
───────────────────────────────────────
Phase 2: Nahla ↔ Salla two-way coupon sync (push, import, dates, discounts).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
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
from services.coupon_salla_push import (  # noqa: E402
    FULL_API_INCOMPLETE_MSG_AR,
    evaluate_salla_coupon_sync_readiness,
    format_salla_datetime,
    parse_salla_datetime,
    push_coupon_to_salla,
)
from services.coupon_sync_visibility import derive_coupon_sync_visibility  # noqa: E402
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
    tenant = Tenant(name="Two Way Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


def _readiness_ready(adapter):
    return {
        "full_api_ready": True,
        "adapter_ready": True,
        "reason": "",
        "adapter": adapter,
    }


def _readiness_incomplete():
    return {
        "full_api_ready": False,
        "adapter_ready": False,
        "reason": FULL_API_INCOMPLETE_MSG_AR,
        "adapter": None,
    }


class _FakePushAdapter:
    def __init__(self, *, result=None, error: str | None = None):
        self._result = result or {"id": 88001, "code": "PUSHME"}
        self._error = error
        self.last_kwargs = None

    async def create_coupon(self, **kwargs):
        self.last_kwargs = kwargs
        if self._error:
            self._last_coupon_create_error = self._error
            return None
        return self._result

    def get_last_coupon_create_error(self):
        return getattr(self, "_last_coupon_create_error", None)

    async def get_coupons(self):
        return []


class _FakeImportAdapter:
    def __init__(self, coupons):
        self._coupons = coupons

    async def get_coupons(self):
        return self._coupons

    async def create_coupon(self, **kwargs):
        return None


SALLA_PERCENT = {
    "id": 99100,
    "code": "PCT10",
    "type": "percentage",
    "amount": 10,
    "start_date": "2026-07-01",
    "expiry_date": "2026-12-31",
    "usage_limit": 50,
    "minimum_amount": 100,
}

SALLA_FIXED = {
    "id": 99101,
    "code": "FIX25",
    "type": "fixed",
    "amount": 25,
    "start_date": "2026-07-01 10:00:00",
    "expire_date": "2026-12-31 23:59:59",
}


def test_parse_salla_datetime_date_and_datetime():
    assert parse_salla_datetime("2026-07-01") is not None
    assert parse_salla_datetime("2026-07-01 10:00:00") is not None


def test_format_salla_datetime_preserves_time():
    dt = datetime(2026, 7, 1, 10, 30, 0, tzinfo=timezone.utc)
    assert format_salla_datetime(dt) == "2026-07-01 10:30:00"


def test_create_coupon_local_when_no_adapter(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import CouponCreateIn, create_coupon  # noqa: E402

    db, tenant_id = _make_db()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)
    monkeypatch.setattr(
        coupons_router,
        "evaluate_salla_coupon_sync_readiness",
        lambda _db, _tid: _readiness_incomplete(),
    )

    result = asyncio.run(create_coupon(
        CouponCreateIn(code="LOCAL01", type="percentage", value="10"),
        request=None,
        db=db,
    ))
    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="LOCAL01").one()
    assert row.source_type == "manual"
    assert result["sync_badge"] == "not_pushed"
    assert row.extra_metadata["sync_status"] == "not_pushed"
    assert FULL_API_INCOMPLETE_MSG_AR in row.extra_metadata["sync_error"]


def test_create_coupon_calls_salla_when_adapter_ready(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import CouponCreateIn, create_coupon  # noqa: E402

    db, tenant_id = _make_db()
    adapter = _FakePushAdapter(result={"id": 555, "code": "PUSH01"})
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)
    monkeypatch.setattr(
        coupons_router,
        "evaluate_salla_coupon_sync_readiness",
        lambda _db, _tid: _readiness_ready(adapter),
    )

    result = asyncio.run(create_coupon(
        CouponCreateIn(
            code="PUSH01",
            type="percentage",
            value="15",
            expires="2026-12-31T23:59:59+00:00",
            limit=5,
        ),
        request=None,
        db=db,
    ))
    assert adapter.last_kwargs is not None
    assert adapter.last_kwargs["code"] == "PUSH01"
    assert adapter.last_kwargs["discount_type"] == "percentage"
    assert adapter.last_kwargs["usage_limit"] == 5

    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="PUSH01").one()
    assert row.source_type == "manual"
    assert row.extra_metadata["salla_coupon_id"] == "555"
    assert row.extra_metadata["sync_status"] == "synced"
    assert result["sync_badge"] == "synced"


def test_create_coupon_failed_push_keeps_local(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import CouponCreateIn, create_coupon  # noqa: E402

    db, tenant_id = _make_db()
    adapter = _FakePushAdapter(error="Salla 422 invalid")
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)
    monkeypatch.setattr(
        coupons_router,
        "evaluate_salla_coupon_sync_readiness",
        lambda _db, _tid: _readiness_ready(adapter),
    )

    result = asyncio.run(create_coupon(
        CouponCreateIn(code="FAIL01", type="fixed", value="20"),
        request=None,
        db=db,
    ))
    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="FAIL01").one()
    assert row.source_type == "manual"
    assert row.extra_metadata["sync_status"] == "failed"
    assert "Salla 422" in row.extra_metadata["sync_error"]
    assert result["sync_badge"] == "failed"


def test_push_salla_endpoint_pushes_manual_coupon(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import push_coupon_salla  # noqa: E402

    db, tenant_id = _make_db()
    row = Coupon(
        tenant_id=tenant_id,
        code="RETRY01",
        description="manual",
        discount_type="percentage",
        discount_value="12",
        source_type="manual",
        extra_metadata={"source": "dashboard"},
    )
    db.add(row)
    db.commit()

    adapter = _FakePushAdapter(result={"id": 999, "code": "RETRY01"})
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)
    monkeypatch.setattr(
        coupons_router,
        "evaluate_salla_coupon_sync_readiness",
        lambda _db, _tid: _readiness_ready(adapter),
    )

    result = asyncio.run(push_coupon_salla(row.id, request=None, db=db))
    db.refresh(row)
    assert result["sync_badge"] == "synced"
    assert row.source_type == "manual"
    assert row.extra_metadata["salla_coupon_id"] == "999"


def test_push_salla_failed_retry_updates_sync_error(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import push_coupon_salla  # noqa: E402
    from fastapi import HTTPException  # noqa: E402

    db, tenant_id = _make_db()
    row = Coupon(
        tenant_id=tenant_id,
        code="RETRY02",
        description="manual",
        discount_type="percentage",
        discount_value="12",
        source_type="manual",
        extra_metadata={"source": "dashboard"},
    )
    db.add(row)
    db.commit()

    adapter = _FakePushAdapter(error="token expired")
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)
    monkeypatch.setattr(
        coupons_router,
        "evaluate_salla_coupon_sync_readiness",
        lambda _db, _tid: _readiness_ready(adapter),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(push_coupon_salla(row.id, request=None, db=db))
    assert exc.value.status_code == 502
    db.refresh(row)
    assert row.extra_metadata["sync_status"] == "failed"
    assert "token expired" in row.extra_metadata["sync_error"]


def test_sync_salla_imports_percentage_coupon():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeImportAdapter([SALLA_PERCENT])
    count = asyncio.run(svc.sync_coupons())
    assert count == 1
    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="PCT10").one()
    assert row.source_type == "imported"
    assert row.discount_type == "percentage"
    assert row.discount_value == "10"
    assert row.extra_metadata["usage_limit"] == 50
    assert row.extra_metadata["min_order_amount"] == 100
    assert row.extra_metadata["starts_at"] is not None


def test_sync_salla_imports_fixed_coupon_and_expiry():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeImportAdapter([SALLA_FIXED])
    asyncio.run(svc.sync_coupons())
    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="FIX25").one()
    assert row.discount_type == "fixed"
    assert row.discount_value == "25"
    assert row.expires_at is not None


def test_sync_salla_reimport_no_duplicate():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeImportAdapter([SALLA_PERCENT])
    asyncio.run(svc.sync_coupons())
    asyncio.run(svc.sync_coupons())
    assert db.query(Coupon).filter_by(tenant_id=tenant_id, code="PCT10").count() == 1


def test_sync_salla_preserves_manual_origin_on_code_match():
    db, tenant_id = _make_db()
    db.add(Coupon(
        tenant_id=tenant_id,
        code="PCT10",
        description="manual",
        discount_type="percentage",
        discount_value="5",
        source_type="manual",
        extra_metadata={"source": "dashboard"},
    ))
    db.commit()

    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeImportAdapter([SALLA_PERCENT])
    asyncio.run(svc.sync_coupons())

    row = db.query(Coupon).filter_by(tenant_id=tenant_id, code="PCT10").one()
    assert row.source_type == "manual"
    assert row.extra_metadata["source"] == "dashboard"
    assert row.discount_value == "10"
    assert row.extra_metadata["salla_coupon_id"] == "99100"


def test_tenant_isolation_push():
    db, tenant_a = _make_db()
    tenant_b = Tenant(name="Tenant B", is_active=True)
    db.add(tenant_b)
    db.commit()

    coupon_a = Coupon(
        tenant_id=tenant_a,
        code="ISO01",
        description="a",
        discount_type="percentage",
        discount_value="10",
        source_type="manual",
        extra_metadata={"source": "dashboard"},
    )
    db.add(coupon_a)
    db.commit()

    adapter = _FakePushAdapter()
    ok, _ = asyncio.run(push_coupon_to_salla(db, tenant_b.id, coupon_a, adapter=adapter))
    assert ok is False


def test_sync_salla_endpoint_requires_full_api(monkeypatch):
    from backend.routers import coupons as coupons_router  # noqa: E402
    from backend.routers.coupons import sync_salla_coupons  # noqa: E402
    from fastapi import HTTPException  # noqa: E402

    db, tenant_id = _make_db()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr(coupons_router, "get_or_create_tenant", lambda _db, _tid: None)
    monkeypatch.setattr(
        coupons_router,
        "evaluate_salla_coupon_sync_readiness",
        lambda _db, _tid: _readiness_incomplete(),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(sync_salla_coupons(request=None, db=db))
    assert exc.value.status_code == 400
    assert FULL_API_INCOMPLETE_MSG_AR in str(exc.value.detail)


def test_evaluate_readiness_without_integration(monkeypatch):
    db, tenant_id = _make_db()
    monkeypatch.setattr(
        "services.coupon_salla_push.pick_active_salla_integration",
        lambda _db, _tid: None,
        raising=False,
    )
    monkeypatch.setattr(
        "store_integration.registry.pick_active_salla_integration",
        lambda _db, _tid: None,
    )
    result = evaluate_salla_coupon_sync_readiness(db, tenant_id)
    assert result["full_api_ready"] is False
