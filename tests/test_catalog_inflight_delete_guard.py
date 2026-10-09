"""Deletion/publish interleavings use the real services and fake Graph only.

SQLite proves state-machine and stale-session behavior, not PostgreSQL row-lock
blocking. Compiled SQL separately asserts FOR UPDATE (without SKIP LOCKED).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from database.models import CatalogChannelRetirement, MetaCatalogMembership, Product, ProductVariant
from core.catalog import OWNERSHIP_EXTERNAL_MANAGED
from services.catalog_deletion_guard import CatalogDeletionDeferred, assert_catalog_deletion_safe
from services.store_sync import StoreSyncService
from services.whatsapp_catalog_retirement import drain_channel_retirement_ledger
from test_meta_catalog_consent_native_sync import CATALOG, RID, _Resp, _sync, world  # noqa: F401


@pytest.fixture(autouse=True)
def no_background_drain(monkeypatch):
    monkeypatch.setattr("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain", lambda *_a, **_k: None)


def _salla(w):
    product = w.session.get(Product, w.pid)
    product.source = "salla"
    product.ownership_mode = OWNERSHIP_EXTERNAL_MANAGED
    product.external_id = "900100"
    variant = w.session.query(ProductVariant).filter_by(product_id=w.pid).one()
    variant.salla_variant_id = "881"
    variant.retailer_id = "900100-881"
    variant.is_default = False
    w.session.commit()
    return variant.retailer_id


def _manual_delete(db, w, monkeypatch):
    import routers.catalog as catalog

    monkeypatch.setattr(catalog, "resolve_tenant_id", lambda _req: w.tid)
    monkeypatch.setattr(catalog, "audit", lambda *_a, **_k: None)
    return asyncio.run(catalog.merchant_catalog_delete_manual_product(
        w.pid, SimpleNamespace(), db=db, _user={"tenant_id": w.tid},
    ))


def _source_event(db, w, monkeypatch):
    from core.webhook_events import persist_event
    from services import salla_integration_resolver as resolver

    monkeypatch.setattr(resolver, "resolve_salla_integration_connection", lambda *_a, **_k:
                        resolver.ResolvedSallaIntegration(1, w.tid, "test_exact_store"))
    return persist_event(
        db, provider="salla", raw_body=None, event_type="product.deleted",
        external_event_id="delete-900100", store_id="store-generic", tenant_id=w.tid,
        parsed_payload={"event": "product.deleted", "data": {"id": "900100"}},
    )


def _process(db, ev):
    from core.webhook_dispatcher import _process_event

    asyncio.run(_process_event(db, ev))


def _assert_retired(w, rid, db):
    rows = db.query(CatalogChannelRetirement).filter_by(tenant_id=w.tid).all()
    assert len(rows) == 1 and rows[0].retailer_id == rid
    assert rows[0].meta_item_id == w.graph.items[rid]["id"]
    result = drain_channel_retirement_ledger(db, w.tid, client=w.graph)
    assert result["retired"] == 1, result
    assert w.graph.items[rid]["availability"] == "out of stock"
    assert w.graph.items[rid]["visibility"] == "staging"


def test_source_create_first_defers_durable_delete_then_retries_and_retires(world, monkeypatch):
    from core import webhook_events as queue

    w = world
    rid = _salla(w)
    with sessionmaker(bind=w.session.get_bind())() as deletion:
        ev = _source_event(deletion, w, monkeypatch)
        post = w.graph.post
        deferred = []

        def interleave(url, **kwargs):
            response = post(url, **kwargs)
            if url.endswith(f"/{CATALOG}/products"):
                # Graph created the item, but the publisher has not yet received
                # its id. Only the prepublication Salla identity slot exists.
                membership = deletion.query(MetaCatalogMembership).filter_by(product_id=w.pid).one()
                assert membership.provenance == "salla_variant_slot"
                assert membership.meta_item_id is None
                _process(deletion, ev)
                deletion.refresh(ev)
                assert ev.status == "failed" and ev.attempts == 1
                assert ev.last_error == "CatalogDeletionDeferred"
                assert ev.processed_at is None and ev.next_retry_at is not None
                assert deletion.get(Product, w.pid) is not None
                assert deletion.query(CatalogChannelRetirement).count() == 0
                assert queue.claim_next_batch(deletion) == []
                deferred.append(True)
            return response

        monkeypatch.setattr(w.graph, "post", interleave)
        result = _sync(w)
        assert result["ok"] and deferred == [True], result
        monkeypatch.setattr(w.graph, "post", post)
        due = ev.next_retry_at.replace(tzinfo=timezone.utc) + timedelta(seconds=1)
        monkeypatch.setattr(queue, "_utcnow", lambda: due)
        ready = queue.claim_next_batch(deletion)
        assert [row.id for row in ready] == [ev.id]
        _process(deletion, ready[0])
        deletion.refresh(ev)
        assert ev.status == "processed"
        assert deletion.query(Product).filter_by(id=w.pid).count() == 0
        _assert_retired(w, rid, deletion)
        # Duplicate provider delivery is harmless and creates no new work.
        _process(deletion, ev)
        assert deletion.query(CatalogChannelRetirement).count() == 1


@pytest.mark.parametrize("manual", [False, True])
def test_delete_first_prevents_stale_publisher_from_creating(world, monkeypatch, manual):
    w = world
    if not manual:
        _salla(w)
    cached = w.session.get(Product, w.pid)
    assert cached.sync_status == "pending"
    with sessionmaker(bind=w.session.get_bind())() as deletion:
        if manual:
            assert _manual_delete(deletion, w, monkeypatch)["deleted"] is True
        else:
            asyncio.run(StoreSyncService(deletion, w.tid).handle_product_deleted("900100"))
        assert deletion.query(Product).filter_by(id=w.pid).count() == 0
        assert deletion.query(CatalogChannelRetirement).count() == 0
    result = _sync(w)
    assert result["error_code"] == "sync_lock_not_acquired", result
    assert w.graph.calls == [] and w.graph.items == {}


def test_manual_create_first_returns_retryable_409_then_deletes_and_retires(world, monkeypatch):
    w = world
    post = w.graph.post
    conflicts = []
    with sessionmaker(bind=w.session.get_bind())() as deletion:
        # Keep a stale pre-acquisition Product in the endpoint session.
        stale = deletion.get(Product, w.pid)
        assert stale.sync_status == "pending"

        def interleave(url, **kwargs):
            response = post(url, **kwargs)
            if url.endswith(f"/{CATALOG}/products"):
                with pytest.raises(HTTPException) as err:
                    _manual_delete(deletion, w, monkeypatch)
                assert err.value.status_code == 409
                assert err.value.detail == {
                    "code": "catalog_deletion_deferred", "reason": "publication_in_flight",
                    "retryable": True, "retry_after_seconds": 60,
                }
                assert err.value.headers == {"Retry-After": "60"}
                assert deletion.get(Product, w.pid) is not None
                assert deletion.query(CatalogChannelRetirement).count() == 0
                conflicts.append(True)
            return response

        monkeypatch.setattr(w.graph, "post", interleave)
        result = _sync(w)
        assert result["ok"] and conflicts == [True], result
        monkeypatch.setattr(w.graph, "post", post)
        assert _manual_delete(deletion, w, monkeypatch)["deleted"] is True
        _assert_retired(w, RID, deletion)


def test_unresolved_successful_create_survives_delete_until_corroborated(world, monkeypatch):
    w = world
    rid = _salla(w)
    get = w.graph.get

    def lagging_lookup(url, **kwargs):
        if w.graph.items and "/products" in url:
            return _Resp(200, {"data": []})
        return get(url, **kwargs)

    monkeypatch.setattr(w.graph, "get", lagging_lookup)
    result = _sync(w)
    assert result["ok"] is False and result["error_code"] == "verification_failed", result
    product = w.session.get(Product, w.pid)
    pending = product.extra_metadata["sync_meta"]["pending_publications"]
    assert pending and product.sync_status != "syncing"
    with pytest.raises(CatalogDeletionDeferred, match="publication_unresolved"):
        asyncio.run(StoreSyncService(w.session, w.tid).handle_product_deleted("900100"))
    assert w.session.get(Product, w.pid).extra_metadata["sync_meta"]["pending_publications"] == pending
    assert w.session.query(CatalogChannelRetirement).count() == 0
    monkeypatch.setattr(w.graph, "get", get)
    result = _sync(w)
    assert result["ok"], result
    asyncio.run(StoreSyncService(w.session, w.tid).handle_product_deleted("900100"))
    _assert_retired(w, rid, w.session)


@pytest.mark.parametrize("started_at", [None, "2001-01-01T00:00:00+00:00"])
def test_stale_or_undated_syncing_never_authorizes_evidence_deletion(world, started_at):
    w = world
    _salla(w)
    product = w.session.get(Product, w.pid)
    product.sync_status = "syncing"
    product.extra_metadata = {**product.extra_metadata, "sync_meta": {"syncing_started_at": started_at}}
    w.session.commit()
    with pytest.raises(CatalogDeletionDeferred, match="publication_in_flight"):
        asyncio.run(StoreSyncService(w.session, w.tid).handle_product_deleted("900100"))
    assert w.session.query(Product).filter_by(id=w.pid).count() == 1


def test_bounded_source_retries_keep_event_and_evidence_replayable(world, monkeypatch):
    from core import webhook_events as queue

    w = world
    rid = _salla(w)
    get = w.graph.get

    def lagging_lookup(url, **kwargs):
        if w.graph.items and "/products" in url:
            return _Resp(200, {"data": []})
        return get(url, **kwargs)

    monkeypatch.setattr(w.graph, "get", lagging_lookup)
    result = _sync(w)
    assert result["error_code"] == "verification_failed", result
    product = w.session.get(Product, w.pid)
    pending = product.extra_metadata["sync_meta"]["pending_publications"]
    graph_calls = list(w.graph.calls)
    ev = _source_event(w.session, w, monkeypatch)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    monkeypatch.setattr(queue, "_utcnow", lambda: now)
    for attempt in range(1, queue.MAX_ATTEMPTS + 1):
        claimed = queue.claim_next_batch(w.session)
        assert [row.id for row in claimed] == [ev.id]
        _process(w.session, claimed[0])
        w.session.refresh(ev)
        assert ev.attempts == attempt and ev.processed_at is None
        assert queue.claim_next_batch(w.session) == []  # no busy loop
        assert w.session.get(Product, w.pid).extra_metadata["sync_meta"]["pending_publications"] == pending
        if attempt < queue.MAX_ATTEMPTS:
            assert ev.status == "failed"
            assert ev.next_retry_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=queue.BACKOFF_SECONDS[attempt - 1])
            now = ev.next_retry_at.replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    assert ev.status == "dead_letter" and ev.next_retry_at is None
    assert ev.parsed_payload["data"]["id"] == "900100"
    assert w.session.query(CatalogChannelRetirement).count() == 0
    assert w.graph.calls == graph_calls
    # Once the exact successful POST is corroborated, the original dead-letter
    # event can still finish through the existing operator replay path.
    monkeypatch.setattr(w.graph, "get", get)
    assert _sync(w)["ok"]
    replayed = queue.replay(w.session, ev.id)
    assert replayed.status == "received" and replayed.attempts == 0
    ready = queue.claim_next_batch(w.session)
    assert [row.id for row in ready] == [ev.id]
    _process(w.session, ready[0])
    w.session.refresh(ev)
    assert ev.status == "processed"
    assert w.session.query(Product).filter_by(id=w.pid).count() == 0
    _assert_retired(w, rid, w.session)


@pytest.mark.parametrize("manual", [False, True])
def test_delete_locks_and_refreshes_cached_product_before_snapshot(world, monkeypatch, manual):
    w = world
    if not manual:
        _salla(w)
    cached = w.session.get(Product, w.pid)
    assert cached.sync_status == "pending"
    with sessionmaker(bind=w.session.get_bind())() as publisher:
        row = publisher.get(Product, w.pid)
        row.sync_status = "syncing"
        publisher.commit()
    assert cached.sync_status == "pending"  # stale identity map must not win
    selects = []

    def observe(state):
        statement = state.statement
        if state.is_select and statement._for_update_arg is not None:
            selects.append((str(statement.compile(dialect=postgresql.dialect())),
                            state.load_options._populate_existing))

    event.listen(w.session, "do_orm_execute", observe)
    try:
        if manual:
            with pytest.raises(HTTPException) as err:
                _manual_delete(w.session, w, monkeypatch)
            assert err.value.status_code == 409
        else:
            with pytest.raises(CatalogDeletionDeferred):
                asyncio.run(StoreSyncService(w.session, w.tid).handle_product_deleted("900100"))
    finally:
        event.remove(w.session, "do_orm_execute", observe)
    assert len(selects) == 1 and selects[0][1] is True
    assert "FOR UPDATE" in selects[0][0] and "SKIP LOCKED" not in selects[0][0]
    assert w.session.query(Product).filter_by(id=w.pid).count() == 1


@pytest.mark.parametrize("change", [None, "product_id", "catalog_id", "retailer_id", "meta_item_id", "provenance"])
def test_only_exact_proven_snapshot_resolves_pending_record(change):
    record = {"product_id": 1, "catalog_id": "CAT", "retailer_id": "SHIRT-BLUE-M", "meta_item_id": "META"}
    proof = {**record, "publication_provenance": "native_product_push"}
    product = SimpleNamespace(id=1, sync_status="failed", extra_metadata={
        "sync_meta": {"pending_publications": {"CAT|SHIRT-BLUE-M": record}},
    })
    if change == "provenance":
        proof["publication_provenance"] = "meta_graph_reconcile"
    elif change:
        proof[change] = 2 if change == "product_id" else "OTHER"
    if change:
        with pytest.raises(CatalogDeletionDeferred):
            assert_catalog_deletion_safe(product, [proof])
    else:
        assert_catalog_deletion_safe(product, [proof])


@pytest.mark.parametrize("pending", [[{"meta_item_id": "META"}], {"corrupt": "META"}, {"empty": {}}])
def test_malformed_pending_records_fail_closed(pending):
    product = SimpleNamespace(id=1, sync_status="failed", extra_metadata={
        "sync_meta": {"pending_publications": pending},
    })
    with pytest.raises(CatalogDeletionDeferred, match="publication_unresolved"):
        assert_catalog_deletion_safe(product, [])
