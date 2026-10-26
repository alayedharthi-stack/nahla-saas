"""Generic regression cases for business-time order reads and Salla identities."""
import asyncio
import socket
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import pytest

from models import Base, Coupon, Integration, Order, WhatsAppConnection
from core.local_order_resolver import read_customer_order_history, order_business_created_at
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.tools.orders import list_customer_orders_impl, resolve_customer_order_impl
from services.coupon_identity import CouponIdentityConflict, resolve_import_coupon, same_code
from services.coupon_active_sync import queue_active_change, dispatch_active_change
from services.store_sync import StoreSyncService
from routers import coupons as coupon_routes
from tests.commerce_scenario_fixtures import (
    DEFAULT_PHONE_E164, make_scenario_db, seed_tenant, seed_customer, seed_conversation, seed_order,
)


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("NAHLA_ACCEPTANCE_PG_URL")
    if not url:
        yield None
        return
    # An explicit synthetic test target, never an ambient production URL.
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if parsed.host != "127.0.0.1" or parsed.database != "acceptance_test" or parsed.username != "acceptance":
        raise ValueError("isolated_acceptance_database_required")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def world(monkeypatch, request, pg_engine):
    original_connect = socket.socket.connect
    def local_only(sock, address):
        # Windows asyncio creates its self-pipe over loopback.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("external network forbidden")
    monkeypatch.setattr("socket.socket.connect", local_only)
    outer = pg_connection = None
    if request.param == "postgresql":
        if pg_engine is None:
            pytest.skip("NAHLA_ACCEPTANCE_PG_URL not supplied")
        engine = pg_engine
        pg_connection = engine.connect()
        outer = pg_connection.begin()
        db = Session(bind=pg_connection, join_transaction_mode="create_savepoint")
    else:
        db, engine = make_scenario_db()
    tenant = seed_tenant(db, name="Generic clothing store")
    customer = seed_customer(db, tenant.id, name="Synthetic customer")
    conversation = seed_conversation(db, tenant.id, customer_id=customer.id)
    whatsapp_connection = db.query(WhatsAppConnection).filter_by(tenant_id=tenant.id).one()
    context = CommerceAgentContext.from_trusted_scope(
        db=db, tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
        normalized_customer_phone=DEFAULT_PHONE_E164, connection_id=str(whatsapp_connection.id), inbound_trace_id="synthetic-test",
    )
    yield SimpleNamespace(db=db, tenant=tenant, customer=customer, context=context)
    db.close()
    if outer is not None:
        outer.rollback()
        pg_connection.close()
    else:
        engine.dispose()


def order(w, reference, timestamp, status="payment_pending"):
    row = seed_order(w.db, w.tenant.id, external_id="synthetic-" + reference,
                     external_order_number=reference, source="salla", status=status,
                     customer_info={"phone": DEFAULT_PHONE_E164},
                     extra_metadata={"salla_created_at": timestamp} if timestamp else {})
    row.customer_id = w.customer.id
    # The common seeder adds its own current created_at; this test deliberately
    # controls the actual persisted chronology, including unknown/malformed cases.
    row.extra_metadata = {"salla_created_at": timestamp} if timestamp else {}
    w.db.commit()
    return row


def history(w, limit=50):
    return read_customer_order_history(w.db, tenant_id=w.tenant.id, customer_id=w.customer.id,
                                       phone=DEFAULT_PHONE_E164, limit=limit)


def test_provider_time_beats_late_import_and_read_limit(world):
    newest = order(world, "NEW-ORDER", "2026-10-25T21:16:00Z")
    older = order(world, "OLD-ORDER", "2026-10-20T20:00:00Z", "delivered")
    assert older.id > newest.id
    rows, complete = history(world, limit=1)
    assert [r.id for r in rows] == [newest.id]
    assert not complete
    assert rows.chronology_complete


