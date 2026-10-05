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
            push._stamp_salla_batch_membership(session, tid, rid, str(result.get("meta_product_id") or ""), CATALOG)
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
