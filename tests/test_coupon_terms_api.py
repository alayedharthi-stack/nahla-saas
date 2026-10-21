"""
tests/test_coupon_terms_api.py
──────────────────────────────
Integration tests for PATCH /coupons/{id} terms handling and related services.

Uses an in-memory SQLite fixture (same pattern as test_coupon_salla_two_way_sync).
Monkeypatches only adapter/network; real Integration binding for bound-coupon paths.
No tests are executed by this file — the test runner (coordinator) executes them.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from database.models import Base, Coupon, Integration, Tenant  # noqa: E402
from services.coupon_terms_sync import (  # noqa: E402
    CouponTermsError,
    dispatch_terms_change,
    provider_expiry_utc,
    queue_terms_change,
    reconcile_imported_terms,
    retry_terms_changes,
    validate_terms,
)
from services.coupon_active_sync import queue_active_change  # noqa: E402
from services.coupon_identity import CouponIdentityConflict  # noqa: E402
from services.coupon_sync_visibility import derive_coupon_sync_visibility  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


# ── Fixtures ──────────────────────────────────────────────────────────────────

_STORE_ID = "55001"


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    tenant = Tenant(name="Terms Tenant", is_active=True)
    db.add(tenant)
    db.commit()
    return db, tenant.id


def _make_integration(db, tenant_id):
    intg = Integration(
        provider="salla",
        external_store_id=_STORE_ID,
        tenant_id=tenant_id,
        enabled=True,
        config={
            "store_id": _STORE_ID,
            "api_sync_enabled": True,
            "refresh_token": "tok",
            "api_key": "key",
        },
    )
    db.add(intg)
    db.commit()
    return intg


def _make_unbound(db, tenant_id, *, value="15", discount_type="percentage"):
    c = Coupon(
        tenant_id=tenant_id,
        code="UNBOUND01",
        description="unbound",
        discount_type=discount_type,
        discount_value=value,
        source_type="manual",
        extra_metadata={"source": "dashboard", "active": True},
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_bound(db, tenant_id, intg_id, *, value="20", discount_type="percentage"):
    c = Coupon(
        tenant_id=tenant_id,
        code="BOUND01",
        description="bound",
        discount_type=discount_type,
        discount_value=value,
        source_type="imported",
        extra_metadata={
            "source": "salla",
            "salla_coupon_id": "88001",
            "external_id": "88001",
            "salla_identity": {
                "tenant_id": tenant_id,
                "integration_id": intg_id,
                "store_id": _STORE_ID,
            },
            "sync_status": "synced",
            "salla_synced": True,
            "active": True,
        },
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


class _FakeTermsAdapter:
    """Minimal adapter for dispatch_terms_change tests."""

    def __init__(self, db_coupon, tenant_id, intg_id):
        self._tenant_id = tenant_id
        self._integration_id = intg_id
        self.store_id = _STORE_ID
        self._coupon = db_coupon
        self.update_calls = []

    async def get_coupon_by_id(self, cid):
        return {
            "id": str(cid),
            "code": self._coupon.code,
            "type": self._coupon.discount_type,
            "amount": self._coupon.discount_value,
            "expiry_date": None,
        }

    async def update_coupon_terms(self, cid, **kwargs):
        self.update_calls.append((cid, kwargs))
        # Reflect the update so readback confirms it
        if "amount" in kwargs:
            self._coupon.discount_value = str(kwargs["amount"])


# ── validate_terms tests ───────────────────────────────────────────────────────

def test_validate_terms_valid_value():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    result = validate_terms(c, {"value": "25"})
    assert result["amount"] == "25"


def test_validate_terms_valid_expires():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    result = validate_terms(c, {"expires": "2027-06-30"})
    assert result["expiry_date"] == "2027-06-30"


def test_validate_terms_both_fields():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    result = validate_terms(c, {"value": "10", "expires": "2027-12-31"})
    assert result["amount"] == "10"
    assert result["expiry_date"] == "2027-12-31"


def test_validate_terms_invalid_value_raises():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    with pytest.raises(CouponTermsError):
        validate_terms(c, {"value": "abc"})


def test_validate_terms_zero_value_raises():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    with pytest.raises(CouponTermsError):
        validate_terms(c, {"value": "0"})


def test_validate_terms_percentage_over_100_raises():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    with pytest.raises(CouponTermsError):
        validate_terms(c, {"value": "101"})


def test_validate_terms_invalid_date_raises():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    with pytest.raises(CouponTermsError):
        validate_terms(c, {"expires": "not-a-date"})


def test_validate_terms_empty_patch_raises():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    with pytest.raises(CouponTermsError):
        validate_terms(c, {})


def test_validate_terms_system_coupon_raises():
    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="SYS01", discount_type="percentage", discount_value="10",
        source_type="system", extra_metadata={"source": "pool"},
    )
    db.add(c)
    db.commit()
    with pytest.raises(CouponTermsError):
        validate_terms(c, {"value": "20"})


def test_validate_terms_retired_coupon_raises():
    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="RET01", discount_type="percentage", discount_value="10",
        source_type="imported", extra_metadata={"salla_retirement": {"version": 1}},
    )
    db.add(c)
    db.commit()
    with pytest.raises(CouponTermsError):
        validate_terms(c, {"value": "20"})


def test_validate_terms_clear_expiry_unbound_allowed():
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    result = validate_terms(c, {"expires": None})
    assert result.get("expiry_date") is None


def test_validate_terms_clear_expiry_bound_rejected():
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = _make_bound(db, tenant_id, intg.id)
    with pytest.raises(CouponTermsError, match="cannot_clear_expiry_on_bound_coupon"):
        validate_terms(c, {"expires": None})


# ── patch_coupon router tests ──────────────────────────────────────────────────

def test_patch_value_unbound_applies_directly(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon

    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    result = asyncio.run(patch_coupon(c.id, CouponPatchIn(value="30"), request=None, db=db))
    db.refresh(c)
    assert c.discount_value == "30"
    assert result["accepted"] is True
    assert result["updated"] is True


def test_patch_expires_unbound_applies_directly(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon

    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    result = asyncio.run(patch_coupon(
        c.id, CouponPatchIn(expires="2027-12-31T00:00:00+00:00"), request=None, db=db,
    ))
    db.refresh(c)
    assert c.expires_at is not None
    assert result["updated"] is True


def test_patch_clear_expires_unbound(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon

    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    c.expires_at = datetime(2026, 1, 1)
    db.commit()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    asyncio.run(patch_coupon(c.id, CouponPatchIn(expires=None), request=None, db=db))
    db.refresh(c)
    assert c.expires_at is None


def test_patch_invalid_value_returns_422(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException

    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    original_value = c.discount_value
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_coupon(c.id, CouponPatchIn(value="bad"), request=None, db=db))
    assert exc.value.status_code == 422
    db.refresh(c)
    assert c.discount_value == original_value  # no mutation


def test_patch_invalid_expiry_returns_422(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException

    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_coupon(c.id, CouponPatchIn(expires="not-a-date"), request=None, db=db))
    assert exc.value.status_code == 422


def test_patch_system_coupon_422(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException

    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="SYS02", discount_type="percentage", discount_value="10",
        source_type="system", extra_metadata={"source": "pool"},
    )
    db.add(c)
    db.commit()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_coupon(c.id, CouponPatchIn(value="20"), request=None, db=db))
    assert exc.value.status_code == 422


def test_patch_wrong_tenant_404(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException

    db, tenant_id = _make_db()
    other_tenant = Tenant(name="Other", is_active=True)
    db.add(other_tenant)
    db.commit()
    c = _make_unbound(db, tenant_id)
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: other_tenant.id)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_coupon(c.id, CouponPatchIn(value="20"), request=None, db=db))
    assert exc.value.status_code == 404


def test_patch_bound_value_queues_terms_pending(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon

    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = _make_bound(db, tenant_id, intg.id)

    dispatched = []

    async def _fake_dispatch(db_, tid, cid, intg_, adapter_):
        dispatched.append(cid)

    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr("services.coupon_terms_sync.dispatch_terms_change", _fake_dispatch)
    monkeypatch.setattr(
        "store_integration.registry.adapter_for_integration",
        lambda _i: MagicMock(),
    )

    result = asyncio.run(patch_coupon(c.id, CouponPatchIn(value="25"), request=None, db=db))
    assert result["accepted"] is True
    assert c.id in dispatched


def test_patch_bound_returns_not_updated_when_terms_pending(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon

    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = _make_bound(db, tenant_id, intg.id)

    # Dispatch does nothing → terms_sync_pending stays
    async def _noop_dispatch(*a, **kw):
        pass

    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    monkeypatch.setattr("services.coupon_terms_sync.dispatch_terms_change", _noop_dispatch)
    monkeypatch.setattr(
        "store_integration.registry.adapter_for_integration",
        lambda _i: MagicMock(),
    )

    result = asyncio.run(patch_coupon(c.id, CouponPatchIn(value="25"), request=None, db=db))
    db.refresh(c)
    # terms_sync_pending still set because dispatch was no-op
    assert (c.extra_metadata or {}).get("terms_sync_pending") is not None
    assert result["updated"] is False


def test_patch_bound_with_code_409(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException

    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = _make_bound(db, tenant_id, intg.id)
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_coupon(c.id, CouponPatchIn(value="25", code="NEWCODE"), request=None, db=db))
    assert exc.value.status_code == 409


def test_patch_bound_with_active_409(monkeypatch):
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException

    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = _make_bound(db, tenant_id, intg.id)
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_coupon(c.id, CouponPatchIn(value="25", active=False), request=None, db=db))
    assert exc.value.status_code == 409


def test_patch_no_source_change(monkeypatch):
    """Patching value must not alter source_type, ai_allocatable, coupon_level."""
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon

    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="NOSRC01", discount_type="percentage", discount_value="10",
        source_type="manual",
        coupon_level="silver",
        allocation_channel="ai",
        extra_metadata={"source": "dashboard", "ai_allocatable": True, "active": True, "usage_limit": 1},
    )
    db.add(c)
    db.commit()
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)

    asyncio.run(patch_coupon(c.id, CouponPatchIn(value="20"), request=None, db=db))
    db.refresh(c)
    assert c.source_type == "manual"
    assert c.coupon_level == "silver"
    assert c.allocation_channel == "ai"
    assert c.extra_metadata.get("ai_allocatable") is True


# ── Active conflict ────────────────────────────────────────────────────────────

def test_queue_active_blocked_when_terms_pending():
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = Coupon(
        tenant_id=tenant_id, code="ACT01", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "77001",
            "salla_identity": {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID},
            "terms_sync_pending": {"operation_id": "abc", "desired": {"amount": "15"}, "integration_id": intg.id},
        },
    )
    db.add(c)
    db.commit()

    with pytest.raises(CouponIdentityConflict, match="terms_sync_pending_active_blocked"):
        queue_active_change(c, intg, False)


def test_dispatch_active_blocked_when_terms_pending():
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    # Coupon with both pending states
    c = Coupon(
        tenant_id=tenant_id, code="DUAL01", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "77002",
            "salla_identity": {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID},
            "active_sync_pending": {
                "operation_id": "op1", "desired": False, "integration_id": intg.id,
                "store_id": _STORE_ID, "requested_at": "2026-01-01T00:00:00+00:00", "attempts": 0,
            },
            "terms_sync_pending": {
                "operation_id": "op2", "desired": {"amount": "15"}, "integration_id": intg.id,
            },
        },
    )
    db.add(c)
    db.commit()

    from services.coupon_active_sync import dispatch_active_change
    adapter = MagicMock()
    adapter._tenant_id = tenant_id
    adapter._integration_id = intg.id
    adapter.store_id = _STORE_ID
    adapter.get_coupon_by_id = AsyncMock()
    adapter.update_coupon = AsyncMock()
    asyncio.run(dispatch_active_change(db, tenant_id, c.id, intg, adapter))
    db.refresh(c)
    # active_sync_pending must be preserved; provider must not be contacted
    assert (c.extra_metadata or {}).get("active_sync_pending") is not None
    assert (c.extra_metadata or {}).get("sync_status") == "failed"
    assert adapter.get_coupon_by_id.call_count == 0
    assert adapter.update_coupon.call_count == 0


def _dispatch_active_no_call(db, tenant_id, coupon_id, intg):
    from services.coupon_active_sync import dispatch_active_change
    adapter = MagicMock()
    adapter._tenant_id = tenant_id
    adapter._integration_id = intg.id
    adapter.store_id = _STORE_ID
    asyncio.run(dispatch_active_change(db, tenant_id, coupon_id, intg, adapter))


def test_dispatch_active_does_not_call_provider_when_terms_pending():
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = Coupon(
        tenant_id=tenant_id, code="DUAL02", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "77003",
            "salla_identity": {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID},
            "active_sync_pending": {
                "operation_id": "op3", "desired": False, "integration_id": intg.id,
                "store_id": _STORE_ID, "requested_at": "2026-01-01T00:00:00+00:00", "attempts": 0,
            },
            "terms_sync_pending": {"operation_id": "op4", "desired": {"amount": "15"}, "integration_id": intg.id},
        },
    )
    db.add(c)
    db.commit()

    _dispatch_active_no_call(db, tenant_id, c.id, intg)
    db.refresh(c)
    meta = c.extra_metadata or {}
    # active_sync_pending still present, sync_status=failed, safe reason
    assert meta.get("active_sync_pending") is not None
    assert meta.get("sync_status") == "failed"
    assert meta.get("sync_error") == "terms_sync_pending_active_blocked"


# ── Importer: reconcile_imported_terms ────────────────────────────────────────

class _FakeImportAdapterWithDetail:
    def __init__(self, coupons, detail_overrides=None, binding=None):
        self._coupons = coupons
        self._detail_overrides = detail_overrides or {}
        if binding is not None:
            self._tenant_id = binding["tenant_id"]
            self._integration_id = binding["integration_id"]
            self.store_id = binding["store_id"]

    async def get_coupons(self):
        return self._coupons

    async def get_coupon_by_id(self, cid):
        if cid in self._detail_overrides:
            return self._detail_overrides[cid]
        for c in self._coupons:
            if str(c.get("id")) == str(cid):
                return c
        raise ValueError(f"coupon {cid} not found")

    async def create_coupon(self, **kw):
        return None


SALLA_RAW = {
    "id": 11001,
    "code": "T10",
    "type": "percentage",
    "amount": 10,
    "expiry_date": "2027-06-30",
    "updated_at": "2026-10-01T12:00:00+00:00",
}


def test_reconcile_imported_terms_first_import():
    """First import: no prior watermarks → returns (raw, delta)."""
    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported", extra_metadata={"salla_coupon_id": "11001"},
    )
    db.add(c)
    db.commit()

    adapter = _FakeImportAdapterWithDetail([SALLA_RAW])
    observed = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)
    terms_raw, delta = asyncio.run(reconcile_imported_terms(db, c, SALLA_RAW, observed, adapter, None))
    assert terms_raw is not None
    assert terms_raw is SALLA_RAW
    assert "terms_applied_at" in delta


def test_reconcile_imported_terms_outoforder():
    """Older payload than confirmed watermark → (None, {})."""
    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "11001",
            "terms_applied_at": "2026-10-05T00:00:00+00:00",
        },
    )
    db.add(c)
    db.commit()

    older_observed = datetime(2026, 10, 3, 0, 0, 0, tzinfo=timezone.utc)
    adapter = _FakeImportAdapterWithDetail([SALLA_RAW])
    terms_raw, delta = asyncio.run(reconcile_imported_terms(db, c, SALLA_RAW, older_observed, adapter, None))
    assert terms_raw is None
    assert delta == {}


def test_reconcile_imported_terms_stale_source_rejected():
    """Source updated_at older than prior watermark → (None, {})."""
    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "11001",
            "terms_source_updated_at": "2026-10-10T00:00:00+00:00",
        },
    )
    db.add(c)
    db.commit()

    stale_raw = dict(SALLA_RAW, updated_at="2026-10-01T12:00:00+00:00")  # older than watermark
    observed = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    adapter = _FakeImportAdapterWithDetail([stale_raw])
    terms_raw, delta = asyncio.run(reconcile_imported_terms(db, c, stale_raw, observed, adapter, None))
    assert terms_raw is None
    assert delta == {}


def test_reconcile_imported_terms_equal_timestamp_no_conflict():
    """Equal timestamp, no value conflict → accepted."""
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    binding = {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID}
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        expires_at=datetime(2027, 6, 29, 21, 0, 0),  # naive UTC: 2027-06-30 00:00 Riyadh (UTC+3)
        extra_metadata={
            "salla_coupon_id": "11001",
            "salla_identity": binding,
            "terms_source_updated_at": "2026-10-01T12:00:00+00:00",
            "terms_applied_at": "2026-10-01T12:00:00+00:00",
        },
    )
    db.add(c)
    db.commit()

    same_raw = dict(SALLA_RAW)  # same amount 10 == row value "10" (numeric equal)
    observed = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    adapter = _FakeImportAdapterWithDetail([same_raw], binding=binding)
    terms_raw, delta = asyncio.run(reconcile_imported_terms(db, c, same_raw, observed, adapter, binding))
    # No conflict → returned raw (not detail)
    assert terms_raw is not None


def test_reconcile_imported_terms_equal_timestamp_conflict_uses_detail():
    """Equal timestamp + value conflict → detail fetch."""
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    binding = {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID}
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="12",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "11001",
            "salla_identity": binding,
            "terms_source_updated_at": "2026-10-01T12:00:00+00:00",
            "terms_applied_at": "2026-10-01T12:00:00+00:00",
        },
    )
    db.add(c)
    db.commit()

    conflicting_raw = dict(SALLA_RAW, amount=10)  # raw says 10, row says 12 → conflict
    # Detail confirms 10 (the newer value)
    detail = dict(conflicting_raw)
    adapter = _FakeImportAdapterWithDetail([conflicting_raw], {"11001": detail}, binding=binding)
    observed = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    terms_raw, delta = asyncio.run(reconcile_imported_terms(db, c, conflicting_raw, observed, adapter, binding))
    assert terms_raw is detail
    assert "terms_applied_at" in delta


def test_reconcile_imported_terms_pending_returns_none():
    """Terms sync pending → returns (None, preserved_status)."""
    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "11001",
            "terms_sync_pending": {"operation_id": "x", "desired": {"amount": "20"}},
            "sync_status": "pending",
        },
    )
    db.add(c)
    db.commit()

    adapter = _FakeImportAdapterWithDetail([SALLA_RAW])
    observed = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    terms_raw, delta = asyncio.run(reconcile_imported_terms(db, c, SALLA_RAW, observed, adapter, None))
    assert terms_raw is None
    assert delta.get("salla_synced") is False


def test_reconcile_imported_terms_missing_updated_at_triggers_detail():
    """No updated_at in payload after a prior watermark → need_detail path."""
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    binding = {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID}
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "11001",
            "salla_identity": binding,
            "terms_applied_at": "2026-10-01T12:00:00+00:00",
        },
    )
    db.add(c)
    db.commit()

    no_ts_raw = {"id": 11001, "code": "T10", "type": "percentage", "amount": 10}
    detail = dict(SALLA_RAW)
    adapter = _FakeImportAdapterWithDetail([no_ts_raw], {"11001": detail}, binding=binding)
    observed = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    terms_raw, delta = asyncio.run(reconcile_imported_terms(db, c, no_ts_raw, observed, adapter, binding))
    # Should have fetched detail
    assert terms_raw is detail


# ── sync_coupons uses reconcile_imported_terms ────────────────────────────────

def test_sync_coupons_uses_terms_raw_for_update():
    """Importer applies terms only from reconcile_imported_terms result."""
    db, tenant_id = _make_db()
    # Existing coupon already imported
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={"salla_coupon_id": "11001", "source": "salla", "sync_direction": "salla_to_nahla"},
    )
    db.add(c)
    db.commit()

    updated_raw = dict(SALLA_RAW, amount=15, updated_at="2026-10-15T00:00:00+00:00")
    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeImportAdapterWithDetail([updated_raw])
    asyncio.run(svc.sync_coupons())
    db.refresh(c)
    assert c.discount_value == "15"


def test_sync_coupons_preserves_pending_when_terms_sync_pending():
    """Importer skips terms fields when terms_sync_pending is set."""
    db, tenant_id = _make_db()
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "11001",
            "source": "salla",
            "sync_direction": "salla_to_nahla",
            "terms_sync_pending": {"operation_id": "op5", "desired": {"amount": "20"}},
            "sync_status": "pending",
        },
    )
    db.add(c)
    db.commit()

    svc = StoreSyncService(db, tenant_id)
    svc._adapter = _FakeImportAdapterWithDetail([dict(SALLA_RAW, amount=15)])
    asyncio.run(svc.sync_coupons())
    db.refresh(c)
    # Pending preserved; value not changed (still 10, not 15)
    assert c.discount_value == "10"
    assert (c.extra_metadata or {}).get("terms_sync_pending") is not None


# ── sync_badge with terms pending ─────────────────────────────────────────────

def test_derive_sync_badge_shows_pending_when_terms_pending():
    meta = {"terms_sync_pending": {"operation_id": "x"}, "sync_status": "synced", "salla_synced": True}
    fields = derive_coupon_sync_visibility(source_type="imported", meta=meta)
    assert fields["sync_badge"] == "pending"


def test_derive_sync_badge_synced_when_no_pending():
    meta = {"sync_status": "synced", "salla_synced": True, "sync_direction": "salla_to_nahla"}
    fields = derive_coupon_sync_visibility(source_type="imported", meta=meta)
    assert fields["sync_badge"] == "imported"


# ── provider_expiry_utc ────────────────────────────────────────────────────────

def test_provider_expiry_utc_plain_date():
    result = provider_expiry_utc("2027-03-15")
    assert result is not None
    assert result.tzinfo is not None
    assert result.year == 2027


def test_provider_expiry_utc_none():
    assert provider_expiry_utc(None) is None


def test_provider_expiry_utc_unknown_format():
    assert provider_expiry_utc("not-a-date") is None


# ── retry_terms_changes ────────────────────────────────────────────────────────

def test_retry_terms_changes_dispatches_due(monkeypatch):
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = Coupon(
        tenant_id=tenant_id, code="RTY01", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "66001",
            "salla_identity": {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID},
            "terms_sync_pending": {
                "operation_id": "rty1", "desired": {"amount": "20"}, "integration_id": intg.id,
                "store_id": _STORE_ID, "provider_id": "66001",
                "requested_at": "2026-01-01T00:00:00+00:00", "attempts": 0,
            },
            "terms_sync_status": "pending",
        },
    )
    db.add(c)
    db.commit()

    dispatched = []

    async def _fake_dispatch(db_, tid, cid, intg_, adapter_):
        dispatched.append(cid)

    monkeypatch.setattr("services.coupon_terms_sync.dispatch_terms_change", _fake_dispatch)
    monkeypatch.setattr(
        "store_integration.registry.adapter_for_integration",
        lambda _i: MagicMock(),
    )

    asyncio.run(retry_terms_changes(db, intg))
    assert c.id in dispatched


def test_retry_terms_changes_skips_retired(monkeypatch):
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = Coupon(
        tenant_id=tenant_id, code="RTY02", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "66002",
            "salla_retirement": {"version": 1},
            "terms_sync_pending": {
                "operation_id": "rty2", "desired": {"amount": "20"}, "integration_id": intg.id,
                "store_id": _STORE_ID, "provider_id": "66002",
                "requested_at": "2026-01-01T00:00:00+00:00", "attempts": 0,
            },
        },
    )
    db.add(c)
    db.commit()

    dispatched = []

    async def _fake_dispatch(db_, tid, cid, intg_, adapter_):
        dispatched.append(cid)

    monkeypatch.setattr("services.coupon_terms_sync.dispatch_terms_change", _fake_dispatch)
    monkeypatch.setattr("store_integration.registry.adapter_for_integration", lambda _i: MagicMock())

    asyncio.run(retry_terms_changes(db, intg))
    assert c.id not in dispatched


def test_retry_terms_changes_respects_next_attempt_at(monkeypatch):
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    # next_attempt_at is far future
    c = Coupon(
        tenant_id=tenant_id, code="RTY03", discount_type="percentage", discount_value="10",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "66003",
            "terms_sync_pending": {
                "operation_id": "rty3", "desired": {"amount": "20"}, "integration_id": intg.id,
                "store_id": _STORE_ID, "provider_id": "66003",
                "requested_at": "2026-01-01T00:00:00+00:00", "attempts": 1,
                "next_attempt_at": "2099-01-01T00:00:00+00:00",  # far future
            },
        },
    )
    db.add(c)
    db.commit()

    dispatched = []

    async def _fake_dispatch(db_, tid, cid, intg_, adapter_):
        dispatched.append(cid)

    monkeypatch.setattr("services.coupon_terms_sync.dispatch_terms_change", _fake_dispatch)
    monkeypatch.setattr("store_integration.registry.adapter_for_integration", lambda _i: MagicMock())

    asyncio.run(retry_terms_changes(db, intg))
    assert c.id not in dispatched  # not due yet


# ── Regression: CouponPatchIn numeric/bool coercion ───────────────────────────

def test_patch_value_int_accepted_legacy(monkeypatch):
    """Numeric int accepted as legacy value in CouponPatchIn and real unbound PATCH."""
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    result = asyncio.run(patch_coupon(c.id, CouponPatchIn(value=25), request=None, db=db))
    db.refresh(c)
    assert result["accepted"] is True
    assert float(c.discount_value) == 25.0


def test_patch_value_float_accepted_legacy(monkeypatch):
    """Numeric float accepted as legacy value in CouponPatchIn and real unbound PATCH."""
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id, discount_type="fixed")
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    result = asyncio.run(patch_coupon(c.id, CouponPatchIn(value=12.5), request=None, db=db))
    db.refresh(c)
    assert result["accepted"] is True
    assert float(c.discount_value) == 12.5


def test_patch_value_high_precision_str_unchanged(monkeypatch):
    """High-precision str value is stored unchanged (not coerced to float)."""
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id, discount_type="fixed")
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    hp = "9.123456789012345"
    asyncio.run(patch_coupon(c.id, CouponPatchIn(value=hp), request=None, db=db))
    db.refresh(c)
    assert c.discount_value == hp


def test_patch_value_bool_rejected(monkeypatch):
    """Boolean value is rejected (bool cannot be used as numeric discount value)."""
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException
    import pydantic
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id)
    original = c.discount_value
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    with pytest.raises((pydantic.ValidationError, HTTPException, CouponTermsError, ValueError)):
        asyncio.run(patch_coupon(c.id, CouponPatchIn(value=True), request=None, db=db))
    db.refresh(c)
    assert c.discount_value == original  # no mutation


# ── Regression: percentage 200 rejects before mutation ────────────────────────

def test_patch_percentage_200_rejects_before_mutation(monkeypatch):
    """Percentage value 200 is rejected (>100) before any DB mutation."""
    from backend.routers import coupons as coupons_router
    from backend.routers.coupons import CouponPatchIn, patch_coupon
    from fastapi import HTTPException
    db, tenant_id = _make_db()
    c = _make_unbound(db, tenant_id, value="15", discount_type="percentage")
    monkeypatch.setattr(coupons_router, "resolve_tenant_id", lambda _r: tenant_id)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_coupon(c.id, CouponPatchIn(value="200"), request=None, db=db))
    assert exc.value.status_code == 422
    db.refresh(c)
    assert c.discount_value == "15"  # no mutation


# ── Regression: push_coupon_to_salla with terms_sync_pending refuses ──────────

def test_push_coupon_to_salla_terms_pending_manual_refuses():
    """push_coupon_to_salla refuses when terms_sync_pending is set; create not called."""
    from services.coupon_salla_push import push_coupon_to_salla
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = Coupon(
        tenant_id=tenant_id, code="PUSH01", discount_type="percentage", discount_value="10",
        source_type="manual",
        extra_metadata={
            "source": "dashboard", "active": True,
            "terms_sync_pending": {"operation_id": "p1", "desired": {"amount": "20"}, "integration_id": intg.id},
        },
    )
    db.add(c)
    db.commit()

    fake_adapter = _FakeImportAdapterWithDetail([])
    fake_adapter.create_coupon = AsyncMock()

    ok, result = asyncio.run(push_coupon_to_salla(db, tenant_id, c, adapter=fake_adapter))
    assert not ok
    assert result
    assert fake_adapter.create_coupon.call_count == 0


# ── Regression: watermark protects newer import from older replay ─────────────

@pytest.mark.parametrize("t2_updated_at,t1_updated_at", [
    ("2026-10-10T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
    (
        {"date": "2026-10-10 03:00:00", "timezone": "Asia/Riyadh"},
        {"date": "2026-10-01 03:00:00", "timezone": "Asia/Riyadh"},
    ),
])
def test_sync_newer_import_protected_from_older_replay(t2_updated_at, t1_updated_at):
    """Newer T2 (value=20, valid expiry) survives older T1 (value=10) replay; one row."""
    from decimal import Decimal
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    binding = {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID}

    raw_T2 = {
        "id": 11001, "code": "T10", "type": "percentage", "amount": 20,
        "expiry_date": "2027-12-31", "updated_at": t2_updated_at,
    }
    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)
    svc._adapter = _FakeImportAdapterWithDetail([raw_T2], binding=binding)
    asyncio.run(svc.sync_coupons(raw_list=[raw_T2], record_sync_meta=False))

    c = db.query(Coupon).filter_by(tenant_id=tenant_id, code="T10").one()
    assert Decimal(str(c.discount_value)) == Decimal("20")
    assert c.expires_at is not None
    expiry_snapshot = c.expires_at
    source_snapshot = c.source_type
    meta_snapshot_active = (c.extra_metadata or {}).get("active")

    raw_T1 = {
        "id": 11001, "code": "T10", "type": "percentage", "amount": 10,
        "expiry_date": None, "updated_at": t1_updated_at,
    }
    svc2 = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)
    svc2._adapter = _FakeImportAdapterWithDetail([raw_T1], binding=binding)
    asyncio.run(svc2.sync_coupons(raw_list=[raw_T1], record_sync_meta=False))
    db.refresh(c)

    assert Decimal(str(c.discount_value)) == Decimal("20")  # retains T2 value
    assert c.expires_at == expiry_snapshot                   # retains T2 expiry (exact)
    assert c.source_type == source_snapshot
    assert (c.extra_metadata or {}).get("active") == meta_snapshot_active
    assert db.query(Coupon).filter_by(tenant_id=tenant_id, code="T10").count() == 1


# ── Regression: malformed expiry preserves old state, global not synced ───────

def test_sync_malformed_nonempty_expiry_preserves_old_expiry_and_terms():
    """Malformed non-empty expiry must preserve old expiry+terms; row stays failed/unconfirmed."""
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="10",
        source_type="imported",
        expires_at=datetime(2027, 6, 30, tzinfo=timezone.utc),
        extra_metadata={
            "salla_coupon_id": "11001",
            "salla_identity": {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID},
            "sync_status": "synced", "salla_synced": True, "active": True,
            "terms_applied_at": "2026-10-01T12:00:00+00:00",
            "source": "salla",
        },
    )
    db.add(c)
    db.commit()
    old_expiry = c.expires_at

    malformed_raw = {
        "id": 11001, "code": "T10", "type": "percentage", "amount": 10,
        "expiry_date": "not-a-date", "updated_at": "2026-10-15T00:00:00+00:00",
    }
    binding = {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID}
    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)
    svc._adapter = _FakeImportAdapterWithDetail([malformed_raw], binding=binding)
    asyncio.run(svc.sync_coupons(raw_list=[malformed_raw], record_sync_meta=False))
    db.refresh(c)

    assert c.expires_at == old_expiry  # old expiry preserved
    meta = c.extra_metadata or {}
    # Malformed row cannot claim globally synced
    assert not (meta.get("sync_status") == "synced" and meta.get("salla_synced") is True)
    # Watermark must not be advanced by the malformed payload
    assert meta.get("terms_source_updated_at") != "2026-10-15T00:00:00+00:00"


# ── Regression: terms_pending badge blocks global salla_synced ────────────────

def test_derive_sync_visibility_terms_pending_blocks_global_synced():
    """terms_sync_pending + raw salla_synced=True must not expose global synced=True."""
    meta = {
        "terms_sync_pending": {"operation_id": "x", "desired": {"amount": "20"}},
        "salla_synced": True,
        "sync_status": "synced",
    }
    fields = derive_coupon_sync_visibility(source_type="imported", meta=meta)
    assert fields.get("salla_synced") is not True


# ── Regression: real tenant/store integration 6 required cols ─────────────────

def test_real_tenant_store_integration_6_cols_valid():
    """Real tenant+store integration must have all 6 required columns populated."""
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    assert intg.id is not None
    assert intg.provider == "salla"
    assert intg.external_store_id == _STORE_ID
    assert intg.tenant_id == tenant_id
    assert intg.enabled is True
    assert intg.config is not None


# ── Regression: fully bound fake provider sync, no unrelated meta calls ───────

def test_sync_fake_provider_fully_bound_record_sync_meta_false():
    """Fully bound fake provider: sync_coupons with record_sync_meta=False avoids unrelated meta calls."""
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    raw = dict(SALLA_RAW, updated_at="2026-10-15T00:00:00+00:00")
    c = Coupon(
        tenant_id=tenant_id, code="T10", discount_type="percentage", discount_value="5",
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "11001",
            "salla_identity": {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID},
            "source": "salla",
        },
    )
    db.add(c)
    db.commit()

    binding = {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID}
    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)
    svc._adapter = _FakeImportAdapterWithDetail([raw], binding=binding)
    asyncio.run(svc.sync_coupons(raw_list=[raw], record_sync_meta=False))
    db.refresh(c)
    assert float(c.discount_value) == 10.0  # value from SALLA_RAW applied


# ── Regression: terms_failures counted for provider detail timeout → partial ──

def test_sync_bound_detail_timeout_marks_partial_and_row_failed():
    """Equal-timestamp + amount conflict: provider detail GET raises TimeoutError.

    The importer must NOT modify value/expiry; the row must expose sync_status=failed;
    Integration.config.coupon_sync_meta must show partial=True and
    failure_class=provider_terms_unconfirmed.
    """
    db, tenant_id = _make_db()
    intg = _make_integration(db, tenant_id)
    binding = {"tenant_id": tenant_id, "integration_id": intg.id, "store_id": _STORE_ID}

    # Timestamp shared by watermark and incoming raw — triggers equal-ts conflict path.
    conflict_ts = "2026-10-01T12:00:00+00:00"

    c = Coupon(
        tenant_id=tenant_id,
        code="BOUND01",
        discount_type="percentage",
        discount_value="20",   # row has 20; incoming raw will say 10 → conflict
        source_type="imported",
        extra_metadata={
            "salla_coupon_id": "88001",
            "external_id": "88001",
            "salla_identity": binding,
            "source": "salla",
            "sync_status": "synced",
            "salla_synced": True,
            "active": True,
            "terms_source_updated_at": conflict_ts,
        },
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    old_value = c.discount_value
    old_expiry = c.expires_at

    raw = {
        "id": 88001,
        "code": "BOUND01",
        "type": "percentage",
        "amount": 10,          # conflicts with row value "20"
        "expiry_date": None,
        "updated_at": conflict_ts,
    }

    class _TimeoutAdapter(_FakeImportAdapterWithDetail):
        """Raises TimeoutError on provider detail GET only."""
        async def get_coupon_by_id(self, cid):
            raise TimeoutError("simulated provider timeout")

    adapter = _TimeoutAdapter([raw], binding=binding)

    observed = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)
    svc._adapter = adapter
    asyncio.run(svc.sync_coupons(raw_list=[raw], record_sync_meta=True, observed_at=observed))
    db.commit()
    db.refresh(c)
    db.refresh(intg)

    # Row value and expiry must be unchanged.
    assert c.discount_value == old_value
    assert c.expires_at == old_expiry

    # Row must reflect provider-side failure.
    row_meta = c.extra_metadata or {}
    assert row_meta.get("sync_status") == "failed"

    # coupon_sync_meta must expose the partial failure to reporting.
    sync_meta = (intg.config or {}).get("coupon_sync_meta") or {}
    assert sync_meta.get("partial") is True
    assert sync_meta.get("failure_class") == "provider_terms_unconfirmed"
