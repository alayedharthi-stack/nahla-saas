"""Ownership gate on live retailer_id matches — proven against a real database.

The mock-based push tests cannot show that the membership lookup filters by
tenant AND catalog; these tests use SQLite rows with two tenants and two
catalogs (generic merchant data: متجر تجريبي عام) and the real query path.
They also prove that a reconcile snapshot cannot manufacture or erase
publication evidence, that the legacy product stamp written by import is not
evidence, and that a sibling LINK adoption is blocked without evidence.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
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
    MetaCatalogMembership,
    Product,
    ProductVariant,
    Tenant,
    WhatsAppConnection,
)
from core.catalog import OWNERSHIP_EXTERNAL_MANAGED  # noqa: E402
from core.meta_catalog_membership import (  # noqa: E402
    PROVENANCE_GRAPH_RECONCILE,
    PROVENANCE_VARIANT_PUSH,
    PUBLICATION_PROVENANCES,
    DesiredMembership,
    apply_membership_snapshot,
)
from services import meta_catalog_push as push  # noqa: E402

CATALOG_A = "CAT-A-900"
CATALOG_OTHER = "CAT-OTHER-901"
RID = "617350990-1"
LIVE_ID = "META-LIVE-617350990-1"


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
        self.content = self.text.encode()

    def json(self):
        return self._body


class FakeGraph:
    """GET returns the live item for the filter lookup; POST records updates."""

    def __init__(self, live_id=LIVE_ID):
        self.live_id = live_id
        self.posts = []

    def get(self, url, params=None, headers=None):
        filt = str((params or {}).get("filter") or "")
        if "/products" in url and RID in filt:
            return _Resp(200, {"data": [{"id": self.live_id, "retailer_id": RID, "name": "قميص قطني أزرق - M",
                                         "price": "120.00 SAR", "currency": "SAR", "availability": "in stock"}]})
        if "/products" in url:
            return _Resp(200, {"data": []})
        return _Resp(200, {"id": url.rsplit("/", 1)[-1], "name": "متجر تجريبي عام", "product_count": 1})

    def post(self, url, data=None, headers=None, params=None):
        self.posts.append((url, dict(data or {})))
        return _Resp(200, {"id": self.live_id, "success": True})

    def request(self, method, url, params=None, data=None, headers=None, json=None):
        return self.get(url, params=params) if method == "GET" else self.post(url, data=data)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    a = Tenant(name="متجر تجريبي عام", is_active=True)
    b = Tenant(name="متجر آخر", is_active=True)
    session.add_all([a, b]); session.commit()
    for tenant in (a, b):
        session.add(WhatsAppConnection(
            tenant_id=tenant.id, whatsapp_business_account_id=f"WABA-{tenant.id}", phone_number_id=f"PN-{tenant.id}",
            access_token="EAAB-secret", meta_catalog_id=CATALOG_A, catalog_enabled=True,
            provider="meta", connection_type="embedded", extra_metadata={},
        ))
    session.commit()
    return session, a.id, b.id, engine


def _salla_product(session, tid, ext="617350990", *, stamp=None):
    p = Product(
        tenant_id=tid, external_id=ext, title="قميص قطني أزرق", price="120", in_stock=True, stock_quantity=3,
        source="salla", ownership_mode=OWNERSHIP_EXTERNAL_MANAGED, catalog_status="active", sync_status="pending",
        meta_item_id=stamp,
        extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/shirt.jpg",
                        "product_url": "https://store.example/p/shirt", "source_status": "sale"},
    )
    session.add(p); session.flush()
    v = ProductVariant(tenant_id=tid, product_id=p.id, salla_variant_id="1", retailer_id=f"{ext}-1",
                       price="120", currency="SAR", stock_quantity=3, in_stock=True, option_summary="M")
    session.add(v); session.commit()
    return p, v


def _membership(session, tid, catalog_id, product, variant, *, meta_item_id=LIVE_ID, provenance=PROVENANCE_VARIANT_PUSH, rid=RID):
    m = MetaCatalogMembership(tenant_id=tid, catalog_id=catalog_id, retailer_id=rid, product_id=product.id,
                              variant_id=variant.id, salla_variant_id="1", meta_item_id=meta_item_id,
                              verified_at=datetime.now(timezone.utc), provenance=provenance)
    session.add(m); session.commit()
    return m


_PREVIEW = {
    "payload": {"retailer_id": RID, "name": "قميص قطني أزرق - M", "description": "وصف", "image_url": "https://cdn.example/shirt.jpg",
                "url": "https://store.example/p/shirt", "price": 12000, "currency": "SAR", "availability": "in stock"},
    "warnings": [], "fatal": False,
}


def _push(session, tid, monkeypatch, graph=None):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS", raising=False)
    graph = graph or FakeGraph()
    with patch.object(push, "preview_meta_variant_payload", return_value=_PREVIEW), \
         patch.object(push, "_resolve_catalog_and_token", lambda conn, **k: (CATALOG_A, "tok")):
        result = push.push_one_meta_catalog_item(session, tid, RID, confirm=True, client=graph)
    return result, graph


def test_publication_membership_of_the_same_tenant_and_catalog_allows_the_update(monkeypatch):
    session, a, _b, engine = _db()
    try:
        p, v = _salla_product(session, a)
        _membership(session, a, CATALOG_A, p, v)
        result, graph = _push(session, a, monkeypatch)
        assert result["ok"] is True and result["action"] == "update"
        assert result["ownership_evidence"]["source"] == f"membership:{PROVENANCE_VARIANT_PUSH}"
        assert len(graph.posts) == 1 and graph.posts[0][0].endswith(f"/{LIVE_ID}")
    finally:
        session.close(); engine.dispose()


def test_another_tenants_membership_for_the_same_catalog_and_retailer_id_is_not_evidence(monkeypatch):
    """Two tenants, one catalog id, one retailer_id: tenant B's publication row must never let
    tenant A update the live item. The real query filters tenant_id."""
    session, a, b, engine = _db()
    try:
        pa, va = _salla_product(session, a)
        pb, vb = _salla_product(session, b)
        _membership(session, b, CATALOG_A, pb, vb)             # B published it
        result, graph = _push(session, a, monkeypatch)        # A pushes the same identity
        assert result["action"] == "block_ownership_unverified"
        assert result["error"] == "live_match_ownership_unverified"
        assert "membership_absent" in result["ownership_evidence"]["reasons"]
        assert graph.posts == []
        # B's own push against the same catalog is allowed: its row is its evidence
        result_b, graph_b = _push(session, b, monkeypatch)
        assert result_b["ok"] is True and result_b["action"] == "update"
    finally:
        session.close(); engine.dispose()


def test_a_membership_for_another_catalog_is_not_evidence(monkeypatch):
    session, a, _b, engine = _db()
    try:
        p, v = _salla_product(session, a)
        _membership(session, a, CATALOG_OTHER, p, v)           # same tenant, same rid, other catalog
        result, graph = _push(session, a, monkeypatch)
        assert result["action"] == "block_ownership_unverified"
        assert "membership_absent" in result["ownership_evidence"]["reasons"]
        assert graph.posts == []
    finally:
        session.close(); engine.dispose()


def test_reconcile_membership_and_import_stamp_are_not_evidence(monkeypatch):
    """A reconcile row proves Graph presence; the Meta-import stamp on the product proves an
    import. Neither proves this path published the item."""
    session, a, _b, engine = _db()
    try:
        p, v = _salla_product(session, a, stamp=LIVE_ID)       # as the import path stamps it
        _membership(session, a, CATALOG_A, p, v, provenance=PROVENANCE_GRAPH_RECONCILE)
        result, graph = _push(session, a, monkeypatch)
        assert result["action"] == "block_ownership_unverified"
        ev = result["ownership_evidence"]
        assert f"membership_provenance_not_publication:{PROVENANCE_GRAPH_RECONCILE}" in ev["reasons"]
        assert ev["legacy_product_meta_item_id"] == {"value": LIVE_ID, "matches_live_item": True}
        assert "legacy_stamp_is_not_publication_evidence" in ev["reasons"]
        assert graph.posts == []
    finally:
        session.close(); engine.dispose()


def test_membership_bound_to_a_different_live_item_is_not_evidence(monkeypatch):
    session, a, _b, engine = _db()
    try:
        p, v = _salla_product(session, a)
        _membership(session, a, CATALOG_A, p, v, meta_item_id="META-OLD-ITEM")
        result, graph = _push(session, a, monkeypatch)
        assert result["action"] == "block_ownership_unverified"
        assert "membership_meta_item_id_mismatch" in result["ownership_evidence"]["reasons"]
        assert graph.posts == []
    finally:
        session.close(); engine.dispose()


def test_sibling_link_adoption_is_blocked_without_evidence_and_allowed_with_it(monkeypatch):
    """A canonical-sibling LINK adopts a live item instead of creating one; the same evidence
    rule applies, so an unproven sibling is blocked and nothing is bound."""
    session, a, _b, engine = _db()
    try:
        p = Product(tenant_id=a, external_id="398551325", title="حذاء رياضي أبيض", price="180", in_stock=True,
                    stock_quantity=2, source="manual", catalog_status="active", sync_status="pending",
                    extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/shoe.jpg", "product_url": "https://store.example/p/shoe"})
        session.add(p); session.flush()
        v = ProductVariant(tenant_id=a, product_id=p.id, salla_variant_id=None, retailer_id="398551325",
                           price="180", currency="SAR", stock_quantity=2, in_stock=True)
        session.add(v); session.commit()
        link = {"action": push.ACTION_LINK, "error": None, "reason": "canonical_sibling", "identity_class": "EXISTING_CANONICAL_SIBLING",
                "meta_product_id": "META-SIB", "sibling_retailer_id": "398551325-591001", "idempotent": True,
                "content_mismatches": [], "canonical_rule": "default_variant"}
        preview = dict(_PREVIEW, payload=dict(_PREVIEW["payload"], retailer_id="398551325"))

        class NoMatchGraph(FakeGraph):
            def get(self, url, params=None, headers=None):
                return _Resp(200, {"data": []}) if "/products" in url else super().get(url, params, headers)

        def run():
            monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
            graph = NoMatchGraph()
            with patch.object(push, "preview_meta_variant_payload", return_value=preview), \
                 patch.object(push, "_resolve_catalog_and_token", lambda conn, **k: (CATALOG_A, "tok")), \
                 patch.object(push, "_canonical_sibling_gate", lambda *a_, **k: dict(link)):
                return push.push_one_meta_catalog_item(session, a, "398551325", confirm=True, client=graph), graph

        result, graph = run()
        assert result["action"] == "block_ownership_unverified"
        assert result["ok"] is False and result["meta_product_id"] == "META-SIB"
        assert "membership_absent" in result["ownership_evidence"]["reasons"]
        assert graph.posts == []

        # with a publication membership for the sibling identity, the LINK is accepted (no POST either way)
        _membership(session, a, CATALOG_A, p, v, meta_item_id="META-SIB", rid="398551325-591001")
        result2, graph2 = run()
        assert result2["action"] == push.ACTION_LINK and result2["ok"] is True
        assert result2["ownership_evidence"]["owned"] is True
        assert graph2.posts == []
    finally:
        session.close(); engine.dispose()


def test_reconcile_snapshot_preserves_publication_provenance_and_cannot_create_it():
    """Reconcile derives memberships from Graph presence: a new row gets the reconcile
    provenance (not evidence), an existing publication row keeps its provenance and item id
    while the live item is unchanged, and a changed live item id downgrades it honestly."""
    session, a, _b, engine = _db()
    try:
        p, v = _salla_product(session, a)
        published = _membership(session, a, CATALOG_A, p, v)                       # evidence from a real push
        p2, v2 = _salla_product(session, a, ext="792574531")
        stats = apply_membership_snapshot(
            session, tenant_id=a, catalog_id=CATALOG_A,
            desired=[
                DesiredMembership(retailer_id=RID, product_id=p.id, variant_id=v.id, meta_item_id=LIVE_ID),
                DesiredMembership(retailer_id="792574531-1", product_id=p2.id, variant_id=v2.id, meta_item_id="META-NEW"),
            ],
        )
        session.commit()
        rows = {m.retailer_id: m for m in session.query(MetaCatalogMembership).filter_by(tenant_id=a, catalog_id=CATALOG_A).all()}
        assert rows[RID].provenance == PROVENANCE_VARIANT_PUSH and rows[RID].meta_item_id == LIVE_ID
        assert rows["792574531-1"].provenance == PROVENANCE_GRAPH_RECONCILE          # presence, not publication
        assert rows["792574531-1"].provenance not in PUBLICATION_PROVENANCES
        assert stats["preserved_publication_provenance"] == 1
        # Graph returned no id for the published row: evidence still kept
        apply_membership_snapshot(session, tenant_id=a, catalog_id=CATALOG_A,
                                  desired=[DesiredMembership(retailer_id=RID, product_id=p.id, variant_id=v.id, meta_item_id=None)])
        session.commit()
        kept = session.query(MetaCatalogMembership).filter_by(tenant_id=a, catalog_id=CATALOG_A, retailer_id=RID).one()
        assert kept.provenance == PROVENANCE_VARIANT_PUSH and kept.meta_item_id == LIVE_ID
        # the live item was replaced under the same retailer_id: the old evidence no longer describes it
        apply_membership_snapshot(session, tenant_id=a, catalog_id=CATALOG_A,
                                  desired=[DesiredMembership(retailer_id=RID, product_id=p.id, variant_id=v.id, meta_item_id="META-REPLACED")])
        session.commit()
        replaced = session.query(MetaCatalogMembership).filter_by(tenant_id=a, catalog_id=CATALOG_A, retailer_id=RID).one()
        assert replaced.provenance == PROVENANCE_GRAPH_RECONCILE and replaced.meta_item_id == "META-REPLACED"
        assert published.id == replaced.id
    finally:
        session.close(); engine.dispose()


def test_batch_records_publication_membership_only_after_a_real_create_or_update(monkeypatch):
    """The push batch must not write publication evidence for a LINK result (adoption)."""
    calls = []
    item = type("Item", (), {"retailer_id": RID})()
    report = type("Report", (), {"error": None, "meta_fetch": {}, "items": [item]})()
    results = iter([
        {"ok": True, "action": push.ACTION_LINK, "meta_product_id": "META-SIB", "catalog_id": CATALOG_A},
        {"ok": True, "action": "create", "meta_product_id": "META-NEW", "catalog_id": CATALOG_A},
    ])
    with patch("services.meta_catalog_readiness.build_meta_catalog_readiness_report", return_value=report), \
         patch("services.meta_catalog_readiness.select_ready_create_push_candidates", return_value=[item, item]), \
         patch("services.meta_catalog_readiness.is_ready_create_in_stock_candidate", return_value=True), \
         patch("services.meta_catalog_readiness.candidate_push_row", return_value={"retailer_id": RID}), \
         patch.object(push, "_prepare_salla_batch_membership_slot", lambda *a, **k: None), \
         patch.object(push, "push_one_meta_catalog_item", lambda *a, **k: next(results)), \
         patch.object(push, "_stamp_salla_batch_membership", lambda db, tid, rid, mid, cid: calls.append((rid, mid))):
        batch = push.push_ready_meta_catalog_batch(object(), 9, confirm=True, stop_on_first_error=False)
    assert batch["summary"]["attempted"] >= 2
    assert calls == [(RID, "META-NEW")]
