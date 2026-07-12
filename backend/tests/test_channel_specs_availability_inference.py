"""Regression: infer Meta availability from in_stock when not explicitly set."""

from __future__ import annotations

from types import SimpleNamespace

from services.channel_specs import CHANNEL_META_CATALOG, CHANNEL_WHATSAPP, extract_field
from services.product_readiness import compute_for_channel


def _native_manual_product_like_179(**overrides):
    base = dict(
        title="اختبار رابط منتج نحلة",
        description=None,
        price="1.0",
        in_stock=True,
        meta_retailer_id="nahla_p_179",
        extra_metadata={
            "product_url": "https://api.nahlah.ai/public/catalog/items/nahla_p_179",
            "image_url": "https://cdn.example.com/item.webp",
            "currency": "SAR",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_extract_availability_in_stock_true():
    product = SimpleNamespace(in_stock=True, extra_metadata={})
    assert extract_field(product, "availability") == "in stock"


def test_extract_availability_in_stock_false():
    product = SimpleNamespace(in_stock=False, extra_metadata={})
    assert extract_field(product, "availability") == "out of stock"


def test_extract_availability_explicit_precedence():
    product = SimpleNamespace(
        in_stock=True,
        extra_metadata={"availability": "preorder"},
    )
    assert extract_field(product, "availability") == "preorder"


def test_meta_readiness_native_product_only_description_blocks():
    product = _native_manual_product_like_179()
    result = compute_for_channel(product, CHANNEL_META_CATALOG)
    blocking = [
        f.field
        for f in result.fields
        if f.required and f.state in ("missing", "error")
    ]
    assert "description" in blocking
    assert "availability" not in blocking
    assert "product_url" not in blocking


def test_whatsapp_readiness_unchanged_for_native_product():
    product = _native_manual_product_like_179()
    result = compute_for_channel(product, CHANNEL_WHATSAPP)
    assert result.ready is True
    assert result.blocking_count == 0
