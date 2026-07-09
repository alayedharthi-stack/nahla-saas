"""Tests for read-only Meta catalog readiness report."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.meta_catalog_eligibility import (  # noqa: E402
    MetaCatalogEligibilityItem,
    classify_variant_eligibility,
)
from services.meta_catalog_readiness import (  # noqa: E402
    build_meta_catalog_readiness_report,
    classify_readiness_status,
    eligibility_to_readiness_item,
    resolve_action_needed,
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
            self.options = {"المقاس": "M"}
        if self.extra_metadata is None:
            self.extra_metadata = {"sale_price": "59.0", "regular_price": "119.0"}


def _eligibility_from(parent: _Parent, variant: _Variant, *, has_real_variants: bool) -> MetaCatalogEligibilityItem:
    return classify_variant_eligibility(parent, variant, has_real_variants=has_real_variants)


def test_simple_product_ready_no_item_group_id():
    parent = _Parent(id=50, tenant_id=9, title="عطر ورد 100ml", meta_retailer_id="99001", external_id="99001")
    variant = _Variant(
        id=200,
        tenant_id=9,
        product_id=50,
        is_default=True,
        retailer_id="99001",
        salla_variant_id=None,
        option_summary=None,
        options={},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=False)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=False,
    )
    assert item.status == "ready"
    assert item.item_group_id is None
    assert item.generated_name == "عطر ورد 100ml"
    assert item.payload_preview is not None
    assert "item_group_id" not in item.payload_preview


def test_multi_variant_ready_human_name():
    parent = _Parent(id=32, tenant_id=9, title="حذاء رياضي أبيض")
    variant = _Variant(
        id=101,
        tenant_id=9,
        product_id=32,
        retailer_id="88001-591001",
        salla_variant_id="591001",
        option_summary="42 - L",
        options={"المقاس": "42 - L"},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "ready"
    assert item.generated_name == "حذاء رياضي أبيض - 42 - L"
    assert item.item_group_id == "88001"


def test_legacy_default_skipped_not_blocked():
    parent = _Parent(id=32, tenant_id=9, title="قميص قطني أزرق", meta_retailer_id="88001")
    variant = _Variant(
        id=100,
        tenant_id=9,
        product_id=32,
        is_default=True,
        retailer_id="88001",
        salla_variant_id=None,
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "skipped"
    assert item.status != "blocked"
    assert "legacy_default_variant" in item.reasons
    assert item.payload_preview is None


def test_fatal_missing_image_and_url():
    parent = _Parent(
        id=60,
        tenant_id=9,
        title="حذاء رياضي أبيض",
        meta_retailer_id="77001",
        extra_metadata={"product_url": None, "url": None},
    )
    parent.extra_metadata.pop("image_url", None)
    variant = _Variant(
        id=300,
        tenant_id=9,
        product_id=60,
        retailer_id="77001-1001",
        image_url=None,
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "blocked"
    assert "missing_image_url" in item.reasons
    assert "missing_url" in item.reasons
    assert item.payload_preview is None


def test_out_of_stock_classified_warn_by_default():
    parent = _Parent(id=32, tenant_id=9)
    variant = _Variant(
        id=500,
        tenant_id=9,
        product_id=32,
        in_stock=False,
        stock_quantity=0,
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    status, reasons = classify_readiness_status(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert status == "warn"
    assert "out_of_stock" in reasons


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
            q.filter.return_value.filter.return_value.filter.return_value.all.return_value = variants
            q.filter.return_value.filter.return_value.all.return_value = variants
        elif name == "Product":
            q.filter.return_value.filter.return_value.all.return_value = parents
        elif name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = None
        return q

    db.query.side_effect = _query
    return db


def test_exclude_out_of_stock_filters_or_skips():
    parent = _Parent(id=32, tenant_id=9, title="قميص قطني أزرق", meta_retailer_id="88001")
    variants = [
        _Variant(id=2, tenant_id=9, product_id=32, retailer_id="88001-591001"),
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

    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_eligibility_report",
    ) as elig_mock:
        elig_mock.return_value.items = [
            classify_variant_eligibility(parent, variants[0], has_real_variants=True),
            classify_variant_eligibility(parent, variants[1], has_real_variants=True),
        ]
        full = build_meta_catalog_readiness_report(db, 9, exclude_out_of_stock=False)
        filtered = build_meta_catalog_readiness_report(db, 9, exclude_out_of_stock=True)

    assert len(full.items) == 2
    assert len(filtered.items) == 1
    assert filtered.items[0].variant_id == 2


def test_raw_option_summary_blocked_or_warned():
    parent = _Parent(id=32, tenant_id=9, title="قميص قطني أزرق")
    variant = _Variant(
        id=400,
        tenant_id=9,
        product_id=32,
        option_summary="['90001']",
        options={"option_value_ids": ["90001"]},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "warn"
    assert item.status != "ready"
    assert "raw_option_summary" in item.reasons


def test_missing_option_summary_warn_multi_variant():
    parent = _Parent(id=32, tenant_id=9, title="قميص قطني أزرق")
    variant = _Variant(
        id=401,
        tenant_id=9,
        product_id=32,
        option_summary=None,
        options={"option_value_ids": ["90001"]},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "warn"
    assert "missing_option_summary" in item.reasons
    assert item.generated_name == "قميص قطني أزرق"


def test_meta_live_create_update_noop_skip():
    local_payload = {
        "retailer_id": "88001-591001",
        "name": "حذاء رياضي أبيض - 42 - L",
        "availability": "in stock",
    }
    assert resolve_action_needed("blocked", local_payload, {"name": "x"}) == "skip"
    assert resolve_action_needed("skipped", local_payload, {"name": "x"}) == "skip"
    assert resolve_action_needed("ready", local_payload, None) == "create"
    assert resolve_action_needed(
        "ready",
        local_payload,
        {"name": "حذاء رياضي أبيض - 42 - L", "availability": "in stock"},
    ) == "noop"
    assert resolve_action_needed(
        "warn",
        local_payload,
        {"name": "حذاء رياضي أبيض - 42 - L", "availability": "out of stock"},
    ) == "update"

    parent = _Parent(id=32, tenant_id=9, title="حذاء رياضي أبيض")
    variant = _Variant(id=101, tenant_id=9, product_id=32, option_summary="42 - L")
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    live = {
        "meta_product_id": "123",
        "name": "حذاء رياضي أبيض - 42 - L",
        "availability": "in stock",
    }
    item = eligibility_to_readiness_item(
        elig,
        parent=parent,
        variant=variant,
        has_real_variants=True,
        live_row=live,
        include_meta=True,
    )
    assert item.in_meta_live is True
    assert item.action_needed == "noop"
    assert item.meta_product_id == "123"

    live_unified_name = {
        "meta_product_id": "123",
        "name": "حذاء رياضي أبيض",
        "availability": "in stock",
    }
    item_needs_name_update = eligibility_to_readiness_item(
        elig,
        parent=parent,
        variant=variant,
        has_real_variants=True,
        live_row=live_unified_name,
        include_meta=True,
    )
    assert item_needs_name_update.action_needed == "update"


def test_tenant_report_counts():
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
    ]
    db = _mock_db([parent], variants)
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_eligibility_report",
    ) as elig_mock:
        elig_mock.return_value.items = [
            classify_variant_eligibility(parent, v, has_real_variants=True) for v in variants
        ]
        report = build_meta_catalog_readiness_report(db, 9)

    c = report.counts
    assert c["products_total"] == 1
    assert c["variant_products"] == 1
    assert c["candidate_items_total"] == 3
    assert c["skipped_items"] == 1
    assert c["ready_items"] >= 1
    assert c["warning_items"] >= 1
    assert c["skipped_legacy_default"] == 1
    assert c["raw_option_label_items"] >= 1


def test_generic_commerce_neutral_product():
    parent = _Parent(
        id=70,
        tenant_id=9,
        title="متجر تجريبي عام",
        meta_retailer_id="55001",
        external_id="55001",
    )
    variant = _Variant(
        id=700,
        tenant_id=9,
        product_id=70,
        retailer_id="55001-1001",
        salla_variant_id="1001",
        option_summary="500g",
        options={"الوزن": "500g"},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "ready"
    assert item.generated_name == "متجر تجريبي عام - 500g"
    assert item.currency == "SAR"


def test_clean_simple_size_variant_stays_ready():
    parent = _Parent(id=37, tenant_id=9, title="فستان", external_id="299542287", meta_retailer_id="299542287")
    variant = _Variant(
        id=318,
        tenant_id=9,
        product_id=37,
        retailer_id="299542287-1568058297",
        salla_variant_id="1568058297",
        option_summary="40 - M",
        options={"المقاس": "40 - M"},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "ready"
    assert not any(
        code in item.reasons
        for code in (
            "composite_option_summary",
            "orphan_option_value_ids",
            "meta_name_no_size",
            "color_size_slash_name",
        )
    )


def test_composite_orphan_variant_warns_not_ready():
    parent = _Parent(id=37, tenant_id=9, title="فستان", external_id="299542287", meta_retailer_id="299542287")
    variant = _Variant(
        id=316,
        tenant_id=9,
        product_id=37,
        retailer_id="299542287-834891199",
        salla_variant_id="834891199",
        option_summary="44 - XL / 42 - L",
        options={"option_value_ids": ["834891199", "618796933"]},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "warn"
    assert item.status != "ready"
    assert "composite_option_summary" in item.reasons
    assert "orphan_option_value_ids" in item.reasons


def test_color_size_slash_variant_warns_with_resolved_size():
    parent = _Parent(id=23, tenant_id=9, title="فستان", external_id="23001", meta_retailer_id="23001")
    variant = _Variant(
        id=230,
        tenant_id=9,
        product_id=23,
        retailer_id="23001-5001",
        salla_variant_id="5001",
        option_summary="فوشي / 38 - S",
        options={"اللون": "فوشي", "المقاس": "38 - S"},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "warn"
    assert "color_size_slash_name" in item.reasons
    assert "composite_option_summary" in item.reasons
    assert (elig.payload or {}).get("size") == "38 - S"


def test_color_only_apparel_warns_meta_name_no_size():
    parent = _Parent(id=38, tenant_id=9, title="فستان", external_id="38001", meta_retailer_id="38001")
    variant = _Variant(
        id=380,
        tenant_id=9,
        product_id=38,
        retailer_id="38001-2001",
        salla_variant_id="2001",
        option_summary="اسود",
        options={"اللون": "اسود"},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    item = eligibility_to_readiness_item(
        elig, parent=parent, variant=variant, has_real_variants=True,
    )
    assert item.status == "warn"
    assert "meta_name_no_size" in item.reasons
    assert "composite_option_summary" not in item.reasons


def test_live_composite_orphan_still_noop_when_matched():
    parent = _Parent(id=37, tenant_id=9, title="فستان", external_id="299542287", meta_retailer_id="299542287")
    variant = _Variant(
        id=316,
        tenant_id=9,
        product_id=37,
        retailer_id="299542287-834891199",
        salla_variant_id="834891199",
        option_summary="44 - XL / 42 - L",
        options={"option_value_ids": ["834891199", "618796933"]},
    )
    elig = _eligibility_from(parent, variant, has_real_variants=True)
    live = {
        "meta_product_id": "27748168301485180",
        "name": str((elig.payload or {}).get("name") or ""),
        "availability": "in stock",
    }
    item = eligibility_to_readiness_item(
        elig,
        parent=parent,
        variant=variant,
        has_real_variants=True,
        live_row=live,
        include_meta=True,
    )
    assert item.status == "warn"
    assert item.action_needed == "noop"
    assert "orphan_option_value_ids" in item.reasons


def test_readiness_module_has_no_push_dependency():
    import inspect

    import services.meta_catalog_readiness as mod

    source = inspect.getsource(mod)
    assert "meta_catalog_push" not in source
    assert "push_one" not in source
