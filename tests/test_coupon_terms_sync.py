"""
tests/test_coupon_terms_sync.py
─────────────────────────────────────────────────────────────────────────────
Regression tests for coupon_terms_sync service.

Uses SQLite in-memory DB (proves behaviour; PG concurrency is separate).
No network calls; adapters are SimpleNamespace + AsyncMock.
asyncio.run() only — no pytest-asyncio dependency required.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from database.models import Base, Coupon, Integration, Tenant  # noqa: E402
from services.coupon_terms_sync import (  # noqa: E402
    CouponTermsError,
    dispatch_terms_change,
    provider_expiry_utc,
    queue_terms_change,
    reconcile_imported_terms,
    validate_terms,
)


# ── SQLite JSONB → JSON remapping (mirrors test_coupon_salla_two_way_sync) ───

@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


# ── shared DB factory (reuse pattern from test_coupon_salla_two_way_sync) ────

def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Terms Test Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


# ── fixture helpers ───────────────────────────────────────────────────────────

def _make_integration(db, tenant_id, store_id="STORE1"):
    ig = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=store_id,
        enabled=True,
        config={
            "api_sync_enabled": True,
            "refresh_token": "fake-token",
            "store_id": store_id,
        },
    )
    db.add(ig)
    db.commit()
    return ig


def _make_bound_coupon(
    db,
    tenant_id,
    ig,
    code="COUP1",
    pid="99001",
    discount_type="fixed",
    discount_value="25.00",
    source_type="imported",
    expires_at=None,
    extra=None,
):
    meta = {
        "source": "salla",
        "salla_coupon_id": pid,
        "external_id": pid,
        "salla_identity": {
            "tenant_id": tenant_id,
            "integration_id": ig.id,
            "store_id": str(ig.external_store_id or (ig.config or {}).get("store_id") or ""),
        },
        "sync_status": "synced",
        "salla_synced": True,
    }
    if extra:
        meta.update(extra)
    row = Coupon(
        tenant_id=tenant_id,
        code=code,
        description="test",
        discount_type=discount_type,
        discount_value=discount_value,
        source_type=source_type,
        expires_at=expires_at,
        extra_metadata=meta,
    )
    db.add(row)
    db.commit()
    return row


def _make_adapter(tenant_id, ig, get_result=None, store_id=None):
    s = store_id or str(ig.external_store_id or (ig.config or {}).get("store_id") or "")
    return SimpleNamespace(
        _tenant_id=tenant_id,
        _integration_id=ig.id,
        store_id=s,
        get_coupon_by_id=AsyncMock(return_value=(get_result or {})),
        update_coupon_terms=AsyncMock(return_value=None),
    )


def _provider_raw(pid="99001", code="COUP1", discount_type="fixed", amount="25.00", expiry=None):
    r = {"id": int(pid), "code": code, "type": discount_type, "amount": amount}
    if expiry:
        r["expiry_date"] = expiry
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 1. validate_terms
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_terms_manual_amount_returns_string():
    db, tid = _make_db()
    row = Coupon(tenant_id=tid, code="C1", discount_type="fixed", discount_value="10",
                 source_type="manual", extra_metadata={"source": "dashboard"})
    db.add(row); db.commit()
    result = validate_terms(row, {"value": "20.50"})
    assert result == {"amount": "20.50"}
    assert isinstance(result["amount"], str)


def test_validate_terms_imported_expiry_returns_string():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig)
    result = validate_terms(row, {"expires": "2027-01-15"})
    assert result["expiry_date"] == "2027-01-15"


def test_validate_terms_system_coupon_blocked():
    db, tid = _make_db()
    row = Coupon(tenant_id=tid, code="C2", discount_type="fixed", discount_value="5",
                 source_type="system",
                 extra_metadata={"source": "pool", "sync_direction": "nahla_to_salla"})
    db.add(row); db.commit()
    with pytest.raises(CouponTermsError, match="retired_or_system_coupon"):
        validate_terms(row, {"value": "5"})


def test_validate_terms_retired_blocked():
    db, tid = _make_db()
    row = Coupon(tenant_id=tid, code="C3", discount_type="fixed", discount_value="5",
                 source_type="imported",
                 extra_metadata={"salla_retirement": {"retired_at": "2026-01-01"}})
    db.add(row); db.commit()
    with pytest.raises(CouponTermsError, match="retired_or_system_coupon"):
        validate_terms(row, {"value": "5"})


def test_validate_terms_system_flag_blocked():
    db, tid = _make_db()
    row = Coupon(tenant_id=tid, code="C4", discount_type="fixed", discount_value="5",
                 source_type="manual",
                 extra_metadata={"source": "dashboard", "is_system_coupon": True})
    db.add(row); db.commit()
    with pytest.raises(CouponTermsError, match="retired_or_system_coupon"):
        validate_terms(row, {"value": "5"})


def test_validate_terms_invalid_numeric_raises():
    db, tid = _make_db()
    row = Coupon(tenant_id=tid, code="C5", discount_type="fixed", discount_value="5",
                 source_type="manual", extra_metadata={"source": "dashboard"})
    db.add(row); db.commit()
    with pytest.raises(CouponTermsError, match="invalid_value"):
        validate_terms(row, {"value": "abc"})
    with pytest.raises(CouponTermsError, match="invalid_value"):
        validate_terms(row, {"value": "-5"})
    with pytest.raises(CouponTermsError, match="invalid_value"):
        validate_terms(row, {"value": "1e2"})


def test_validate_terms_invalid_date_raises():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig)
    with pytest.raises(CouponTermsError):
        validate_terms(row, {"expires": "31-01-2027"})  # wrong format
    with pytest.raises(CouponTermsError):
        validate_terms(row, {"expires": "2026-02-30"})  # impossible date


def test_validate_terms_percentage_exceeds_100():
    db, tid = _make_db()
    row = Coupon(tenant_id=tid, code="C6", discount_type="percentage", discount_value="10",
                 source_type="manual", extra_metadata={"source": "dashboard"})
    db.add(row); db.commit()
    with pytest.raises(CouponTermsError, match="percentage_exceeds_100"):
        validate_terms(row, {"value": "101"})


def test_validate_terms_bound_cannot_clear_expiry():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig)
    with pytest.raises(CouponTermsError, match="cannot_clear_expiry"):
        validate_terms(row, {"expires": ""})


def test_validate_terms_auto_generated_blocked():
    """Coupons with auto/vip/promotion origin metadata are system-classified → blocked."""
    db, tid = _make_db()
    row = Coupon(tenant_id=tid, code="AUTO1", discount_type="fixed", discount_value="5",
                 source_type="system",
                 extra_metadata={"source": "auto"})
    db.add(row); db.commit()
    with pytest.raises(CouponTermsError, match="retired_or_system_coupon"):
        validate_terms(row, {"value": "5"})


# ─────────────────────────────────────────────────────────────────────────────
# 2. queue_terms_change
# ─────────────────────────────────────────────────────────────────────────────

def test_queue_creates_pending_with_provider_id():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig)
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    pending = row.extra_metadata.get("terms_sync_pending") or {}
    assert pending["desired"] == desired
    assert pending["integration_id"] == ig.id
    assert pending["provider_id"] == "99001"
    assert "operation_id" in pending
    assert row.extra_metadata["sync_status"] == "pending"
    assert row.extra_metadata["salla_synced"] is False


def test_queue_same_intent_reuses_operation_id():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig)
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    op_id = row.extra_metadata["terms_sync_pending"]["operation_id"]
    queue_terms_change(row, ig, desired)  # identical intent
    assert row.extra_metadata["terms_sync_pending"]["operation_id"] == op_id


def test_queue_conflicting_pending_rejected():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig)
    queue_terms_change(row, ig, {"amount": "30.00"})
    with pytest.raises(CouponTermsError, match="terms_change_already_pending"):
        queue_terms_change(row, ig, {"amount": "40.00"})


def test_queue_active_sync_pending_rejected():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, extra={"active_sync_pending": True})
    with pytest.raises(CouponTermsError, match="terms_change_already_pending"):
        queue_terms_change(row, ig, {"amount": "30.00"})


def test_queue_retired_coupon_blocked():
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig,
                             extra={"salla_retirement": {"retired_at": "2026-01-01"}})
    with pytest.raises(CouponTermsError, match="retired_or_system_coupon"):
        queue_terms_change(row, ig, {"amount": "30.00"})


def test_queue_global_pending_preserves_over_import():
    """Global sync_status=pending after queue must not be overwritten to synced by ordinary import."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig)
    queue_terms_change(row, ig, {"amount": "30.00"})
    assert row.extra_metadata["sync_status"] == "pending"
    assert row.extra_metadata["salla_synced"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 3. dispatch_terms_change
# ─────────────────────────────────────────────────────────────────────────────

def test_dispatch_value_update_stores_exact_string():
    """row.discount_value must be a plain decimal string, never a Decimal object."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99001", discount_type="fixed", discount_value="25.00")
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99001", code="COUP1", discount_type="fixed", amount="30.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.discount_value == "30.00"
    assert isinstance(row.discount_value, str)
    assert "E" not in row.discount_value and "e" not in row.discount_value
    assert row.extra_metadata["terms_sync_status"] == "synced"


def test_dispatch_expiry_stores_naive_utc():
    """expires_at must be stored as naive UTC datetime."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99002", discount_type="fixed", discount_value="10")
    desired = {"expiry_date": "2027-01-15"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99002", code="COUP1", discount_type="fixed",
                        amount="10", expiry="2027-01-15")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.expires_at is not None
    assert row.expires_at.tzinfo is None  # stored as naive UTC
    assert row.extra_metadata["terms_sync_status"] == "synced"