def test_history_exact_limit_is_complete_and_provider_date_has_priority(world):
    row = order(world, "ONE", "2026-10-25T21:16:00Z")
    row.extra_metadata = {"salla_created_at": "2026-10-25T21:16:00Z",
                          "created_at": "2026-10-30T21:16:00Z"}
    world.db.commit()
    rows, complete = history(world, limit=1)
    assert complete and rows.chronology_complete
    assert order_business_created_at(rows[0]) == datetime(2026, 10, 25, 21, 16, tzinfo=timezone.utc)


def test_tool_latest_is_global_across_groups_and_explicit_lookup_unchanged(world):
    newest = order(world, "NEW-ORDER", "2026-10-25T21:16:00Z", "delivered")
    order(world, "OLD-ORDER", "2026-10-20T20:00:00Z")
    result = asyncio.run(list_customer_orders_impl(world.context))
    assert result.latest_order.order_reference == "NEW-ORDER"
    assert result.latest_order.created_at == "2026-10-25T21:16:00+00:00"
    assert result.finished.orders[0].evidence_ref == result.latest_order.evidence_ref
    assert result.ongoing.orders[0].order_reference == "OLD-ORDER"
    selected = asyncio.run(resolve_customer_order_impl(world.context, order_number="NEW-ORDER"))
    assert selected.status == "ok"
    assert selected.order.order_reference == "NEW-ORDER"
    assert newest.id in world.context.authorized_order_ids


def test_offsets_microseconds_and_ties(world):
    first = order(world, "FIRST", "2026-10-26T00:00:00.000002+03:00")
    older = order(world, "OLDER", "2026-10-25T21:00:00.000001Z")
    tie = order(world, "TIE", "2026-10-25T21:00:00.000002Z")
    rows, _ = history(world)
    assert [r.id for r in rows] == [tie.id, first.id, older.id]


@pytest.mark.parametrize("stamp", [None, "not-a-date", "2026-99-99T00:00:00Z"])
def test_missing_import_chronology_does_not_claim_latest(world, stamp):
    order(world, "KNOWN", "2026-10-25T21:16:00Z")
    order(world, "UNKNOWN", stamp)
    result = asyncio.run(list_customer_orders_impl(world.context))
    assert result.status == "ok"
    assert result.latest_order is None
    assert result.latest_order_unavailable_reason == "order_chronology_unverified"
    assert result.total_orders == 2


def test_missing_creation_metadata_never_infers_business_time_from_insertion():
    value = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert order_business_created_at(SimpleNamespace(extra_metadata={"created_at": value})) == value
    assert order_business_created_at(SimpleNamespace(source="manual", created_at=value)) is None
    assert order_business_created_at(SimpleNamespace(source="salla", created_at=value)) is None


def test_history_keeps_tenant_customer_and_cart_exclusions(world):
    expected = order(world, "OURS", "2026-10-20T00:00:00Z")
    cart = order(world, "CART", "2026-10-29T00:00:00Z")
    cart.is_abandoned = True
    another = seed_tenant(world.db, name="Generic cosmetics store")
    alien = seed_order(world.db, another.id, external_id="other-store", customer_info={"phone": DEFAULT_PHONE_E164},
                       source="salla", extra_metadata={"salla_created_at": "2026-10-30T00:00:00Z"})
    world.db.commit()
    rows, _ = history(world)
    assert [r.id for r in rows] == [expected.id]


def coupon_scope(w):
    integration = Integration(tenant_id=w.tenant.id, provider="salla", enabled=True,
                              external_store_id="synthetic-store", config={"store_id": "synthetic-store",
                                  "api_sync_enabled": True, "refresh_token": "PUBLIC_SYNTHETIC_NOT_SECRET"})
    w.db.add(integration)
    w.db.commit()
    binding = {"tenant_id": w.tenant.id, "integration_id": integration.id, "store_id": "synthetic-store"}
    adapter = SimpleNamespace(_tenant_id=w.tenant.id, _integration_id=integration.id, store_id="synthetic-store")
    return integration, binding, adapter


def manual(w, binding, code="COAT10", provider="101"):
    row = Coupon(tenant_id=w.tenant.id, code=code, discount_type="percentage", discount_value="1",
                 source_type="manual", extra_metadata={"source": "dashboard", "salla_coupon_id": provider,
                     "external_id": provider, "salla_identity": dict(binding), "active": True, "usage_limit": 1})
    w.db.add(row)
    w.db.commit()
    return row


