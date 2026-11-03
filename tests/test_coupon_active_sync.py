"""Status synchronization regression: provider fakes only, no network."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_coupon_salla_two_way_sync import _make_db
from database.models import Coupon, Integration
from services.coupon_active_sync import (
    dispatch_active_change, imported_state, normalized_active, queue_active_change,
)
from services.coupon_sync_visibility import derive_sync_badge
from services.store_sync import StoreSyncService


def setup_state():
    db, tid = _make_db()
    coupon = Coupon(tenant_id=tid, code="STATUS1", source_type="manual",
                    extra_metadata={"source": "dashboard", "salla_coupon_id": "101",
                                    "salla_identity": {"tenant_id": tid, "integration_id": 22, "store_id": "202"},
                                    "active": True, "sync_status": "synced", "salla_synced": True})
    db.add(coupon)
    db.add(Integration(id=22, tenant_id=tid, provider="salla", enabled=True,
                       external_store_id="202", config={"store_id": "202"}))
    db.commit()
    integration = SimpleNamespace(id=22, tenant_id=tid, external_store_id="202", provider="salla",
                                  enabled=True, config={"api_sync_enabled": True, "refresh_token": "fake"})
    adapter = SimpleNamespace(_tenant_id=tid, _integration_id=22, store_id="202")
    adapter.get_coupon_by_id = AsyncMock(return_value={"id": 101, "code": "STATUS1", "status": "inactive"})
    adapter.set_coupon_active = AsyncMock()
    return db, coupon, integration, adapter


@pytest.mark.parametrize("raw,expected", [({"status": "active"}, True), ({"status": "inactive"}, False),
    ({"active": False}, False), ({"status": " ACTIVE "}, True), ({}, None), ({"active": "false"}, None)])
def test_normalization(raw, expected):
    assert normalized_active(raw) is expected


def test_source_latency_requires_known_timezone():
    from services.coupon_active_sync import timestamp
    assert timestamp("2026-10-17 20:00:00") is None
    assert timestamp({"date": "2026-10-17 20:00:00", "timezone": "Asia/Riyadh"}).hour == 17


def test_disable_and_reenable_confirm_before_changing_active():
    db, row, integration, adapter = setup_state()
    queue_active_change(row, integration, False)
    db.commit()
    assert row.extra_metadata["active"] is True
    assert derive_sync_badge("manual", row.extra_metadata) == "pending"
    asyncio.run(dispatch_active_change(db, row.tenant_id, row.id, integration, adapter))
    assert row.extra_metadata["active"] is False
    assert row.extra_metadata["sync_status"] == "synced"
    # GET confirmed a timed-out prior PUT or already converged state: no repeat PUT.
    adapter.set_coupon_active.assert_not_called()
    queue_active_change(row, integration, True)
    db.commit()
    adapter.get_coupon_by_id.side_effect = [{"id": 101, "code": row.code, "status": "inactive"},
                                          {"id": 101, "code": row.code, "status": "active"}]
    asyncio.run(dispatch_active_change(db, row.tenant_id, row.id, integration, adapter))
    adapter.set_coupon_active.assert_awaited_once_with("101", True)
    assert row.extra_metadata["active"] is True
    assert row.extra_metadata["active_api_to_apply_ms"] >= 0
    assert row.extra_metadata["active_request_to_apply_ms"] >= 0
    db.close()


def test_provider_failure_durable_retry_and_duplicate_intent():
    db, row, integration, adapter = setup_state()
    queue_active_change(row, integration, False)
    db.commit()
    operation = row.extra_metadata["active_sync_pending"]["operation_id"]
    queue_active_change(row, integration, False)
    assert row.extra_metadata["active_sync_pending"]["operation_id"] == operation
    adapter.get_coupon_by_id.side_effect = TimeoutError("private-body-must-not-leak")
    asyncio.run(dispatch_active_change(db, row.tenant_id, row.id, integration, adapter))
    assert row.extra_metadata["active"] is True
    assert row.extra_metadata["sync_status"] == "failed"
    assert row.extra_metadata["salla_synced"] is False
    assert row.extra_metadata["sync_error"] == "TimeoutError"
    assert row.extra_metadata["active_sync_pending"]["attempts"] == 1
    adapter.get_coupon_by_id.side_effect = None
    before = adapter.get_coupon_by_id.await_count
    asyncio.run(dispatch_active_change(db, row.tenant_id, row.id, integration, adapter))
    assert adapter.get_coupon_by_id.await_count == before  # backoff honored
    meta = dict(row.extra_metadata)
    meta["active_sync_pending"] = dict(meta["active_sync_pending"], next_attempt_at="2000-01-01T00:00:00Z")
    row.extra_metadata = meta
    db.commit()
    asyncio.run(dispatch_active_change(db, row.tenant_id, row.id, integration, adapter))
    assert row.extra_metadata["active"] is False
    assert "active_sync_pending" not in row.extra_metadata
    # Replayed completion is a no-op.
    before = adapter.get_coupon_by_id.await_count
    asyncio.run(dispatch_active_change(db, row.tenant_id, row.id, integration, adapter))
    assert adapter.get_coupon_by_id.await_count == before
    db.close()


@pytest.mark.parametrize("mismatch", ["tenant", "adapter", "store", "integration", "coupon"])
def test_binding_mismatch_never_writes_provider(mismatch):
    db, row, integration, adapter = setup_state()
    queue_active_change(row, integration, False)
    db.commit()
    if mismatch == "tenant": integration.tenant_id += 1
    if mismatch == "adapter": adapter._tenant_id += 1
    if mismatch == "store": integration.external_store_id = "999"
    if mismatch == "integration": integration.id += 1
    if mismatch == "coupon": adapter.get_coupon_by_id.return_value = {"id": 999, "code": row.code, "status": "active"}
    asyncio.run(dispatch_active_change(db, row.tenant_id, row.id, integration, adapter))
    adapter.set_coupon_active.assert_not_called()
    assert row.extra_metadata["active"] is True
    assert row.extra_metadata["sync_status"] == "failed"
    db.close()


def test_tenant_query_cannot_dispatch_other_tenant_coupon():
    db, row, integration, adapter = setup_state()
    queue_active_change(row, integration, False)
    db.commit()
    asyncio.run(dispatch_active_change(db, row.tenant_id + 1, row.id, integration, adapter))
    adapter.get_coupon_by_id.assert_not_called()
    db.close()


def test_stale_duplicate_and_pending_observations_do_not_overwrite_state():
    now = datetime.now(timezone.utc)
    meta = {"active": False, "active_source_updated_at": now.isoformat()}
    for raw in [{"status": "active", "updated_at": (now - timedelta(seconds=1)).isoformat()}]:
        assert imported_state(raw, meta, now) == {}
    assert imported_state({"status": "active"}, {"active_applied_at": now.isoformat()}, now - timedelta(seconds=1)) == {}
    assert imported_state({"status": "active"}, {"active_sync_pending": {"desired": False}}, now) == {}


def test_equal_second_conflicting_event_reads_current_provider_detail():
    from services.coupon_active_sync import reconcile_imported_state
    now = datetime.now(timezone.utc)
    existing = {"active": False, "active_source_updated_at": now.isoformat()}
    raw = {"id": 101, "code": "STATUS1", "status": "active", "updated_at": now.isoformat()}
    adapter = SimpleNamespace(get_coupon_by_id=AsyncMock(return_value=dict(raw, status="inactive")))
    state = asyncio.run(reconcile_imported_state(raw, existing, now, adapter))
    assert state["active"] is False
    adapter.get_coupon_by_id.return_value = raw
    assert asyncio.run(reconcile_imported_state(raw, existing, now, adapter))["active"] is True
    adapter.get_coupon_by_id.side_effect = TimeoutError()
    assert asyncio.run(reconcile_imported_state(raw, existing, now, adapter))["sync_status"] == "failed"


def test_retry_filters_backoff_before_batch_cap(monkeypatch):
    from services.coupon_active_sync import retry_active_changes
    db, first, integration, adapter = setup_state()
    queue_active_change(first, integration, False)
    meta = dict(first.extra_metadata)
    meta["active_sync_pending"] = dict(meta["active_sync_pending"], next_attempt_at="2099-01-01T00:00:00Z")
    first.extra_metadata = meta
    for idx in range(21):
        db.add(Coupon(tenant_id=first.tenant_id, code=f"WAIT{idx}", extra_metadata=meta))
    last = Coupon(tenant_id=first.tenant_id, code="DUE-LAST", extra_metadata={"active": True, "salla_coupon_id": "102"})
    db.add(last)
    db.flush()
    queue_active_change(last, integration, False)
    db.commit()
    dispatch = AsyncMock()
    monkeypatch.setattr("services.coupon_active_sync.dispatch_active_change", dispatch)
    monkeypatch.setattr("store_integration.registry.adapter_for_integration", lambda _: adapter)
    asyncio.run(retry_active_changes(db, integration))
    assert dispatch.await_count == 1
    assert dispatch.await_args.args[2] == last.id
    db.close()


def test_invalid_patch_does_not_commit_partial_fields_or_housekeep(monkeypatch):
    from fastapi import HTTPException
    from routers.coupons import patch_coupon, CouponPatchIn
    db, row, integration, adapter = setup_state()
    # Two connections formerly caused the mutating picker to commit early.
    for iid in [23]:
        db.add(Integration(id=iid, tenant_id=row.tenant_id, provider="salla", enabled=True,
                           external_store_id=str(iid), config={}))
    db.commit()
    monkeypatch.setattr("routers.coupons.resolve_tenant_id", lambda _: row.tenant_id)
    def forbidden(*args, **kwargs):
        raise AssertionError("mutating_picker_must_not_run")
    monkeypatch.setattr("store_integration.registry.pick_active_salla_integration", forbidden)
    with pytest.raises(HTTPException):
        asyncio.run(patch_coupon(row.id, CouponPatchIn(code="PARTIAL", active=False, coupon_level="invalid"), None, db))
    db.rollback()
    db.refresh(row)
    assert row.code == "STATUS1"
    assert row.extra_metadata["active"] is True
    assert all(item.enabled for item in db.query(Integration).all())
    db.close()


def test_status_adapter_sends_only_status_and_propagates_failure(monkeypatch):
    import httpx
    from store_adapters.salla_adapter import SallaAdapter
    adapter = SallaAdapter(api_key="fake-test-only", store_id="202", tenant_id=1, integration_id=22)
    adapter._ensure_token_fresh = AsyncMock()
    request = httpx.Request("PUT", "https://api.salla.dev/admin/v2/coupons/101")
    responses = [httpx.Response(200, request=request), httpx.Response(422, request=request)]
    seen = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def put(self, url, **kwargs):
            seen.append((url, kwargs["json"]))
            return responses.pop(0)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    asyncio.run(adapter.set_coupon_active("101", False))
    assert seen[0][1] == {"status": "inactive"}
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.set_coupon_active("101", True))
    assert seen[1][1] == {"status": "active"}


def test_terms_adapter_sends_only_requested_fields_and_propagates_failure(monkeypatch):
    import json
    import httpx
    from store_adapters.salla_adapter import SallaAdapter

    adapter = SallaAdapter(api_key="fake-test-only", store_id="202", tenant_id=1, integration_id=22)
    adapter._ensure_token_fresh = AsyncMock()
    request = httpx.Request("PUT", "https://api.salla.dev/admin/v2/coupons/101")
    responses = [httpx.Response(200, request=request), httpx.Response(422, request=request)]
    seen = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def put(self, url, **kwargs):
            seen.append((url, json.loads(kwargs["content"])))
            return responses.pop(0)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    asyncio.run(adapter.update_coupon_terms("101", amount="2.5", expiry_date="2027-03-02"))
    assert seen[0][1] == {"amount": 2.5, "expiry_date": "2027-03-02"}
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.update_coupon_terms("101", amount="3"))
    assert seen[1][1] == {"amount": 3}


@pytest.mark.parametrize("origin", ["manual", "imported", "system"])
def test_poll_imports_active_both_directions_without_duplicate_or_pending_override(origin):
    db, row, integration, adapter = setup_state()
    row.source_type = origin
    db.commit()
    svc = StoreSyncService(db, row.tenant_id, adapter=adapter)
    for active in [False, True, False]:
        raw = {"id": 101, "code": row.code, "status": "active" if active else "inactive", "amount": 1}
        asyncio.run(svc.sync_coupons(raw_list=[raw], record_sync_meta=False))
        db.commit()
        assert row.extra_metadata["active"] is active
    assert db.query(Coupon).filter_by(tenant_id=row.tenant_id, code=row.code).count() == 1
    queue_active_change(row, integration, True)
    db.commit()
    asyncio.run(svc.sync_coupons(raw_list=[raw], record_sync_meta=False))
    db.commit()
    assert row.extra_metadata["sync_status"] == "pending"
    assert row.extra_metadata["active_sync_pending"]["desired"] is True
    db.close()
