"""Generic commerce identity regression; fake provider, no external network."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from test_coupon_salla_two_way_sync import _make_db
from database.models import Coupon, Integration, Tenant
from services.store_sync import StoreSyncService
from services.coupon_salla_push import push_coupon_to_salla
from services.coupon_active_sync import dispatch_active_change, queue_active_change


class Provider:
    def __init__(self, tenant_id, integration_id=22, store_id="202"):
        self._tenant_id, self._integration_id, self.store_id = tenant_id, integration_id, store_id
        self.raw, self.puts = {}, []

    async def create_coupon(self, **kwargs):
        self.raw = dict(id=101, code=kwargs["code"], status="active", amount=1, type="percentage")
        return deepcopy(self.raw)

    async def get_coupon_by_id(self, coupon_id):
        assert coupon_id == "101"
        return deepcopy(self.raw)

    async def set_coupon_active(self, coupon_id, active):
        self.puts.append((coupon_id, active))
        self.raw["status"] = "active" if active else "inactive"


def setup():
    db, tid = _make_db()
    integration = Integration(id=22, tenant_id=tid, provider="salla", enabled=True,
                              external_store_id="202", config={"store_id": "202", "api_sync_enabled": True, "refresh_token": "fake"})
    db.add(integration)
    db.commit()
    provider = Provider(tid)
    svc = StoreSyncService(db, tid, integration_connection_id=22, adapter=provider)
    return db, tid, integration, provider, svc


def apply(db, svc, raw):
    result = asyncio.run(svc.sync_coupons(raw_list=[deepcopy(raw)], record_sync_meta=False))
    db.commit()
    return result


def test_create_then_case_edit_preserves_single_row_origin_and_fields():
    db, tid, integration, provider, svc = setup()
    row = Coupon(tenant_id=tid, code="SHOES123", source_type="manual", coupon_level="silver",
                 allocation_channel="campaign", description="Generic shoe promotion",
                 discount_type="percentage", discount_value="1",
                 expires_at=datetime(2027, 1, 1), extra_metadata={"source": "dashboard", "usage_limit": 1,
                     "usage_count": 0, "ai_allocatable": False, "merchant_note": "retain"})
    db.add(row)
    db.commit()
    ok, _ = asyncio.run(push_coupon_to_salla(db, tid, row, adapter=provider))
    assert ok
    db.commit()
    original_id = row.id
    provider.raw.update(code="shoes123", amount=2)
    assert apply(db, svc, provider.raw) == 1
    rows = db.query(Coupon).filter_by(tenant_id=tid).all()
    assert len(rows) == 1  # live failure: old code created a second lowercase row
    assert row.id == original_id and row.code == "SHOES123"
    assert row.discount_value == "2"
    assert row.source_type == "manual" and row.coupon_level == "silver" and row.allocation_channel == "campaign"
    assert row.extra_metadata["source"] == "dashboard"
    for key, value in {"usage_limit": 1, "usage_count": 0, "ai_allocatable": False, "merchant_note": "retain"}.items():
        assert row.extra_metadata[key] == value
    assert row.extra_metadata["salla_identity"] == {"tenant_id": tid, "integration_id": 22, "store_id": "202"}
    assert row.extra_metadata["salla_provider_code"] == "shoes123"
    for code, active in [("shoes123", False), ("SHOES123", True), ("Shoes123", False)]:
        provider.raw["code"] = code
        queue_active_change(row, integration, active)
        db.commit()
        asyncio.run(dispatch_active_change(db, tid, row.id, integration, provider))
        assert row.extra_metadata["active"] is active and row.extra_metadata["sync_status"] == "synced"
        # Replayed provider observations must update the same row and never re-send.
        for _ in range(3):
            apply(db, svc, provider.raw)
        assert db.query(Coupon).filter_by(tenant_id=tid).count() == 1
    assert provider.puts == [("101", False), ("101", True), ("101", False)]
    db.close()


def test_import_create_case_replay_disable_reenable():
    db, tid, integration, provider, svc = setup()
    for code, status, amount in [("GIFT123", "active", 1), ("gift123", "inactive", 2), ("Gift123", "active", 2)]:
        raw = dict(id=101, code=code, status=status, type="percentage", amount=amount)
        for _ in range(3):
            apply(db, svc, raw)
        row = db.query(Coupon).filter_by(tenant_id=tid).one()
        assert row.extra_metadata["active"] is (status == "active")
        assert row.source_type == "imported" and row.code == "GIFT123"
        assert row.discount_value == str(amount)
    db.close()


@pytest.mark.parametrize("conflict", ["other_provider", "unbound_local", "other_store", "duplicate_provider", "alias_disagreement"])
def test_ambiguous_identity_never_overwrites_or_creates(conflict):
    db, tid, integration, provider, svc = setup()
    binding = {"tenant_id": tid, "integration_id": 22, "store_id": "202"}
    meta = {"salla_coupon_id": "101", "source": "dashboard", "salla_identity": binding, "merchant_note": "keep"}
    if conflict == "other_provider": meta["salla_coupon_id"] = "999"
    if conflict == "unbound_local": meta = {"source": "dashboard"}
    if conflict == "other_store": meta["salla_identity"] = dict(binding, store_id="303", integration_id=23)
    if conflict == "alias_disagreement": meta["external_id"] = "999"
    row = Coupon(tenant_id=tid, code="SHOES123", source_type="manual", discount_value="9", extra_metadata=meta)
    db.add(row)
    if conflict == "duplicate_provider":
        db.add(Coupon(tenant_id=tid, code="shoes123", source_type="imported", extra_metadata=deepcopy(meta)))
    db.commit()
    before = [(r.id, r.code, r.discount_value, deepcopy(r.extra_metadata)) for r in db.query(Coupon).all()]
    assert apply(db, svc, dict(id=101, code="shoes123", amount=2, status="inactive")) == 0
    assert [(r.id, r.code, r.discount_value, r.extra_metadata) for r in db.query(Coupon).all()] == before
    db.close()


def test_identity_conflict_is_reported_as_partial_sync(monkeypatch):
    from unittest.mock import Mock
    db, tid, integration, provider, svc = setup()
    db.add(Coupon(tenant_id=tid, code="SHOES123", extra_metadata={"salla_coupon_id": "999"}))
    db.commit()
    record = Mock()
    monkeypatch.setattr(svc, "_record_coupon_sync_meta", record)
    result = asyncio.run(svc.sync_coupons(raw_list=[dict(id=101,code="shoes123",status="active")],
                         fetch_result={"ok": True, "partial": False}))
    assert result == 0
    assert record.call_args.kwargs["failure_class"] == "coupon_identity_conflict"
    assert record.call_args.kwargs["partial"] is True
    assert record.call_args.kwargs["fetch_ok"] is True  # fetch succeeded; identity failed
    db.close()


def test_other_tenant_same_provider_id_and_code_is_untouched():
    db, tid, integration, provider, svc = setup()
    other = Tenant(name="Other generic merchant", is_active=True)
    db.add(other)
    db.flush()
    foreign = Coupon(tenant_id=other.id, code="shoes123", discount_value="99", extra_metadata={"salla_coupon_id": "101", "active": True})
    db.add(foreign)
    db.commit()
    apply(db, svc, dict(id=101, code="SHOES123", amount=2, status="inactive"))
    own = db.query(Coupon).filter_by(tenant_id=tid).one()
    assert own.id != foreign.id and own.extra_metadata["active"] is False
    assert foreign.discount_value == "99" and foreign.extra_metadata == {"salla_coupon_id": "101", "active": True}
    db.close()


def test_legacy_case_adoption_refuses_multiple_stores():
    db, tid, integration, provider, svc = setup()
    db.add(Integration(id=23, tenant_id=tid, provider="salla", enabled=True, external_store_id="303", config={}))
    db.add(Coupon(tenant_id=tid, code="SHOES123", extra_metadata={"salla_coupon_id": "101"}))
    db.commit()
    assert apply(db, svc, dict(id=101, code="shoes123", amount=2, status="inactive")) == 0
    assert db.query(Coupon).count() == 1
    db.close()


@pytest.mark.parametrize("code", ["SHOES123", "shoes123"])
def test_legacy_dispatch_refuses_multiple_stores_even_exact_code(code):
    db, tid, integration, provider, svc = setup()
    db.add(Integration(id=23, tenant_id=tid, provider="salla", enabled=True, external_store_id="303", config={}))
    row = Coupon(tenant_id=tid, code="SHOES123", extra_metadata={"salla_coupon_id": "101", "active": True})
    db.add(row)
    db.commit()
    provider.raw = dict(id=101, code=code, status="active")
    queue_active_change(row, integration, False)
    db.commit()
    asyncio.run(dispatch_active_change(db, tid, row.id, integration, provider))
    assert provider.puts == [] and row.extra_metadata["sync_error"] == "legacy_store_ambiguous"
    assert "salla_identity" not in row.extra_metadata
    db.close()


def test_existing_duplicates_block_dispatch_without_deleting_history():
    db, tid, integration, provider, svc = setup()
    meta = {"salla_coupon_id": "101", "active": True}
    row = Coupon(tenant_id=tid, code="SHOES123", extra_metadata=deepcopy(meta))
    db.add_all([row, Coupon(tenant_id=tid, code="shoes123", extra_metadata=deepcopy(meta))])
    db.commit()
    provider.raw = dict(id=101, code="shoes123", status="active")
    queue_active_change(row, integration, False)
    db.commit()
    asyncio.run(dispatch_active_change(db, tid, row.id, integration, provider))
    assert provider.puts == [] and row.extra_metadata["sync_error"] == "coupon_identity_ambiguous"
    assert db.query(Coupon).count() == 2
    db.close()


@pytest.mark.parametrize("left,right", [("STRASSE", "straße"), ("A1", "Ａ1"), ("A1", "A1 "), ("Ä1", "ä1"), ("A-1", "A1")])
def test_normalization_does_not_collapse_other_identifiers(left, right):
    from services.coupon_identity import same_code
    assert not same_code(left, right)


@pytest.mark.parametrize("marker", [None, {}, {"version":999}])
def test_retirement_marker_rejects_operations_even_with_legacy_active_flags(marker):
    from services.native_ai_coupon_eligibility import explicit_ai_allocatable, is_native_ai_allocatable_coupon
    from services.coupon_salla_push import is_pushable_manual_coupon
    from services.coupon_identity import CouponIdentityConflict
    db,tid,integration,provider,_ = setup()
    row = Coupon(tenant_id=tid,code="FROZEN",source_type="manual",coupon_level="silver",allocation_channel="ai",
        extra_metadata={"active":True,"ai_allocatable":True,"usage_limit":1,"salla_retirement":marker})
    assert not explicit_ai_allocatable(row.extra_metadata)
    assert not is_native_ai_allocatable_coupon(row,tenant_id=tid,resolved_level="silver")
    assert not is_pushable_manual_coupon(row)
    assert not asyncio.run(push_coupon_to_salla(db,tid,row,adapter=provider))[0]
    with pytest.raises(CouponIdentityConflict,match="coupon_retired"):
        queue_active_change(row,integration,True)
    assert provider.raw == {} and provider.puts == []
    db.close()


@pytest.mark.parametrize("change", ["tenant", "store", "integration", "disabled", "config_store"])
def test_import_source_binding_mismatch_fails_before_any_write(change):
    from services.coupon_identity import CouponIdentityConflict
    db, tid, integration, provider, svc = setup()
    if change == "tenant": provider._tenant_id = tid + 1
    if change == "store": provider.store_id = "303"
    if change == "integration": provider._integration_id = 999
    if change == "disabled": integration.enabled = False
    if change == "config_store": integration.config = {"store_id": "303"}
    db.commit()
    with pytest.raises(CouponIdentityConflict):
        apply(db, svc, dict(id=101, code="SHOES123", amount=2, status="inactive"))
    assert db.query(Coupon).count() == 0 and provider.puts == []
    db.close()


@pytest.mark.parametrize("channel", ["salla", "salla_oauth"])
def test_coupon_webhook_replay_is_currently_noop_not_coupon_sync(monkeypatch, channel):
    """Exercise actual persistence/dispatcher; coupon events have no handler today.

    This proves duplicate intake is harmless, NOT webhook-driven coupon sync.
    Poll payload replay is covered separately above; no routing added here.
    """
    import json
    from core.webhook_events import persist_event
    from core.webhook_dispatcher import _process_event
    from database.models import WebhookEvent
    from services.salla_integration_resolver import ResolvedSallaIntegration
    db, tid, integration, provider, svc = setup()
    apply(db, svc, dict(id=101, code="SHOES123", amount=1, status="active"))
    # Model create_all omits migration0023's partial unique index. Install that
    # existing queue contract in this SQLite fixture (not a runtime change).
    from sqlalchemy import text
    db.execute(text("CREATE UNIQUE INDEX uq_webhook_events_provider_event ON webhook_events (provider, external_event_id) WHERE external_event_id IS NOT NULL"))
    db.commit()
    monkeypatch.setattr("services.salla_integration_resolver.resolve_salla_integration_connection",
                        lambda *a, **k: ResolvedSallaIntegration(integration_id=22, tenant_id=tid, matched_via="test"))
    payload = {"event": "coupon.updated", "merchant": "202", "data": {"id": 101, "code": "shoes123", "status": "inactive"}}
    ids = []
    for _ in range(3):
        event = persist_event(db, provider=channel, raw_body=json.dumps(payload), parsed_payload=payload,
                              event_type="coupon.updated", external_event_id="generic-coupon-event", store_id="202", tenant_id=tid)
        ids.append(event.id)
        asyncio.run(_process_event(db, event))
        assert event.status == "processed"
    assert len(set(ids)) == 1 and db.query(WebhookEvent).count() == 1
    row = db.query(Coupon).filter_by(tenant_id=tid).one()
    assert row.code == "SHOES123" and row.extra_metadata["active"] is True
    assert provider.puts == []
    db.close()