def test_dispatch_partial_amount_only_leaves_expiry_unchanged():
    """Amount-only update must not touch expires_at."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    original_expiry = datetime(2026, 12, 31, 21, 0, 0)  # naive UTC
    row = _make_bound_coupon(db, tid, ig, pid="99003", discount_type="fixed",
                             discount_value="10", expires_at=original_expiry)
    desired = {"amount": "15.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99003", code="COUP1", discount_type="fixed", amount="15.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.discount_value == "15.00"
    assert row.expires_at == original_expiry  # unchanged


def test_dispatch_type_missing_no_put():
    """Provider raw without type → no PUT, failure recorded."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99004", discount_type="fixed")
    desired = {"amount": "20.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = {"id": 99004, "code": "COUP1", "amount": "20.00"}  # type missing
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert adapter.update_coupon_terms.call_count == 0
    assert row.extra_metadata["terms_sync_status"] == "failed"
    assert row.discount_value == "25.00"  # unchanged


def test_dispatch_type_mismatch_no_put():
    """Type mismatch between provider and local → no PUT."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99005", discount_type="fixed")
    desired = {"amount": "20.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99005", code="COUP1", discount_type="percentage", amount="20.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert adapter.update_coupon_terms.call_count == 0
    assert row.extra_metadata["terms_sync_status"] == "failed"


def test_dispatch_wrong_tenant_rejected():
    db, tid = _make_db()
    other_tenant = Tenant(name="OtherT", is_active=True)
    db.add(other_tenant); db.commit()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99006")
    desired = {"amount": "20.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99006", code="COUP1", discount_type="fixed", amount="20.00")
    # Integration belongs to other tenant
    ig2 = Integration(
        tenant_id=other_tenant.id, provider="salla", external_store_id="STORE2",
        enabled=True,
        config={"api_sync_enabled": True, "refresh_token": "tok", "store_id": "STORE2"},
    )
    db.add(ig2); db.commit()
    adapter = _make_adapter(other_tenant.id, ig2, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig2, adapter))
    db.refresh(row)
    assert row.extra_metadata["terms_sync_status"] == "failed"
    assert row.discount_value == "25.00"  # unchanged


def test_dispatch_wrong_adapter_store_rejected():
    """Adapter store_id different from integration store_id → adapter_tenant_mismatch."""
    db, tid = _make_db()
    ig = _make_integration(db, tid, store_id="STORE_A")
    row = _make_bound_coupon(db, tid, ig, pid="99007")
    desired = {"amount": "20.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99007", code="COUP1", discount_type="fixed", amount="20.00")
    # Adapter has wrong store_id
    adapter = SimpleNamespace(
        _tenant_id=tid,
        _integration_id=ig.id,
        store_id="STORE_WRONG",
        get_coupon_by_id=AsyncMock(return_value=raw),
        update_coupon_terms=AsyncMock(return_value=None),
    )

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.extra_metadata["terms_sync_status"] == "failed"


def test_dispatch_wrong_provider_code_rejected():
    """Provider returns different code → identity mismatch → no mutation."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, code="MYCODE", pid="99008")
    desired = {"amount": "20.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99008", code="DIFFERENT", discount_type="fixed", amount="20.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.extra_metadata["terms_sync_status"] == "failed"
    assert row.discount_value == "25.00"  # unchanged


def test_dispatch_readback_mismatch_fails_without_mutation():
    """If readback after PUT still doesn't match desired, status=failed, no DB mutation."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99009", discount_type="fixed", discount_value="25.00")
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    get_before = _provider_raw(pid="99009", code="COUP1", discount_type="fixed", amount="25.00")
    get_after = _provider_raw(pid="99009", code="COUP1", discount_type="fixed", amount="25.00")  # still old
    adapter = SimpleNamespace(
        _tenant_id=tid,
        _integration_id=ig.id,
        store_id="STORE1",
        get_coupon_by_id=AsyncMock(side_effect=[get_before, get_after]),
        update_coupon_terms=AsyncMock(return_value=None),
    )

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.extra_metadata["terms_sync_status"] == "failed"
    assert row.discount_value == "25.00"  # unchanged


def test_dispatch_already_matching_skips_put():
    """GET shows value already matches desired → no PUT call."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99010", discount_type="fixed", discount_value="30.00")
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99010", code="COUP1", discount_type="fixed", amount="30.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert adapter.update_coupon_terms.call_count == 0
    assert row.extra_metadata["terms_sync_status"] == "synced"


def test_dispatch_timeout_after_put_then_get_convergence_no_duplicate_put():
    """Simulate timeout on first PUT; retry shows GET matches → no second PUT."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99011", discount_type="fixed", discount_value="25.00")
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    before_raw = _provider_raw(pid="99011", code="COUP1", discount_type="fixed", amount="25.00")
    after_raw = _provider_raw(pid="99011", code="COUP1", discount_type="fixed", amount="30.00")

    # First dispatch: GET returns not-matching, PUT raises timeout
    adapter1 = SimpleNamespace(
        _tenant_id=tid,
        _integration_id=ig.id,
        store_id="STORE1",
        get_coupon_by_id=AsyncMock(return_value=before_raw),
        update_coupon_terms=AsyncMock(side_effect=TimeoutError("timeout")),
    )
    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter1))
    db.refresh(row)
    assert row.extra_metadata["terms_sync_status"] == "failed"
    # Reset next_attempt_at so second dispatch is due now
    meta = dict(row.extra_metadata)
    pending = dict(meta["terms_sync_pending"])
    pending["next_attempt_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    meta["terms_sync_pending"] = pending
    row.extra_metadata = meta
    db.commit()

    # Second dispatch: GET returns matching (PUT succeeded despite timeout)
    adapter2 = SimpleNamespace(
        _tenant_id=tid,
        _integration_id=ig.id,
        store_id="STORE1",
        get_coupon_by_id=AsyncMock(return_value=after_raw),
        update_coupon_terms=AsyncMock(return_value=None),
    )
    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter2))
    db.refresh(row)
    assert adapter2.update_coupon_terms.call_count == 0  # no duplicate PUT
    assert row.extra_metadata["terms_sync_status"] == "synced"
    assert row.discount_value == "30.00"


def test_dispatch_provider_id_drift_blocked():
    """If provider_id in row changes after queue, dispatch must fail without mutation."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99012", discount_type="fixed", discount_value="25.00")
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    # Simulate provider_id drift in row metadata after queueing
    meta = dict(row.extra_metadata)
    meta["salla_coupon_id"] = "99999"  # drifted
    meta["external_id"] = "99999"
    row.extra_metadata = meta
    db.commit()

    raw = _provider_raw(pid="99999", code="COUP1", discount_type="fixed", amount="30.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.extra_metadata["terms_sync_status"] == "failed"
    assert row.discount_value == "25.00"


def test_dispatch_retired_row_skipped():
    """Retired row in dispatch path exits without writing failure status."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99013",
                             extra={"salla_retirement": {"retired_at": "2026-01-01"}})
    # Inject pending directly (bypassing queue which would reject)
    meta = dict(row.extra_metadata)
    meta["terms_sync_pending"] = {
        "operation_id": "abc",
        "desired": {"amount": "30.00"},
        "integration_id": ig.id,
        "store_id": "STORE1",
        "provider_id": "99013",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
    }
    row.extra_metadata = meta
    db.commit()

    raw = _provider_raw(pid="99013", code="COUP1", discount_type="fixed", amount="30.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    # Retired row skips processing; terms_sync_status not set to synced
    assert row.extra_metadata.get("terms_sync_status") != "synced"
    assert adapter.update_coupon_terms.call_count == 0


def test_dispatch_backoff_not_due_skips():
    """If next_attempt_at is in the future, dispatch skips without any calls."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99014", discount_type="fixed", discount_value="25.00")
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    meta = dict(row.extra_metadata)
    pending = dict(meta["terms_sync_pending"])
    pending["next_attempt_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    meta["terms_sync_pending"] = pending
    row.extra_metadata = meta
    db.commit()

    raw = _provider_raw(pid="99014", code="COUP1", discount_type="fixed", amount="30.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    assert adapter.get_coupon_by_id.call_count == 0


def test_dispatch_attempts_incremented_on_failure():
    """Each failure increments attempts and sets next_attempt_at for backoff."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99015", discount_type="fixed", discount_value="25.00")
    queue_terms_change(row, ig, {"amount": "30.00"})
    db.commit()

    raw = _provider_raw(pid="99015", code="COUP1", discount_type="fixed", amount="25.00")
    # update_coupon_terms raises so readback still doesn't match
    adapter = SimpleNamespace(
        _tenant_id=tid,
        _integration_id=ig.id,
        store_id="STORE1",
        get_coupon_by_id=AsyncMock(side_effect=[raw, raw]),
        update_coupon_terms=AsyncMock(side_effect=RuntimeError("network")),
    )
    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    pending = row.extra_metadata["terms_sync_pending"]
    assert pending["attempts"] == 1
    assert "next_attempt_at" in pending


def test_dispatch_preserves_source_type_and_origin():
    """After successful dispatch, source_type and meta source origin must be unchanged."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99016", discount_type="fixed",
                             discount_value="25.00", source_type="manual",
                             extra={"source": "dashboard"})
    desired = {"amount": "30.00"}
    queue_terms_change(row, ig, desired)
    db.commit()

    raw = _provider_raw(pid="99016", code="COUP1", discount_type="fixed", amount="30.00")
    adapter = _make_adapter(tid, ig, get_result=raw)

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert row.source_type == "manual"
    assert row.extra_metadata["source"] == "dashboard"
    assert row.extra_metadata["terms_sync_status"] == "synced"


def test_dispatch_dual_active_and_terms_intent_not_falsely_synced():
    """active_sync_pending=True blocks queueing, preserving pending/failed state."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99017",
                             extra={"active_sync_pending": True, "sync_status": "pending"})
    with pytest.raises(CouponTermsError, match="terms_change_already_pending"):
        queue_terms_change(row, ig, {"amount": "30.00"})
    # Confirm global state was not touched
    assert row.extra_metadata["sync_status"] == "pending"


# ─────────────────────────────────────────────────────────────────────────────
# 4. provider_expiry_utc
# ─────────────────────────────────────────────────────────────────────────────

def test_provider_expiry_explicit_dubai_differs_riyadh():
    """Asia/Dubai (UTC+4) and Asia/Riyadh (UTC+3) produce different UTC instants."""
    dubai = provider_expiry_utc({"date": "2027-01-15 00:00:00", "timezone": "Asia/Dubai"})
    riyadh = provider_expiry_utc({"date": "2027-01-15 00:00:00", "timezone": "Asia/Riyadh"})
    assert dubai is not None
    assert riyadh is not None
    assert dubai != riyadh
    # Dubai is UTC+4, Riyadh is UTC+3; same local time → Dubai is earlier UTC
    assert dubai < riyadh


def test_provider_expiry_unknown_timezone_returns_none():
    result = provider_expiry_utc({"date": "2027-01-15 00:00:00", "timezone": "NotAZone/Fake"})
    assert result is None


def test_provider_expiry_non_midnight_preserves_time():
    """Non-midnight time-of-day is preserved in the UTC conversion."""
    result = provider_expiry_utc({"date": "2027-01-15 14:30:00", "timezone": "Asia/Dubai"})
    assert result is not None
    # 14:30 Dubai (UTC+4) = 10:30 UTC
    assert result.hour == 10
    assert result.minute == 30


def test_provider_expiry_conflicting_zones_returns_none():
    """ISO string with +04:00 offset combined with Asia/Riyadh (UTC+3) → conflict → None."""
    result = provider_expiry_utc({"date": "2027-01-15T12:00:00+04:00", "timezone": "Asia/Riyadh"})
    assert result is None


def test_provider_expiry_naive_string_uses_riyadh():
    """Flat naive string without dict → Riyadh assumption documented."""
    result = provider_expiry_utc("2027-01-15")
    assert result is not None
    assert result.tzinfo == timezone.utc
    # Riyadh midnight = UTC 21:00 previous day
    assert result.hour == 21
    assert result.day == 14


def test_provider_expiry_iso_with_explicit_offset_honored():
    """ISO with explicit Z offset → UTC directly, no Riyadh assumption."""
    result = provider_expiry_utc("2027-01-15T00:00:00Z")
    assert result is not None
    assert result == datetime(2027, 1, 15, 0, 0, 0, tzinfo=timezone.utc)


def test_provider_expiry_none_returns_none():
    assert provider_expiry_utc(None) is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. reconcile_imported_terms
# ─────────────────────────────────────────────────────────────────────────────

def test_reconcile_pending_no_false_synced():
    """Row with terms_sync_pending → reconcile returns None and preserves pending state."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99020")
    queue_terms_change(row, ig, {"amount": "30.00"})

    raw = _provider_raw(pid="99020", code="COUP1", discount_type="fixed", amount="25.00")
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    adapter = _make_adapter(tid, ig, get_result=raw)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert result_raw is None
    assert delta.get("salla_synced") is False
    assert adapter.get_coupon_by_id.call_count == 0


