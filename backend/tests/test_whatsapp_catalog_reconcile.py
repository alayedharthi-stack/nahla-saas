"""Periodic reconciliation: Graph is read, drift is re-queued, nothing is written."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Product, Tenant, WhatsAppConnection  # noqa: E402
from core.catalog import OWNERSHIP_EXTERNAL_MANAGED  # noqa: E402
from services.whatsapp_catalog_reconcile import (  # noqa: E402
    RECONCILE_META_KEY,
    reconcile_due_tenants,
    reconcile_is_due,
    reconcile_tenant_channel_catalog,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _LiveGraph:
    def __init__(self, rows, *, fail=False):
        self.rows = rows
        self.fail = fail
        self.posts = []

    def get(self, url, params=None, headers=None):
        if self.fail:
            return _Resp(500, {"error": {"code": 1, "message": "down"}})
        return _Resp(200, {"data": self.rows, "paging": {}})

    def post(self, *a, **k):  # pragma: no cover - must never be called
        self.posts.append((a, k))
        raise AssertionError("reconcile must not write to Graph")


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.add(
        WhatsAppConnection(
            tenant_id=tenant.id,
            whatsapp_business_account_id="WABA-1",
            phone_number_id="PN-1",
            access_token="EAAB-test",
            meta_catalog_id="CAT-GENERIC-001",
            catalog_enabled=True,
            extra_metadata={},
        )
    )
    session.commit()
    return session, tenant.id, engine


def _synced(session, tenant_id, ext, rid, *, price_minor=24900, availability="in stock", retired=False):
    product = Product(
        tenant_id=tenant_id,
        external_id=ext,
        title="قميص قطني أزرق",
        price="249",
        in_stock=True,
        stock_quantity=3,
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        catalog_status="merchant_hidden" if retired else "active",
        merchant_hidden_at=datetime.now(timezone.utc) if retired else None,
        sync_status="retired" if retired else "synced",
        last_synced_at=datetime.now(timezone.utc),
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/shirt.jpg",
            "product_url": "https://store.example/p/shirt",
            "sync_meta": {
                "content_verified": not retired,
                "channel_retired_at": datetime.now(timezone.utc).isoformat() if retired else None,
                "retire_results": {rid: {"ok": True}} if retired else {},
                "expected_payloads_by_retailer_id": {rid: {"price": price_minor, "currency": "SAR", "availability": availability}},
            },
        },
    )
    session.add(product)
    session.commit()
    return product


_READY = patch(
    "services.whatsapp_catalog_sync.get_entitlements",
    lambda *a, **k: SimpleNamespace(has_feature=lambda key: key == "meta_catalog_sync"),
)
_TOKEN = patch("services.meta_catalog_reconcile.select_catalog_graph_token", lambda *a, **k: {"token": "tok", "token_source": "merchant"})
_LINK = patch("services.meta_catalog_linking.get_waba_catalog_link_status", lambda db, tid: {"ok": True, "link_status": "linked"})


@_READY
@_TOKEN
@_LINK
def test_drift_and_missing_items_are_requeued_without_graph_writes():
    session, tid, engine = _make_db()
    try:
        ok = _synced(session, tid, "1001", "1001-1")
        drifted = _synced(session, tid, "1002", "1002-1")
        missing = _synced(session, tid, "1003", "1003-1")
        graph = _LiveGraph([
            {"id": "M1", "retailer_id": "1001-1", "price": "249.00 SAR", "currency": "SAR", "availability": "in stock"},
            {"id": "M2", "retailer_id": "1002-1", "price": "199.00 SAR", "currency": "SAR", "availability": "in stock"},
        ])
        out = reconcile_tenant_channel_catalog(session, tid, client=graph)
        assert out["ok"] is True
        assert out["live_items"] == 2 and out["live_complete"] is True
        assert out["drifted"] == 1 and out["missing"] == 1 and out["requeued"] == 2
        assert out["waba_link_state"] == "linked"
        assert graph.posts == []
        session.refresh(ok); session.refresh(drifted); session.refresh(missing)
        assert ok.sync_status == "synced"
        assert drifted.sync_status == "pending" and missing.sync_status == "pending"
        conn = session.query(WhatsAppConnection).filter_by(tenant_id=tid).first()
        snap = conn.extra_metadata[RECONCILE_META_KEY]
        assert snap["requeued"] == 2 and snap["catalog_id"] == "CAT-GENERIC-001"
        assert reconcile_is_due(conn) is False
    finally:
        session.close(); engine.dispose()


@_READY
@_TOKEN
@_LINK
def test_incomplete_live_read_never_requeues():
    session, tid, engine = _make_db()
    try:
        row = _synced(session, tid, "1001", "1001-1")
        out = reconcile_tenant_channel_catalog(session, tid, client=_LiveGraph([], fail=True))
        assert out["ok"] is False and out["error"]
        assert out["requeued"] == 0
        session.refresh(row)
        assert row.sync_status == "synced"
    finally:
        session.close(); engine.dispose()


@_READY
@_TOKEN
@_LINK
def test_withdrawn_item_still_sellable_in_graph_is_requeued_for_retirement():
    session, tid, engine = _make_db()
    try:
        row = _synced(session, tid, "2001", "2001-1", retired=True)
        graph = _LiveGraph([
            {"id": "M9", "retailer_id": "2001-1", "price": "249.00 SAR", "currency": "SAR", "availability": "in stock"},
        ])
        out = reconcile_tenant_channel_catalog(session, tid, client=graph)
        assert out["retire_requeued"] == 1
        session.refresh(row)
        assert row.extra_metadata["sync_meta"]["retire_pending"] is True
    finally:
        session.close(); engine.dispose()


def test_due_tenants_respect_flag_and_interval(monkeypatch):
    session, tid, engine = _make_db()
    try:
        monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", raising=False)
        assert reconcile_due_tenants(session)["skipped"] is True
        monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
        conn = session.query(WhatsAppConnection).filter_by(tenant_id=tid).first()
        conn.extra_metadata = {RECONCILE_META_KEY: {"at": datetime.now(timezone.utc).isoformat()}}
        session.commit()
        with patch("services.whatsapp_catalog_reconcile.reconcile_tenant_channel_catalog") as rec:
            summary = reconcile_due_tenants(session)
        assert summary["tenants"] == 0 and not rec.called
        conn.extra_metadata = {RECONCILE_META_KEY: {"at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}}
        session.commit()
        with patch("services.whatsapp_catalog_reconcile.reconcile_tenant_channel_catalog", return_value={"requeued": 0, "retire_requeued": 0}) as rec:
            summary = reconcile_due_tenants(session)
        assert summary["tenants"] == 1 and rec.called
    finally:
        session.close(); engine.dispose()


def test_blocked_tenant_persists_skip_and_does_not_starve_others(monkeypatch):
    """A tenant whose readiness is blocked records the skip, so the next tick
    moves on to the tenant that is actually due."""
    session, tid, engine = _make_db()
    try:
        monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
        blocked = Tenant(name="متجر محجوب", is_active=True)
        session.add(blocked); session.commit()
        session.add(WhatsAppConnection(tenant_id=blocked.id, whatsapp_business_account_id="WABA-2", phone_number_id="PN-2",
                                       access_token="", meta_catalog_id="CAT-X", catalog_enabled=True, extra_metadata={}))
        session.commit()
        calls = []
        real = __import__("services.whatsapp_catalog_reconcile", fromlist=["x"]).reconcile_tenant_channel_catalog

        def _fake_ready(db, tenant_id):
            if int(tenant_id) == blocked.id:
                return {"ready": False, "blocker_code": "access_token_missing"}
            return {"ready": True, "blocker_code": None, "connection_fp": "fp"}

        def _spy(db, tenant_id, **kw):
            calls.append(int(tenant_id))
            if int(tenant_id) == blocked.id:
                with patch("services.whatsapp_catalog_sync.evaluate_whatsapp_catalog_sync_readiness", _fake_ready):
                    return real(db, tenant_id, **kw)
            # a healthy tenant records its own snapshot (as the real function does)
            conn = db.query(WhatsAppConnection).filter_by(tenant_id=int(tenant_id)).first()
            conn.extra_metadata = {RECONCILE_META_KEY: {"at": datetime.now(timezone.utc).isoformat()}}
            db.commit()
            return {"requeued": 0, "retire_requeued": 0}

        with patch("services.whatsapp_catalog_reconcile.reconcile_tenant_channel_catalog", _spy):
            for _ in range(3):
                reconcile_due_tenants(session)
        assert blocked.id in calls and tid in calls
        assert calls.count(blocked.id) == 1
        conn_b = session.query(WhatsAppConnection).filter_by(tenant_id=blocked.id).first()
        assert conn_b.extra_metadata[RECONCILE_META_KEY]["skipped"] is True
        assert conn_b.extra_metadata[RECONCILE_META_KEY]["blocker_code"] == "access_token_missing"
    finally:
        session.close(); engine.dispose()
