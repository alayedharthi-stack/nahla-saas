"""tests/test_store_sync_writes_variants.py
─────────────────────────────────
Phase 2 coverage for the catalog refactor: ``store_sync`` must
persist real ``product_variants`` rows from the adapter payload,
update them in place on re-sync, and SOFT-PRUNE variants that
disappeared instead of deleting them.

We exercise the helper ``_upsert_variants_for`` directly against a
seeded in-memory DB so the contract is pinned without spinning up
the full Salla adapter mock chain.
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
from services.store_sync import _upsert_variants_for  # noqa: E402


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


def _seed(db, *, variants=None, external_id="ext_p", meta_retailer_id=None):
    t = Tenant(name=f"T-{id(db)}", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    p = Product(
        tenant_id=t.id, title="P",
        external_id=external_id,
        meta_retailer_id=meta_retailer_id,
        price="100",
        in_stock=True,
        source="salla",
    )
    db.add(p); db.commit(); db.refresh(p)
    return t, p


# ─────────────────────────────────────────────────────────────────────
# Variant create / update flow
# ─────────────────────────────────────────────────────────────────────


class TestVariantUpsert:

    def test_first_sync_creates_one_row_per_variant(self):
        db, _ = _make_db()
        _, p = _seed(db)
        normalised = {
            "external_id": "ext_p",
            "title": "P",
            "price": "100",
            "in_stock": True,
            "currency": "SAR",
            "image_url": "https://img/parent.jpg",
            "variants": [
                {"id": "v1", "title": "Small", "price": 90, "in_stock": True,
                 "options": {"size": "S"}},
                {"id": "v2", "title": "Med", "price": 100, "in_stock": True,
                 "options": {"size": "M"}},
                {"id": "v3", "title": "Large", "price": 110, "in_stock": False,
                 "options": {"size": "L"}},
            ],
        }
        _upsert_variants_for(db, p, normalised)
        db.commit()

        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        assert len(rows) == 3
        by_sid = {r.salla_variant_id: r for r in rows}
        assert set(by_sid) == {"v1", "v2", "v3"}
        # Per-variant retailer_id pattern: {parent_external}-{salla_variant_id}
        assert by_sid["v1"].retailer_id == "ext_p-v1"
        assert by_sid["v2"].retailer_id == "ext_p-v2"
        # in_stock false propagated
        assert by_sid["v3"].in_stock is False
        # Parent flags re-stamped
        db.refresh(p)
        assert p.has_variants is True
        assert p.default_variant_id is not None

    def test_resync_updates_existing_rows_in_place(self):
        db, _ = _make_db()
        _, p = _seed(db)
        first = {
            "external_id": "ext_p", "title": "P", "price": "100",
            "in_stock": True, "currency": "SAR",
            "variants": [
                {"id": "v1", "price": 90, "in_stock": True,
                 "options": {"size": "S"}},
            ],
        }
        _upsert_variants_for(db, p, first); db.commit()
        v1 = db.query(ProductVariant).filter_by(product_id=p.id).one()
        first_id = v1.id

        # Re-sync with a price change
        second = dict(first)
        second["variants"] = [
            {"id": "v1", "price": 99, "in_stock": True,
             "options": {"size": "S"}},
        ]
        _upsert_variants_for(db, p, second); db.commit()
        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        assert len(rows) == 1, "re-sync must NOT duplicate the variant row"
        assert rows[0].id == first_id, "primary key must be preserved"
        assert rows[0].price == "99"

    def test_disappeared_variant_is_soft_pruned_not_deleted(self):
        """v2 drops off the next sync — its row must persist with
        in_stock=False so order_items.variant_id history is safe."""
        db, _ = _make_db()
        _, p = _seed(db)
        _upsert_variants_for(db, p, {
            "external_id": "ext_p", "title": "P", "price": "100",
            "in_stock": True,
            "variants": [
                {"id": "v1", "in_stock": True},
                {"id": "v2", "in_stock": True},
            ],
        }); db.commit()
        assert db.query(ProductVariant).filter_by(product_id=p.id).count() == 2

        _upsert_variants_for(db, p, {
            "external_id": "ext_p", "title": "P", "price": "100",
            "in_stock": True,
            "variants": [
                {"id": "v1", "in_stock": True},
            ],
        }); db.commit()

        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        assert len(rows) == 2, "soft prune — row must still exist"
        v2 = next(r for r in rows if r.salla_variant_id == "v2")
        assert v2.in_stock is False, "pruned variant must be marked OOS"

    def test_no_variants_creates_synthetic_default(self):
        db, _ = _make_db()
        _, p = _seed(db)
        _upsert_variants_for(db, p, {
            "external_id": "ext_p", "title": "P", "price": "55",
            "in_stock": True, "variants": [],
        }); db.commit()
        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        assert len(rows) == 1
        assert rows[0].is_default is True
        assert rows[0].salla_variant_id is None
        # Retailer id falls back to external_id then synthetic
        assert rows[0].retailer_id == "ext_p"
        db.refresh(p)
        assert p.has_variants is False

    def test_parent_with_hyphenated_override_routes_per_variant(self):
        """A merchant who set ``meta_retailer_id='custom-base'`` on
        the parent has signalled "use this prefix" — variants should
        round-trip as ``custom-{salla_variant_id}``."""
        db, _ = _make_db()
        _, p = _seed(db, meta_retailer_id="custom-base")
        _upsert_variants_for(db, p, {
            "external_id": "ext_p", "title": "P", "price": "10",
            "in_stock": True,
            "variants": [
                {"id": "vX", "in_stock": True},
                {"id": "vY", "in_stock": True},
            ],
        }); db.commit()
        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        rids = {r.salla_variant_id: r.retailer_id for r in rows}
        assert rids == {"vX": "custom-vX", "vY": "custom-vY"}


# ─────────────────────────────────────────────────────────────────────
# Env flag rollback
# ─────────────────────────────────────────────────────────────────────


class TestVariantSyncFlag:

    def test_disabled_flag_skips_variant_write(self, monkeypatch):
        """Operators can ship Phase 2 then flip CATALOG_VARIANT_SYNC
        off without redeploying — the parent path must still run but
        the variant rows MUST NOT be touched."""
        monkeypatch.setenv("CATALOG_VARIANT_SYNC", "false")
        db, _ = _make_db()
        _, p = _seed(db)
        _upsert_variants_for(db, p, {
            "external_id": "ext_p", "title": "P", "price": "10",
            "in_stock": True,
            "variants": [{"id": "v1"}],
        }); db.commit()
        assert db.query(ProductVariant).filter_by(product_id=p.id).count() == 0