def test_reconcile_stale_observation_rejected():
    """Observation older than confirmed applied timestamp is rejected."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    applied_at = datetime.now(timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99021",
                             extra={"terms_applied_at": applied_at.isoformat()})

    raw = _provider_raw(pid="99021", code="COUP1", discount_type="fixed", amount="25.00")
    # Observation is before the applied timestamp
    observed_at = applied_at - timedelta(seconds=10)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    adapter = _make_adapter(tid, ig, get_result=raw)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert result_raw is None


def test_reconcile_missing_timestamp_after_watermark_triggers_detail():
    """raw without updated_at after a known watermark → need_detail=True → canonical GET."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    prior_at = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99022",
                             extra={"terms_source_updated_at": prior_at.isoformat()})

    raw = {"id": 99022, "code": "COUP1", "type": "fixed", "amount": "25.00"}  # no updated_at
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    detail = dict(raw)
    detail["updated_at"] = (prior_at + timedelta(seconds=1)).isoformat()
    adapter = _make_adapter(tid, ig, get_result=detail)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 1


def test_reconcile_equal_timestamp_same_amount_no_detail():
    """Same source timestamp AND same amount as actual row → no canonical GET needed."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    ts = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99023", discount_value="25.00",
                             extra={"terms_source_updated_at": ts.isoformat()})

    raw = {"id": 99023, "code": "COUP1", "type": "fixed", "amount": "25.00",
           "updated_at": ts.isoformat()}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    adapter = _make_adapter(tid, ig, get_result=raw)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 0
    assert result_raw is not None


def test_reconcile_equal_timestamp_different_amount_triggers_canonical_get():
    """Equal watermark but incoming amount differs from actual row.discount_value → GET."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    ts = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99024", discount_value="25.00",
                             extra={"terms_source_updated_at": ts.isoformat()})

    raw = {"id": 99024, "code": "COUP1", "type": "fixed", "amount": "30.00",  # different
           "updated_at": ts.isoformat()}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    detail = dict(raw)
    detail["updated_at"] = (ts + timedelta(seconds=5)).isoformat()
    adapter = _make_adapter(tid, ig, get_result=detail)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 1