def test_case_variant_import_updates_same_bound_identity_and_origin(world):
    integration, binding, adapter = coupon_scope(world)
    row = manual(world, binding)
    svc = StoreSyncService(world.db, world.tenant.id, integration_connection_id=integration.id, adapter=adapter)
    raw = {"id": 101, "code": "coat10", "type": "percentage", "amount": 2, "status": "inactive",
           "expiry_date": "2026-10-27T00:00:00+03:00", "usage_limit": 1}
    for _ in range(2):
        assert asyncio.run(svc.sync_coupons(raw_list=[raw], record_sync_meta=False)) == 1
        world.db.commit()
    world.db.refresh(row)
    assert world.db.query(Coupon).count() == 1
    assert row.code == "COAT10" and row.discount_value == "2"
    assert row.source_type == "manual" and row.extra_metadata["source"] == "dashboard"
    assert row.extra_metadata["active"] is False
    assert row.extra_metadata["usage_limit"] == 1


@pytest.mark.parametrize("mutation", ["provider", "store", "alias", "duplicate", "unbound"])
def test_identity_conflicts_never_merge_or_attach_rows(world, mutation):
    _, binding, _ = coupon_scope(world)
    row = manual(world, binding)
    raw = {"id": 101, "code": "coat10"}
    if mutation == "provider": raw["id"] = 202
    if mutation == "store": binding = dict(binding, store_id="different-store")
    if mutation == "alias": row.extra_metadata = dict(row.extra_metadata, external_id="202")
    if mutation == "unbound": row.extra_metadata = {"source": "dashboard"}
    if mutation == "duplicate": manual(world, binding, code="coat10")
    world.db.commit()
    before = world.db.query(Coupon).count()
    with pytest.raises(CouponIdentityConflict):
        resolve_import_coupon(world.db, world.tenant.id, raw, binding)
    assert world.db.query(Coupon).count() == before
    assert row.discount_value == "1"


def test_non_ascii_lookalikes_are_not_case_aliases():
    assert same_code("CoAt10", "coat10")
    assert not same_code("COAT10", " COAT10")
    assert not same_code("STRASSE", "Straße")
    assert not same_code("COAT10", "СOAT10")  # Cyrillic initial, not Latin.


def test_other_store_cannot_adopt_case_variant_even_under_same_tenant(world):
    _, binding, _ = coupon_scope(world)
    manual(world, binding, code="COAT10")
    other = Integration(tenant_id=world.tenant.id, provider="salla", enabled=True,
                        external_store_id="second-store", config={"store_id": "second-store"})
    world.db.add(other)
    world.db.commit()
    other_binding = {"tenant_id": world.tenant.id, "integration_id": other.id, "store_id": "second-store"}
    with pytest.raises(CouponIdentityConflict, match="coupon_identity_ambiguous"):
        resolve_import_coupon(world.db, world.tenant.id, {"id": 202, "code": "coat10"}, other_binding)
    assert world.db.query(Coupon).count() == 1


def test_active_change_accepts_only_same_id_ascii_case_and_confirms(world):
    integration, binding, adapter = coupon_scope(world)
    row = manual(world, binding)
    adapter.get_coupon_by_id = AsyncMock(side_effect=[
        {"id": 101, "code": "coat10", "status": "active"},
        {"id": 101, "code": "coat10", "status": "inactive"},
    ])
    adapter.set_coupon_active = AsyncMock()
    queue_active_change(row, integration, False)
    world.db.commit()
    asyncio.run(dispatch_active_change(world.db, world.tenant.id, row.id, integration, adapter))
    assert row.extra_metadata["active"] is False
    assert row.extra_metadata["sync_status"] == "synced"
    adapter.set_coupon_active.assert_awaited_once_with("101", False)


