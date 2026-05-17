"""tests/test_google_merchant_feed.py
─────────────────────────────────
Phase 4 coverage for the catalog refactor: a tenant with a single
parent and N variants must produce N feed items, all sharing the
same ``item_group_id`` and carrying per-variant ``id``, ``size``,
``color``, ``material`` extracted from ``options``.

Edge cases:
  * Variant with empty ``retailer_id`` → skipped + counted in
    ``items_skipped`` (Google's validator rejects items without id).
  * Synthetic ``is_default=true`` row mirroring the parent — still
    a valid item; goes out as one entry per Google's spec for
    single-SKU products.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Product, ProductVariant, Tenant  # noqa: E402
from services.google_merchant_feed import (  # noqa: E402
    build_feed, variant_to_feed_item,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db):
    t = Tenant(name=f"T-{id(db)}", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _seed_parent(db, t, *, title, external_id, price="100",
                 image="https://img/p.jpg"):
    p = Product(
        tenant_id=t.id, title=title, external_id=external_id,
        price=price, in_stock=True, source="salla",
        has_variants=True,
        extra_metadata={"image_url": image,
                        "product_url": "https://store/p"},
    )
    db.add(p); db.commit(); db.refresh(p)
    return p


def _add_variant(db, p, **kw):
    v = ProductVariant(tenant_id=p.tenant_id, product_id=p.id,
                       in_stock=kw.pop("in_stock", True),
                       is_default=kw.pop("is_default", False),
                       **kw)
    db.add(v); db.commit(); db.refresh(v)
    return v


# ─────────────────────────────────────────────────────────────────────
# Feed shape
# ─────────────────────────────────────────────────────────────────────


class TestFeedShape:

    def test_one_parent_three_variants_yields_three_items(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        p = _seed_parent(db, t, title="فستان", external_id="salla_p")
        _add_variant(db, p, salla_variant_id="v1",
                     retailer_id="salla_p-v1", price="120", currency="SAR",
                     options={"size": "S", "color": "Red"},
                     option_summary="S / Red")
        _add_variant(db, p, salla_variant_id="v2",
                     retailer_id="salla_p-v2", price="130", currency="SAR",
                     options={"size": "M", "color": "Red"},
                     option_summary="M / Red")
        _add_variant(db, p, salla_variant_id="v3",
                     retailer_id="salla_p-v3", price="140", currency="SAR",
                     options={"size": "L", "color": "Blue"},
                     option_summary="L / Blue", in_stock=False)
        feed = build_feed(db, t.id)
        assert feed["items_count"] == 3
        assert feed["items_skipped"] == 0
        items = feed["items"]
        # All three items share the same item_group_id (the parent id).
        groups = {i["item_group_id"] for i in items}
        assert groups == {str(p.id)}
        # Per-item id is the variant retailer_id, not the parent.
        ids = sorted(i["id"] for i in items)
        assert ids == ["salla_p-v1", "salla_p-v2", "salla_p-v3"]
        # Availability flips for the OOS variant.
        l_item = next(i for i in items if i["id"] == "salla_p-v3")
        assert l_item["availability"] == "out_of_stock"
        m_item = next(i for i in items if i["id"] == "salla_p-v2")
        assert m_item["availability"] == "in_stock"
        # Title is parent + " — " + summary; size/color extracted.
        assert m_item["title"] == "فستان — M / Red"
        assert m_item["size"] == "M"
        assert m_item["color"] == "Red"
        # Price carries currency.
        assert m_item["price"] == "130 SAR"

    def test_variant_without_retailer_id_is_skipped(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        p = _seed_parent(db, t, title="P", external_id="salla_x")
        _add_variant(db, p, salla_variant_id="v1",
                     retailer_id="ok", price="50")
        _add_variant(db, p, salla_variant_id="v2",
                     retailer_id=None, price="60")
        feed = build_feed(db, t.id)
        assert feed["items_count"] == 1
        assert feed["items_skipped"] == 1
        assert feed["items"][0]["id"] == "ok"

    def test_synthetic_default_variant_emits_one_item_per_parent(self):
        """A one-SKU product (only a synthetic ``is_default=true``
        row) must produce ONE feed entry — Google still wants the
        item_group_id for the catalog hierarchy."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        p = _seed_parent(db, t, title="عسل", external_id="ext_hb")
        # No has_variants for a simple product
        p.has_variants = False
        db.commit()
        _add_variant(db, p, salla_variant_id=None, is_default=True,
                     retailer_id="ext_hb", price="55")
        feed = build_feed(db, t.id)
        assert feed["items_count"] == 1
        only = feed["items"][0]
        assert only["id"] == "ext_hb"
        assert only["item_group_id"] == str(p.id)
        # The synthetic row uses the parent title verbatim (no " — "
        # suffix because the option_summary was empty).
        assert only["title"] == "عسل"

    def test_arabic_option_keys_map_to_google_attributes(self):
        """Some adapters write Arabic option keys ('مقاس' / 'لون').
        The mapper must catch them so size/color land on the feed."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        p = _seed_parent(db, t, title="ت", external_id="x")
        _add_variant(
            db, p, salla_variant_id="v1", retailer_id="x-v1",
            options={"مقاس": "L", "لون": "أزرق", "خامة": "قطن"},
            option_summary="L / أزرق",
        )
        item = build_feed(db, t.id)["items"][0]
        assert item["size"] == "L"
        assert item["color"] == "أزرق"
        assert item["material"] == "قطن"

    def test_variant_image_falls_back_to_parent_image(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        p = _seed_parent(db, t, title="T", external_id="x",
                         image="https://img/parent.jpg")
        # Variant without its own image_url
        _add_variant(db, p, salla_variant_id="v1", retailer_id="x-v1",
                     image_url=None)
        item = build_feed(db, t.id)["items"][0]
        assert item["image_link"] == "https://img/parent.jpg"


# ─────────────────────────────────────────────────────────────────────
# Per-item builder (pure function, no DB)
# ─────────────────────────────────────────────────────────────────────


class TestVariantToFeedItem:

    def test_returns_none_for_empty_retailer_id(self):
        class V: retailer_id = ""
        class P: id = 1; title = "X"
        assert variant_to_feed_item(P(), V()) is None