def test_reconcile_equal_timestamp_different_expiry_instant_triggers_canonical_get():
    """Equal watermark but exact UTC expiry instant differs from row.expires_at → GET."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    ts = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    # row.expires_at = naive UTC representing 2027-01-15 21:00 UTC (Riyadh midnight 2027-01-16)
    row_expires = datetime(2027, 1, 15, 21, 0, 0)  # naive UTC
    row = _make_bound_coupon(db, tid, ig, pid="99025", discount_value="25.00",
                             expires_at=row_expires,
                             extra={"terms_source_updated_at": ts.isoformat()})

    # Provider returns a different expiry date
    raw = {"id": 99025, "code": "COUP1", "type": "fixed", "amount": "25.00",
           "expiry_date": "2027-02-01",  # different date
           "updated_at": ts.isoformat()}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    detail = dict(raw)
    detail["updated_at"] = (ts + timedelta(seconds=5)).isoformat()
    adapter = _make_adapter(tid, ig, get_result=detail)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 1


def test_reconcile_ordinary_import_equal_value_without_confirmed_no_detail():
    """Without watermark/applied history, no confirmed_* comparison: accept raw directly."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    # No terms_source_updated_at or terms_applied_at in meta
    row = _make_bound_coupon(db, tid, ig, pid="99026", discount_value="25.00")

    raw = {"id": 99026, "code": "COUP1", "type": "fixed", "amount": "25.00",
           "updated_at": datetime.now(timezone.utc).isoformat()}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    adapter = _make_adapter(tid, ig, get_result=raw)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 0
    assert result_raw is raw  # accepted as-is


