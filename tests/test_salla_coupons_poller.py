"""Regression tests for Salla coupon poller and Riyadh date push normalization."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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

from database.models import Base, Coupon, Integration, Tenant  # noqa: E402
from services.coupon_salla_push import normalize_salla_coupon_push_dates, salla_coupon_today  # noqa: E402
from services.salla_coupons_poller import _poll_integration  # noqa: E402
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
    tenant = Tenant(name="Poller Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


def test_salla_coupon_today_uses_riyadh_not_utc():
    # 2026-08-25 22:01 UTC == 2026-08-26 01:01 Asia/Riyadh
    now = datetime(2026, 8, 25, 22, 1, 0, tzinfo=timezone.utc)
    assert salla_coupon_today(now).isoformat() == "2026-08-26"


def test_normalize_push_dates_clamp_start_to_riyadh_today():
    now = datetime(2026, 8, 25, 22, 1, 0, tzinfo=timezone.utc)
    start, expiry = normalize_salla_coupon_push_dates(
        datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc),
        now=now,
    )
    assert start == "2026-08-26"
    assert expiry == "2026-09-02"


def test_sync_coupons_pagination_imports_coupon_outside_first_page():
    db, tenant_id = _make_db()
    merged_pages = [
        {"id": 1, "code": "PAGE1", "type": "percentage", "amount": 5, "name": "Page One"},
        {"id": 200, "code": "PAGE2", "type": "percentage", "amount": 7, "name": "Page Two"},
    ]
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = AsyncMock(get_coupons=AsyncMock(return_value=merged_pages))
    count = asyncio.run(svc.sync_coupons(triggered_by="test"))
    assert count == 2
    assert db.query(Coupon).filter_by(tenant_id=tenant_id, code="PAGE2").count() == 1


def test_sync_coupons_records_metadata_on_integration():
    db, tenant_id = _make_db()
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        enabled=True,
        external_store_id="9001",
        config={"api_key": "token", "store_id": "9001", "api_sync_enabled": True},
    )
    db.add(intg)
    db.commit()

    svc = StoreSyncService(
        db,
        tenant_id,
        integration_connection_id=intg.id,
        adapter=AsyncMock(get_coupons=AsyncMock(return_value=[{
            "id": 42,
            "code": "NAH1111",
            "type": "percentage",
            "amount": 11,
            "name": "Generic Offer",
        }])),
    )
    count = asyncio.run(svc.sync_coupons(triggered_by="test_poller"))
    db.commit()
    db.refresh(intg)
    assert count == 1
    meta = (intg.config or {}).get("coupon_sync_meta") or {}
    assert meta.get("items_seen") == 1
    assert meta.get("created") == 1
    assert meta.get("failure_class") is None
    assert meta.get("triggered_by") == "test_poller"


def test_coupons_poller_uses_canonical_integration(monkeypatch):
    db, tenant_id = _make_db()
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        enabled=True,
        external_store_id="22825873",
        config={"api_key": "token", "store_id": "22825873", "api_sync_enabled": True},
    )
    db.add(intg)
    db.commit()

    fake_adapter = MagicMock()
    fake_adapter.get_coupons = AsyncMock(return_value=[{
        "id": 99,
        "code": "GENPCT",
        "type": "percentage",
        "amount": 10,
        "name": "Generic Promo",
    }])

    async def _fake_sync_coupons(self, *, triggered_by="store_sync"):
        assert triggered_by == "salla_coupons_poller"
        assert self._integration_connection_id == intg.id
        return 1

    monkeypatch.setattr(
        "store_integration.registry.adapter_for_integration",
        lambda _intg: fake_adapter,
    )
    monkeypatch.setattr(StoreSyncService, "sync_coupons", _fake_sync_coupons)

    stats = asyncio.run(_poll_integration(db, intg))
    assert stats["upserted"] == 1
