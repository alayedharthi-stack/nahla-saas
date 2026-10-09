"""Catalog-only consent reaches the existing bulk/auto engine without a WABA."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from test_meta_catalog_consent_native_sync import (  # reuse isolated SQLite/Graph fixture
    world, CATALOG, RID, Product, ProductVariant, MetaCatalogAuthorization, WhatsAppConnection,
)


@pytest.fixture()
def catalog_world(world, monkeypatch):
    import core.plan_entitlements as ent
    from services import whatsapp_catalog_sync as sync

    monkeypatch.setattr(sync, "get_entitlements", lambda *a, **k: ent.get_entitlements(*a, **k))
    world.session.query(WhatsAppConnection).filter_by(tenant_id=world.tid).delete()
    world.session.commit()
    return world


@pytest.mark.parametrize("source", ["nahla_native", "salla"])
def test_bulk_consent_without_whatsapp_uses_existing_orchestrator(catalog_world, source):
    from services import whatsapp_catalog_sync as sync

    w = catalog_world
    if source == "salla":
        product = w.session.get(Product, w.pid)
        product.source = "salla"
        product.ownership_mode = "external_managed"
        product.external_id = "50101"
        product.extra_metadata = {**product.extra_metadata, "status": "active", "source_status": "sale"}
        variant = w.session.query(ProductVariant).filter_by(product_id=w.pid).one()
        variant.salla_variant_id = "60101"
        variant.retailer_id = "50101-60101"
        w.session.commit()
    readiness = sync.evaluate_whatsapp_catalog_sync_readiness(w.session, w.tid)
    assert readiness["ready"] is True, readiness
    assert w.graph.calls == [] and w.probes == []  # local gate, no Graph
    assert sync.enqueue_whatsapp_catalog_sync(w.session, w.tid, trigger="manual")["enqueued"] == 1
    result = sync.drain_whatsapp_catalog_sync(w.session, w.tid, client=w.graph)
    assert result["synced"] == 1, result
    assert w.session.query(WhatsAppConnection).count() == 0
    assert {token for _, _, token in w.graph.calls} == {w.consent_token}
    assert {token for token, _ in w.probes} == {w.consent_token}
    assert all("whatsapp" not in url and "product_catalogs" not in url for _, url, _ in w.graph.calls)


@pytest.mark.parametrize("block", ["revoked", "expired", "data_expired", "disabled", "approval_changed", "entitlement", "scope"])
def test_inactive_consent_blocks_bulk_before_graph_without_fallback(catalog_world, monkeypatch, block):
    from services import whatsapp_catalog_sync as sync

    w = catalog_world
    row = w.session.query(MetaCatalogAuthorization).one()
    if block == "revoked":
        row.status = "revoked"
    elif block == "expired":
        row.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif block == "data_expired":
        row.data_access_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif block == "disabled":
        monkeypatch.delenv("NAHLA_META_CATALOG_CONSENT_ENABLED")
    elif block == "approval_changed":
        monkeypatch.setenv("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{w.tid}:880000000000999:770000000000001")
    elif block == "entitlement":
        w.entitled["ok"] = False
    elif block == "scope":
        monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", str(w.tid + 100))
    w.session.commit()
    readiness = sync.evaluate_whatsapp_catalog_sync_readiness(w.session, w.tid)
    assert readiness["ready"] is False
    assert readiness["blocker_code"] in {"catalog_consent_inactive", "feature_locked", "sync_scope_excluded"}
    assert sync.enqueue_whatsapp_catalog_sync(w.session, w.tid)["queued"] is False
    assert sync.drain_whatsapp_catalog_sync(w.session, w.tid, client=w.graph)["processed"] == 0
    assert not w.graph.calls and not w.probes


def test_automatic_tick_discovers_catalog_only_authorization(catalog_world, monkeypatch):
    from services import whatsapp_catalog_sync as sync

    w = catalog_world
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    seen = []
    def attempt(db, tenant_id, product_id, **kwargs):
        seen.append((tenant_id, product_id))
        db.get(Product, product_id).sync_status = "synced"
        db.commit()
        return {"ok": True}
    monkeypatch.setattr(sync, "attempt_native_meta_sync", attempt)
    result = sync.drain_ready_tenants(w.session)
    assert result["tenants"] == 1 and result["processed"] == 1
    assert seen == [(w.tid, w.pid)]


@pytest.mark.parametrize("result_kind", ["success", "failure", "lock_skipped"])
def test_background_walks_all_batches_once_without_failure_spin(monkeypatch, result_kind):
    from services import whatsapp_catalog_sync as sync
    import core.database as database

    rows = [SimpleNamespace(id=i, tenant_id=9, source="salla", ownership_mode="external_managed",
                            catalog_status="active", merchant_hidden_at=None,
                            sync_status="pending", extra_metadata={}) for i in range(1, 79)]
    db = MagicMock()
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(sync, "evaluate_whatsapp_catalog_sync_readiness", lambda *_: {"ready": True})
    monkeypatch.setattr(sync, "iter_tenant_products", lambda *_: iter(rows))
    monkeypatch.setattr(sync, "_drain_retirements", lambda *a, **k: {})
    monkeypatch.setattr(sync, "_stamp_connection_fp_on_failure", lambda *a: None)
    seen = []
    def attempt(_db, _tid, pid, **kwargs):
        seen.append(pid)
        if result_kind == "failure":
            raise RuntimeError("test failure leaves the row pending")
        return {"ok": result_kind == "success", "skipped": result_kind == "lock_skipped"}
    monkeypatch.setattr(sync, "attempt_native_meta_sync", attempt)
    sync.run_whatsapp_catalog_drain_background(9)
    assert seen == list(range(1, 79))
    db.close.assert_called_once()


def test_explicit_manual_bulk_runs_with_auto_flag_disabled(monkeypatch):
    import asyncio
    from routers import catalog
    from services import whatsapp_catalog_sync as sync

    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", raising=False)
    monkeypatch.setattr(catalog, "resolve_tenant_id", lambda _: 9)
    monkeypatch.setattr(catalog, "_enforce_catalog_feature", lambda *_: None)
    monkeypatch.setattr(catalog, "audit", lambda *a, **k: None)
    monkeypatch.setattr(sync, "enqueue_whatsapp_catalog_sync", lambda *a, **k: {"queued": True, "eligible": 78, "enqueued": 78})
    schedule = MagicMock()
    monkeypatch.setattr(sync, "schedule_whatsapp_catalog_drain", schedule)
    result = asyncio.run(catalog.merchant_whatsapp_catalog_sync(catalog._WhatsappCatalogSyncBody(), MagicMock(), MagicMock(), {}))
    assert result["queued"] is True
    schedule.assert_called_once_with(9, allow_without_auto_flag=True)


def test_catalog_status_remains_readable_after_consent_revocation(catalog_world):
    from services import whatsapp_catalog_sync as sync

    w = catalog_world
    row = w.session.query(MetaCatalogAuthorization).one()
    row.status = "revoked"
    w.session.commit()
    status = sync.build_whatsapp_catalog_sync_status(w.session, w.tid)
    assert status["ready"] is False
    assert status["blocker_code"] == "catalog_consent_inactive"
    assert not w.graph.calls and not w.probes


def test_consent_link_status_ignores_stale_whatsapp_product_stamp(catalog_world):
    from services import whatsapp_catalog_sync as sync

    w = catalog_world
    stale = {"at": datetime.now(timezone.utc).isoformat(), "linked": True}
    link = sync.catalog_link_evidence(w.session, w.tid, product_evidence=stale)
    assert link["state"] == "unknown" and link["waba_id"] is None
    assert not w.graph.calls and not w.probes


def test_background_walks_hidden_retirement_batches_once(monkeypatch):
    from services import whatsapp_catalog_sync as sync
    from services import whatsapp_catalog_retirement as retirement
    import core.database as database

    rows = [SimpleNamespace(id=i, tenant_id=9, source="salla", ownership_mode="external_managed",
                            catalog_status="merchant_hidden", merchant_hidden_at=datetime.now(timezone.utc),
                            sync_status="pending", extra_metadata={}) for i in range(1, 79)]
    db = MagicMock()
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(sync, "evaluate_whatsapp_catalog_sync_readiness", lambda *_: {"ready": True})
    monkeypatch.setattr(sync, "iter_tenant_products", lambda *_: iter(rows))
    monkeypatch.setattr(sync, "retirement_is_due", lambda *_: True)
    monkeypatch.setattr(retirement, "drain_channel_retirement_ledger", lambda *a, **k: {})
    seen = []
    def retire(_db, _tid, pid, **kwargs):
        seen.append(pid)
        return {"ok": False}  # unchanged hidden rows must not spin
    monkeypatch.setattr(retirement, "attempt_product_channel_retirement", retire)
    sync.run_whatsapp_catalog_drain_background(9)
    assert seen == list(range(1, 79))
    db.close.assert_called_once()
