"""Trial write scope: only the named tenant/products may reach Meta.

Three generic stores stand in for the production shapes: a Salla store in
scope (the trial tenant), a second Salla store (not in scope), and a store
whose products were imported from an existing Meta catalog (meta_readonly,
never written by this path). Every Graph-writing entry point is exercised
with the scope set and must produce zero Graph calls for the other stores
and no state change on their rows.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import (  # noqa: E402
    Base,
    CatalogChannelRetirement,
    MetaCatalogMembership,
    Product,
    ProductVariant,
    Tenant,
    WhatsAppConnection,
)
from core.catalog import OWNERSHIP_EXTERNAL_MANAGED, OWNERSHIP_META_READONLY  # noqa: E402
from services.whatsapp_catalog_sync_scope import (  # noqa: E402
    PRODUCT_SCOPE_ENV,
    SCOPE_BLOCKER_CODE,
    TENANT_SCOPE_ENV,
    product_in_sync_scope,
    scope_description,
    scoped_product_ids,
    tenant_in_sync_scope,
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


class CountingGraph:
    """Records every call; any write to a tenant outside scope is a test failure."""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url))
        if params and params.get("filter"):
            rid = json.loads(params["filter"])["retailer_id"]["eq"]
            return _Resp(200, {"data": [{"id": f"META-{rid}", "retailer_id": rid, "price": "100.00 SAR",
                                         "currency": "SAR", "availability": "in stock"}]})
        return _Resp(200, {"data": [], "paging": {}})

    def post(self, url, data=None, headers=None):
        self.calls.append(("POST", url))
        return _Resp(200, {"success": True, "id": "META-NEW"})

    @property
    def writes(self):
        return [c for c in self.calls if c[0] == "POST"]


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    trial = Tenant(name="متجر تجريبي عام", is_active=True)
    other = Tenant(name="متجر تجريبي ثانٍ", is_active=True)
    imported = Tenant(name="متجر مستورد من Meta", is_active=True)
    session.add_all([trial, other, imported])
    session.commit()
    for tenant, cat in ((trial, "CAT-TRIAL"), (other, "CAT-OTHER"), (imported, "CAT-IMPORTED")):
        session.add(WhatsAppConnection(
            tenant_id=tenant.id, whatsapp_business_account_id=f"WABA-{tenant.id}", phone_number_id=f"PN-{tenant.id}",
            access_token="EAAB-test", meta_catalog_id=cat, catalog_enabled=True, extra_metadata={},
        ))
    session.commit()
    return session, trial.id, other.id, imported.id, engine


def _salla_row(session, tenant_id, ext, *, pending=True, hidden=False):
    product = Product(
        tenant_id=tenant_id, external_id=ext, title="حذاء رياضي أبيض", price="100", in_stock=True, stock_quantity=2,
        source="salla", ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        catalog_status="merchant_hidden" if hidden else "active",
        merchant_hidden_at=datetime.now(timezone.utc) if hidden else None,
        sync_status="synced" if hidden else ("pending" if pending else None),
        last_synced_at=datetime.now(timezone.utc) if hidden else None,
        extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/shoe.jpg",
                        "product_url": "https://store.example/p/shoe",
                        "sync_meta": {"retire_pending": True, "retire_reason": "merchant_hidden"} if hidden else {}},
    )
    session.add(product); session.flush()
    session.add(ProductVariant(tenant_id=tenant_id, product_id=product.id, salla_variant_id="1", retailer_id=f"{ext}-1",
                               price="100", currency="SAR", stock_quantity=2, in_stock=True, is_default=False))
    if hidden:
        session.add(MetaCatalogMembership(tenant_id=tenant_id, catalog_id="CAT-X", retailer_id=f"{ext}-1",
                                          product_id=product.id, salla_variant_id="1", meta_item_id=f"META-{ext}-1",
                                          verified_at=datetime.now(timezone.utc), provenance="salla_variant_push"))
    session.commit()
    return product


def _imported_row(session, tenant_id):
    product = Product(tenant_id=tenant_id, title="فستان مستورد", price="500", source="meta",
                      ownership_mode=OWNERSHIP_META_READONLY, catalog_status="active", meta_item_id="META-IMPORTED",
                      sync_status=None, extra_metadata={})
    session.add(product); session.commit()
    return product


_READY = patch(
    "services.whatsapp_catalog_sync.get_entitlements",
    lambda *a, **k: SimpleNamespace(has_feature=lambda key: key == "meta_catalog_sync"),
)


def test_scope_parsing_and_membership(monkeypatch):
    monkeypatch.delenv(TENANT_SCOPE_ENV, raising=False)
    monkeypatch.delenv(PRODUCT_SCOPE_ENV, raising=False)
    assert tenant_in_sync_scope(33) is True and product_in_sync_scope(33, 5) is True
    monkeypatch.setenv(TENANT_SCOPE_ENV, "1")
    assert tenant_in_sync_scope(1) is True and tenant_in_sync_scope(33) is False and tenant_in_sync_scope(35) is False
    assert product_in_sync_scope(1, 7) is True
    monkeypatch.setenv(PRODUCT_SCOPE_ENV, "7, 8")
    assert scoped_product_ids() == {1: {7, 8}}
    assert product_in_sync_scope(1, 7) is True and product_in_sync_scope(1, 9) is False
    assert product_in_sync_scope(1, None) is False
    monkeypatch.setenv(TENANT_SCOPE_ENV, "1,35")
    monkeypatch.setenv(PRODUCT_SCOPE_ENV, "1:7,35:9,oops,12")
    assert scoped_product_ids() == {1: {7}, 35: {9}}  # bare ids are ambiguous with two tenants
    desc = scope_description()
    assert desc["active"] is True and desc["tenant_ids"] == [1, 35]


@_READY
def test_only_the_trial_tenant_and_products_reach_graph(monkeypatch):
    from services.whatsapp_catalog_sync import (
        build_whatsapp_catalog_sync_status,
        drain_ready_tenants,
        drain_whatsapp_catalog_sync,
    )

    session, trial, other, imported, engine = _make_db()
    try:
        allowed = _salla_row(session, trial, "100100")
        not_listed = _salla_row(session, trial, "100200")
        foreign = _salla_row(session, other, "200100")
        foreign_hidden = _salla_row(session, other, "200200", hidden=True)
        _imported_row(session, imported)
        monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
        monkeypatch.setenv(TENANT_SCOPE_ENV, str(trial))
        monkeypatch.setenv(PRODUCT_SCOPE_ENV, f"{trial}:{allowed.id}")
        graph = CountingGraph()
        pushed = []

        def _fake_attempt(db, tenant_id, product_id, *, client=None, **kw):
            # stands in for the Graph push; the real orchestrator's own scope
            # guard is covered in the entry-point test below
            pushed.append((int(tenant_id), int(product_id)))
            row = db.get(Product, int(product_id))
            row.sync_status = "synced"
            db.commit()
            return {"ok": True, "sync_status": "synced"}

        with patch("services.whatsapp_catalog_sync.attempt_native_meta_sync", _fake_attempt), \
             patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-TRIAL", "tok")):
            summary = drain_ready_tenants(session, limit_per_tenant=25)
            # a direct per-tenant drain for the other stores makes no Graph call either
            for tid in (other, imported):
                out = drain_whatsapp_catalog_sync(session, tid, client=graph)
                assert out["skipped"] is True and out["blocker_code"] == SCOPE_BLOCKER_CODE
            out_trial = drain_whatsapp_catalog_sync(session, trial, client=graph)
        assert summary["tenants"] == 1
        assert out_trial["skipped_scope"] >= 1
        # exactly the listed product of the trial tenant was pushed, once
        assert pushed == [(trial, allowed.id)]
        assert graph.calls == []
        session.expire_all()
        assert session.get(Product, allowed.id).sync_status == "synced"
        assert session.get(Product, not_listed.id).sync_status == "pending"
        assert session.get(Product, foreign.id).sync_status == "pending"
        hidden_meta = session.get(Product, foreign_hidden.id).extra_metadata["sync_meta"]
        assert hidden_meta["retire_pending"] is True and "channel_retired_at" not in hidden_meta
        # status for an excluded store says so honestly, with no action for the merchant
        st = build_whatsapp_catalog_sync_status(session, other)
        assert st["ready"] is False and st["blocker_code"] == SCOPE_BLOCKER_CODE and st["phase"] == "blocked"
        assert st["sync_scope"]["active"] is True and st["sync_scope"]["tenant_ids"] == [trial]
        assert build_whatsapp_catalog_sync_status(session, trial)["ready"] is True
    finally:
        session.close(); engine.dispose()


def test_every_write_entry_point_refuses_out_of_scope_tenants(monkeypatch):
    from services.meta_catalog_push import push_one_meta_catalog_item, retire_meta_catalog_item
    from services.meta_catalog_reconnect import reconcile_meta_catalog_after_whatsapp_change
    from services.native_meta_sync_orchestrator import attempt_native_meta_sync
    from services.whatsapp_catalog_reconcile import reconcile_due_tenants
    from services.whatsapp_catalog_retirement import (
        REASON_SOURCE_DELETED,
        attempt_product_channel_retirement,
        drain_channel_retirement_ledger,
        enqueue_channel_retirement_ledger,
    )
    from services.whatsapp_catalog_sync import schedule_whatsapp_catalog_drain

    session, trial, other, imported, engine = _make_db()
    try:
        foreign = _salla_row(session, other, "300100")
        foreign_hidden = _salla_row(session, other, "300200", hidden=True)
        enqueue_channel_retirement_ledger(session, other, [{"retailer_id": "300300-1", "meta_item_id": "META-300300-1",
                                                            "catalog_id": "CAT-OTHER", "product_id": 999,
                                                            "publication_provenance": "salla_variant_push"}],
                                          reason=REASON_SOURCE_DELETED)
        session.commit()
        monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
        monkeypatch.setenv(TENANT_SCOPE_ENV, str(trial))
        graph = CountingGraph()
        conn_other = session.query(WhatsAppConnection).filter_by(tenant_id=other).first()
        with patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=("CAT-OTHER", "tok")), \
             patch("services.meta_catalog_push.select_catalog_graph_token", lambda *a, **k: {"token": "tok"}), \
             patch("services.meta_catalog_reconcile.select_catalog_graph_token", lambda *a, **k: {"token": "tok"}), \
             patch("services.meta_catalog_access.select_catalog_graph_token", lambda *a, **k: {"token": "tok"}):
            # 1. publish (orchestrator) – no lease, no state change
            res = attempt_native_meta_sync(session, other, foreign.id, client=graph)
            assert res["skipped"] is True and res["error_code"] == SCOPE_BLOCKER_CODE
            # 2. raw push helper
            res = push_one_meta_catalog_item(session, other, "300100-1", confirm=True, client=graph)
            assert res["ok"] is False and res["error"] == SCOPE_BLOCKER_CODE
            # 3. raw retirement helper
            res = retire_meta_catalog_item(conn_other, "CAT-OTHER", "300200-1", "META-300200-1",
                                           publication_evidence={"owned": True, "meta_product_id": "META-300200-1",
                                                                 "catalog_id": "CAT-OTHER", "retailer_id": "300200-1"},
                                           client=graph)
            assert res["ok"] is False and res["error"] == SCOPE_BLOCKER_CODE
            # 4. product retirement + 5. ledger drain
            res = attempt_product_channel_retirement(session, other, foreign_hidden.id, client=graph)
            assert res["skipped"] is True and res["error_code"] == SCOPE_BLOCKER_CODE
            out = drain_channel_retirement_ledger(session, other, client=graph)
            assert out["processed"] == 0 and out["skipped_scope"] == 1
            # 6. reconnect product sync
            monkeypatch.delenv("NAHLA_AUTO_CATALOG_ONBOARDING", raising=False)
            with patch("services.meta_catalog_reconnect.bind_current_waba_to_merchant_catalog",
                       return_value={"ok": True, "skipped": False, "catalog_id": "CAT-OTHER"}):
                rec = reconcile_meta_catalog_after_whatsapp_change(session, other, confirm=True, client=graph)
            assert rec["error"] == SCOPE_BLOCKER_CODE and rec["skipped"] >= 1
            # 7. periodic reconcile picks only in-scope tenants
            with patch("services.whatsapp_catalog_reconcile.reconcile_tenant_channel_catalog",
                       return_value={"requeued": 0, "retire_requeued": 0}) as rec_mock:
                reconcile_due_tenants(session, max_tenants=5)
            assert [c.args[1] for c in rec_mock.call_args_list] == [trial]
            # 8. event-driven drain scheduling is a no-op for excluded tenants
            with patch("services.whatsapp_catalog_sync._whatsapp_catalog_drain_coalescer") as coal:
                schedule_whatsapp_catalog_drain(other, allow_without_auto_flag=True)
            assert not coal.called
        assert graph.calls == []
        session.expire_all()
        assert session.get(Product, foreign.id).sync_status == "pending"
        assert session.get(Product, foreign_hidden.id).sync_status == "synced"
        row = session.query(CatalogChannelRetirement).filter_by(tenant_id=other).one()
        assert row.status == "pending" and row.attempts == 0
    finally:
        session.close(); engine.dispose()


def test_reconnect_bind_and_onboarding_are_read_only_for_out_of_scope_tenants(monkeypatch):
    """WhatsApp reconnect triggers the catalog bind for any tenant; under the
    trial scope an excluded tenant must not get a share/link POST or a
    catalog create, only the dry-run read."""
    from services.meta_catalog_onboarding import ensure_waba_catalog_for_tenant
    from services.meta_catalog_reconnect import bind_current_waba_to_merchant_catalog

    session, trial, other, imported, engine = _make_db()
    try:
        monkeypatch.setenv(TENANT_SCOPE_ENV, str(trial))
        monkeypatch.setenv("NAHLA_AUTO_CATALOG_ONBOARDING", "1")
        posts = []
        with patch("services.meta_catalog_reconnect.select_catalog_graph_token",
                   lambda conn, cid, client=None: {"token": "tok", "token_source": "merchant", "catalog": {"business_id": "BM-1"}}), \
             patch("services.meta_catalog_reconnect._select_graph_token", lambda conn: {"token": "tok"}), \
             patch("services.meta_catalog_reconnect.fetch_waba_owner_business_id", lambda waba, tok, client=None: {"business_id": "BM-1"}), \
             patch("services.meta_catalog_reconnect.link_waba_to_catalog",
                   lambda waba, cid, tok, confirm=False, client=None: (posts.append(("link", confirm)) or
                       {"ok": True, "dry_run": not confirm, "already_linked": False, "link_status": "not_linked", "action": "dry_run"})):
            res = bind_current_waba_to_merchant_catalog(session, other, confirm=True)
        assert res["scope_dry_run"] is True and res["dry_run"] is True
        assert posts == [("link", False)]
        with patch("services.meta_catalog_onboarding._select_graph_token", lambda conn: {"token": "tok"}), \
             patch("services.meta_catalog_onboarding.fetch_waba_owner_business_id", lambda waba, tok, client=None: {"business_id": "BM-1"}), \
             patch("services.meta_catalog_onboarding._fetch_waba_product_catalogs", lambda waba, tok, client=None: ([], 200, None)), \
             patch("services.meta_catalog_onboarding.probe_catalog_readable", lambda tok, cid, client=None: {"ok": True, "business_id": "BM-1"}), \
             patch("services.meta_catalog_onboarding.link_waba_to_catalog", side_effect=AssertionError("must not link")), \
             patch("services.meta_catalog_onboarding._create_owned_catalog", side_effect=AssertionError("must not create")):
            ens = ensure_waba_catalog_for_tenant(session, other, confirm=True)
        assert ens["dry_run"] is True and ens["scope_dry_run"] is True
        assert ens["ok"] is True and ens["created"] is False
        conn = session.query(WhatsAppConnection).filter_by(tenant_id=other).first()
        assert conn.meta_catalog_id == "CAT-OTHER"
    finally:
        session.close(); engine.dispose()


@_READY
def test_proposed_tenant_35_trial_scope_refuses_tenants_1_33_and_every_other_tenant(monkeypatch):
    """The exact environment proposed for the limited trial
    (``NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS=35`` and a ``35:<id>`` product
    list) must leave tenant 1 (Salla review store), tenant 33 (read-only honey
    store) and any other tenant untouched: no Graph call, no product state
    change, no connection-row change, and only the listed tenant-35 products
    may be published."""
    from services.meta_catalog_push import push_one_meta_catalog_item, retire_meta_catalog_item
    from services.native_meta_sync_orchestrator import attempt_native_meta_sync
    from services.whatsapp_catalog_reconcile import reconcile_due_tenants
    from services.whatsapp_catalog_retirement import (
        REASON_SOURCE_DELETED,
        attempt_product_channel_retirement,
        drain_channel_retirement_ledger,
        enqueue_channel_retirement_ledger,
    )
    from services.whatsapp_catalog_sync import (
        drain_ready_tenants,
        evaluate_whatsapp_catalog_sync_readiness,
        schedule_whatsapp_catalog_drain,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        # Explicit ids mirror the production roles named by the owner; the
        # store data itself stays generic.
        for tid, name in ((1, "متجر مراجعة سلة"), (2, "متجر تجريبي عام"), (33, "متجر قراءة فقط"), (35, "متجر التجربة")):
            session.add(Tenant(id=tid, name=name, is_active=True))
        session.commit()
        for tid in (1, 2, 33, 35):
            session.add(WhatsAppConnection(
                tenant_id=tid, whatsapp_business_account_id=f"WABA-{tid}", phone_number_id=f"PN-{tid}",
                access_token="EAAB-test", meta_catalog_id=f"CAT-{tid}", catalog_enabled=True,
                extra_metadata={"marker": f"untouched-{tid}"},
            ))
        session.commit()

        trial_rows = [_salla_row(session, 35, f"35000{i}") for i in range(3)]
        unlisted_trial_row = _salla_row(session, 35, "350099")
        foreign = {tid: _salla_row(session, tid, f"{tid}00100") for tid in (1, 2, 33)}
        foreign_hidden = {tid: _salla_row(session, tid, f"{tid}00200", hidden=True) for tid in (1, 2, 33)}
        imported_33 = _imported_row(session, 33)
        for tid in (1, 2, 33):
            enqueue_channel_retirement_ledger(
                session, tid,
                [{"retailer_id": f"{tid}00300-1", "meta_item_id": f"META-{tid}00300-1", "catalog_id": f"CAT-{tid}", "product_id": 999,
                  "publication_provenance": "salla_variant_push"}],
                reason=REASON_SOURCE_DELETED,
            )
        session.commit()

        before = {
            tid: (c.meta_catalog_id, c.catalog_enabled, dict(c.extra_metadata or {}))
            for tid, c in ((c.tenant_id, c) for c in session.query(WhatsAppConnection).all())
        }

        monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
        monkeypatch.setenv(TENANT_SCOPE_ENV, "35")
        monkeypatch.setenv(PRODUCT_SCOPE_ENV, ",".join(f"35:{p.id}" for p in trial_rows))

        # Membership: only tenant 35 and only the listed products.
        assert tenant_in_sync_scope(35) is True
        assert all(tenant_in_sync_scope(t) is False for t in (1, 2, 33))
        assert all(product_in_sync_scope(35, p.id) for p in trial_rows)
        assert product_in_sync_scope(35, unlisted_trial_row.id) is False
        assert product_in_sync_scope(1, foreign[1].id) is False and product_in_sync_scope(33, imported_33.id) is False
        assert scope_description()["tenant_ids"] == [35]

        graph = CountingGraph()
        with patch("services.meta_catalog_push._resolve_catalog_and_token",
                   lambda conn, *a, **k: (str(getattr(conn, "meta_catalog_id", "") or "CAT-35"), "tok")), \
             patch("services.meta_catalog_push.select_catalog_graph_token", lambda *a, **k: {"token": "tok"}), \
             patch("services.meta_catalog_reconcile.select_catalog_graph_token", lambda *a, **k: {"token": "tok"}), \
             patch("services.meta_catalog_access.select_catalog_graph_token", lambda *a, **k: {"token": "tok"}):
            for tid in (1, 2, 33):
                conn = session.query(WhatsAppConnection).filter_by(tenant_id=tid).first()
                assert evaluate_whatsapp_catalog_sync_readiness(session, tid)["blocker_code"] == SCOPE_BLOCKER_CODE
                res = attempt_native_meta_sync(session, tid, foreign[tid].id, client=graph)
                assert res["skipped"] is True and res["error_code"] == SCOPE_BLOCKER_CODE
                res = push_one_meta_catalog_item(session, tid, f"{tid}00100-1", confirm=True, client=graph)
                assert res["ok"] is False and res["error"] == SCOPE_BLOCKER_CODE
                res = retire_meta_catalog_item(conn, f"CAT-{tid}", f"{tid}00200-1", f"META-{tid}00200-1",
                                               publication_evidence={"owned": True, "meta_product_id": f"META-{tid}00200-1",
                                                                     "catalog_id": f"CAT-{tid}", "retailer_id": f"{tid}00200-1"},
                                               client=graph)
                assert res["ok"] is False and res["error"] == SCOPE_BLOCKER_CODE
                res = attempt_product_channel_retirement(session, tid, foreign_hidden[tid].id, client=graph)
                assert res["skipped"] is True and res["error_code"] == SCOPE_BLOCKER_CODE
                out = drain_channel_retirement_ledger(session, tid, client=graph)
                assert out["processed"] == 0 and out["skipped_scope"] == 1
                with patch("services.whatsapp_catalog_sync._whatsapp_catalog_drain_coalescer") as coal:
                    schedule_whatsapp_catalog_drain(tid, allow_without_auto_flag=True)
                assert not coal.called
            # The imported (meta_readonly) honey-store product is never a publish candidate either.
            res = attempt_native_meta_sync(session, 33, imported_33.id, client=graph)
            assert res["skipped"] is True
            # Tenant 35 itself: an unlisted product is refused without state change.
            res = attempt_native_meta_sync(session, 35, unlisted_trial_row.id, client=graph)
            assert res["skipped"] is True and res["error_code"] == SCOPE_BLOCKER_CODE
            assert graph.calls == []
            # Periodic reconcile picks tenant 35 only; the hourly drain iterates tenant 35 only.
            with patch("services.whatsapp_catalog_reconcile.reconcile_tenant_channel_catalog",
                       return_value={"requeued": 0, "retire_requeued": 0}) as rec_mock:
                reconcile_due_tenants(session, max_tenants=10)
            assert [c.args[1] for c in rec_mock.call_args_list] == [35]
            with patch("services.whatsapp_catalog_sync.drain_whatsapp_catalog_sync",
                       return_value={"processed": 0, "synced": 0, "failed": 0}) as drain_mock:
                drain_ready_tenants(session)
            assert sorted(c.args[1] for c in drain_mock.call_args_list) == [35]
            # The listed tenant-35 products are the only ones handed to the publisher.
            pushed = []

            def _fake_attempt(db, tenant_id, product_id, *, client=None, **kw):
                pushed.append((int(tenant_id), int(product_id)))
                row = db.get(Product, int(product_id))
                row.sync_status = "synced"
                db.commit()
                return {"ok": True, "sync_status": "synced"}

            with patch("services.whatsapp_catalog_sync.attempt_native_meta_sync", _fake_attempt):
                summary = drain_ready_tenants(session, limit_per_tenant=25)
            assert summary["tenants"] == 1
            assert sorted(pushed) == sorted((35, p.id) for p in trial_rows)
        assert graph.calls == []

        session.expire_all()
        for tid in (1, 2, 33):
            assert session.get(Product, foreign[tid].id).sync_status == "pending"
            assert session.get(Product, foreign_hidden[tid].id).sync_status == "synced"
            row = session.query(CatalogChannelRetirement).filter_by(tenant_id=tid).one()
            assert row.status == "pending" and row.attempts == 0
        assert session.get(Product, imported_33.id).sync_status is None
        assert session.get(Product, unlisted_trial_row.id).sync_status == "pending"
        after = {
            c.tenant_id: (c.meta_catalog_id, c.catalog_enabled, dict(c.extra_metadata or {}))
            for c in session.query(WhatsAppConnection).all()
        }
        for tid in (1, 2, 33):
            assert after[tid] == before[tid]
    finally:
        session.close(); engine.dispose()
