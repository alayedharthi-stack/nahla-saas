"""tests/test_product_variants_schema.py
─────────────────────────────────
Phase 1 coverage for the parent/variant catalog refactor (migration
``0064``). We exercise the SQL backfill directly against an in-memory
SQLite engine so the contract is pinned without spinning up Alembic:

  * Every existing ``products`` row ends up with at least one
    ``product_variants`` row after the backfill — products with
    ``metadata->'variants'`` get one row per element, products
    without get a single ``is_default=True`` synthetic mirror.
  * ``meta_retailer_id`` override on the parent propagates to the
    variant ``retailer_id`` (the merchant's published id survives).
  * ``effective_variant_retailer_id`` resolves variant → parent →
    legacy chain without raising on any of the three input shapes.
  * Parent stamps: ``has_variants`` flips True iff 2+ variants exist
    OR a single non-default variant; ``default_variant_id`` points
    at the synthetic / first row.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import JSON, create_engine, inspect, text
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
from core.catalog import (  # noqa: E402
    effective_retailer_id,
    effective_variant_retailer_id,
)


def _make_db():
    """Spin up an in-memory SQLite DB with the JSONB columns coerced to
    JSON so Base.metadata.create_all works on a non-Postgres dialect.
    Returns ``(session, engine)``."""
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
    t = Tenant(name=f"Tenant-{id(db)}", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _seed_product(db, tenant_id, *, title="Product",
                  external_id=None, meta_retailer_id=None,
                  price=None, variants=None, in_stock=True,
                  source="salla"):
    """Insert a Product row directly so we can mimic the pre-migration
    state (variants live as JSON inside ``metadata->'variants'``)."""
    metadata = {}
    if variants is not None:
        metadata["variants"] = variants
    p = Product(
        tenant_id=tenant_id,
        title=title,
        external_id=external_id,
        meta_retailer_id=meta_retailer_id,
        price=price,
        in_stock=in_stock,
        extra_metadata=metadata or None,
        source=source,
    )
    db.add(p); db.commit(); db.refresh(p)
    return p


def _run_backfill(db, engine):
    """Run the migration's SQLite backfill SQL directly. Mirrors the
    upgrade() function in ``0064_product_variants.py`` — kept as a
    private helper here so the contract is exercised even when the
    Alembic runner isn't wired up in the test harness."""
    with engine.begin() as conn:
        # Pass (a) — variants in metadata JSON.
        conn.execute(text("""
            INSERT INTO product_variants (
                tenant_id, product_id, salla_variant_id, sku,
                retailer_id, price, currency, stock_quantity,
                in_stock, options, option_summary, image_url,
                is_default, metadata
            )
            SELECT
                p.tenant_id,
                p.id,
                NULLIF(json_extract(v.value, '$.id'), ''),
                NULLIF(json_extract(v.value, '$.sku'), ''),
                COALESCE(
                    NULLIF(p.meta_retailer_id, ''),
                    NULLIF(p.external_id, '')
                ),
                COALESCE(NULLIF(json_extract(v.value, '$.price'), ''), p.price),
                NULLIF(json_extract(v.value, '$.currency'), ''),
                CAST(NULLIF(json_extract(v.value, '$.stock_quantity'), '') AS INTEGER),
                CASE
                    WHEN json_extract(v.value, '$.in_stock') = 0 THEN 0
                    WHEN p.in_stock IS NOT NULL THEN p.in_stock
                    ELSE 1
                END,
                CASE
                    WHEN json_type(v.value, '$.options') = 'object'
                        THEN json_extract(v.value, '$.options')
                    ELSE NULL
                END,
                NULLIF(json_extract(v.value, '$.option_summary'), ''),
                NULLIF(json_extract(v.value, '$.image_url'), ''),
                0,
                v.value
            FROM products p, json_each(
                CASE
                    WHEN json_type(p.metadata, '$.variants') = 'array'
                        THEN json_extract(p.metadata, '$.variants')
                    ELSE '[]'
                END
            ) AS v
            WHERE NOT EXISTS (
                SELECT 1 FROM product_variants pv WHERE pv.product_id = p.id
            )
        """))

        # Pass (b) — synthetic default for products with no variants.
        conn.execute(text("""
            INSERT INTO product_variants (
                tenant_id, product_id, salla_variant_id, sku,
                retailer_id, price, currency, stock_quantity,
                in_stock, options, option_summary, image_url,
                is_default
            )
            SELECT
                p.tenant_id, p.id, NULL, p.sku,
                COALESCE(
                    NULLIF(p.meta_retailer_id, ''),
                    NULLIF(p.external_id, ''),
                    'nahla_p_' || p.id
                ),
                p.price, NULL, p.stock_quantity,
                COALESCE(p.in_stock, 1),
                NULL, NULL, NULL,
                1
            FROM products p
            WHERE NOT EXISTS (
                SELECT 1 FROM product_variants pv WHERE pv.product_id = p.id
            )
        """))

        # Parent stamps.
        conn.execute(text("""
            UPDATE products
               SET has_variants = (
                   SELECT
                       CASE
                           WHEN COUNT(*) > 1 THEN 1
                           WHEN MAX(CASE WHEN pv.is_default = 0 THEN 1 ELSE 0 END) = 1 THEN 1
                           ELSE 0
                       END
                     FROM product_variants pv
                    WHERE pv.product_id = products.id
               )
        """))
        conn.execute(text("""
            UPDATE products
               SET default_variant_id = (
                   SELECT pv.id FROM product_variants pv
                    WHERE pv.product_id = products.id
                 ORDER BY pv.is_default DESC, pv.id ASC
                    LIMIT 1
               )
             WHERE default_variant_id IS NULL
        """))


