"""
backend/tests/test_meta_catalog_reconcile.py
────────────────────────────────────────────
Meta catalog publish stamp reconcile + resync guard tests.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import httpx

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.native_catalog_capability import (  # noqa: E402
    evaluate_native_catalog_capability,
)
from routers.catalog import _run_catalog_resync  # noqa: E402
from services.meta_catalog_reconcile import (  # noqa: E402
    build_meta_catalog_reconcile_plan,
    fetch_meta_catalog_retailer_ids,
    reconcile_meta_catalog_publish_stamps,
)


@dataclass
class _Conn:
    tenant_id: int = 7
    meta_catalog_id: str = "CAT-TEST"
    catalog_enabled: bool = True
    phone_number_id: str = "PHONE1"
    status: str = "connected"
    sending_enabled: bool = True


@dataclass
class _Variant:
    id: int
    tenant_id: int
    product_id: int
    retailer_id: str = ""


@dataclass
class _Product:
    id: int
    tenant_id: int
    title: str = "Item"
    meta_retailer_id: Optional[str] = "rid-a"
    external_id: Optional[str] = None
    meta_catalog_published_at: Any = None
    catalog_status: str = "active"
    in_stock: bool = True


def _mock_db(products: List[_Product], variants: Optional[List[_Variant]] = None):
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = products
    db.query.return_value.filter.return_value.all.side_effect = [
        variants or [],
        products,
    ]
    return db


def test_resync_does_not_set_meta_catalog_published_at():
    product = _Product(id=1, tenant_id=9, meta_retailer_id=None)
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [product]
    with patch("routers.catalog.assign_canonical_retailer_id", return_value=True):
        report = _run_catalog_resync(db, 9)
    assert report["published_stamped"] == 0
    assert product.meta_catalog_published_at is None


def test_reconcile_dry_run_does_not_write():
    products = [
        _Product(id=1, tenant_id=5, meta_retailer_id="in-meta", meta_catalog_published_at=None),
        _Product(
            id=2,
            tenant_id=5,
            meta_retailer_id="stale",
            meta_catalog_published_at=datetime.now(timezone.utc),
        ),
    ]
    db = MagicMock()
    conn = _Conn(tenant_id=5)

    def _query(model):
        q = MagicMock()
        if model.__name__ == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        elif model.__name__ == "ProductVariant":
            q.filter.return_value.all.return_value = []
        elif model.__name__ == "Product":
            q.filter.return_value.order_by.return_value.all.return_value = products
            q.filter.return_value.filter.return_value.all.return_value = products
        return q

    db.query.side_effect = _query

    with patch(
        "services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
        return_value=(
            {"in-meta": {"meta_product_id": "mg-1"}},
            {"complete": True, "error": None, "pages": 1, "items": 1},
        ),
    ):
        report = reconcile_meta_catalog_publish_stamps(db, 5, apply=False)

    assert report.dry_run is True
    assert report.applied_stamp_count == 0
    assert report.applied_clear_count == 0
    assert len(report.to_stamp) == 1
    assert len(report.to_clear) == 1
    db.flush.assert_not_called()


def test_reconcile_apply_stamps_only_meta_live_ids():
    now = datetime.now(timezone.utc)
    p_ok = _Product(id=10, tenant_id=8, meta_retailer_id="live-1")
    p_bad = _Product(
        id=11,
        tenant_id=8,
        meta_retailer_id="ghost",
        meta_catalog_published_at=now,
    )
    db = MagicMock()
    conn = _Conn(tenant_id=8)

    def _query(model):
        q = MagicMock()
        if model.__name__ == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        elif model.__name__ == "ProductVariant":
            q.filter.return_value.all.return_value = []
        elif model.__name__ == "Product":
            q.filter.return_value.order_by.return_value.all.return_value = [p_ok, p_bad]
            q.filter.return_value.filter.return_value.all.return_value = [p_ok, p_bad]
        return q

    db.query.side_effect = _query

    with patch(
        "services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
        return_value=(
            {"live-1": {"meta_product_id": "mg-live"}},
            {"complete": True, "error": None, "pages": 1, "items": 1},
        ),
    ), patch(
        "core.meta_catalog_membership.apply_membership_snapshot",
        return_value={"upserted": 1, "removed": 1},
    ):
        report = reconcile_meta_catalog_publish_stamps(db, 8, apply=True)

    assert report.snapshot_applied is True
    assert report.memberships_upserted == 1
    assert report.memberships_removed == 1
    assert report.error is None


def test_reconcile_apply_clears_stale_stamps():
    now = datetime.now(timezone.utc)
    stale = _Product(
        id=20,
        tenant_id=4,
        meta_retailer_id="nrmqkc6f09",
        meta_catalog_published_at=now,
    )
    db = MagicMock()
    conn = _Conn(tenant_id=4)

    def _query(model):
        q = MagicMock()
        if model.__name__ == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        elif model.__name__ == "ProductVariant":
            q.filter.return_value.all.return_value = []
        elif model.__name__ == "Product":
            q.filter.return_value.order_by.return_value.all.return_value = [stale]
            q.filter.return_value.filter.return_value.all.return_value = [stale]
        return q

    db.query.side_effect = _query

    with patch(
        "services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
        return_value=(
            {},
            {"complete": True, "error": None, "pages": 1, "items": 0},
        ),
    ), patch(
        "core.meta_catalog_membership.apply_membership_snapshot",
        return_value={"upserted": 0, "removed": 1},
    ):
        report = reconcile_meta_catalog_publish_stamps(db, 4, apply=True)

    assert report.snapshot_applied is True
    assert report.memberships_removed == 1
    assert report.error is None


def test_fetch_meta_catalog_retailer_ids_paginates():
    conn = _Conn()
    page1 = httpx.Response(
        200,
        json={
            "data": [{"retailer_id": "a"}, {"retailer_id": "b"}],
            "paging": {"next": "https://graph.facebook.com/v999/CAT/products?after=1"},
        },
        request=httpx.Request("GET", "https://graph.facebook.com/v999/CAT/products"),
    )
    page2 = httpx.Response(
        200,
        json={"data": [{"retailer_id": "c"}]},
        request=httpx.Request(
            "GET",
            "https://graph.facebook.com/v999/CAT/products?after=1",
        ),
    )
    client = MagicMock()
    client.get.side_effect = [page1, page2]

    with patch(
        "services.meta_catalog_reconcile._select_graph_token",
        return_value={"token": "tok"},
    ):
        ids, info = fetch_meta_catalog_retailer_ids(conn, "CAT-TEST", client=client)

    assert ids == {"a", "b", "c"}
    assert info["pages"] == 2
    assert client.get.call_count == 2


def test_ghost_retailer_id_not_eligible_for_native_catalog():
    db = MagicMock()
    conn = _Conn()
    products = [
        _Product(
            id=1,
            tenant_id=7,
            meta_retailer_id="nrmqkc6f09",
            meta_catalog_published_at=None,
        ),
        _Product(
            id=2,
            tenant_id=7,
            meta_retailer_id="verified-live",
            meta_catalog_published_at=datetime.now(timezone.utc),
        ),
    ]
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.side_effect = [
        [products[1]],
        products,
    ]
    with patch(
        "core.native_catalog_capability.count_memberships_for_catalog",
        return_value=1,
    ), patch(
        "core.native_catalog_capability.first_membership_retailer_id",
        return_value="verified-live",
    ):
        cap = evaluate_native_catalog_capability(db, 7, connection=conn)
    assert cap.eligible is True
    assert cap.thumbnail_retailer_id == "verified-live"


def test_native_catalog_false_when_no_verified_ids():
    db = MagicMock()
    conn = _Conn()
    products = [
        _Product(id=1, tenant_id=7, meta_retailer_id="nrmqkc6f09"),
        _Product(id=2, tenant_id=7, meta_retailer_id="krbe36pn31"),
    ]
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = products
    cap = evaluate_native_catalog_capability(db, 7, connection=conn)
    assert cap.eligible is False


def test_native_catalog_true_with_one_verified_id():
    db = MagicMock()
    conn = _Conn()
    verified = _Product(
        id=3,
        tenant_id=7,
        meta_retailer_id="live-ok",
        meta_catalog_published_at=datetime.now(timezone.utc),
    )
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.side_effect = [
        [verified],
        [verified],
    ]
    with patch(
        "core.native_catalog_capability.count_memberships_for_catalog",
        return_value=1,
    ), patch(
        "core.native_catalog_capability.first_membership_retailer_id",
        return_value="live-ok",
    ):
        cap = evaluate_native_catalog_capability(db, 7, connection=conn)
    assert cap.eligible is True
    assert cap.thumbnail_retailer_id == "live-ok"


def test_local_not_in_meta_omits_parent_when_variant_is_live():
    products = [
        _Product(id=32, tenant_id=9, meta_retailer_id="88001", title="قميص قطني أزرق"),
    ]
    variants = [
        _Variant(id=207, tenant_id=9, product_id=32, retailer_id="88001-591001"),
    ]
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        if model.__name__ == "ProductVariant":
            q.filter.return_value.all.return_value = variants
        elif model.__name__ == "Product":
            q.filter.return_value.order_by.return_value.all.return_value = products
        return q

    db.query.side_effect = _query

    _, _, local_missing, _, _ = build_meta_catalog_reconcile_plan(
        db, 9, {"88001-591001"},
    )
    assert local_missing == []


def test_local_not_in_meta_includes_parent_when_no_ids_live():
    products = [
        _Product(id=32, tenant_id=9, meta_retailer_id="88001", title="قميص قطني أزرق"),
    ]
    variants = [
        _Variant(id=207, tenant_id=9, product_id=32, retailer_id="88001-591001"),
    ]
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        if model.__name__ == "ProductVariant":
            q.filter.return_value.all.return_value = variants
        elif model.__name__ == "Product":
            q.filter.return_value.order_by.return_value.all.return_value = products
        return q

    db.query.side_effect = _query

    _, _, local_missing, _, _ = build_meta_catalog_reconcile_plan(db, 9, set())
    assert len(local_missing) == 1
    assert local_missing[0].meta_retailer_id == "88001"


def test_reconcile_plan_is_platform_generic():
    products = [
        _Product(id=100, tenant_id=999, meta_retailer_id="x1"),
        _Product(id=101, tenant_id=999, meta_retailer_id="x2"),
    ]
    db = _mock_db(products)
    to_stamp, to_clear, local_missing, meta_unstamped, counts = build_meta_catalog_reconcile_plan(
        db, 999, {"x1"},
    )
    assert counts["nahla_meta_retailer_id_count"] == 2
    assert len(to_stamp) == 1
    assert to_stamp[0].meta_retailer_id == "x1"
    assert len(local_missing) == 1
    assert local_missing[0].meta_retailer_id == "x2"
    assert meta_unstamped == [{"retailer_id": "x1"}]


def test_incomplete_graph_fetch_does_not_apply_snapshot():
    db = MagicMock()
    conn = _Conn(tenant_id=6)

    def _query(model):
        q = MagicMock()
        if model.__name__ == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        else:
            q.filter.return_value.all.return_value = []
            q.filter.return_value.order_by.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    with patch(
        "services.meta_catalog_reconcile.fetch_meta_catalog_live_products",
        return_value=({"x": {"meta_product_id": "1"}}, {"complete": False, "error": "timeout", "pages": 1}),
    ), patch("core.meta_catalog_membership.apply_membership_snapshot") as apply_mock:
        report = reconcile_meta_catalog_publish_stamps(db, 6, apply=True)
    assert report.snapshot_applied is False
    assert report.error
    apply_mock.assert_not_called()
