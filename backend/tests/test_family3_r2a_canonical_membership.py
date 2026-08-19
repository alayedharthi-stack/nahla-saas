"""Family 3 R2-A — canonical MetaCatalogMembership owner tests."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO = _BACKEND.parent
for _p in (str(_BACKEND), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import UniqueConstraint  # noqa: E402

from core.meta_catalog_membership import (  # noqa: E402
    DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING,
    PROVENANCE_GRAPH_RECONCILE,
    DesiredMembership,
    MetaCatalogMembershipFact,
    invalidate_meta_catalog_membership,
    join_graph_to_local_memberships,
    membership_authorizes_send,
)
from core.native_catalog_capability import (  # noqa: E402
    REASON_META_CATALOG_UNVERIFIED,
    count_matchable_catalog_products,
    evaluate_native_catalog_capability,
    evaluate_native_catalog_product_capability,
    pick_thumbnail_retailer_id,
)
from services.catalog_product_orchestrator import (  # noqa: E402
    ProductCardSendAction,
    evaluate_product_card_send,
)
from services.whatsapp_platform.catalog_sender import (  # noqa: E402
    _classify_catalog_provider_failure,
)

_NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)
_CAT_A = "catalog-aaa"
_CAT_B = "catalog-bbb"


def _fact(**kw) -> MetaCatalogMembershipFact:
    base = dict(
        tenant_id=7,
        catalog_id=_CAT_A,
        retailer_id="rid-a",
        product_id=101,
        variant_id=None,
        meta_item_id="mg-1",
        verified_at=_NOW,
        provenance=PROVENANCE_GRAPH_RECONCILE,
    )
    base.update(kw)
    return MetaCatalogMembershipFact(**base)


def _conn(catalog_id: str = _CAT_A):
    return SimpleNamespace(
        status="connected",
        sending_enabled=True,
        phone_number_id="1",
        catalog_enabled=True,
        meta_catalog_id=catalog_id,
        provider="meta",
    )


class TestSchemaOwner:
    def test_orm_has_production_membership_fields(self):
        from models import MetaCatalogMembership

        cols = {c.name for c in MetaCatalogMembership.__table__.columns}
        assert {
            "tenant_id",
            "catalog_id",
            "retailer_id",
            "product_id",
            "variant_id",
            "meta_item_id",
            "verified_at",
            "provenance",
        }.issubset(cols)
        names = {c.name for c in MetaCatalogMembership.__table__.constraints}
        assert "uq_meta_catalog_memberships_tenant_catalog_retailer" in names

    def test_unique_constraint_is_tenant_catalog_retailer(self):
        from models import MetaCatalogMembership

        uqs = [
            c
            for c in MetaCatalogMembership.__table__.constraints
            if isinstance(c, UniqueConstraint)
        ]
        assert any(
            list(uq.columns.keys()) == ["tenant_id", "catalog_id", "retailer_id"]
            for uq in uqs
        )


class TestStampIsNotAuthorization:
    def test_published_stamp_alone_does_not_authorize(self):
        product = SimpleNamespace(
            id=101,
            tenant_id=7,
            external_id="ext-101",
            meta_retailer_id="rid-a",
            has_variants=False,
            default_variant_id=None,
            meta_catalog_published_at=_NOW,
        )
        cap = evaluate_native_catalog_product_capability(
            product, catalog_id=_CAT_A, tenant_id=7, membership=None,
        )
        assert cap.available is False
        assert cap.reason == REASON_META_CATALOG_UNVERIFIED

    def test_external_id_alone_does_not_authorize(self):
        product = SimpleNamespace(
            id=101,
            tenant_id=7,
            external_id="ext-101",
            meta_retailer_id=None,
            has_variants=False,
            default_variant_id=None,
            meta_catalog_published_at=None,
        )
        cap = evaluate_native_catalog_product_capability(
            product, catalog_id=_CAT_A, tenant_id=7,
        )
        assert cap.available is False


class TestExactIdentity:
    def test_catalog_a_fact_cannot_authorize_catalog_b(self):
        assert not membership_authorizes_send(
            _fact(catalog_id=_CAT_A),
            tenant_id=7,
            catalog_id=_CAT_B,
            retailer_id="rid-a",
            product_id=101,
        )

    def test_variant_a_membership_does_not_authorize_variant_b(self):
        fact = _fact(retailer_id="rid-a", variant_id=1)
        assert membership_authorizes_send(
            fact,
            tenant_id=7,
            catalog_id=_CAT_A,
            retailer_id="rid-a",
            product_id=101,
            bound_variant_id=1,
            explicit_variant=True,
        )
        assert not membership_authorizes_send(
            fact,
            tenant_id=7,
            catalog_id=_CAT_A,
            retailer_id="rid-b",
            product_id=101,
            bound_variant_id=2,
            explicit_variant=True,
        )

    def test_multi_variant_cannot_bypass_choice(self):
        fact = _fact(variant_id=1)
        assert not membership_authorizes_send(
            fact,
            tenant_id=7,
            catalog_id=_CAT_A,
            retailer_id="rid-a",
            product_id=101,
            product_has_variants=True,
            explicit_variant=False,
            canonical_default_variant_id=1,
        )

    def test_simple_product_default_variant_membership_authorizes(self):
        fact = _fact(variant_id=9)
        assert membership_authorizes_send(
            fact,
            tenant_id=7,
            catalog_id=_CAT_A,
            retailer_id="rid-a",
            product_id=101,
            product_has_variants=False,
            canonical_default_variant_id=9,
        )


class TestAmbiguity:
    def test_two_products_same_retailer_id_are_ambiguous(self):
        db = MagicMock()
        p1 = SimpleNamespace(
            id=1, tenant_id=7, meta_retailer_id="R", external_id=None,
            has_variants=False, default_variant_id=None,
        )
        p2 = SimpleNamespace(
            id=2, tenant_id=7, meta_retailer_id="R", external_id=None,
            has_variants=False, default_variant_id=None,
        )

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", "")
            if name == "Product":
                q.filter.return_value.all.return_value = [p1, p2]
            else:
                q.filter.return_value.all.return_value = []
            return q

        db.query.side_effect = _query
        report = join_graph_to_local_memberships(
            db, tenant_id=7, live_products={"R": {"meta_product_id": "m1"}},
        )
        assert report.desired == []
        assert report.ambiguous
        assert report.ambiguous[0]["diagnostic"] == DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING

    def test_parent_and_default_variant_same_sku_collapse(self):
        db = MagicMock()
        product = SimpleNamespace(
            id=10, tenant_id=7, meta_retailer_id="R", external_id=None,
            has_variants=False, default_variant_id=99,
        )
        variant = SimpleNamespace(
            id=99, product_id=10, tenant_id=7, retailer_id="R", is_default=True,
        )

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", "")
            if name == "Product":
                q.filter.return_value.all.return_value = [product]
            else:
                q.filter.return_value.all.return_value = [variant]
            return q

        db.query.side_effect = _query
        report = join_graph_to_local_memberships(
            db, tenant_id=7, live_products={"R": {"meta_product_id": "m1"}},
        )
        assert len(report.desired) == 1
        assert report.desired[0].product_id == 10
        assert report.desired[0].variant_id == 99
        assert report.ambiguous == []

    def test_parent_and_non_default_variant_same_retailer_is_ambiguous(self):
        db = MagicMock()
        product = SimpleNamespace(
            id=10, tenant_id=7, meta_retailer_id="R", external_id=None,
            has_variants=True, default_variant_id=1,
        )
        default_v = SimpleNamespace(
            id=1, product_id=10, tenant_id=7, retailer_id="OTHER", is_default=True,
        )
        other_v = SimpleNamespace(
            id=2, product_id=10, tenant_id=7, retailer_id="R", is_default=False,
        )

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", "")
            if name == "Product":
                q.filter.return_value.all.return_value = [product]
            else:
                q.filter.return_value.all.return_value = [default_v, other_v]
            return q

        db.query.side_effect = _query
        report = join_graph_to_local_memberships(
            db, tenant_id=7, live_products={"R": {"meta_product_id": "m1"}},
        )
        assert report.desired == []
        assert report.ambiguous
        assert report.ambiguous[0]["diagnostic"] == DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING

    def test_parent_and_is_default_variant_not_pointed_to_is_ambiguous(self):
        db = MagicMock()
        product = SimpleNamespace(
            id=10, tenant_id=7, meta_retailer_id="R", external_id=None,
            has_variants=True, default_variant_id=1,
        )
        pointed = SimpleNamespace(
            id=1, product_id=10, tenant_id=7, retailer_id="OTHER", is_default=False,
        )
        flagged = SimpleNamespace(
            id=2, product_id=10, tenant_id=7, retailer_id="R", is_default=True,
        )

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", "")
            if name == "Product":
                q.filter.return_value.all.return_value = [product]
            else:
                q.filter.return_value.all.return_value = [pointed, flagged]
            return q

        db.query.side_effect = _query
        report = join_graph_to_local_memberships(
            db, tenant_id=7, live_products={"R": {"meta_product_id": "m1"}},
        )
        assert report.desired == []
        assert report.ambiguous
        assert report.ambiguous[0]["diagnostic"] == DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING

    def test_multiple_is_default_variants_same_retailer_are_ambiguous(self):
        db = MagicMock()
        product = SimpleNamespace(
            id=10, tenant_id=7, meta_retailer_id="R", external_id=None,
            has_variants=False, default_variant_id=None,
        )
        v1 = SimpleNamespace(
            id=11, product_id=10, tenant_id=7, retailer_id="R", is_default=True,
        )
        v2 = SimpleNamespace(
            id=12, product_id=10, tenant_id=7, retailer_id="R", is_default=True,
        )

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", "")
            if name == "Product":
                q.filter.return_value.all.return_value = [product]
            else:
                q.filter.return_value.all.return_value = [v1, v2]
            return q

        db.query.side_effect = _query
        report = join_graph_to_local_memberships(
            db, tenant_id=7, live_products={"R": {"meta_product_id": "m1"}},
        )
        assert report.desired == []
        assert report.ambiguous
        assert report.ambiguous[0]["diagnostic"] == DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING


class TestProviderContradiction:
    def test_invalidation_requires_catalog_id(self):
        db = MagicMock()
        assert invalidate_meta_catalog_membership(
            db, tenant_id=7, catalog_id="", retailer_id="rid-a",
        ) == 0

    def test_duplicate_button_title_is_not_products_not_found(self):
        reason, _ = _classify_catalog_provider_failure(
            {
                "error": {
                    "code": 131009,
                    "message": "(#131009) Duplicate button title",
                    "error_data": {"details": "Duplicate button title"},
                }
            }
        )
        assert reason != "meta_products_not_found"


class TestSingleOwner:
    def test_published_stamp_does_not_make_browse_eligible(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        cap = evaluate_native_catalog_capability(db, 7, connection=_conn())
        assert cap.eligible is False

    def test_extra_metadata_is_not_authorization_source(self):
        src = (_BACKEND / "core" / "native_catalog_capability.py").read_text(
            encoding="utf-8"
        )
        assert "extra_metadata" not in src
        assert "meta_membership_catalog_id" not in src

    def test_count_and_thumbnail_read_membership_owner(self):
        db = MagicMock()
        with patch(
            "core.native_catalog_capability.count_memberships_for_catalog",
            return_value=2,
        ), patch(
            "core.native_catalog_capability.first_membership_retailer_id",
            return_value="rid-thumb",
        ):
            assert count_matchable_catalog_products(db, 7, _CAT_A) == 2
            assert pick_thumbnail_retailer_id(db, 7, _CAT_A) == "rid-thumb"

    def test_connection_switch_a_to_b_fails_closed_without_b_rows(self):
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(_CAT_B),
            attachment={
                "kind": "product_card",
                "id": 101,
                "title": "x",
                "external_id": "rid-a",
                "file_url": "https://cdn.example/a.jpg",
                "product_url": "https://shop.example/a",
                "in_stock": True,
                "confidence": "fts",
                "needs_variant_choice": False,
                "variants": [],
            },
            product_row=SimpleNamespace(
                id=101, tenant_id=7, external_id="rid-a",
                meta_retailer_id="rid-a", in_stock=True,
                has_variants=False, default_variant_id=None,
                meta_catalog_published_at=_NOW,
            ),
            membership=_fact(catalog_id=_CAT_A, retailer_id="rid-a"),
            positive_commerce_intent=True,
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY

    def test_desired_membership_carries_exact_graph_join_fields(self):
        desired = DesiredMembership(
            retailer_id="rid-a",
            product_id=23,
            variant_id=None,
            meta_item_id="111",
        )
        assert desired.retailer_id == "rid-a"
        assert desired.product_id == 23
        assert desired.meta_item_id == "111"