# ─────────────────────────────────────────────────────────────────────
# Backfill coverage
# ─────────────────────────────────────────────────────────────────────


class TestBackfillCoversEveryRow:
    """After the migration backfill the invariant is: every product
    has at least one variant row. Three production-realistic shapes are
    exercised here — a multi-variant Salla product, a one-SKU product
    with no variants array, and a product whose ``meta_retailer_id``
    was hand-edited by the merchant."""

    def test_multi_variant_product_gets_one_row_per_variant(self):
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(
            db, t.id,
            title="فستان",
            external_id="salla_999",
            variants=[
                {"id": "v1", "price": "120", "in_stock": 1,
                 "options": {"size": "S"}, "option_summary": "S"},
                {"id": "v2", "price": "130", "in_stock": 1,
                 "options": {"size": "M"}, "option_summary": "M"},
                {"id": "v3", "price": "130", "in_stock": 0,
                 "options": {"size": "L"}, "option_summary": "L"},
            ],
        )
        _run_backfill(db, engine)

        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        assert len(rows) == 3, "expected one variant row per JSON element"
        salla_ids = sorted(r.salla_variant_id for r in rows)
        assert salla_ids == ["v1", "v2", "v3"]
        # All three inherit the parent's external_id as their retailer
        # because we didn't override at the parent level.
        assert all(r.retailer_id == "salla_999" for r in rows)
        # The L size's ``in_stock=0`` propagated as False.
        l_row = next(r for r in rows if r.salla_variant_id == "v3")
        assert l_row.in_stock is False
        assert l_row.options == {"size": "L"} or json.loads(l_row.options or "null") == {"size": "L"}

        db.refresh(p)
        assert p.has_variants is True
        # default_variant_id points at the synthetic row when one
        # exists; here there's no synthetic, so we point at the
        # lowest-id non-default row (deterministic).
        assert p.default_variant_id == min(r.id for r in rows)

    def test_simple_product_gets_one_synthetic_default(self):
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(
            db, t.id,
            title="عطر",
            external_id="salla_42",
            price="55",
            variants=None,  # no variants array at all
        )
        _run_backfill(db, engine)

        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        assert len(rows) == 1
        v = rows[0]
        assert v.is_default is True
        assert v.salla_variant_id is None
        assert v.retailer_id == "salla_42"
        assert v.price == "55"

        db.refresh(p)
        assert p.has_variants is False, (
            "a single default-only variant must NOT flip has_variants"
        )
        assert p.default_variant_id == v.id

    def test_meta_retailer_override_survives_backfill(self):
        """A merchant who manually overrode ``meta_retailer_id`` keeps
        that value as the variant's ``retailer_id``."""
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(
            db, t.id,
            title="حذاء",
            external_id="salla_77",
            meta_retailer_id="custom_meta_77",
            variants=[
                {"id": "vA", "price": "200", "in_stock": 1},
                {"id": "vB", "price": "210", "in_stock": 1},
            ],
        )
        _run_backfill(db, engine)
        rows = db.query(ProductVariant).filter_by(product_id=p.id).all()
        assert {r.retailer_id for r in rows} == {"custom_meta_77"}, (
            "merchant override must take precedence over external_id"
        )

    def test_backfill_is_idempotent(self):
        """Running the backfill twice must NOT duplicate variant rows
        — the ``NOT EXISTS`` guard keeps the second pass a no-op."""
        db, engine = _make_db()
        t = _seed_tenant(db)
        _seed_product(
            db, t.id, title="A", external_id="salla_1",
            variants=[{"id": "v1"}, {"id": "v2"}],
        )
        _seed_product(db, t.id, title="B", external_id="salla_2")  # no variants
        _run_backfill(db, engine)
        count_first = db.query(ProductVariant).count()
        _run_backfill(db, engine)
        count_second = db.query(ProductVariant).count()
        assert count_first == count_second == 3, (
            "backfill must be idempotent — 2 real variants + 1 default = 3"
        )


# ─────────────────────────────────────────────────────────────────────
# effective_variant_retailer_id helper
# ─────────────────────────────────────────────────────────────────────