def test_reconcile_older_source_than_watermark_rejected():
    """Source timestamp older than prior watermark → reject without detail GET."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    prior_at = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99027",
                             extra={"terms_source_updated_at": prior_at.isoformat()})

    older_at = (prior_at - timedelta(days=1)).isoformat()
    raw = {"id": 99027, "code": "COUP1", "type": "fixed", "amount": "25.00",
           "updated_at": older_at}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    adapter = _make_adapter(tid, ig, get_result=raw)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert result_raw is None
    assert adapter.get_coupon_by_id.call_count == 0


def test_reconcile_detail_older_than_watermark_rejected():
    """Canonical detail returns timestamp older than watermark → rejected."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    prior_at = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99028",
                             extra={"terms_source_updated_at": prior_at.isoformat()})

    # raw has no timestamp → triggers detail fetch
    raw = {"id": 99028, "code": "COUP1", "type": "fixed", "amount": "25.00"}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}

    # Detail returns timestamp older than watermark
    detail = {"id": 99028, "code": "COUP1", "type": "fixed", "amount": "25.00",
              "updated_at": (prior_at - timedelta(days=1)).isoformat()}
    adapter = _make_adapter(tid, ig, get_result=detail)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert result_raw is None
    assert delta.get("sync_status") == "failed"


