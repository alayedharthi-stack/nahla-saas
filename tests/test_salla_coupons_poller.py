"""Regression tests for Salla coupon poller hardening and Riyadh push dates."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
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

from database.models import Base, Coupon, Integration, Tenant
from services.coupon_salla_push import normalize_salla_coupon_push_dates, salla_coupon_today
from services.salla_coupon_fetch import tenant_poll_due
from services.salla_coupons_poller import _poll_integration, _retry_after_active, get_poller_state
from services.store_sync import StoreSyncService


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
    fetch_result = {
        "ok": True,
        "items": merged_pages,
        "pages_fetched": 2,
        "items_seen": 2,
        "partial": False,
        "http_status": None,
        "failure_class": None,
        "retry_after": None,
    }
    svc = StoreSyncService(db, tenant_id)
    count = asyncio.run(svc.sync_coupons(triggered_by="test", raw_list=merged_pages, fetch_result=fetch_result))
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
    raw = [{"id": 42, "code": "NAH1111", "type": "percentage", "amount": 11, "name": "Generic Offer"}]
    fetch_result = {
        "ok": True,
        "items": raw,
        "pages_fetched": 1,
        "items_seen": 1,
        "partial": False,
        "http_status": None,
        "failure_class": None,
        "retry_after": None,
    }
    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id, adapter=MagicMock())
    count = asyncio.run(svc.sync_coupons(triggered_by="test_poller", raw_list=raw, fetch_result=fetch_result))
    db.commit()
    db.refresh(intg)
    assert count == 1
    meta = (intg.config or {}).get("coupon_sync_meta") or {}
    assert meta.get("items_seen") == 1
    assert meta.get("created") == 1
    assert meta.get("failure_class") is None
    assert meta.get("triggered_by") == "test_poller"
    assert meta.get("poll_interval_seconds") == 60
    assert meta.get("last_poll_at")


def test_sync_coupons_skips_upsert_on_hard_fetch_failure():
    db, tenant_id = _make_db()
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        enabled=True,
        external_store_id="9001",
        config={"api_key": "token", "store_id": "9001", "coupon_sync_meta": {"last_success_at": "2026-01-01T00:00:00+00:00"}},
    )
    db.add(intg)
    db.commit()
    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)
    fetch_result = {
        "ok": False,
        "items": [],
        "pages_fetched": 0,
        "items_seen": 0,
        "partial": False,
        "http_status": 500,
        "failure_class": "server_error",
        "retry_after": None,
    }
    count = asyncio.run(svc.sync_coupons(triggered_by="test", raw_list=[], fetch_result=fetch_result))
    db.commit()
    db.refresh(intg)
    assert count == 0
    assert db.query(Coupon).filter_by(tenant_id=tenant_id).count() == 0
    meta = (intg.config or {}).get("coupon_sync_meta") or {}
    assert meta.get("failure_class") == "server_error"
    assert meta.get("last_success_at") == "2026-01-01T00:00:00+00:00"


def test_sync_coupons_upserts_partial_pagination_items():
    db, tenant_id = _make_db()
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        enabled=True,
        external_store_id="9001",
        config={"api_key": "token", "store_id": "9001"},
    )
    db.add(intg)
    db.commit()
    partial_items = [{"id": 1, "code": "PART1", "type": "percentage", "amount": 5, "name": "Partial"}]
    fetch_result = {
        "ok": False,
        "items": partial_items,
        "pages_fetched": 1,
        "items_seen": 1,
        "partial": True,
        "http_status": 500,
        "failure_class": "partial_pagination",
        "retry_after": None,
    }
    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)
    count = asyncio.run(svc.sync_coupons(triggered_by="test", raw_list=partial_items, fetch_result=fetch_result))
    db.commit()
    assert count == 1
    meta = (intg.config or {}).get("coupon_sync_meta") or {}
    assert meta.get("failure_class") == "partial_pagination"
    assert meta.get("partial") is True


def test_tenant_poll_due_respects_adaptive_interval():
    now = datetime(2026, 1, 1, 0, 4, 0, tzinfo=timezone.utc)
    meta = {"last_poll_at": "2026-01-01T00:00:00+00:00", "poll_interval_seconds": 300}
    assert tenant_poll_due(meta, now=now) is False
    assert tenant_poll_due(meta, now=now + timedelta(minutes=6)) is True


def test_coupons_poller_uses_fetch_coupons_paginated_once(monkeypatch):
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
    fetch_result = {
        "ok": True,
        "items": [{"id": 99, "code": "GENPCT", "type": "percentage", "amount": 10, "name": "Generic Promo"}],
        "pages_fetched": 1,
        "items_seen": 1,
        "partial": False,
        "http_status": None,
        "failure_class": None,
        "retry_after": None,
    }
    fake_adapter = MagicMock()
    fake_adapter.fetch_coupons_paginated = AsyncMock(return_value=fetch_result)
    fake_adapter.get_coupons = AsyncMock(side_effect=AssertionError("get_coupons must not be called"))
    captured = {}

    async def _fake_sync_coupons(self, *, triggered_by="store_sync", raw_list=None, fetch_result=None, duration_ms=None):
        captured["triggered_by"] = triggered_by
        captured["raw_list"] = raw_list
        captured["fetch_result"] = fetch_result
        assert self._integration_connection_id == intg.id
        return 1

    monkeypatch.setattr("store_integration.registry.adapter_for_integration", lambda _intg: fake_adapter)
    monkeypatch.setattr(StoreSyncService, "sync_coupons", _fake_sync_coupons)
    stats = asyncio.run(_poll_integration(db, intg))
    fake_adapter.fetch_coupons_paginated.assert_awaited_once_with(per_page=60)
    assert captured["triggered_by"] == "salla_coupons_poller"
    assert stats["upserted"] == 1


def test_retry_after_active_blocks_until_window_expires():
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    meta = {"retry_after_until": "2026-01-01T00:10:00+00:00"}
    assert _retry_after_active(meta, now=now) is True
    assert _retry_after_active(meta, now=now + timedelta(minutes=11)) is False


def test_get_poller_state_exposes_adaptive_config():
    state = get_poller_state()
    assert "adaptive_sla" in state["config"]
    assert state["config"]["adaptive_sla"]["small_catalog_seconds"] == 60

