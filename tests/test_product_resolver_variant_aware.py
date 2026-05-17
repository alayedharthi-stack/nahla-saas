"""tests/test_product_resolver_variant_aware.py
─────────────────────────────────
Phase 2 coverage: ``CatalogContextBuilder._format`` reads from
``Product.variants`` (the new ``product_variants`` table) and
emits:

  * a structured per-variant array on the formatted dict,
  * a ``needs_variant_choice`` boolean that's True iff 2+ real
    (non-default) in-stock variants exist,
  * the parent's ``default_variant_id`` / retailer_id so simple
    products skip the brain's variant prompt.

We also assert ``_dict_to_resolution`` propagates the new fields
onto the ``ProductResolution`` DTO the sender consumes.
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

from models import (  # noqa: E402
    Base, Product, ProductVariant, Tenant,
)
from core.store_knowledge import CatalogContextBuilder  # noqa: E402
from services.product_resolver import _dict_to_resolution  # noqa: E402


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


def _seed_parent(db, *, title="P", external_id="ext"):
    t = Tenant(name=f"T-{id(db)}", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    p = Product(
        tenant_id=t.id, title=title, external_id=external_id,
        price="100", in_stock=True, source="salla",
        has_variants=False,
    )
    db.add(p); db.commit(); db.refresh(p)
    return t, p


def _add_variant(db, p, *, sid=None, retailer_id=None, in_stock=True,
                 is_default=False, options=None, option_summary=None):
    v = ProductVariant(
        tenant_id=p.tenant_id, product_id=p.id,
        salla_variant_id=sid, retailer_id=retailer_id,
        in_stock=in_stock, is_default=is_default,
        options=options, option_summary=option_summary,
    )
    db.add(v); db.commit(); db.refresh(v)
    return v


class TestFormatExposesVariants:

    def test_multi_variant_in_stock_sets_needs_variant_choice(self):
        db, _ = _make_db()
        t, p = _seed_parent(db, title="فستان")
        _add_variant(db, p, sid="v1", retailer_id="ext-v1",
                     options={"size": "S"}, option_summary="S")
        _add_variant(db, p, sid="v2", retailer_id="ext-v2",
                     options={"size": "M"}, option_summary="M")
        p.has_variants = True
        db.commit(); db.refresh(p)

        builder = CatalogContextBuilder(db, t.id)
        formatted = builder._format(p)
        assert formatted["needs_variant_choice"] is True
        assert formatted["has_variants"] is True
        assert len(formatted["variants"]) == 2
        sids = sorted(v["salla_variant_id"] for v in formatted["variants"])
        assert sids == ["v1", "v2"]
        # Each variant carries its retailer_id so the sender can pick one
        rids = {v["salla_variant_id"]: v["retailer_id"]
                for v in formatted["variants"]}
        assert rids == {"v1": "ext-v1", "v2": "ext-v2"}

    def test_single_real_variant_does_not_force_choice(self):
        db, _ = _make_db()
        t, p = _seed_parent(db)
        _add_variant(db, p, sid="v1", retailer_id="ext-v1",
                     options={"color": "red"})
        p.has_variants = True
        db.commit(); db.refresh(p)
        formatted = CatalogContextBuilder(db, t.id)._format(p)
        # Only ONE real in-stock variant — no point asking the customer
        assert formatted["needs_variant_choice"] is False
        assert len(formatted["variants"]) == 1

    def test_synthetic_default_only_does_not_force_choice(self):
        db, _ = _make_db()
        t, p = _seed_parent(db)
        v = _add_variant(db, p, is_default=True, retailer_id="ext")
        p.default_variant_id = v.id
        db.commit(); db.refresh(p)
        formatted = CatalogContextBuilder(db, t.id)._format(p)
        assert formatted["needs_variant_choice"] is False
        assert formatted["default_variant_id"] == v.id
        assert formatted["default_variant_retailer_id"] == "ext"
        assert formatted["has_variants"] is False

    def test_oos_variants_excluded_from_choice_count(self):
        """One real variant in stock + one out of stock = no choice prompt."""
        db, _ = _make_db()
        t, p = _seed_parent(db)
        _add_variant(db, p, sid="v1", retailer_id="ext-v1", in_stock=True)
        _add_variant(db, p, sid="v2", retailer_id="ext-v2", in_stock=False)
        p.has_variants = True
        db.commit(); db.refresh(p)
        formatted = CatalogContextBuilder(db, t.id)._format(p)
        assert formatted["needs_variant_choice"] is False
        assert formatted["variants_in_stock"] == 1


class TestResolutionDTO:

    def test_dict_to_resolution_propagates_variant_fields(self):
        """The ProductResolution DTO is what the sender / brain
        actually consumes. The new fields must round-trip."""
        d = {
            "id": 7,
            "external_id": "ext_p",
            "title": "Test",
            "price": "100",
            "needs_variant_choice": True,
            "default_variant_id": 42,
            "default_variant_retailer_id": "ext_p-default",
            "has_variants": True,
            "variants": [
                {"id": 1, "salla_variant_id": "v1",
                 "retailer_id": "ext_p-v1", "in_stock": True,
                 "options": {"size": "S"}},
                {"id": 2, "salla_variant_id": "v2",
                 "retailer_id": "ext_p-v2", "in_stock": True,
                 "options": {"size": "M"}},
            ],
        }
        res = _dict_to_resolution(d)
        assert res.needs_variant_choice is True
        assert res.default_variant_id == 42
        assert res.default_variant_retailer_id == "ext_p-default"
        assert res.has_variants is True
        assert len(res.variants) == 2
        assert res.variants[0]["retailer_id"] == "ext_p-v1"

    def test_dict_to_resolution_defaults_for_legacy_callers(self):
        """An old-style dict (no variant fields) must still produce
        a valid resolution — no AttributeError, sane defaults."""
        d = {"id": 1, "title": "Old", "external_id": "x", "price": "5"}
        res = _dict_to_resolution(d)
        assert res.needs_variant_choice is False
        assert res.has_variants is False
        assert res.default_variant_id is None
        assert res.variants == []