def test_reconcile_missing_watermark_legacy_unbound_retains_legacy():
    """No prior watermark AND binding=None → legacy unbound path, raw accepted."""
    db, tid = _make_db()
    # Unbound row (no salla_identity in meta)
    row = Coupon(tenant_id=tid, code="LEG1", discount_type="fixed",
                 discount_value="10", source_type="manual",
                 extra_metadata={"source": "dashboard"})
    db.add(row); db.commit()

    raw = {"id": 99029, "code": "LEG1", "type": "fixed", "amount": "10",
           "updated_at": datetime.now(timezone.utc).isoformat()}
    observed_at = datetime.now(timezone.utc)
    adapter = _make_adapter(tid, SimpleNamespace(id=None, external_store_id=None, config={}),
                            get_result=raw, store_id="")

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding=None)
    )
    assert result_raw is raw


# ─────────────────────────────────────────────────────────────────────────────
# A. queue_terms_change – source_type / meta source / flag blocking
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source_type,meta_source,extra_flags", [
    ("system", "auto",           {}),
    ("system", "pool",           {}),
    ("system", "widget",         {}),
    ("system", "auto_generated", {}),
    ("manual", "dashboard",      {"auto_generated": True}),
    ("imported", "salla",        {"category": "vip"}),
    ("manual", "dashboard",      {"category": "vip"}),
    ("imported", "auto",         {}),
    ("system",  "dashboard",     {}),
])
def test_queue_rejects_system_meta_sources_no_pending_mutation(source_type, meta_source, extra_flags):
    """queue_terms_change blocks system/auto/pool/widget/auto_generated/vip origins;
    no pending intent written and sync_status not set to pending."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = Coupon(
        tenant_id=tid, code="BLK1", discount_type="fixed", discount_value="5",
        source_type=source_type,
        extra_metadata={"source": meta_source, **extra_flags},
    )
    db.add(row); db.commit()
    with pytest.raises(CouponTermsError, match="retired_or_system_coupon"):
        queue_terms_change(row, ig, {"amount": "10.00"})
    assert "terms_sync_pending" not in (row.extra_metadata or {})
    assert row.extra_metadata.get("sync_status") != "pending"


# ─────────────────────────────────────────────────────────────────────────────
# B. dispatch – pre-existing intent preserved under automation / dual pending
# ─────────────────────────────────────────────────────────────────────────────

def test_dispatch_alias_conflict_after_queue_safe_failed_no_mutation():
    """Queue normally; then change external_id to contradict salla_coupon_id and commit.
    dispatch must record failed/provider_alias_conflict, preserve pending intent,
    leave discount_value unchanged, issue 0 GET/PUT."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99100", discount_type="fixed", discount_value="25.00")
    queue_terms_change(row, ig, {"amount": "30.00"})
    # Contradict salla_coupon_id after queueing to trigger alias conflict in _provider_id
    meta = dict(row.extra_metadata)
    meta["external_id"] = "00000"  # contradicts salla_coupon_id="99100"
    row.extra_metadata = meta
    db.commit()

    adapter = _make_adapter(tid, ig, get_result=_provider_raw(
        pid="99100", code="COUP1", discount_type="fixed", amount="30.00"))

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert adapter.get_coupon_by_id.call_count == 0
    assert adapter.update_coupon_terms.call_count == 0
    assert row.extra_metadata["terms_sync_status"] == "failed"
    assert row.extra_metadata["terms_sync_error"] == "provider_alias_conflict"
    assert row.extra_metadata.get("terms_sync_pending") is not None  # pending preserved
    assert row.discount_value == "25.00"  # unchanged


