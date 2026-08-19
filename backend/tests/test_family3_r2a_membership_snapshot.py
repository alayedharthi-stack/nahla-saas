"""Family 3 R2-A — sqlite snapshot / isolation / no-backfill tests."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO = _BACKEND.parent
_DATABASE = _REPO / "database"
for _p in (str(_BACKEND), str(_REPO), str(_DATABASE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import JSON, create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.meta_catalog_membership import (
    DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING,
    PROVENANCE_GRAPH_RECONCILE,
    DesiredMembership,
    apply_membership_snapshot,
    invalidate_meta_catalog_membership,
    join_graph_to_local_memberships,
    load_meta_catalog_membership,
)
from services.meta_catalog_reconcile import reconcile_meta_catalog_publish_stamps

_NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)
_CAT_A = "catalog-aaa"
_CAT_B = "catalog-bbb"


def _make_db():
    from models import Base

    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)()


def _seed_tenant(db, tenant_id=7):
    from models import Tenant

    db.add(Tenant(id=tenant_id, name="tenant-%s" % tenant_id))
    db.commit()


def _seed_product(db, *, product_id, retailer_id, title="Generic cotton shirt", has_variants=False, default_variant_id=None):
    from models import Product

    row = Product(
        id=product_id,
        tenant_id=7,
        title=title,
        price="10",
        in_stock=True,
        external_id=retailer_id,
        meta_retailer_id=retailer_id,
        has_variants=has_variants,
        default_variant_id=default_variant_id,
    )
    db.add(row)
    db.commit()
    return row


def _seed_variant(db, *, variant_id, product_id, retailer_id, is_default=False):
    from models import ProductVariant

    row = ProductVariant(
        id=variant_id,
        tenant_id=7,
        product_id=product_id,
        retailer_id=retailer_id,
        is_default=is_default,
        in_stock=True,
    )
    db.add(row)
    db.commit()
    return row


def _seed_connection(db, catalog_id=_CAT_A):
    from models import WhatsAppConnection

    row = WhatsAppConnection(
        tenant_id=7,
        phone_number_id="1",
        status="connected",
        sending_enabled=True,
        catalog_enabled=True,
        meta_catalog_id=catalog_id,
        provider="meta",
        connection_type="direct",
        access_token="dummy",
    )
    db.add(row)
    db.commit()
    return row


def _row(**kw):
    from models import MetaCatalogMembership

    payload = dict(
        tenant_id=7,
        catalog_id=_CAT_A,
        retailer_id="rid-a",
        product_id=1,
        variant_id=None,
        meta_item_id="mg",
        verified_at=_NOW,
        provenance=PROVENANCE_GRAPH_RECONCILE,
    )
    payload.update(kw)
    return MetaCatalogMembership(**payload)


class TestMigrationDoesNotBackfill:
    def test_migration_file_does_not_seed_legacy_stamps(self):
        src = (_DATABASE / "migrations" / "versions" / "0099_meta_catalog_memberships.py").read_text(encoding="utf-8")
        assert "op.bulk_insert" not in src
        assert "INSERT INTO meta_catalog_memberships" not in src
        assert "meta_catalog_published_at" in src

    def test_create_all_starts_empty_even_if_product_has_stamp(self):
        from models import MetaCatalogMembership, Product

        db = _make_db()
        _seed_tenant(db)
        p = _seed_product(db, product_id=23, retailer_id="398551325")
        p.meta_catalog_published_at = _NOW
        db.commit()
        assert db.query(MetaCatalogMembership).count() == 0
        assert inspect(db.get_bind()).has_table("meta_catalog_memberships")


class TestUniqueSemanticKey:
    def test_unique_tenant_catalog_retailer(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_product(db, product_id=1, retailer_id="R")
        _seed_product(db, product_id=2, retailer_id="S")
        db.add(_row(product_id=1, retailer_id="R"))
        db.commit()
        db.add(_row(product_id=2, retailer_id="R"))
        try:
            db.commit()
            raise AssertionError("duplicate membership must fail")
        except IntegrityError:
            db.rollback()


class TestCompleteSnapshot:
    def test_complete_reconcile_writes_exact_join(self):
        from models import MetaCatalogMembership

        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        _seed_product(db, product_id=101, retailer_id="rid-a")
        live = {"rid-a": {"meta_product_id": "mg-101"}}
        info = {"complete": True, "error": None, "pages": 1, "items": 1}
        with patch(
            "services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
            return_value=(live, info),
        ):
            report = reconcile_meta_catalog_publish_stamps(db, 7, apply=True)
        assert report.snapshot_applied is True
        row = db.query(MetaCatalogMembership).one()
        assert row.catalog_id == _CAT_A
        assert row.retailer_id == "rid-a"
        assert row.product_id == 101
        assert row.meta_item_id == "mg-101"
        assert row.verified_at is not None
        assert row.provenance == PROVENANCE_GRAPH_RECONCILE

    def test_failed_fetch_preserves_previous_snapshot(self):
        from models import MetaCatalogMembership

        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        _seed_product(db, product_id=101, retailer_id="rid-a")
        db.add(_row(product_id=101, retailer_id="rid-a", meta_item_id="keep-me"))
        db.commit()
        with patch(
            "services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
            return_value=({}, {"complete": False, "error": "graph_timeout", "pages": 0}),
        ):
            report = reconcile_meta_catalog_publish_stamps(db, 7, apply=True)
        assert report.snapshot_applied is False
        assert db.query(MetaCatalogMembership).one().meta_item_id == "keep-me"

    def test_partial_pagination_does_not_replace_snapshot(self):
        from models import MetaCatalogMembership

        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        _seed_product(db, product_id=101, retailer_id="rid-a")
        db.add(_row(product_id=101, retailer_id="rid-a", meta_item_id="complete-old"))
        db.commit()
        with patch(
            "services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
            return_value=(
                {"other": {"meta_product_id": "x"}},
                {"complete": False, "error": "paging_incomplete", "pages": 1},
            ),
        ):
            report = reconcile_meta_catalog_publish_stamps(db, 7, apply=True)
        assert report.snapshot_applied is False
        assert db.query(MetaCatalogMembership).one().meta_item_id == "complete-old"

    def test_successful_reconcile_removes_stale_current_catalog_only(self):
        from models import MetaCatalogMembership

        db = _make_db()
        _seed_tenant(db)
        _seed_product(db, product_id=1, retailer_id="keep")
        _seed_product(db, product_id=2, retailer_id="stale")
        _seed_product(db, product_id=3, retailer_id="other-cat")
        apply_membership_snapshot(
            db, tenant_id=7, catalog_id=_CAT_A,
            desired=[
                DesiredMembership(retailer_id="keep", product_id=1, variant_id=None, meta_item_id="1"),
                DesiredMembership(retailer_id="stale", product_id=2, variant_id=None, meta_item_id="2"),
            ],
            verified_at=_NOW,
        )
        apply_membership_snapshot(
            db, tenant_id=7, catalog_id=_CAT_B,
            desired=[
                DesiredMembership(retailer_id="other-cat", product_id=3, variant_id=None, meta_item_id="3"),
            ],
            verified_at=_NOW,
        )
        apply_membership_snapshot(
            db, tenant_id=7, catalog_id=_CAT_A,
            desired=[
                DesiredMembership(retailer_id="keep", product_id=1, variant_id=None, meta_item_id="1b"),
            ],
            verified_at=_NOW,
        )
        keys = {(r.catalog_id, r.retailer_id, r.product_id) for r in db.query(MetaCatalogMembership).all()}
        assert keys == {(_CAT_A, "keep", 1), (_CAT_B, "other-cat", 3)}


class TestExactVariantGrain:
    def test_meta_contains_variant_a_only(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_product(db, product_id=10, retailer_id="parent", has_variants=True)
        _seed_variant(db, variant_id=1, product_id=10, retailer_id="rid-a")
        _seed_variant(db, variant_id=2, product_id=10, retailer_id="rid-b")
        report = join_graph_to_local_memberships(
            db, tenant_id=7, live_products={"rid-a": {"meta_product_id": "mg-a"}},
        )
        assert [d.variant_id for d in report.desired] == [1]
        apply_membership_snapshot(db, tenant_id=7, catalog_id=_CAT_A, desired=report.desired, verified_at=_NOW)
        assert load_meta_catalog_membership(db, tenant_id=7, catalog_id=_CAT_A, retailer_id="rid-a").variant_id == 1
        assert load_meta_catalog_membership(db, tenant_id=7, catalog_id=_CAT_A, retailer_id="rid-b") is None


class TestAmbiguityInvalidates:
    def test_ambiguous_mapping_is_not_written_and_old_row_removed(self):
        from models import MetaCatalogMembership

        db = _make_db()
        _seed_tenant(db)
        _seed_product(db, product_id=1, retailer_id="R")
        apply_membership_snapshot(
            db, tenant_id=7, catalog_id=_CAT_A,
            desired=[DesiredMembership(retailer_id="R", product_id=1, variant_id=None, meta_item_id="old")],
            verified_at=_NOW,
        )
        _seed_product(db, product_id=2, retailer_id="R", title="Other generic perfume")
        report = join_graph_to_local_memberships(
            db, tenant_id=7, live_products={"R": {"meta_product_id": "mg"}},
        )
        assert report.desired == []
        assert report.ambiguous[0]["diagnostic"] == DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING
        apply_membership_snapshot(db, tenant_id=7, catalog_id=_CAT_A, desired=report.desired, verified_at=_NOW)
        assert db.query(MetaCatalogMembership).count() == 0


class TestProviderContradictionExact:
    def test_131009_invalidates_only_exact_retailer(self):
        from models import MetaCatalogMembership

        db = _make_db()
        _seed_tenant(db)
        _seed_product(db, product_id=10, retailer_id="parent")
        _seed_variant(db, variant_id=1, product_id=10, retailer_id="rid-a")
        _seed_variant(db, variant_id=2, product_id=10, retailer_id="rid-b")
        apply_membership_snapshot(
            db, tenant_id=7, catalog_id=_CAT_A,
            desired=[
                DesiredMembership(retailer_id="rid-a", product_id=10, variant_id=1, meta_item_id="a"),
                DesiredMembership(retailer_id="rid-b", product_id=10, variant_id=2, meta_item_id="b"),
            ],
            verified_at=_NOW,
        )
        apply_membership_snapshot(
            db, tenant_id=7, catalog_id=_CAT_B,
            desired=[
                DesiredMembership(retailer_id="rid-a", product_id=10, variant_id=1, meta_item_id="a-b"),
            ],
            verified_at=_NOW,
        )
        n = invalidate_meta_catalog_membership(db, tenant_id=7, catalog_id=_CAT_A, retailer_id="rid-a")
        assert n == 1
        remaining = {(r.catalog_id, r.retailer_id) for r in db.query(MetaCatalogMembership).all()}
        assert remaining == {(_CAT_A, "rid-b"), (_CAT_B, "rid-a")}