class TestEffectiveVariantRetailerId:
    """The variant-aware helper must accept variant / parent / dict
    shapes and never raise. Each branch is exercised below."""

    def test_variant_row_returns_its_retailer_id(self):
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(
            db, t.id, title="X", external_id="ext_parent",
            variants=[{"id": "v1"}],
        )
        _run_backfill(db, engine)
        v = db.query(ProductVariant).filter_by(product_id=p.id).first()
        # Mutate the variant to a different retailer_id so we can prove
        # the helper reads from the variant (not the parent fallback).
        v.retailer_id = "variant_explicit_id"
        db.commit()
        assert effective_variant_retailer_id(v) == "variant_explicit_id"

    def test_parent_row_falls_back_to_default_variant(self):
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(
            db, t.id, title="Y", external_id="ext_y",
            variants=None,  # synthetic default created
        )
        _run_backfill(db, engine)
        db.refresh(p)
        # SQLAlchemy needs the default_variant relationship loaded:
        v = db.get(ProductVariant, p.default_variant_id)
        # Manually attach to mimic an eager-loaded parent row that
        # the resolver would pass in.
        p.default_variant = v
        # The synthetic default's retailer_id is the parent's
        # external_id (set by the backfill).
        assert effective_variant_retailer_id(p) == "ext_y"

    def test_legacy_parent_dict_falls_through_to_effective_retailer_id(self):
        """A bare dict (resolver path) with no ``default_variant`` and
        no ``product_id`` must fall through to the legacy
        ``effective_retailer_id`` chain rather than crash."""
        legacy_dict = {
            "id": 1,
            "title": "Legacy",
            "meta_retailer_id": "legacy_meta",
            "external_id": "legacy_ext",
        }
        # Variant-aware helper falls through to effective_retailer_id
        # which picks meta_retailer_id over external_id.
        assert effective_variant_retailer_id(legacy_dict) == "legacy_meta"
        # And the bare effective_retailer_id agrees.
        assert effective_retailer_id(legacy_dict) == "legacy_meta"

    def test_none_input_returns_empty_string(self):
        assert effective_variant_retailer_id(None) == ""

    def test_variant_with_empty_retailer_returns_empty(self):
        """A variant row that somehow ended up with an empty
        ``retailer_id`` and no legacy fallback must return ""; the
        caller will then route to the non-catalog send path."""
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(db, t.id, title="Z", external_id=None,
                          variants=[{"id": "v1"}])
        _run_backfill(db, engine)
        v = db.query(ProductVariant).filter_by(product_id=p.id).first()
        v.retailer_id = None
        db.commit()
        assert effective_variant_retailer_id(v) == ""


# ─────────────────────────────────────────────────────────────────────
# Parent stamps
# ─────────────────────────────────────────────────────────────────────


class TestParentStamps:
    """``products.has_variants`` and ``products.default_variant_id``
    are the cheap read paths the sender / brain use. They must be
    populated correctly by the backfill."""

    def test_has_variants_true_for_multi_variant(self):
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(
            db, t.id, title="A", external_id="ext",
            variants=[{"id": "v1"}, {"id": "v2"}, {"id": "v3"}],
        )
        _run_backfill(db, engine)
        db.refresh(p)
        assert p.has_variants is True

    def test_has_variants_false_for_default_only(self):
        db, engine = _make_db()
        t = _seed_tenant(db)
        p = _seed_product(db, t.id, title="A", external_id="ext")
        _run_backfill(db, engine)
        db.refresh(p)
        assert p.has_variants is False

    def test_default_variant_id_always_populated_after_backfill(self):
        db, engine = _make_db()
        t = _seed_tenant(db)
        _seed_product(db, t.id, title="A", external_id="ea")
        _seed_product(db, t.id, title="B", external_id="eb",
                      variants=[{"id": "v1"}])
        _run_backfill(db, engine)
        for p in db.query(Product).all():
            db.refresh(p)
            assert p.default_variant_id is not None, (
                f"product {p.id!r} ended up without a default_variant_id "
                f"after backfill"
            )


# ─────────────────────────────────────────────────────────────────────
# Schema sanity — every column the migration claims to add is real
# ─────────────────────────────────────────────────────────────────────


class TestSchemaSurface:
    """Asserts the ORM-level schema picture lines up with the migration
    promise. If the ORM definition diverges from the migration the
    suite catches it here instead of in a runtime KeyError six layers
    deep in the sender."""

    def test_product_variant_table_has_required_columns(self):
        db, engine = _make_db()
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("product_variants")}
        required = {
            "id", "tenant_id", "product_id", "salla_variant_id",
            "sku", "retailer_id", "price", "currency",
            "stock_quantity", "in_stock", "options", "option_summary",
            "image_url", "is_default", "metadata",
            "created_at", "updated_at",
        }
        missing = required - cols
        assert not missing, f"product_variants is missing columns: {missing}"

    def test_products_table_has_parent_flag_columns(self):
        db, engine = _make_db()
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("products")}
        assert "has_variants" in cols
        assert "default_variant_id" in cols