def test_queue_alias_conflict_raises_no_pending_mutation():
    """Row with external_id contradicting salla_coupon_id: queue raises provider_alias_conflict,
    no pending intent written."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99101",
                             extra={"external_id": "00001"})  # contradicts salla_coupon_id="99101"
    with pytest.raises(CouponTermsError, match="provider_alias_conflict"):
        queue_terms_change(row, ig, {"amount": "10.00"})
    assert "terms_sync_pending" not in (row.extra_metadata or {})


def test_dispatch_automation_metadata_preserves_queued_intent():
    """Row with terms_sync_pending; automation source_type causes dispatch to exit
    without GET/PUT while keeping the pending intent intact."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99050", source_type="system",
                             extra={"source": "auto"})
    meta = dict(row.extra_metadata)
    meta["terms_sync_pending"] = {
        "operation_id": "op-auto-1",
        "desired": {"amount": "30.00"},
        "integration_id": ig.id,
        "store_id": "STORE1",
        "provider_id": "99050",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
    }
    row.extra_metadata = meta
    db.commit()

    adapter = _make_adapter(tid, ig, get_result=_provider_raw(
        pid="99050", code="COUP1", discount_type="fixed", amount="25.00"))

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert adapter.get_coupon_by_id.call_count == 0
    assert adapter.update_coupon_terms.call_count == 0
    assert row.extra_metadata["terms_sync_pending"]["operation_id"] == "op-auto-1"
    assert row.extra_metadata.get("terms_sync_status") != "synced"


def test_dispatch_dual_active_and_terms_no_calls_no_false_success():
    """active_sync_pending + terms_sync_pending both set: no network calls,
    intent preserved, not falsely marked synced."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99051",
                             extra={"active_sync_pending": True})
    meta = dict(row.extra_metadata)
    meta["terms_sync_pending"] = {
        "operation_id": "op-dual-1",
        "desired": {"amount": "30.00"},
        "integration_id": ig.id,
        "store_id": "STORE1",
        "provider_id": "99051",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
    }
    row.extra_metadata = meta
    db.commit()

    adapter = _make_adapter(tid, ig, get_result=_provider_raw(
        pid="99051", code="COUP1", discount_type="fixed", amount="25.00"))

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert adapter.get_coupon_by_id.call_count == 0
    assert adapter.update_coupon_terms.call_count == 0
    assert row.extra_metadata.get("terms_sync_status") != "synced"
    assert row.extra_metadata["terms_sync_pending"]["operation_id"] == "op-dual-1"


# ─────────────────────────────────────────────────────────────────────────────
# C. reconcile_imported_terms – pre-network binding rejection & detail validation
# ─────────────────────────────────────────────────────────────────────────────

def test_reconcile_wrong_adapter_tenant_rejects_before_network():
    """Adapter _tenant_id doesn't match binding → rejected before any GET."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99060")

    raw = _provider_raw(pid="99060", code="COUP1", discount_type="fixed", amount="25.00")
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    other_tenant = Tenant(name="Other Tenant", is_active=True)
    db.add(other_tenant)
    db.commit()
    other_tid = other_tenant.id
    assert other_tid != tid
    adapter = SimpleNamespace(
        _tenant_id=other_tid,
        _integration_id=ig.id,
        store_id="STORE1",
        get_coupon_by_id=AsyncMock(return_value=raw),
        update_coupon_terms=AsyncMock(return_value=None),
    )

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 0
    assert result_raw is None


def test_reconcile_wrong_incoming_provider_id_rejects_before_network():
    """Raw provider id doesn't match row salla_coupon_id → rejected before GET."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99061")

    raw = _provider_raw(pid="88888", code="COUP1", discount_type="fixed", amount="25.00")
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    adapter = _make_adapter(tid, ig, get_result=raw)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 0
    assert result_raw is None


def test_reconcile_detail_missing_type_refuses_preserves_watermark():
    """Canonical GET returns payload without 'type' → rejected, watermark unchanged."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    prior_ts = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99062",
                             extra={"terms_source_updated_at": prior_ts.isoformat()})

    raw = {"id": 99062, "code": "COUP1", "type": "fixed", "amount": "25.00"}  # no updated_at
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    detail_no_type = {"id": 99062, "code": "COUP1", "amount": "25.00",
                      "updated_at": (prior_ts + timedelta(seconds=1)).isoformat()}
    adapter = _make_adapter(tid, ig, get_result=detail_no_type)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert result_raw is None
    assert adapter.get_coupon_by_id.call_count == 1
    assert row.extra_metadata.get("terms_source_updated_at") == prior_ts.isoformat()


