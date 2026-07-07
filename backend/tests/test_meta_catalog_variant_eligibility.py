"""Tests for read-only Meta catalog variant eligibility report."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.meta_catalog_eligibility import (  # noqa: E402
    build_meta_catalog_eligibility_report,
    classify_variant_eligibility,
    product_has_real_variants,
)


@dataclass
class _Parent:
    id: int
    tenant_id: int
    title: str = "قميص قطني أزرق"
    meta_retailer_id: Optional[str] = "88001"
    external_id: Optional[str] = "88001"
    description: str = "وصف المنتج"
    extra_metadata: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.extra_metadata is None:
            self.extra_metadata = {
                "image_url": "https://cdn.example/parent.jpg",
                "product_url": "https://store.example/p/88001",
                "currency": "SAR",
            }


@dataclass
class _Variant:
    id: int
    tenant_id: int
    product_id: int
    retailer_id: Optional[str] = "88001-591001"
    salla_variant_id: Optional[str] = "591001"
    price: str = "59"
    currency: str = "SAR"
    stock_quantity: int = 1
    in_stock: bool = True
    is_default: bool = False
    option_summary: Optional[str] = "M"
    options: Optional[dict] = None
    image_url: Optional[str] = None
    extra_metadata: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.options is None:
            self.options = {"option_value_ids": ["90001"]}
        if self.extra_metadata is None:
            self.extra_metadata = {"sale_price": "59.0", "regular_price": "119.0"}


def _mock_db(parents: List[_Parent], variants: List[_Variant]):
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "ProductVariant":
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = variants
            q.filter.return_value.order_by.return_value.all.return_value = variants
            q.filter.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = variants
            q.filter.return_value.filter.return_value.order_by.return_value.all.return_value = variants
        elif name == "Product":
            q.filter.return_value.filter.return_value.all.return_value = parents
        return q

    db.query.side_effect = _query
    return db


def test_product_has_real_variants_multi_sku():
    variants = [
        _Variant(id=1, tenant_id=9, product_id=32, is_default=True, retailer_id="88001", salla_variant_id=None),
        _Variant(id=2, tenant_id=9, product_id=32, retailer_id="88001-591001"),
        _Variant(id=3, tenant_id=9, product_id=32, retailer_id="88001-591002", salla_variant_id="591002"),
    ]
    assert product_has_real_variants(variants) is True


def test_product_has_real_variants_simple_default_only():
    variants = [
        _Variant(id=10, tenant_id=9, product_id=50, is_default=True, retailer_id="88001", salla_variant_id=None),
    ]
    assert product_has_real_variants(variants) is False


def test_multi_variant_skips_default_parent_retailer_id():
    parent = _Parent(id=32, tenant_id=9, title="قميص قطني أزرق", meta_retailer_id="88001")
    default_variant = _Variant(
        id=100,
        tenant_id=9,
        product_id=32,
        is_default=True,
        retailer_id="88001",
        salla_variant_id=None,
    )
    real_variant = _Variant(
        id=101,
        tenant_id=9,
        product_id=32,
        retailer_id="88001-591001",
        salla_variant_id="591001",
    )

    skipped = classify_variant_eligibility(parent, default_variant, has_real_variants=True)
    eligible = classify_variant_eligibility(parent, real_variant, has_real_variants=True)

    assert skipped.status == "skipped"
    assert skipped.skip_reason == "skipped_default_variant"
    assert eligible.status == "eligible"
    assert eligible.fatal is False
    assert eligible.payload is not None
    assert eligible.payload["price"] == 5900
    assert eligible.payload["currency"] == "SAR"


def test_simple_product_default_variant_eligible():
    parent = _Parent(id=50, tenant_id=9, title="عطر ورد 100ml", meta_retailer_id="99001")
    default_variant = _Variant(
        id=200,
        tenant_id=9,
        product_id=50,
        is_default=True,
        retailer_id="99001",
        salla_variant_id=None,
        option_summary=None,
        options={},
    )

    item = classify_variant_eligibility(parent, default_variant, has_real_variants=False)
    assert item.status == "eligible"
    assert item.skip_reason is None


def test_missing_image_is_fatal():
    parent = _Parent(
        id=60,
        tenant_id=9,
        title="حذاء رياضي أبيض",
        meta_retailer_id="77001",
        extra_metadata={"product_url": "https://store.example/p/77001"},
    )
    variant = _Variant(
        id=300,
        tenant_id=9,
        product_id=60,
        retailer_id="77001-1001",
        image_url=None,
    )

    item = classify_variant_eligibility(parent, variant, has_real_variants=True)
    assert item.status == "fatal"
    assert item.fatal is True
    assert "missing_image_url" in item.warnings


def test_raw_option_label_is_warning_not_fatal():
    parent = _Parent(id=32, tenant_id=9, title="قميص قطني أزرق")
    variant = _Variant(
        id=400,
        tenant_id=9,
        product_id=32,
        option_summary="['90001']",
        options={"option_value_ids": ["90001"]},
    )

    item = classify_variant_eligibility(parent, variant, has_real_variants=True)
    assert item.fatal is False
    assert item.status == "eligible_with_warnings"
    assert "raw_option_label" in item.warnings


def test_out_of_stock_is_not_fatal():
    parent = _Parent(id=32, tenant_id=9)
    variant = _Variant(
        id=500,
        tenant_id=9,
        product_id=32,
        in_stock=False,
        stock_quantity=0,
    )

    item = classify_variant_eligibility(parent, variant, has_real_variants=True)
    assert item.fatal is False
    assert item.status == "eligible_with_warnings"
    assert "out_of_stock" in item.warnings


def test_build_report_counts_and_no_graph_calls():
    parent = _Parent(id=32, tenant_id=9, title="قميص قطني أزرق", meta_retailer_id="88001")
    variants = [
        _Variant(id=1, tenant_id=9, product_id=32, is_default=True, retailer_id="88001", salla_variant_id=None),
        _Variant(id=2, tenant_id=9, product_id=32, retailer_id="88001-591001"),
        _Variant(
            id=3,
            tenant_id=9,
            product_id=32,
            retailer_id="88001-591002",
            salla_variant_id="591002",
            option_summary="['90002']",
            options={"option_value_ids": ["90002"]},
        ),
        _Variant(
            id=4,
            tenant_id=9,
            product_id=32,
            retailer_id="88001-591003",
            salla_variant_id="591003",
            in_stock=False,
            stock_quantity=0,
        ),
    ]
    db = _mock_db([parent], variants)

    with patch("services.meta_catalog_eligibility.preview_meta_variant_payload") as preview_mock:
        preview_mock.side_effect = lambda parent, variant: __import__(
            "services.meta_catalog_export", fromlist=["preview_meta_variant_payload"]
        ).preview_meta_variant_payload(parent, variant)
        report = build_meta_catalog_eligibility_report(db, 9)

    assert report.dry_run is True
    data = report.to_dict()
    assert data["counts"]["skipped_default_variant"] == 1
    assert data["counts"]["eligible"] >= 2
    assert data["counts"]["fatal"] == 0
    assert data["counts"]["raw_option_label"] >= 1
    assert data["counts"]["out_of_stock"] >= 1


def test_eligibility_module_has_no_httpx_dependency():
    import inspect

    import services.meta_catalog_eligibility as mod

    source = inspect.getsource(mod)
    assert "httpx" not in source
    assert "meta_catalog_push" not in source
