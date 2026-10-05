"""Channel retirement writes to Meta only with publication evidence.

The publish path refuses to update a live Graph item unless a
``meta_catalog_memberships`` row with a publication provenance names that
exact item (``live_item_publication_evidence``). Retirement is a Meta write
too, so it obeys the same rule: reconcile-derived memberships, the legacy
``Product.meta_item_id`` stamp and native ``sync_meta`` expectations never
authorize it, in the product path, in the delete ledger and in the Graph
helper itself. An item this path did publish is still retired.

Generic merchant data only (متجر تجريبي عام). Graph is a fake client that
records every call; nothing here talks to Meta.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from database.models import (
    Base,
    CatalogChannelRetirement,
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
from services.meta_catalog_push import (
    RETIRED_AVAILABILITY,
    live_item_publication_evidence,
    retire_meta_catalog_item,
)
from services.whatsapp_catalog_retirement import (
    LEDGER_STATUS_DONE,
    LEDGER_STATUS_REFUSED,
    REASON_MANUAL_DELETED,
    REASON_MERCHANT_HIDDEN,
    SYNC_STATUS_RETIRED,
    attempt_product_channel_retirement,
    channel_identities_for_product,
    drain_channel_retirement_ledger,
    enqueue_channel_retirement_ledger,
    ledger_snapshot,
    mark_product_channel_retire_pending,
)

CATALOG = "CAT-GENERIC-001"


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class RecordingGraph:
    """One catalog, items keyed by retailer_id; records every GET and POST."""

    def __init__(self, items=None):
        self.items = {k: dict(v) for k, v in (items or {}).items()}
        self.gets = []
        self.posts = []

    def get(self, url, params=None, headers=None):
        self.gets.append((url, params))
        if "/products" in url and params and params.get("filter"):
            rid = json.loads(params["filter"])["retailer_id"]["eq"]
            item = self.items.get(rid)
            data = []
            if item is not None:
                row = {"id": item["id"], "retailer_id": rid, "availability": item.get("availability")}
                if "visibility" in (params.get("fields") or ""):
                    row["visibility"] = item.get("visibility", "published")
                data.append(row)
            return _Response(200, {"data": data})
        return _Response(200, {"data": []})

    def post(self, url, data=None, headers=None):
        self.posts.append((url, dict(data or {})))
        meta_id = url.rstrip("/").split("/")[-1]
        for item in self.items.values():
            if item["id"] == meta_id:
                item.update(dict(data or {}))
                return _Response(200, {"success": True})
        return _Response(404, {"error": {"code": 100, "message": "unknown item"}})


def _db():
    engine = create_engine("sqlite:///:memory:")
    # PostgreSQL enforces ON DELETE CASCADE (memberships go with the product);
    # SQLite only does so with foreign keys switched on.
    event.listen(engine, "connect", lambda dbapi_conn, _rec: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.add(
        WhatsAppConnection(
            tenant_id=tenant.id,
            whatsapp_business_account_id=f"WABA-{tenant.id}",
            phone_number_id=f"PN-{tenant.id}",
            access_token="EAAB-test",
            meta_catalog_id=CATALOG,
            catalog_enabled=True,
            extra_metadata={},
        )
    )
    session.commit()
    return session, tenant.id, engine


def _salla_shirt(session, tenant_id, *, ext, provenance=None, meta_item_id=None, legacy_stamp=None):
    """Generic Salla product «قميص قطني أزرق» with one variant.

    *provenance* adds a membership with that provenance for the variant;
    *legacy_stamp* writes the product-level ``meta_item_id`` stamp only.
    """
    now = datetime.now(timezone.utc)
    product = Product(
        tenant_id=tenant_id,
        external_id=ext,
        title="قميص قطني أزرق",
        price="140",
        in_stock=True,
        stock_quantity=5,
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        catalog_status="active",
        meta_item_id=legacy_stamp,
        sync_status="synced",
        last_synced_at=now,
        extra_metadata={"currency": "SAR", "status": "sale"},
    )
    session.add(product)
    session.flush()
    variant = ProductVariant(
        tenant_id=tenant_id, product_id=product.id, salla_variant_id="881", retailer_id=f"{ext}-881",
        price="140", currency="SAR", stock_quantity=5, in_stock=True, is_default=False,
    )
    session.add(variant)
    session.flush()
    if provenance:
        session.add(
            MetaCatalogMembership(
                tenant_id=tenant_id, catalog_id=CATALOG, retailer_id=f"{ext}-881", product_id=product.id,
                variant_id=variant.id, salla_variant_id="881", meta_item_id=meta_item_id or f"META-{ext}",
                verified_at=now, provenance=provenance,
            )
        )
    session.commit()
    return product


def _hide(session, product):
    product.catalog_status = "merchant_hidden"
    product.merchant_hidden_at = datetime.now(timezone.utc)
    session.commit()


def _token():
    return patch("services.meta_catalog_push._resolve_catalog_and_token", return_value=(CATALOG, "tok"))


def _retire_hidden(session, tenant_id, product, graph):
    _hide(session, product)
    requested = mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN)
    session.commit()
    with _token():
        out = attempt_product_channel_retirement(session, tenant_id, product.id, client=graph)
    session.expire_all()
    return requested, out, session.get(Product, product.id)


# ── Product path ──────────────────────────────────────────────────────────

def test_reconcile_membership_and_legacy_stamp_never_authorize_a_retire_write():
    """The reviewer's case: never published by Nahla, legacy stamp + a
    reconcile-derived membership for the live item. The publish path refuses
    it, so retirement must not read or write it either."""
    session, tid, engine = _db()
    try:
        product = _salla_shirt(session, tid, ext="800100", provenance="meta_graph_reconcile",
                               meta_item_id="META-FOREIGN-1", legacy_stamp="META-FOREIGN-1")
        evidence = live_item_publication_evidence(session, tenant_id=tid, catalog_id=CATALOG,
                                                  retailer_id="800100-881", meta_product_id="META-FOREIGN-1",
                                                  parent=product)
        assert evidence["owned"] is False
        assert channel_identities_for_product(session, product) == []
        graph = RecordingGraph({"800100-881": {"id": "META-FOREIGN-1", "availability": "in stock"}})
        _requested, out, row = _retire_hidden(session, tid, product, graph)
        assert graph.posts == [] and graph.gets == []
        assert out["retired"] == 0 and out["error_code"] == "no_publication_evidence"
        assert row.sync_status != SYNC_STATUS_RETIRED
        meta = row.extra_metadata["sync_meta"]
        assert meta["retire_pending"] is False and meta["retire_blocked"] == "no_publication_evidence"
        assert "channel_retired_at" not in meta
        assert graph.items["800100-881"]["availability"] == "in stock"
    finally:
        session.close(); engine.dispose()


def test_legacy_product_stamp_alone_is_not_evidence():
    session, tid, engine = _db()
    try:
        product = _salla_shirt(session, tid, ext="800200", legacy_stamp="META-LEGACY-2")
        assert channel_identities_for_product(session, product) == []
        graph = RecordingGraph({"800200-881": {"id": "META-LEGACY-2", "availability": "in stock"}})
        _requested, out, row = _retire_hidden(session, tid, product, graph)
        assert graph.posts == [] and graph.gets == []
        assert row.sync_status != SYNC_STATUS_RETIRED
        assert out["error_code"] == "no_publication_evidence"
    finally:
        session.close(); engine.dispose()


def test_native_row_with_expected_payloads_and_stamp_is_not_retired():
    """Native ``sync_meta`` expectations and the product stamp prove intent,
    not publication; the publish path would not update this item either."""
    session, tid, engine = _db()
    try:
        product = Product(
            tenant_id=tid, title="عطر ورد 100ml", price="320", in_stock=True, stock_quantity=2,
            source=SOURCE_NAHLA_NATIVE, ownership_mode=OWNERSHIP_NAHLA_MANAGED, catalog_status="active",
            meta_retailer_id="nahla_p_rose", meta_item_id="META-ROSE-1", sync_status="synced",
            last_synced_at=datetime.now(timezone.utc),
            extra_metadata={"currency": "SAR", "sync_meta": {"expected_payloads_by_retailer_id": {
                "nahla_p_rose": {"price": 32000, "currency": "SAR", "availability": "in stock"}}}},
        )
        session.add(product)
        session.commit()
        assert channel_identities_for_product(session, product) == []
        graph = RecordingGraph({"nahla_p_rose": {"id": "META-ROSE-1", "availability": "in stock"}})
        _requested, out, _row = _retire_hidden(session, tid, product, graph)
        assert graph.posts == [] and graph.gets == []
        assert out["retired"] == 0
    finally:
        session.close(); engine.dispose()


def test_owned_item_is_still_retired():
    session, tid, engine = _db()
    try:
        product = _salla_shirt(session, tid, ext="800300", provenance="salla_variant_push", meta_item_id="META-OWNED-3")
        graph = RecordingGraph({"800300-881": {"id": "META-OWNED-3", "availability": "in stock"}})
        requested, out, row = _retire_hidden(session, tid, product, graph)
        assert requested is True
        assert out["ok"] is True and out["retired"] == 1
        assert graph.posts and all(url.endswith("/META-OWNED-3") for url, _ in graph.posts)
        assert graph.posts and graph.posts[0][1]["availability"] == RETIRED_AVAILABILITY
        assert row.sync_status == SYNC_STATUS_RETIRED
        assert graph.items["800300-881"]["availability"] == "out of stock"
    finally:
        session.close(); engine.dispose()


def test_evidence_downgraded_before_the_drain_blocks_the_write():
    """Requested while owned; a reconcile pass then replaced the item id, so
    the membership no longer proves publication. The drain must not write."""
    session, tid, engine = _db()
    try:
        product = _salla_shirt(session, tid, ext="800400", provenance="salla_variant_push", meta_item_id="META-OWNED-4")
        _hide(session, product)
        assert mark_product_channel_retire_pending(session, product, reason=REASON_MERCHANT_HIDDEN) is True
        session.commit()
        membership = session.query(MetaCatalogMembership).filter_by(tenant_id=tid, retailer_id="800400-881").one()
        membership.provenance = "meta_graph_reconcile"
        membership.meta_item_id = "META-SOMEONE-ELSE"
        session.commit()
        graph = RecordingGraph({"800400-881": {"id": "META-SOMEONE-ELSE", "availability": "in stock"}})
        with _token():
            out = attempt_product_channel_retirement(session, tid, product.id, client=graph)
        assert graph.posts == [] and graph.gets == []
        assert out["retired"] == 0 and out["error_code"] == "no_publication_evidence"
    finally:
        session.close(); engine.dispose()


def test_live_item_replaced_under_the_same_retailer_id_is_not_written():
    """Evidence names META-OWNED-5; Graph now serves a different item for the
    retailer id. The write is refused (mismatch), never sent."""
    session, tid, engine = _db()
    try:
        product = _salla_shirt(session, tid, ext="800500", provenance="salla_variant_push", meta_item_id="META-OWNED-5")
        graph = RecordingGraph({"800500-881": {"id": "META-NEW-OWNER", "availability": "in stock"}})
        _requested, out, row = _retire_hidden(session, tid, product, graph)
        assert graph.posts == []
        assert out["ok"] is False and out["failed"] == 1
        assert row.sync_status != SYNC_STATUS_RETIRED
    finally:
        session.close(); engine.dispose()


# ── Delete ledger ─────────────────────────────────────────────────────────

def test_deleted_owned_product_is_ledgered_with_its_evidence_and_retired():
    session, tid, engine = _db()
    try:
        product = _salla_shirt(session, tid, ext="800600", provenance="salla_variant_push", meta_item_id="META-OWNED-6")
        ids = channel_identities_for_product(session, product)
        assert enqueue_channel_retirement_ledger(session, tid, ids, reason=REASON_MANUAL_DELETED) == 1
        session.delete(product)
        session.commit()
        # the membership went with the product; the ledger row carries the evidence copy
        assert session.query(MetaCatalogMembership).filter_by(tenant_id=tid).count() == 0
        row = session.query(CatalogChannelRetirement).filter_by(tenant_id=tid).one()
        assert (row.catalog_id, row.retailer_id, row.meta_item_id) == (CATALOG, "800600-881", "META-OWNED-6")
        graph = RecordingGraph({"800600-881": {"id": "META-OWNED-6", "availability": "in stock"}})
        with _token():
            out = drain_channel_retirement_ledger(session, tid, client=graph)
        assert out["retired"] == 1
        assert session.query(CatalogChannelRetirement).filter_by(tenant_id=tid).one().status == LEDGER_STATUS_DONE
    finally:
        session.close(); engine.dispose()


def test_deleted_unowned_product_queues_nothing():
    session, tid, engine = _db()
    try:
        product = _salla_shirt(session, tid, ext="800700", provenance="meta_graph_reconcile",
                               meta_item_id="META-FOREIGN-7", legacy_stamp="META-FOREIGN-7")
        ids = channel_identities_for_product(session, product)
        assert ids == []
        assert enqueue_channel_retirement_ledger(session, tid, ids, reason=REASON_MANUAL_DELETED) == 0
        # identities handed in without publication provenance are not recorded either
        forged = [{"retailer_id": "800700-881", "meta_item_id": "META-FOREIGN-7", "catalog_id": CATALOG,
                   "product_id": product.id}]
        assert enqueue_channel_retirement_ledger(session, tid, forged, reason=REASON_MANUAL_DELETED) == 0
        session.delete(product)
        session.commit()
        assert session.query(CatalogChannelRetirement).filter_by(tenant_id=tid).count() == 0
    finally:
        session.close(); engine.dispose()


def test_ledger_row_without_evidence_is_refused_without_any_graph_call():
    """A ledger row lacking the evidence copy (no Graph item id) is never
    retried and never written; the snapshot reports it."""
    session, tid, engine = _db()
    try:
        now = datetime.now(timezone.utc)
        session.add(CatalogChannelRetirement(
            tenant_id=tid, catalog_id=CATALOG, retailer_id="800800-881", meta_item_id=None, product_id=None,
            reason=REASON_MANUAL_DELETED, status="pending", attempts=0, created_at=now, updated_at=now,
        ))
        session.commit()
        graph = RecordingGraph({"800800-881": {"id": "META-FOREIGN-8", "availability": "in stock"}})
        with _token():
            out = drain_channel_retirement_ledger(session, tid, client=graph)
        assert graph.posts == [] and graph.gets == []
        assert out["refused"] == 1 and out["remaining"] == 0
        row = session.query(CatalogChannelRetirement).filter_by(tenant_id=tid).one()
        assert row.status == LEDGER_STATUS_REFUSED and row.last_error == "no_publication_evidence"
        assert ledger_snapshot(session, tid)["refused"] == 1
    finally:
        session.close(); engine.dispose()


# ── Graph helper ──────────────────────────────────────────────────────────

def _conn(tenant_id=7):
    return SimpleNamespace(tenant_id=tenant_id, meta_catalog_id=CATALOG, access_token="EAAB-test", extra_metadata={})


def _owned(catalog_id=CATALOG, retailer_id="800900-881", meta_item_id="META-OWNED-9"):
    return {"owned": True, "meta_product_id": meta_item_id, "catalog_id": catalog_id, "retailer_id": retailer_id}


def test_graph_helper_refuses_every_write_without_matching_evidence():
    graph = RecordingGraph({"800900-881": {"id": "META-OWNED-9", "availability": "in stock"}})
    cases = [
        None,
        {"owned": False, "meta_product_id": "META-OWNED-9", "catalog_id": CATALOG, "retailer_id": "800900-881"},
        _owned(meta_item_id=""),
        _owned(catalog_id="CAT-SOMEONE-ELSE"),
        _owned(retailer_id="800900-999"),
    ]
    with _token():
        for evidence in cases:
            res = retire_meta_catalog_item(_conn(), CATALOG, "800900-881", publication_evidence=evidence, client=graph)
            assert res["ok"] is False and res["action"] == "block_ownership_unverified", evidence
    assert graph.posts == [] and graph.gets == []


def test_graph_helper_refuses_an_item_in_a_catalog_the_tenant_no_longer_holds():
    graph = RecordingGraph({"800900-881": {"id": "META-OWNED-9", "availability": "in stock"}})
    with _token():
        res = retire_meta_catalog_item(_conn(), "CAT-OLD-002", "800900-881",
                                       publication_evidence=_owned(catalog_id="CAT-OLD-002"), client=graph)
    assert res["ok"] is False and res["error"] == "catalog_not_current"
    assert graph.posts == [] and graph.gets == []


def test_graph_helper_retires_the_evidenced_item():
    graph = RecordingGraph({"800900-881": {"id": "META-OWNED-9", "availability": "in stock"}})
    with _token():
        res = retire_meta_catalog_item(_conn(), CATALOG, "800900-881", publication_evidence=_owned(), client=graph)
    assert res["ok"] is True and res["meta_product_id"] == "META-OWNED-9"
    assert len(graph.posts) == 1 and graph.posts[0][0].endswith("/META-OWNED-9")