def test_reconcile_detail_wrong_id_refuses_preserves_watermark():
    """Canonical GET returns mismatched coupon id → refused, watermark preserved."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    prior_ts = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99063",
                             extra={"terms_source_updated_at": prior_ts.isoformat()})

    raw = {"id": 99063, "code": "COUP1", "type": "fixed", "amount": "25.00"}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    detail_wrong_id = {"id": 11111, "code": "COUP1", "type": "fixed", "amount": "25.00",
                       "updated_at": (prior_ts + timedelta(seconds=1)).isoformat()}
    adapter = _make_adapter(tid, ig, get_result=detail_wrong_id)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert result_raw is None
    assert adapter.get_coupon_by_id.call_count == 1
    assert row.extra_metadata.get("terms_source_updated_at") == prior_ts.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# D. dispatch – readback missing type → old value preserved, failed, exactly one PUT
# ─────────────────────────────────────────────────────────────────────────────

def test_dispatch_readback_missing_type_leaves_old_value_failed_one_put():
    """Initial GET valid; PUT issued; readback GET missing type → failed,
    old discount_value unchanged, exactly one PUT."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    row = _make_bound_coupon(db, tid, ig, pid="99070", discount_type="fixed", discount_value="25.00")
    queue_terms_change(row, ig, {"amount": "30.00"})
    db.commit()

    before_raw = _provider_raw(pid="99070", code="COUP1", discount_type="fixed", amount="25.00")
    readback_no_type = {"id": 99070, "code": "COUP1", "amount": "30.00"}  # type missing

    adapter = SimpleNamespace(
        _tenant_id=tid,
        _integration_id=ig.id,
        store_id="STORE1",
        get_coupon_by_id=AsyncMock(side_effect=[before_raw, readback_no_type]),
        update_coupon_terms=AsyncMock(return_value=None),
    )

    asyncio.run(dispatch_terms_change(db, tid, row.id, ig, adapter))
    db.refresh(row)
    assert adapter.update_coupon_terms.call_count == 1
    assert row.discount_value == "25.00"
    assert row.extra_metadata["terms_sync_status"] == "failed"


# ─────────────────────────────────────────────────────────────────────────────
# E. queue_terms_change – binding-change rejection, no replacement
# ─────────────────────────────────────────────────────────────────────────────

def test_queue_changed_integration_rejects_no_replacement():
    """Pending intent exists for ig1; attempt with ig2 → rejected, original intent intact."""
    db, tid = _make_db()
    ig1 = _make_integration(db, tid, store_id="STORE1")
    ig2 = _make_integration(db, tid, store_id="STORE2")
    row = _make_bound_coupon(db, tid, ig1, pid="99080")
    queue_terms_change(row, ig1, {"amount": "30.00"})
    op_id = row.extra_metadata["terms_sync_pending"]["operation_id"]

    with pytest.raises(CouponTermsError, match="integration_binding_unavailable"):
        queue_terms_change(row, ig2, {"amount": "30.00"})
    assert row.extra_metadata["terms_sync_pending"]["integration_id"] == ig1.id
    assert row.extra_metadata["terms_sync_pending"]["operation_id"] == op_id


# ─────────────────────────────────────────────────────────────────────────────
# F. provider_expiry_utc – ZoneInfo fallback; Decimal numeric watermark
# ─────────────────────────────────────────────────────────────────────────────

def test_provider_expiry_zoneinfo_unavailable_falls_back_to_riyadh():
    """ZoneInfo raises → _RIYADH UTC+3 fallback: midnight Riyadh = 21:00 UTC prev day."""
    import services.coupon_terms_sync as _cts
    _riyadh_tz = timezone(timedelta(hours=3))
    with patch.object(_cts, "_RIYADH", new=_riyadh_tz):
        with patch("services.coupon_terms_sync.ZoneInfo", side_effect=ZoneInfoNotFoundError("no zoneinfo")):
            result = provider_expiry_utc({"date": "2027-01-15 00:00:00", "timezone": "Asia/Riyadh"})
    assert result is not None
    assert result.hour == 21
    assert result.day == 14


def test_provider_expiry_zoneinfo_unavailable_unknown_zone_returns_none():
    """ZoneInfo raises for non-Riyadh zone → None (no fallback for unknown zones)."""
    import services.coupon_terms_sync as _cts
    _riyadh_tz = timezone(timedelta(hours=3))
    with patch.object(_cts, "_RIYADH", new=_riyadh_tz):
        with patch("services.coupon_terms_sync.ZoneInfo", side_effect=ZoneInfoNotFoundError("no zoneinfo")):
            result = provider_expiry_utc({"date": "2027-01-15 00:00:00", "timezone": "America/Unknown"})
    assert result is None


def test_reconcile_decimal_identical_formats_equal_watermark_no_canonical_get():
    """'25.00' vs '25.0' are numerically equal; equal watermark + matching actual
    expiry/type must not trigger canonical GET."""
    db, tid = _make_db()
    ig = _make_integration(db, tid)
    ts = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
    row = _make_bound_coupon(db, tid, ig, pid="99090", discount_value="25.00",
                             discount_type="fixed",
                             extra={"terms_source_updated_at": ts.isoformat()})

    raw = {"id": 99090, "code": "COUP1", "type": "fixed", "amount": "25.0",
           "updated_at": ts.isoformat()}
    observed_at = datetime.now(timezone.utc)
    binding = {"tenant_id": tid, "integration_id": ig.id, "store_id": "STORE1"}
    adapter = _make_adapter(tid, ig, get_result=raw)

    result_raw, delta = asyncio.run(
        reconcile_imported_terms(db, row, raw, observed_at, adapter, binding)
    )
    assert adapter.get_coupon_by_id.call_count == 0
