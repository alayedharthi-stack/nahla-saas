"""Publication authority: one design for Salla and native products, proven on a database.

Write authority over a Meta item (update, retire, re-publish) is a
``meta_catalog_memberships`` row for (tenant, catalog, retailer_id) with a
publication provenance whose ``meta_item_id`` is the live item. Only a
successful create/update POST writes it. These tests prove, against SQLite
rows and a fake Graph client that records every call:

* a pre-POST identity slot never turns a reconcile row (pointing at a foreign
  item) into authority — neither for the publish path nor for retirement;
* a native product goes create → update → hide/retire → re-publish, with no
  legacy stamp trusted;
* a failed POST records no authority;
* native ownership rows never widen (or narrow) native catalog-card
  capability: for numeric ids, generic SKUs, ``nahla_*`` ids, ambiguous and
  unmatched rows, every capability reader returns exactly what reconcile alone
  yields, before and after reconcile passes;
* the trial readout resolves its Salla adapter with SELECTs only;
* refused withdrawals are visible in the merchant status and never counted as
  retired; a catalog that is not current is retried, never written.

Generic merchant data (متجر تجريبي عام, «حذاء رياضي أبيض», «قميص قطني أزرق»).
Nothing talks to Meta or Salla.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from database.models import (
    Base,
    CatalogChannelRetirement,
    Integration,
    MetaCatalogMembership,
    Product,
    ProductVariant,
    Tenant,
    WhatsAppConnection,
)
from core.catalog import (
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_NAHLA_NATIVE,
)
from core.meta_catalog_membership import (
    PROVENANCE_GRAPH_RECONCILE,
    PROVENANCE_NATIVE_PUSH,
    PROVENANCE_NATIVE_PUSH_RECONCILED,
    PUBLICATION_PROVENANCES,
    list_memberships_for_catalog,
    load_meta_catalog_membership,
)
from services import meta_catalog_push as push
from services.meta_catalog_reconcile import reconcile_meta_catalog_publish_stamps
from services.salla_variant_catalog_identity import (
    PROVENANCE_VARIANT_SLOT,
    ensure_variant_membership_slot,
    identity_for_retailer_id,
    upsert_native_publication_membership,
)
from services.whatsapp_catalog_retirement import (
    LEDGER_STATUS_PENDING,
    REASON_MANUAL_DELETED,
    REASON_MERCHANT_HIDDEN,
    SYNC_STATUS_RETIRED,
    attempt_product_channel_retirement,
    channel_identities_for_product,
    drain_channel_retirement_ledger,
    enqueue_channel_retirement_ledger,
    mark_product_channel_retire_pending,
)

CATALOG = "CAT-GENERIC-NATIVE"

_JSONB_ORIGINALS: dict = {}


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    if connection.dialect.name != "sqlite":
        return
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _JSONB_ORIGINALS[(table.name, col.name)] = col.type
                col.type = JSON()


@event.listens_for(Base.metadata, "after_create")
def _restore_jsonb(target, connection, **kw):
    if connection.dialect.name != "sqlite":
        return
    for table in target.sorted_tables:
        for col in table.columns:
            orig = _JSONB_ORIGINALS.pop((table.name, col.name), None)
            if orig is not None:
                col.type = orig


@pytest.fixture(autouse=True)
def _all_tenants_in_scope(monkeypatch):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS", raising=False)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class CatalogGraph:
    """One catalog keyed by retailer_id. Creates, updates and lookups; records every call."""

    def __init__(self, items=None):
        self.items = {rid: dict(v) for rid, v in (items or {}).items()}
        self.gets = []
        self.posts = []
        self.fail_posts = False
        self._n = 0

    def get(self, url, params=None, headers=None):
        self.gets.append((url, dict(params or {})))
        filt = (params or {}).get("filter")
        if "/products" in url and filt:
            rid = json.loads(filt)["retailer_id"]["eq"]
            item = self.items.get(rid)
            if item is None:
                return _Resp(200, {"data": []})
            return _Resp(200, {"data": [{"retailer_id": rid, "visibility": "published", **item}]})
        if "/products" in url:
            return _Resp(200, {"data": []})
        return _Resp(200, {"id": url.rsplit("/", 1)[-1], "name": "متجر تجريبي عام"})

    def post(self, url, data=None, headers=None, params=None):
        body = dict(data or {})
        self.posts.append((url, body))
        if self.fail_posts:
            return _Resp(400, {"error": {"code": 100, "message": "rejected"}})
        if url.rstrip("/").endswith(f"/{CATALOG}/products"):
            self._n += 1
            meta_id = f"META-NATIVE-{self._n}"
            self.items[body["retailer_id"]] = {**body, "id": meta_id}
            return _Resp(200, {"id": meta_id})
        meta_id = url.rstrip("/").rsplit("/", 1)[-1]
        for item in self.items.values():
            if item["id"] == meta_id:
                item.update(body)
                return _Resp(200, {"success": True})
        return _Resp(404, {"error": {"code": 100, "message": "unknown item"}})

    def close(self):
        return None


def _db():
    engine = create_engine("sqlite:///:memory:")
    event.listen(engine, "connect", lambda dbapi_conn, _rec: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.add(WhatsAppConnection(
        tenant_id=tenant.id, whatsapp_business_account_id=f"WABA-{tenant.id}", phone_number_id=f"PN-{tenant.id}",
        access_token="EAAB-test", meta_catalog_id=CATALOG, catalog_enabled=True,
        provider="meta", connection_type="embedded", extra_metadata={},
    ))
    session.commit()
    return session, tenant.id, engine


def _native(session, tid, rid, *, title="حذاء رياضي أبيض", price="250", meta_retailer_id=None, legacy_stamp=None):
    product = Product(
        tenant_id=tid, title=title, price=price, in_stock=True, stock_quantity=4,
        source=SOURCE_NAHLA_NATIVE, ownership_mode=OWNERSHIP_NAHLA_MANAGED, catalog_status="active",
        meta_retailer_id=meta_retailer_id, meta_item_id=legacy_stamp, sync_status="pending",
        extra_metadata={"currency": "SAR"},
    )
    session.add(product)
    session.flush()
    variant = ProductVariant(tenant_id=tid, product_id=product.id, retailer_id=rid, price=price, currency="SAR",
                             stock_quantity=4, in_stock=True, is_default=True)
    session.add(variant)
    session.commit()
    return product, variant


def _salla(session, tid, ext):
    product = Product(
        tenant_id=tid, external_id=ext, title="قميص قطني أزرق", price="140", in_stock=True, stock_quantity=5,
        source="salla", ownership_mode=OWNERSHIP_EXTERNAL_MANAGED, catalog_status="active", sync_status="synced",
        extra_metadata={"currency": "SAR", "status": "sale"},
    )
    session.add(product)
    session.flush()
    variant = ProductVariant(tenant_id=tid, product_id=product.id, salla_variant_id="881", retailer_id=f"{ext}-881",
                             price="140", currency="SAR", stock_quantity=5, in_stock=True)
    session.add(variant)
    session.commit()
    return product, variant


def _preview(rid, price):
    return {"payload": {"retailer_id": rid, "name": "حذاء رياضي أبيض", "description": "حذاء جري",
                        "image_url": "https://cdn.example/shoe.jpg", "url": "https://store.example/p/shoe",
                        "price": int(price) * 100, "currency": "SAR", "availability": "in stock"},
            "warnings": [], "fatal": False}


def _token():
    return patch.object(push, "_resolve_catalog_and_token", lambda conn, **k: (CATALOG, "tok"))


def _publish(session, tid, rid, graph, *, price="250", overrides=None):
    """One publish exactly as the push batch runs it: POST, then evidence only after a real create/update."""
    with patch.object(push, "preview_meta_variant_payload", return_value=_preview(rid, price)), _token():
        result = push.push_one_meta_catalog_item(session, tid, rid, confirm=True, client=graph,
                                                 payload_overrides=overrides)
        if result.get("ok") and result.get("action") in ("create", "update"):
            push._stamp_salla_batch_membership(session, tid, rid, str(result.get("meta_product_id") or ""), CATALOG,
                                              action=str(result["action"]), client=graph)
            # the orchestrator's success stamp (local state only)
            variant = session.query(ProductVariant).filter_by(tenant_id=tid, retailer_id=rid).first()
            parent = session.get(Product, variant.product_id)
            parent.sync_status = "synced"
            parent.last_synced_at = datetime.now(timezone.utc)
            session.commit()
    session.expire_all()
    return result


def _membership(session, tid, rid):
    return session.query(MetaCatalogMembership).filter_by(tenant_id=tid, catalog_id=CATALOG, retailer_id=rid).first()


# ── BL-1: a pre-POST slot never grants authority over a foreign item ───────

def _salla_with_reconcile_row(session, tid, ext, foreign_id):
    product, variant = _salla(session, tid, ext)
    session.add(MetaCatalogMembership(
        tenant_id=tid, catalog_id=CATALOG, retailer_id=f"{ext}-881", product_id=product.id, variant_id=variant.id,
        salla_variant_id="881", meta_item_id=foreign_id, verified_at=datetime.now(timezone.utc),
        provenance=PROVENANCE_GRAPH_RECONCILE,
    ))
    session.commit()
    return product, variant


def test_slot_then_push_never_updates_a_foreign_item_behind_a_reconcile_row():
    session, tid, engine = _db()
    try:
        _salla_with_reconcile_row(session, tid, "910100", "META-FOREIGN-X")
        graph = CatalogGraph({"910100-881": {"id": "META-FOREIGN-X", "availability": "in stock"}})
        with _token():
            push._prepare_salla_batch_membership_slot(session, tid, "910100-881")   # the batch's pre-POST step
        row = _membership(session, tid, "910100-881")
        assert row.provenance == PROVENANCE_GRAPH_RECONCILE and row.meta_item_id == "META-FOREIGN-X"
        result = _publish(session, tid, "910100-881", graph, price="140")
        assert result["action"] == push.ACTION_BLOCK_OWNERSHIP
        assert graph.posts == []
        assert _membership(session, tid, "910100-881").provenance == PROVENANCE_GRAPH_RECONCILE
    finally:
        session.close(); engine.dispose()


def test_slot_then_retire_never_writes_a_foreign_item_behind_a_reconcile_row():
    session, tid, engine = _db()
    try:
        product, variant = _salla_with_reconcile_row(session, tid, "910200", "META-FOREIGN-Y")
        ident = identity_for_retailer_id(product, [variant], "910200-881")
        assert ensure_variant_membership_slot(session, tenant_id=tid, catalog_id=CATALOG, identity=ident)["ok"]
        session.commit()
        assert channel_identities_for_product(session, product) == []
        graph = CatalogGraph({"910200-881": {"id": "META-FOREIGN-Y", "availability": "in stock"}})
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
        session.commit()
        with _token():
            out = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        assert graph.posts == [] and graph.gets == []
        assert out["retired"] == 0 and out["error_code"] == "no_publication_evidence"
        assert graph.items["910200-881"]["availability"] == "in stock"
    finally:
        session.close(); engine.dispose()


def test_a_new_slot_row_is_not_publication_evidence():
    session, tid, engine = _db()
    try:
        product, variant = _salla(session, tid, "910300")
        ident = identity_for_retailer_id(product, [variant], "910300-881")
        ensure_variant_membership_slot(session, tenant_id=tid, catalog_id=CATALOG, identity=ident)
        session.commit()
        row = _membership(session, tid, "910300-881")
        assert row.provenance == PROVENANCE_VARIANT_SLOT and row.meta_item_id is None
        assert row.provenance not in PUBLICATION_PROVENANCES
        # a later Salla create still records evidence for the item it created
        graph = CatalogGraph()
        result = _publish(session, tid, "910300-881", graph, price="140")
        assert result["action"] == "create"
        row = _membership(session, tid, "910300-881")
        assert row.provenance in PUBLICATION_PROVENANCES and row.meta_item_id == result["meta_product_id"]
    finally:
        session.close(); engine.dispose()


# ── BL-2: the full native lifecycle ───────────────────────────────────────

@pytest.mark.parametrize("rid", ["SHOE-WHITE-42", "nahla_v_77", "4455667"])
def test_native_create_update_retire_republish(rid):
    session, tid, engine = _db()
    try:
        product, _variant = _native(session, tid, rid, legacy_stamp=None)
        graph = CatalogGraph()
        created = _publish(session, tid, rid, graph, price="250")
        assert created["action"] == "create" and created["ok"]
        mid = created["meta_product_id"]
        row = _membership(session, tid, rid)
        assert (row.provenance, row.meta_item_id, row.product_id) == (PROVENANCE_NATIVE_PUSH, mid, product.id)

        updated = _publish(session, tid, rid, graph, price="230")
        assert updated["action"] == "update" and updated["ok"]
        assert graph.posts[-1][0].endswith(f"/{mid}") and graph.items[rid]["price"] == 23000

        product = session.get(Product, product.id)
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
        session.commit()
        with _token():
            out = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        session.expire_all()
        assert out["retired"] == 1 and out["refused"] == 0
        assert graph.items[rid]["availability"] == "out of stock" and graph.items[rid]["visibility"] == "staging"
        assert session.get(Product, product.id).sync_status == SYNC_STATUS_RETIRED

        republished = _publish(session, tid, rid, graph, price="230", overrides={"visibility": "published"})
        assert republished["action"] == "update" and republished["ok"]
        assert graph.items[rid]["visibility"] == "published" and graph.items[rid]["availability"] == "in stock"
        assert [u.rsplit("/", 1)[-1] for u, _ in graph.posts] == ["products", mid, mid, mid]
    finally:
        session.close(); engine.dispose()


def test_native_legacy_stamp_alone_still_never_authorizes_an_update():
    session, tid, engine = _db()
    try:
        _native(session, tid, "SHOE-LEGACY-1", legacy_stamp="META-OLD-1")
        graph = CatalogGraph({"SHOE-LEGACY-1": {"id": "META-OLD-1", "availability": "in stock"}})
        result = _publish(session, tid, "SHOE-LEGACY-1", graph)
        assert result["action"] == push.ACTION_BLOCK_OWNERSHIP and graph.posts == []
        assert _membership(session, tid, "SHOE-LEGACY-1") is None
    finally:
        session.close(); engine.dispose()


def test_a_failed_post_records_no_authority_and_a_later_lookup_cannot_adopt_the_item():
    session, tid, engine = _db()
    try:
        _native(session, tid, "SHOE-FAIL-1")
        graph = CatalogGraph()
        graph.fail_posts = True
        failed = _publish(session, tid, "SHOE-FAIL-1", graph)
        assert failed["ok"] is False and failed["error"] == "meta_http_error"
        assert _membership(session, tid, "SHOE-FAIL-1") is None
        # the item turns up on Graph anyway (another actor): a later attempt only sees an unproven match
        graph.fail_posts = False
        graph.items["SHOE-FAIL-1"] = {"id": "META-ELSEWHERE", "availability": "in stock"}
        posts_before = len(graph.posts)
        again = _publish(session, tid, "SHOE-FAIL-1", graph)
        assert again["action"] == push.ACTION_BLOCK_OWNERSHIP and len(graph.posts) == posts_before
        assert _membership(session, tid, "SHOE-FAIL-1") is None
    finally:
        session.close(); engine.dispose()


def test_native_evidence_never_rebinds_another_item_or_another_product():
    session, tid, engine = _db()
    try:
        product, variant = _native(session, tid, "SHOE-BIND-1")
        other, _ov = _native(session, tid, "SHOE-BIND-2", title="قميص قطني أزرق")
        assert upsert_native_publication_membership(
            session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-BIND-1", product_id=product.id,
            variant_id=variant.id, meta_item_id="META-B1")["ok"]
        session.commit()
        moved = upsert_native_publication_membership(
            session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-BIND-1", product_id=product.id,
            variant_id=variant.id, meta_item_id="META-B1-OTHER")
        assert moved["ok"] is False and moved["reason"] == "meta_item_id_immutable"
        stolen = upsert_native_publication_membership(
            session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-BIND-1", product_id=other.id,
            variant_id=None, meta_item_id="META-B1")
        assert stolen["ok"] is False
        reused = upsert_native_publication_membership(
            session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-BIND-2", product_id=other.id,
            variant_id=None, meta_item_id="META-B1")
        assert reused["ok"] is False and reused["reason"] == "meta_item_id_owned_other"
        assert _membership(session, tid, "SHOE-BIND-1").meta_item_id == "META-B1"
    finally:
        session.close(); engine.dispose()


# ── Native ownership rows never change catalog-card capability ─────────────

def _capability_view(session, tid, rids):
    listed = sorted((f.retailer_id, f.product_id, f.variant_id) for f in list_memberships_for_catalog(
        session, tenant_id=tid, catalog_id=CATALOG))
    loaded = {}
    for rid in rids:
        fact = load_meta_catalog_membership(session, tenant_id=tid, catalog_id=CATALOG, retailer_id=rid)
        loaded[rid] = None if fact is None else (fact.product_id, fact.variant_id, fact.meta_item_id)
    return listed, loaded


SCENARIO_RIDS = ["4455667", "SHOE-WHITE-42", "nahla_v_903", "nahla_p_904", "SKU-AMB-1", "SKU-RENAMED-1", "SKU-LEGACY-8"]


def _world(*, with_native_publication):
    """Same products and Graph in both worlds. The new world also holds the
    ownership rows a verified native POST writes; the baseline has none."""
    session, tid, engine = _db()
    products = {}
    for i, rid in enumerate(SCENARIO_RIDS):
        products[rid] = _native(session, tid, rid, title=f"منتج عام {i}")
    # SKU-AMB-1 is also claimed by another product: reconcile cannot map it
    _native(session, tid, "SKU-AMB-1-SIBLING", title="منتج عام مكرر", meta_retailer_id="SKU-AMB-1")
    live = {rid: {"meta_product_id": f"META-{rid}"} for rid in SCENARIO_RIDS}
    # a legitimate reconcile row that existed before any native push (capability to preserve)
    p8, v8 = products["SKU-LEGACY-8"]
    session.add(MetaCatalogMembership(
        tenant_id=tid, catalog_id=CATALOG, retailer_id="SKU-LEGACY-8", product_id=p8.id, variant_id=v8.id,
        meta_item_id="META-SKU-LEGACY-8", verified_at=datetime.now(timezone.utc), provenance=PROVENANCE_GRAPH_RECONCILE,
    ))
    session.commit()
    if with_native_publication:
        for rid in SCENARIO_RIDS[:-1]:
            p, v = products[rid]
            assert upsert_native_publication_membership(
                session, tenant_id=tid, catalog_id=CATALOG, retailer_id=rid, product_id=p.id, variant_id=v.id,
                meta_item_id=f"META-{rid}")["ok"], rid
        session.commit()
    # after the push, SKU-RENAMED-1 is renamed locally: reconcile finds no local claim for it
    products["SKU-RENAMED-1"][1].retailer_id = "SKU-RENAMED-2"
    session.commit()
    return session, tid, engine, products, live


def _reconcile(session, tid, live):
    with patch("services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
               return_value=(live, {"complete": True})):
        report = reconcile_meta_catalog_publish_stamps(session, tid, apply=True)
    session.commit()
    session.expire_all()
    assert report.error is None
    return report


def test_native_ownership_rows_match_reconcile_only_capability_before_and_after_reconcile():
    base = _world(with_native_publication=False)
    new = _world(with_native_publication=True)
    try:
        b_session, b_tid, _be, b_products, b_live = base
        n_session, n_tid, _ne, n_products, n_live = new
        # before any reconcile pass: identical (only the pre-existing reconcile row is visible)
        assert _capability_view(b_session, b_tid, SCENARIO_RIDS) == _capability_view(n_session, n_tid, SCENARIO_RIDS)
        listed, _loaded = _capability_view(n_session, n_tid, SCENARIO_RIDS)
        assert [r for r, _p, _v in listed] == ["SKU-LEGACY-8"]

        _reconcile(b_session, b_tid, b_live)
        _reconcile(n_session, n_tid, n_live)
        b_view = _capability_view(b_session, b_tid, SCENARIO_RIDS)
        n_view = _capability_view(n_session, n_tid, SCENARIO_RIDS)
        assert b_view == n_view
        visible = {r for r, _p, _v in n_view[0]}
        # mappable rids are visible exactly as reconcile records them; the rest are not
        assert {"4455667", "SHOE-WHITE-42", "SKU-LEGACY-8"} <= visible
        assert not visible & {"SKU-AMB-1", "SKU-RENAMED-1"}
        assert n_view[1]["nahla_v_903"] is None and n_view[1]["nahla_p_904"] is None

        # ownership survives where Graph still shows the published item
        prov = {m.retailer_id: m.provenance for m in n_session.query(MetaCatalogMembership).all()}
        assert prov["4455667"] == prov["SHOE-WHITE-42"] == PROVENANCE_NATIVE_PUSH_RECONCILED
        for rid in ("nahla_v_903", "nahla_p_904", "SKU-AMB-1", "SKU-RENAMED-1"):
            assert prov[rid] == PROVENANCE_NATIVE_PUSH, rid
        assert prov["SKU-LEGACY-8"] == PROVENANCE_GRAPH_RECONCILE

        # a later pass no longer maps SHOE-WHITE-42 (now ambiguous): baseline drops the row,
        # the new world keeps ownership only — capability identical again
        for session, tid, products in ((b_session, b_tid, b_products), (n_session, n_tid, n_products)):
            _native(session, tid, "SHOE-WHITE-42-SIB", title="منتج عام آخر", meta_retailer_id="SHOE-WHITE-42")
        # and the Graph item for 4455667 is gone, nahla_v_903 was replaced by another item
        for live in (b_live, n_live):
            live.pop("4455667")
            live["nahla_v_903"] = {"meta_product_id": "META-REPLACED"}
        _reconcile(b_session, b_tid, b_live)
        _reconcile(n_session, n_tid, n_live)
        assert _capability_view(b_session, b_tid, SCENARIO_RIDS) == _capability_view(n_session, n_tid, SCENARIO_RIDS)
        rows = {m.retailer_id: m for m in n_session.query(MetaCatalogMembership).all()}
        assert rows["SHOE-WHITE-42"].provenance == PROVENANCE_NATIVE_PUSH
        assert "4455667" not in rows and "nahla_v_903" not in rows     # observed gone / replaced: no authority
    finally:
        for world in (base, new):
            world[0].close(); world[2].dispose()


# ── S4: the readout resolves its Salla adapter with SELECTs only ──────────

ANOMALY = {"product_id": 7, "external_id": "910400", "in_stock": True, "stock_quantity": 2,
           "variant_count": 0, "variants_in_stock_count": 0}


def _integrations(session, tid, *, canonical_expires_at, canonical_needs_reauth=False, with_manual=True):
    rows = []
    if with_manual:
        rows.append(Integration(tenant_id=tid, provider="salla", enabled=True,
                                config={"api_key": "manual-token", "store_id": "S-1"}))
    rows.append(Integration(tenant_id=tid, provider="salla", enabled=True, config={
        "api_key": "sync-token", "refresh_token": "sync-refresh", "api_sync_enabled": True, "store_id": "S-1",
        "expires_at": canonical_expires_at, "needs_reauth": canonical_needs_reauth,
    }))
    session.add_all(rows)
    session.commit()


def _integration_state(session):
    session.expire_all()
    return sorted((i.id, bool(i.enabled), json.dumps(i.config, sort_keys=True)) for i in session.query(Integration).all())


def _count_writes(session):
    writes = {"flush": 0, "commit": 0}
    event.listen(session, "after_flush", lambda *_a: writes.__setitem__("flush", writes["flush"] + 1))
    event.listen(session, "after_commit", lambda *_a: writes.__setitem__("commit", writes["commit"] + 1))
    return writes


@pytest.mark.parametrize("expired", [False, True])
def test_readout_adapter_resolution_with_duplicate_salla_rows_writes_nothing(expired):
    from services import catalog_trial_readout as readout

    session, tid, engine = _db()
    try:
        when = datetime.now(timezone.utc) + (timedelta(minutes=-5) if expired else timedelta(hours=2))
        _integrations(session, tid, canonical_expires_at=when.isoformat())
        before = _integration_state(session)
        calls = []

        async def _http_get(url, headers, params=None):
            calls.append(headers.get("Authorization"))
            body = {"data": []} if url.endswith("/variants") else {"data": {"id": 910400, "quantity": 2}}
            return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)

        writes = _count_writes(session)
        with patch.object(readout, "_salla_http_get", _http_get), \
             patch("store_adapters.salla_adapter.SallaAdapter._get", side_effect=AssertionError("refreshing helper")), \
             patch("store_integration.registry.pick_active_salla_integration",
                   side_effect=AssertionError("canonicalising resolver")):
            out = readout._salla_check(session, tid, [ANOMALY])
        assert writes == {"flush": 0, "commit": 0}
        assert not session.dirty and not session.new
        assert _integration_state(session) == before                   # no toggles, no annotations
        if expired:
            assert out["checked"] == [{"product_id": 7, "external_id": "910400",
                                       "error": readout.SALLA_READ_REFUSED_TOKEN_EXPIRED}]
            assert calls == []
        else:
            assert calls == ["Bearer sync-token", "Bearer sync-token"]   # the registry's canonical row, read as is
            assert out["reads"] == ["GET /products/910400", "GET /products/910400/variants"]
    finally:
        session.close(); engine.dispose()


def test_readout_refuses_a_canonical_row_that_needs_reauth_without_writing():
    from services import catalog_trial_readout as readout

    session, tid, engine = _db()
    try:
        # a flagged row always loses the registry ranking, so it is chosen only when it is the only row
        _integrations(session, tid, canonical_expires_at=None, canonical_needs_reauth=True, with_manual=False)
        before = _integration_state(session)
        writes = _count_writes(session)
        out = readout._salla_check(session, tid, [ANOMALY])
        assert out["error"] == readout.SALLA_READ_REFUSED_NEEDS_REAUTH
        assert writes == {"flush": 0, "commit": 0} and _integration_state(session) == before
    finally:
        session.close(); engine.dispose()


# ── Refusals are visible; catalog_not_current is retried, never written ────

def test_refused_withdrawal_is_visible_in_status_and_not_counted_as_retired():
    from services.whatsapp_catalog_sync import _drain_retirements, build_whatsapp_catalog_sync_status

    session, tid, engine = _db()
    try:
        product, _v = _native(session, tid, "SHOE-UNPROVEN-1", legacy_stamp="META-UNPROVEN")
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
        # a ledger row written before evidence was required (no catalog): carries no evidence
        session.add(CatalogChannelRetirement(
            tenant_id=tid, catalog_id=None, retailer_id="SHOE-GONE-1", meta_item_id="META-GONE",
            status="pending", attempts=0, reason=REASON_MANUAL_DELETED,
        ))
        session.commit()
        graph = CatalogGraph({"SHOE-UNPROVEN-1": {"id": "META-UNPROVEN", "availability": "in stock"}})
        with _token():
            summary = _drain_retirements(session, tid, [product.id], limit=10, client=graph)
        assert graph.posts == []
        assert summary["retired"] == 0 and summary["refused"] == 1
        assert summary["ledger"]["refused"] == 1
        status = build_whatsapp_catalog_sync_status(session, tid)
        retirement = status["retirement"]
        assert retirement["refused"] == 2 and retirement["refused_products"] == 1 and retirement["ledger_refused"] == 1
        assert retirement["retired_products"] == 0
        assert status["stages"]["retirement"]["state"] == "attention"
        assert status["stages"]["retirement"]["refused"] == 2
        codes = {(f["product_id"], f["error_code"], f["action_code"]) for f in status["failures"]}
        assert (product.id, "retire_ownership_unverified", "check_item_in_meta") in codes
    finally:
        session.close(); engine.dispose()


def test_catalog_not_current_is_retried_and_written_only_once_the_catalog_is_current_again():
    session, tid, engine = _db()
    try:
        product, _v = _native(session, tid, "SHOE-MOVE-1")
        graph = CatalogGraph()
        created = _publish(session, tid, "SHOE-MOVE-1", graph)
        mid = created["meta_product_id"]
        product = session.get(Product, product.id)
        assert enqueue_channel_retirement_ledger(
            session, tid, channel_identities_for_product(session, product), reason=REASON_MANUAL_DELETED) == 1
        session.commit()
        posts_before = len(graph.posts)
        other = patch.object(push, "_resolve_catalog_and_token", lambda conn, **k: ("CAT-SOMEONE-ELSE", "tok"))
        with other:
            first = drain_channel_retirement_ledger(session, tid, limit=10, client=graph)
        session.expire_all()
        row = session.query(CatalogChannelRetirement).filter_by(tenant_id=tid, retailer_id="SHOE-MOVE-1").one()
        assert len(graph.posts) == posts_before                    # never written while the catalog is not current
        assert first["refused"] == 0 and row.status == LEDGER_STATUS_PENDING and int(row.attempts) == 1
        assert row.last_error == "catalog_not_current"
        row.next_attempt_at = None
        session.commit()
        with _token():
            second = drain_channel_retirement_ledger(session, tid, limit=10, client=graph)
        assert second["retired"] == 1
        assert graph.posts[-1][0].endswith(f"/{mid}") and graph.items["SHOE-MOVE-1"]["availability"] == "out of stock"
    finally:
        session.close(); engine.dispose()


def test_product_withdrawal_in_a_catalog_that_is_not_current_is_retried_not_refused():
    session, tid, engine = _db()
    try:
        product, _v = _native(session, tid, "SHOE-MOVE-2")
        graph = CatalogGraph()
        mid = _publish(session, tid, "SHOE-MOVE-2", graph)["meta_product_id"]
        product = session.get(Product, product.id)
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
        session.commit()
        posts_before = len(graph.posts)
        with patch.object(push, "_resolve_catalog_and_token", lambda conn, **k: ("CAT-SOMEONE-ELSE", "tok")):
            first = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        session.expire_all()
        meta = session.get(Product, product.id).extra_metadata["sync_meta"]
        assert len(graph.posts) == posts_before
        assert first["refused"] == 0 and first["failed"] == 1
        assert meta["retire_pending"] is True and not meta.get("retire_blocked")
        row = session.get(Product, product.id)
        row.extra_metadata = {**row.extra_metadata, "sync_meta": {**meta, "next_retire_at": None}}
        session.commit()
        with _token():
            second = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        assert second["retired"] == 1
        assert graph.posts[-1][0].endswith(f"/{mid}")
    finally:
        session.close(); engine.dispose()


def test_native_evidence_never_changes_what_capability_readers_see():
    session, tid, engine = _db()
    try:
        # ownership-only row: a second verified update keeps it invisible
        p1, v1 = _native(session, tid, "SHOE-OWN-1")
        for _ in range(2):
            assert upsert_native_publication_membership(
                session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-OWN-1", product_id=p1.id,
                variant_id=v1.id, meta_item_id="META-OWN-1")["ok"]
            session.commit()
            assert _membership(session, tid, "SHOE-OWN-1").provenance == PROVENANCE_NATIVE_PUSH
            assert load_meta_catalog_membership(session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-OWN-1") is None
        # a reconcile row (parent-level, no Graph id) stays visible with the same local referent
        p2, v2 = _native(session, tid, "SHOE-VIS-1")
        session.add(MetaCatalogMembership(
            tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-VIS-1", product_id=p2.id, variant_id=None,
            meta_item_id=None, verified_at=datetime.now(timezone.utc), provenance=PROVENANCE_GRAPH_RECONCILE,
        ))
        session.commit()
        before = load_meta_catalog_membership(session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-VIS-1")
        assert upsert_native_publication_membership(
            session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-VIS-1", product_id=p2.id,
            variant_id=v2.id, meta_item_id="META-VIS-1")["ok"]
        session.commit()
        after = load_meta_catalog_membership(session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-VIS-1")
        assert (after.product_id, after.variant_id) == (before.product_id, before.variant_id) == (p2.id, None)
        assert _membership(session, tid, "SHOE-VIS-1").provenance == PROVENANCE_NATIVE_PUSH_RECONCILED
    finally:
        session.close(); engine.dispose()


# ── SF-A: a verified create replaces only a stale reconcile observation ────

class ReplacingGraph(CatalogGraph):
    """The create POST returns one id, but by the corroborating lookup Graph shows *lookup_item* under that retailer_id."""

    def __init__(self, lookup_item):
        super().__init__()
        self.lookup_item = lookup_item

    def post(self, url, data=None, headers=None, params=None):
        resp = super().post(url, data=data, headers=headers, params=params)
        body = dict(data or {})
        if url.rstrip("/").endswith(f"/{CATALOG}/products"):
            rid = body["retailer_id"]
            if self.lookup_item is None:
                self.items.pop(rid, None)                      # not visible yet / gone again
            elif self.lookup_item == "AMBIGUOUS":
                self.items[rid]["dup"] = True
            else:
                self.items[rid] = {**self.items[rid], "id": self.lookup_item}
        return resp

    def get(self, url, params=None, headers=None):
        filt = (params or {}).get("filter")
        if "/products" in url and filt:
            rid = json.loads(filt)["retailer_id"]["eq"]
            item = self.items.get(rid)
            if item and item.get("dup"):
                self.gets.append((url, dict(params or {})))
                row = {"retailer_id": rid, **item}
                return _Resp(200, {"data": [row, {**row, "id": "META-TWIN"}]})
        return super().get(url, params=params, headers=headers)


def _stale_observation(session, tid, product, variant, rid, *, stale="GONE-1", provenance=PROVENANCE_GRAPH_RECONCILE,
                       salla_variant_id=None):
    session.add(MetaCatalogMembership(
        tenant_id=tid, catalog_id=CATALOG, retailer_id=rid, product_id=product.id,
        variant_id=variant.id if salla_variant_id else None, salla_variant_id=salla_variant_id, meta_item_id=stale,
        verified_at=datetime.now(timezone.utc), provenance=provenance,
    ))
    session.commit()


@pytest.mark.parametrize("kind", ["native", "salla"])
def test_stale_observation_then_verified_create_update_retire_republish(kind):
    session, tid, engine = _db()
    try:
        if kind == "native":
            rid = "SHOE-STALE-1"
            product, variant = _native(session, tid, rid)
            _stale_observation(session, tid, product, variant, rid)
        else:
            product, variant = _salla(session, tid, "910500")
            rid = "910500-881"
            _stale_observation(session, tid, product, variant, rid, salla_variant_id="881")
        graph = CatalogGraph()                                     # GONE-1 is not on Graph: lookup finds nothing
        created = _publish(session, tid, rid, graph, price="250")
        assert created["action"] == "create" and created["ok"]
        mid = created["meta_product_id"]
        row = _membership(session, tid, rid)
        assert row.meta_item_id == mid and row.provenance in PUBLICATION_PROVENANCES
        assert row.product_id == product.id                       # same local referent
        if kind == "native":
            assert row.provenance == PROVENANCE_NATIVE_PUSH_RECONCILED and row.variant_id is None
        updated = _publish(session, tid, rid, graph, price="230")
        assert updated["action"] == "update" and graph.posts[-1][0].endswith(f"/{mid}")
        product = session.get(Product, product.id)
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
        session.commit()
        with _token():
            out = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        assert out["retired"] == 1 and graph.items[rid]["visibility"] == "staging"
        republished = _publish(session, tid, rid, graph, price="230", overrides={"visibility": "published"})
        assert republished["action"] == "update" and graph.items[rid]["visibility"] == "published"
    finally:
        session.close(); engine.dispose()


@pytest.mark.parametrize("lookup_item, label", [
    ("META-REPLACED", "replaced between POST and lookup"),
    (None, "lookup finds no item (stale / not yet visible)"),
    ("AMBIGUOUS", "lookup returns two rows"),
])
def test_uncorroborated_create_never_replaces_the_observation(lookup_item, label):
    session, tid, engine = _db()
    try:
        product, variant = _native(session, tid, "SHOE-STALE-2")
        _stale_observation(session, tid, product, variant, "SHOE-STALE-2")
        graph = ReplacingGraph(lookup_item)
        with patch.object(push, "preview_meta_variant_payload", return_value=_preview("SHOE-STALE-2", "250")), _token():
            result = push.push_one_meta_catalog_item(session, tid, "SHOE-STALE-2", confirm=True, client=graph)
            assert result["action"] == "create" and result["ok"], label
            with pytest.raises(push.MetaCatalogPushError):
                push._stamp_salla_batch_membership(session, tid, "SHOE-STALE-2", result["meta_product_id"], CATALOG,
                                                   action="create", client=graph)
        session.rollback()
        row = _membership(session, tid, "SHOE-STALE-2")
        assert (row.meta_item_id, row.provenance) == ("GONE-1", PROVENANCE_GRAPH_RECONCILE), label
    finally:
        session.close(); engine.dispose()


def test_repair_refuses_everything_but_a_corroborated_create_over_a_reconcile_observation():
    from services.salla_variant_catalog_identity import replace_stale_observation_after_create

    session, tid, engine = _db()
    try:
        other_tenant = Tenant(name="متجر تجريبي ثانٍ", is_active=True)
        session.add(other_tenant)
        session.commit()
        p, v = _native(session, tid, "SHOE-R-1")
        _stale_observation(session, tid, p, v, "SHOE-R-1")
        q, w = _native(session, tid, "SHOE-R-2")
        _stale_observation(session, tid, q, w, "SHOE-R-2", stale="META-OURS", provenance=PROVENANCE_NATIVE_PUSH)
        r, x = _native(session, tid, "SHOE-R-3")
        _stale_observation(session, tid, r, x, "SHOE-R-3", stale="META-HELD")
        o, ov = _native(session, other_tenant.id, "SHOE-R-1")
        session.add(MetaCatalogMembership(
            tenant_id=other_tenant.id, catalog_id=CATALOG, retailer_id="SHOE-R-1", product_id=o.id, variant_id=None,
            meta_item_id="GONE-OTHER", verified_at=datetime.now(timezone.utc), provenance=PROVENANCE_GRAPH_RECONCILE,
        ))
        session.commit()

        def repair(rid, product, created, corroborated, *, catalog=CATALOG, tenant=tid):
            return replace_stale_observation_after_create(
                session, tenant_id=tenant, catalog_id=catalog, retailer_id=rid, product_id=product.id, variant_id=None,
                created_meta_item_id=created, corroborated_meta_item_id=corroborated,
                publication_provenance=PROVENANCE_NATIVE_PUSH_RECONCILED)

        assert repair("SHOE-R-1", p, "META-NEW", "META-OTHER")["reason"] == "create_not_corroborated"
        assert repair("SHOE-R-1", p, "META-NEW", "")["reason"] == "create_not_corroborated"
        assert repair("SHOE-R-1", p, "", "")["reason"] == "create_not_corroborated"
        assert repair("SHOE-R-2", q, "META-NEW-2", "META-NEW-2")["reason"] == "meta_item_id_immutable"   # publication row
        assert repair("SHOE-R-1", q, "META-NEW", "META-NEW")["reason"] == "product_id_immutable"        # another product
        assert repair("SHOE-R-1", p, "META-HELD", "META-HELD")["reason"] == "meta_item_id_owned_other"
        assert repair("SHOE-R-1", p, "META-NEW", "META-NEW", catalog="CAT-OTHER")["reason"] == "no_observation_to_replace"
        assert repair("SHOE-UNKNOWN", p, "META-NEW", "META-NEW")["reason"] == "no_observation_to_replace"
        session.commit()
        rows = {(m.tenant_id, m.retailer_id): (m.meta_item_id, m.provenance) for m in session.query(MetaCatalogMembership).all()}
        assert rows[(tid, "SHOE-R-1")] == ("GONE-1", PROVENANCE_GRAPH_RECONCILE)
        assert rows[(tid, "SHOE-R-2")] == ("META-OURS", PROVENANCE_NATIVE_PUSH)
        # the corroborated create repairs only this tenant's row, never another tenant's same key
        assert repair("SHOE-R-1", p, "META-NEW", "META-NEW")["ok"]
        session.commit()
        rows = {(m.tenant_id, m.retailer_id): (m.meta_item_id, m.provenance) for m in session.query(MetaCatalogMembership).all()}
        assert rows[(tid, "SHOE-R-1")] == ("META-NEW", PROVENANCE_NATIVE_PUSH_RECONCILED)
        assert rows[(other_tenant.id, "SHOE-R-1")] == ("GONE-OTHER", PROVENANCE_GRAPH_RECONCILE)
    finally:
        session.close(); engine.dispose()


def test_an_update_never_triggers_the_stale_observation_repair():
    session, tid, engine = _db()
    try:
        product, variant = _native(session, tid, "SHOE-STALE-3")
        _stale_observation(session, tid, product, variant, "SHOE-STALE-3")
        graph = CatalogGraph({"SHOE-STALE-3": {"id": "META-LIVE-3", "availability": "in stock"}})
        with _token(), pytest.raises(push.MetaCatalogPushError):
            push._stamp_salla_batch_membership(session, tid, "SHOE-STALE-3", "META-LIVE-3", CATALOG,
                                               action="update", client=graph)
        session.rollback()
        assert graph.gets == []                                    # no corroborating lookup for an update
        assert _membership(session, tid, "SHOE-STALE-3").meta_item_id == "GONE-1"
    finally:
        session.close(); engine.dispose()


def test_repair_is_skipped_when_the_connection_catalog_changed():
    session, tid, engine = _db()
    try:
        product, variant = _native(session, tid, "SHOE-STALE-4")
        _stale_observation(session, tid, product, variant, "SHOE-STALE-4")
        graph = CatalogGraph({"SHOE-STALE-4": {"id": "META-NEW-4", "availability": "in stock"}})
        conn = session.query(WhatsAppConnection).filter_by(tenant_id=tid).one()
        conn.meta_catalog_id = "CAT-MOVED"
        session.commit()
        with _token(), pytest.raises(push.MetaCatalogPushError):
            push._stamp_salla_batch_membership(session, tid, "SHOE-STALE-4", "META-NEW-4", CATALOG,
                                               action="create", client=graph)
        session.rollback()
        assert graph.gets == []
        assert _membership(session, tid, "SHOE-STALE-4").meta_item_id == "GONE-1"
    finally:
        session.close(); engine.dispose()


def test_generic_upserts_keep_their_immutable_id_contract():
    """Only the narrow create repair may replace an id; the generic upserts never do."""
    from services.salla_variant_catalog_identity import upsert_variant_membership

    session, tid, engine = _db()
    try:
        sp, sv = _salla(session, tid, "910800")
        _stale_observation(session, tid, sp, sv, "910800-881", stale="META-PUB", provenance="salla_variant_push",
                           salla_variant_id="881")
        sq, sw = _salla(session, tid, "910801")
        _stale_observation(session, tid, sq, sw, "910801-881", stale="GONE-S", salla_variant_id="881")
        np_, nv = _native(session, tid, "SHOE-IMM-1")
        _stale_observation(session, tid, np_, nv, "SHOE-IMM-1", stale="GONE-N")
        for product, variant, rid in ((sp, sv, "910800-881"), (sq, sw, "910801-881")):
            ident = identity_for_retailer_id(product, [variant], rid)
            out = upsert_variant_membership(session, tenant_id=tid, catalog_id=CATALOG, identity=ident, meta_item_id="META-NEW")
            assert out["ok"] is False and out["reason"] == "meta_item_id_immutable", rid
        out = upsert_native_publication_membership(session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-IMM-1",
                                                   product_id=np_.id, variant_id=nv.id, meta_item_id="META-NEW")
        assert out["ok"] is False and out["reason"] == "meta_item_id_immutable"
        session.commit()
        ids = {m.retailer_id: (m.meta_item_id, m.provenance) for m in session.query(MetaCatalogMembership).all()}
        assert ids == {"910800-881": ("META-PUB", "salla_variant_push"),
                       "910801-881": ("GONE-S", PROVENANCE_GRAPH_RECONCILE),
                       "SHOE-IMM-1": ("GONE-N", PROVENANCE_GRAPH_RECONCILE)}
    finally:
        session.close(); engine.dispose()


def _referent_view(session, tid, rids):
    """What native catalog-card capability reads: membership_authorizes_send decides on
    (tenant, catalog, retailer_id, product_id, variant_id) only — never on meta_item_id."""
    listed = sorted((f.retailer_id, f.product_id, f.variant_id) for f in list_memberships_for_catalog(
        session, tenant_id=tid, catalog_id=CATALOG))
    loaded = {}
    for rid in rids:
        fact = load_meta_catalog_membership(session, tenant_id=tid, catalog_id=CATALOG, retailer_id=rid)
        loaded[rid] = None if fact is None else (fact.product_id, fact.variant_id)
    return listed, loaded


REPAIR_RIDS = ["5566778", "SHOE-STALE-SKU", "nahla_v_905", "SHOE-NULLID", "SHOE-AMB-2", "910700-881"]


def _repair_world(*, repaired):
    """Stale reconcile observations (variant-level, parent-level, nahla_*, null-id, later
    ambiguous, Salla); Graph now shows the items a verified create made. The repaired world
    also applies what that create records; the other is reconcile alone."""
    from services.salla_variant_catalog_identity import replace_stale_observation_after_create

    session, tid, engine = _db()
    made = {}
    for i, rid in enumerate(REPAIR_RIDS[:-1]):
        made[rid] = _native(session, tid, rid, title=f"منتج عام {i}")
    sp, sv = _salla(session, tid, "910700")
    made["910700-881"] = (sp, sv)
    now = datetime.now(timezone.utc)
    rows = {
        "5566778": dict(variant=True, stale="GONE-1"),
        "SHOE-STALE-SKU": dict(variant=False, stale="GONE-2"),
        "nahla_v_905": dict(variant=False, stale="GONE-3"),
        "SHOE-NULLID": dict(variant=False, stale=None),
        "SHOE-AMB-2": dict(variant=True, stale="GONE-5"),
        "910700-881": dict(variant=True, stale="GONE-6"),
    }
    for rid, spec in rows.items():
        p, v = made[rid]
        session.add(MetaCatalogMembership(
            tenant_id=tid, catalog_id=CATALOG, retailer_id=rid, product_id=p.id, variant_id=v.id if spec["variant"] else None,
            salla_variant_id="881" if rid == "910700-881" else None, meta_item_id=spec["stale"], verified_at=now,
            provenance=PROVENANCE_GRAPH_RECONCILE,
        ))
    session.commit()
    live = {rid: {"meta_product_id": f"CREATED-{rid}"} for rid in REPAIR_RIDS}
    if repaired:
        for rid, spec in rows.items():
            p, v = made[rid]
            if spec["stale"] is None:
                out = upsert_native_publication_membership(
                    session, tenant_id=tid, catalog_id=CATALOG, retailer_id=rid, product_id=p.id, variant_id=v.id,
                    meta_item_id=f"CREATED-{rid}")
            else:
                salla = rid == "910700-881"
                out = replace_stale_observation_after_create(
                    session, tenant_id=tid, catalog_id=CATALOG, retailer_id=rid, product_id=p.id,
                    variant_id=None if salla else v.id, created_meta_item_id=f"CREATED-{rid}",
                    corroborated_meta_item_id=f"CREATED-{rid}",
                    publication_provenance="salla_variant_push" if salla else PROVENANCE_NATIVE_PUSH_RECONCILED,
                    salla_identity=identity_for_retailer_id(p, [v], rid) if salla else None)
            assert out["ok"], (rid, out)
        session.commit()
    return session, tid, engine, made, live


def test_stale_observation_repair_neither_widens_nor_narrows_catalog_card_capability():
    base = _repair_world(repaired=False)
    new = _repair_world(repaired=True)
    try:
        b_session, b_tid, _be, b_made, b_live = base
        n_session, n_tid, _ne, n_made, n_live = new
        # right after the create: the repaired rows keep the observation's referent and visibility
        assert _referent_view(b_session, b_tid, REPAIR_RIDS) == _referent_view(n_session, n_tid, REPAIR_RIDS)
        _reconcile(b_session, b_tid, b_live)
        _reconcile(n_session, n_tid, n_live)
        assert _referent_view(b_session, b_tid, REPAIR_RIDS) == _referent_view(n_session, n_tid, REPAIR_RIDS)
        owned = {m.retailer_id for m in n_session.query(MetaCatalogMembership).all() if m.provenance in PUBLICATION_PROVENANCES}
        assert {"5566778", "SHOE-STALE-SKU", "SHOE-NULLID", "SHOE-AMB-2", "910700-881", "nahla_v_905"} == owned
        assert not {m.retailer_id for m in b_session.query(MetaCatalogMembership).all()
                    if m.provenance in PUBLICATION_PROVENANCES}
        # mappings change: SHOE-AMB-2 becomes ambiguous, SHOE-STALE-SKU's item is replaced on Graph
        for session, tid, live in ((b_session, b_tid, b_live), (n_session, n_tid, n_live)):
            _native(session, tid, "SHOE-AMB-2-SIB", title="منتج عام مكرر", meta_retailer_id="SHOE-AMB-2")
            live["SHOE-STALE-SKU"] = {"meta_product_id": "META-REPLACED"}
        _reconcile(b_session, b_tid, b_live)
        _reconcile(n_session, n_tid, n_live)
        assert _referent_view(b_session, b_tid, REPAIR_RIDS) == _referent_view(n_session, n_tid, REPAIR_RIDS)
        rows = {m.retailer_id: m for m in n_session.query(MetaCatalogMembership).all()}
        assert rows["SHOE-AMB-2"].provenance == PROVENANCE_NATIVE_PUSH                      # ownership kept, not visible
        assert rows["SHOE-STALE-SKU"].provenance == PROVENANCE_GRAPH_RECONCILE             # replaced item: no authority
        assert rows["SHOE-STALE-SKU"].meta_item_id == "META-REPLACED"
    finally:
        for world in (base, new):
            world[0].close(); world[2].dispose()


def test_repair_never_overwrites_membership_changed_since_the_create():
    from services.salla_variant_catalog_identity import replace_stale_observation_after_create

    session, tid, engine = _db()
    try:
        product, variant = _native(session, tid, "SHOE-RACE-1")
        _stale_observation(session, tid, product, variant, "SHOE-RACE-1")
        other = sessionmaker(bind=engine)()

        def repair():
            return replace_stale_observation_after_create(
                session, tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-RACE-1", product_id=product.id,
                variant_id=variant.id, created_meta_item_id="META-MINE", corroborated_meta_item_id="META-MINE",
                publication_provenance=PROVENANCE_NATIVE_PUSH_RECONCILED)

        # this session read the stale observation first and still holds that copy,
        # through the class the repair queries (``models``; ``database.models``
        # maps a separate class in this layout)
        import models as service_models

        held = session.query(service_models.MetaCatalogMembership).filter_by(
            tenant_id=tid, catalog_id=CATALOG, retailer_id="SHOE-RACE-1").one()
        assert held.provenance == PROVENANCE_GRAPH_RECONCILE
        # another writer commits newer publication evidence for the same key
        row = other.query(MetaCatalogMembership).filter_by(tenant_id=tid, retailer_id="SHOE-RACE-1").one()
        row.meta_item_id, row.provenance = "META-NEWER", PROVENANCE_NATIVE_PUSH
        other.commit()
        assert repair()["reason"] == "meta_item_id_immutable"
        session.commit()
        session.expire_all()
        assert (_membership(session, tid, "SHOE-RACE-1").meta_item_id,
                _membership(session, tid, "SHOE-RACE-1").provenance) == ("META-NEWER", PROVENANCE_NATIVE_PUSH)
        # a concurrent reconcile already recorded the created item: nothing stale to replace
        row = other.query(MetaCatalogMembership).filter_by(tenant_id=tid, retailer_id="SHOE-RACE-1").one()
        row.meta_item_id, row.provenance = "META-MINE", PROVENANCE_GRAPH_RECONCILE
        other.commit()
        assert repair()["reason"] == "no_stale_observation"
        # the row was deleted meanwhile
        other.delete(other.query(MetaCatalogMembership).filter_by(tenant_id=tid, retailer_id="SHOE-RACE-1").one())
        other.commit()
        assert repair()["reason"] == "no_observation_to_replace"
        other.close()
    finally:
        session.close(); engine.dispose()


# ── Read-after-write lag: a verified-later create is recovered, nothing else is ──

class LaggingGraph(CatalogGraph):
    """The lookup right after a create sees nothing (read-after-write lag); later lookups see the item."""

    def __init__(self, items=None):
        super().__init__(items)
        self.lag = 0
        self.after_create = None          # optional callable(rid) run once the create POST returned

    def post(self, url, data=None, headers=None, params=None):
        resp = super().post(url, data=data, headers=headers, params=params)
        if url.rstrip("/").endswith(f"/{CATALOG}/products") and resp.status_code == 200:
            self.lag = 1
            if self.after_create:
                self.after_create(dict(data or {})["retailer_id"])
        return resp

    def get(self, url, params=None, headers=None):
        if self.lag and (params or {}).get("filter"):
            self.lag -= 1
            self.gets.append((url, dict(params or {})))
            return _Resp(200, {"data": []})
        return super().get(url, params=params, headers=headers)


def _sync(session, tid, product_id, graph):
    from services.native_meta_sync_orchestrator import attempt_native_meta_sync

    with _token(), patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status",
                         return_value={"ok": True, "expected_catalog_linked": True}):
        out = attempt_native_meta_sync(session, tid, product_id, client=graph)
    session.expire_all()
    return out


def _syncable(product, session):
    product.extra_metadata = {**(product.extra_metadata or {}), "image_url": "https://cdn.example/item.jpg",
                              "product_url": "https://store.example/p/item"}
    product.sync_status = "pending"
    session.commit()
    return product


def _pending(session, product_id):
    sm = (session.get(Product, product_id).extra_metadata or {}).get("sync_meta") or {}
    return sm.get("pending_publications") or {}


def _lagged_create(session, tid, product, graph, rid):
    first = _sync(session, tid, product.id, graph)
    assert first["ok"] is False and first["error_code"] == "verification_failed"
    assert _membership(session, tid, rid) is None or _membership(session, tid, rid).provenance not in PUBLICATION_PROVENANCES
    recorded = _pending(session, product.id)[f"{CATALOG}|{rid}"]
    assert recorded["meta_item_id"] == graph.items[rid]["id"] and recorded["product_id"] == product.id
    return recorded["meta_item_id"]


@pytest.mark.parametrize("kind", ["native", "salla"])
def test_create_with_a_lagging_lookup_is_recovered_by_a_corroborating_retry(kind):
    session, tid, engine = _db()
    try:
        if kind == "native":
            rid = "SHOE-LAG-1"
            product, _v = _native(session, tid, rid)
        else:
            product, _v = _salla(session, tid, "910900")
            rid = "910900-881"
        _syncable(product, session)
        graph = LaggingGraph()
        mid = _lagged_create(session, tid, product, graph, rid)
        # retry: the scoped lookup now returns exactly the recorded id
        second = _sync(session, tid, product.id, graph)
        assert second["ok"] is True, second
        row = _membership(session, tid, rid)
        assert row.meta_item_id == mid and row.provenance in PUBLICATION_PROVENANCES
        if kind == "salla":
            # Salla evidence is the variant identity's own row, never a native row
            assert (row.provenance, row.salla_variant_id, row.variant_id) == ("salla_variant_push", "881", _v.id)
        else:
            assert row.provenance == PROVENANCE_NATIVE_PUSH
        assert _pending(session, product.id) == {}
        assert [u.rsplit("/", 1)[-1] for u, _ in graph.posts] == ["products", mid]
        # then the ordinary lifecycle
        product = session.get(Product, product.id)
        product.price = "199"
        session.commit()
        updated = _publish(session, tid, rid, graph, price="199")
        assert updated["action"] == "update" and graph.posts[-1][0].endswith(f"/{mid}")
        product = session.get(Product, product.id)
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
        session.commit()
        with _token():
            out = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        assert out["retired"] == 1 and graph.items[rid]["visibility"] == "staging"
        republished = _publish(session, tid, rid, graph, price="199", overrides={"visibility": "published"})
        assert republished["action"] == "update" and graph.items[rid]["visibility"] == "published"
    finally:
        session.close(); engine.dispose()


@pytest.mark.parametrize("case", [
    "item_replaced_before_the_retry",
    "two_rows_on_the_retry",
    "stale_observation_with_another_id",
    "newer_publication_for_another_item",
    "record_copied_to_another_product",
    "connection_moved_to_another_catalog",
])
def test_a_recorded_attempt_never_becomes_authority_without_exact_corroboration(case):
    session, tid, engine = _db()
    try:
        product, variant = _native(session, tid, "SHOE-LAG-2")
        _syncable(product, session)
        graph = LaggingGraph()
        mid = _lagged_create(session, tid, product, graph, "SHOE-LAG-2")
        posts_before = len(graph.posts)
        if case == "item_replaced_before_the_retry":
            graph.items["SHOE-LAG-2"] = {**graph.items["SHOE-LAG-2"], "id": "META-SOMEONE-ELSE"}
        elif case == "two_rows_on_the_retry":
            graph.items["SHOE-LAG-2"]["dup"] = True
            graph.get = ReplacingGraph.get.__get__(graph)
        elif case == "stale_observation_with_another_id":
            _stale_observation(session, tid, product, variant, "SHOE-LAG-2", stale="GONE-9")
        elif case == "newer_publication_for_another_item":
            _stale_observation(session, tid, product, variant, "SHOE-LAG-2", stale="META-NEWER",
                               provenance=PROVENANCE_NATIVE_PUSH)
        elif case == "record_copied_to_another_product":
            other, _ov = _native(session, tid, "SHOE-LAG-2-OTHER", title="قميص قطني أزرق")
            moved = session.get(Product, product.id)
            record = dict(_pending(session, product.id))
            sm = dict(moved.extra_metadata["sync_meta"])
            sm.pop("pending_publications")
            moved.extra_metadata = {**moved.extra_metadata, "sync_meta": sm}
            other.extra_metadata = {**(other.extra_metadata or {}), "sync_meta": {"pending_publications": record}}
            session.commit()
        elif case == "connection_moved_to_another_catalog":
            conn = session.query(WhatsAppConnection).filter_by(tenant_id=tid).one()
            conn.meta_catalog_id = "CAT-MOVED"
            session.commit()
        before = [(m.retailer_id, m.meta_item_id, m.provenance) for m in session.query(MetaCatalogMembership).all()]
        if case == "connection_moved_to_another_catalog":
            # the moved connection reads its own catalog; the attempt was recorded for the old one
            with patch.object(push, "_resolve_catalog_and_token", lambda conn, **k: ("CAT-MOVED", "tok")), \
                 patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status",
                       return_value={"ok": True, "expected_catalog_linked": True}):
                from services.native_meta_sync_orchestrator import attempt_native_meta_sync
                graph.items_moved = True
                second = attempt_native_meta_sync(session, tid, product.id, client=graph)
            session.expire_all()
        else:
            second = _sync(session, tid, product.id, graph)
        assert second["ok"] is False, (case, second)
        after = [(m.retailer_id, m.meta_item_id, m.provenance) for m in session.query(MetaCatalogMembership).all()]
        assert after == before, case                                 # nothing recorded, nothing rebound
        assert len(graph.posts) == posts_before or case == "connection_moved_to_another_catalog", case
        # no update POST ever went to the item
        assert not any(u.endswith(f"/{mid}") for u, _ in graph.posts), case
    finally:
        session.close(); engine.dispose()


def test_a_recorded_attempt_is_tenant_scoped_and_never_retires_on_its_own():
    session, tid, engine = _db()
    try:
        product, _v = _native(session, tid, "SHOE-LAG-3")
        _syncable(product, session)
        graph = LaggingGraph()
        mid = _lagged_create(session, tid, product, graph, "SHOE-LAG-3")
        # another tenant with the same catalog id and retailer_id has no record of its own
        other = Tenant(name="متجر تجريبي ثانٍ", is_active=True)
        session.add(other)
        session.commit()
        session.add(WhatsAppConnection(
            tenant_id=other.id, whatsapp_business_account_id="WABA-X", phone_number_id="PN-X", access_token="EAAB-test",
            meta_catalog_id=CATALOG, catalog_enabled=True, provider="meta", connection_type="embedded", extra_metadata={}))
        session.commit()
        foreign, _fv = _native(session, other.id, "SHOE-LAG-3")
        _syncable(foreign, session)
        foreign_result = _sync(session, other.id, foreign.id, graph)
        assert foreign_result["ok"] is False
        assert session.query(MetaCatalogMembership).filter_by(tenant_id=other.id).count() == 0
        # hidden before any corroboration: the record alone never authorizes a withdrawal
        product = session.get(Product, product.id)
        product.catalog_status = "merchant_hidden"
        product.merchant_hidden_at = datetime.now(timezone.utc)
        product.sync_status = "failed"
        product.last_synced_at = datetime.now(timezone.utc)
        mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
        session.commit()
        posts_before = len(graph.posts)
        with _token():
            out = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        assert out["retired"] == 0 and out["error_code"] == "no_publication_evidence"
        assert len(graph.posts) == posts_before and graph.items["SHOE-LAG-3"]["id"] == mid
    finally:
        session.close(); engine.dispose()


def test_failed_posts_record_no_attempt():
    session, tid, engine = _db()
    try:
        product, _v = _native(session, tid, "SHOE-LAG-4")
        _syncable(product, session)
        graph = LaggingGraph()
        graph.fail_posts = True
        failed = _sync(session, tid, product.id, graph)
        assert failed["ok"] is False and _pending(session, product.id) == {}
    finally:
        session.close(); engine.dispose()


def test_corroboration_refuses_any_record_that_is_not_exactly_this_attempt():
    session, tid, engine = _db()
    try:
        product, variant = _native(session, tid, "SHOE-LAG-5")
        good = {"product_id": product.id, "catalog_id": CATALOG, "retailer_id": "SHOE-LAG-5", "meta_item_id": "META-L5"}
        cases = {
            "another product": {**good, "product_id": product.id + 1000},
            "another catalog in the record": {**good, "catalog_id": "CAT-OTHER"},
            "another retailer_id in the record": {**good, "retailer_id": "SHOE-OTHER"},
            "no item id": {**good, "meta_item_id": ""},
            "another item id": {**good, "meta_item_id": "META-OTHER"},
        }

        def attempt(record, *, live="META-L5", key=f"{CATALOG}|SHOE-LAG-5", catalog=CATALOG):
            row = session.get(Product, product.id)
            row.extra_metadata = {**(row.extra_metadata or {}), "sync_meta": {"pending_publications": {key: record}}}
            session.commit()
            return push.corroborate_pending_publication(
                session, row, variant, tenant_id=tid, catalog_id=catalog, retailer_id="SHOE-LAG-5",
                live_meta_item_id=live)

        for label, record in cases.items():
            assert attempt(record)["ok"] is False, label
        assert attempt(good, live="")["ok"] is False
        assert attempt(good, catalog="CAT-OTHER")["ok"] is False                 # recorded for another catalog
        assert attempt(good, key=f"{CATALOG}|SHOE-OTHER")["ok"] is False         # nothing recorded for this key
        session.commit()
        assert session.query(MetaCatalogMembership).count() == 0                # nothing was written
        assert attempt(good)["ok"] is True                                       # exactly this attempt
        session.commit()
        assert _membership(session, tid, "SHOE-LAG-5").meta_item_id == "META-L5"
    finally:
        session.close(); engine.dispose()



def test_repair_refuses_a_salla_observation_bound_to_another_variant_identity():
    from services.salla_variant_catalog_identity import replace_stale_observation_after_create

    session, tid, engine = _db()
    try:
        product, variant = _salla(session, tid, "911000")
        ident = identity_for_retailer_id(product, [variant], "911000-881")
        other_variant = ProductVariant(tenant_id=tid, product_id=product.id, salla_variant_id="882",
                                       retailer_id="911000-882", price="140", currency="SAR", stock_quantity=1, in_stock=True)
        session.add(other_variant)
        session.commit()
        cases = {
            "another salla_variant_id": dict(variant_id=variant.id, salla_variant_id="999"),
            "another local variant": dict(variant_id=other_variant.id, salla_variant_id="881"),
        }
        for label, binding in cases.items():
            session.query(MetaCatalogMembership).delete()
            session.add(MetaCatalogMembership(
                tenant_id=tid, catalog_id=CATALOG, retailer_id="911000-881", product_id=product.id,
                meta_item_id="GONE-S1", verified_at=datetime.now(timezone.utc), provenance=PROVENANCE_GRAPH_RECONCILE,
                **binding))
            session.commit()
            out = replace_stale_observation_after_create(
                session, tenant_id=tid, catalog_id=CATALOG, retailer_id="911000-881", product_id=product.id,
                variant_id=None, created_meta_item_id="META-S1", corroborated_meta_item_id="META-S1",
                publication_provenance="salla_variant_push", salla_identity=ident)
            assert out["ok"] is False, label
            session.commit()
            row = _membership(session, tid, "911000-881")
            assert (row.meta_item_id, row.provenance) == ("GONE-S1", PROVENANCE_GRAPH_RECONCILE), label
    finally:
        session.close(); engine.dispose()



@pytest.mark.parametrize("kind", ["native", "salla"])
def test_a_create_whose_lookup_shows_another_item_never_gains_authority_over_it(kind):
    """The POST created X, but the lookup right after (and every later one) shows Y
    under the retailer_id: only X was ever recorded, so Y is never adopted."""
    session, tid, engine = _db()
    try:
        if kind == "native":
            rid = "SHOE-LAG-6"
            product, _v = _native(session, tid, rid)
        else:
            product, _v = _salla(session, tid, "911100")
            rid = "911100-881"
        _syncable(product, session)
        graph = ReplacingGraph("META-FOREIGN-Y")
        first = _sync(session, tid, product.id, graph)
        assert first["ok"] is False and first["error_code"] == "verification_failed"
        recorded = _pending(session, product.id)[f"{CATALOG}|{rid}"]["meta_item_id"]
        assert recorded != "META-FOREIGN-Y"                                   # the POST's own id
        posts_before = len(graph.posts)
        second = _sync(session, tid, product.id, graph)
        assert second["ok"] is False
        row = _membership(session, tid, rid)
        assert row is None or row.provenance not in PUBLICATION_PROVENANCES
        assert len(graph.posts) == posts_before                              # no update POST to Y (or X)
    finally:
        session.close(); engine.dispose()



def test_corroborated_salla_evidence_is_the_variant_row_even_when_the_update_post_fails():
    """Corroboration itself writes the Salla variant identity's own evidence row,
    independent of what the following update POST does."""
    session, tid, engine = _db()
    try:
        product, variant = _salla(session, tid, "911200")
        rid = "911200-881"
        _syncable(product, session)
        graph = LaggingGraph()
        mid = _lagged_create(session, tid, product, graph, rid)
        graph.fail_posts = True                                            # the update POST is rejected
        second = _sync(session, tid, product.id, graph)
        assert second["ok"] is False
        row = _membership(session, tid, rid)
        assert (row.meta_item_id, row.provenance, row.salla_variant_id, row.variant_id) == (
            mid, "salla_variant_push", "881", variant.id)
    finally:
        session.close(); engine.dispose()