def test_wrong_provider_readback_does_not_dispatch(world):
    integration, binding, adapter = coupon_scope(world)
    row = manual(world, binding)
    adapter.get_coupon_by_id = AsyncMock(return_value={"id": 999, "code": "coat10", "status": "active"})
    adapter.set_coupon_active = AsyncMock()
    queue_active_change(row, integration, False)
    world.db.commit()
    asyncio.run(dispatch_active_change(world.db, world.tenant.id, row.id, integration, adapter))
    assert row.extra_metadata["sync_error"] == "provider_coupon_identity_mismatch"
    adapter.set_coupon_active.assert_not_awaited()


@pytest.mark.parametrize("expiry", ["2027-03-02T00:30:00.000Z", "2027-03-01T21:30:00.000Z"])
def test_coupon_api_persists_submitted_expiry_without_default(world, monkeypatch, expiry):
    monkeypatch.setattr(coupon_routes, "resolve_tenant_id", lambda _request: world.tenant.id)
    monkeypatch.setattr(coupon_routes, "evaluate_salla_coupon_sync_readiness", lambda *_: {
        "full_api_ready": False, "adapter_ready": False, "reason": "synthetic_offline",
    })
    body = coupon_routes.CouponCreateIn(code="DATECASE", value="2", expires=expiry)
    result = asyncio.run(coupon_routes.create_coupon(body, SimpleNamespace(), world.db))
    saved = world.db.query(Coupon).filter_by(id=result["id"], tenant_id=world.tenant.id).one()
    stored = saved.expires_at
    # Both test dialects use the model's naive timestamp column. Its stored
    # clock must be UTC, independent of the PostgreSQL session time zone.
    stored = stored.replace(tzinfo=timezone.utc) if stored.tzinfo is None else stored.astimezone(timezone.utc)
    assert stored == datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    assert saved.code == "DATECASE"
    later = "2027-03-03T05:45:00.000Z"
    asyncio.run(coupon_routes.patch_coupon(
        saved.id, coupon_routes.CouponPatchIn(expires=later), SimpleNamespace(), world.db,
    ))
    world.db.refresh(saved)
    changed = saved.expires_at
    changed = changed.replace(tzinfo=timezone.utc) if changed.tzinfo is None else changed.astimezone(timezone.utc)
    assert changed == datetime.fromisoformat(later.replace("Z", "+00:00"))


def test_coupon_api_rejects_case_variant_in_same_tenant(world, monkeypatch):
    monkeypatch.setattr(coupon_routes, "resolve_tenant_id", lambda _request: world.tenant.id)
    manual(world, {"tenant_id": world.tenant.id, "integration_id": 1, "store_id": "synthetic"}, code="COAT10")
    with pytest.raises(HTTPException) as error:
        asyncio.run(coupon_routes.create_coupon(
            coupon_routes.CouponCreateIn(code="coat10", value="1"), SimpleNamespace(), world.db,
        ))
    assert error.value.status_code == 409


def test_concurrent_api_creates_serialize_case_variants(pg_engine, monkeypatch):
    if pg_engine is None:
        pytest.skip("isolated PostgreSQL target required")
    with Session(pg_engine) as db:
        tenant = seed_tenant(db, name="Synthetic concurrent clothing store")
        tenant_id = tenant.id
        db.commit()
    monkeypatch.setattr(coupon_routes, "resolve_tenant_id", lambda _request: tenant_id)
    monkeypatch.setattr(coupon_routes, "evaluate_salla_coupon_sync_readiness", lambda *_: {
        "full_api_ready": False, "adapter_ready": False, "reason": "synthetic_offline",
    })
    ready = Barrier(2)

    def create(code):
        with Session(pg_engine) as db:
            ready.wait(timeout=10)
            try:
                asyncio.run(coupon_routes.create_coupon(
                    coupon_routes.CouponCreateIn(code=code, value="1"), SimpleNamespace(), db,
                ))
                return 201
            except HTTPException as exc:
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=15) for future in (
            pool.submit(create, "CASELOCK"), pool.submit(create, "caselock"),
        )]
    assert sorted(results) == [201, 409]
    with Session(pg_engine) as db:
        assert db.query(Coupon).filter_by(tenant_id=tenant_id).count() == 1
