"""Unit tests for Meta catalog variant payload preview builders."""

from __future__ import annotations

from types import SimpleNamespace

from services.meta_catalog_export import (
    build_meta_variant_display_name,
    build_meta_variant_payload,
    format_meta_price,
    meta_price_amount,
    meta_price_minor_units,
    preview_meta_variant_payload,
    resolve_variant_image_url,
)


def _parent(**kwargs):
    defaults = {
        "id": 32,
        "title": "قميص قطني أزرق",
        "external_id": "88001",
        "description": "وصف المنتج",
        "extra_metadata": {
            "image_url": "https://cdn.example/parent.jpg",
            "product_url": "https://store.example/p/88001",
            "currency": "SAR",
        },
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _variant(**kwargs):
    defaults = {
        "id": 207,
        "product_id": 32,
        "salla_variant_id": "591539870",
        "retailer_id": "88001-591539870",
        "price": "59",
        "currency": "SAR",
        "stock_quantity": 1,
        "in_stock": True,
        "option_summary": "M",
        "options": {"option_value_ids": ["2019873167"]},
        "image_url": None,
        "extra_metadata": {"sale_price": "59.0", "regular_price": "119.0"},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_format_meta_price_whole_number():
    assert format_meta_price("59", "SAR") == "59.00 SAR"


def test_variant_payload_uses_variant_retailer_id():
    payload = build_meta_variant_payload(_parent(), _variant())
    assert payload["retailer_id"] == "88001-591539870"
    assert payload["retailer_id"] != "88001"


def test_meta_price_amount_numeric_for_display():
    assert meta_price_amount("59") == 59.0
    assert meta_price_amount("59.5") == 59.5


def test_meta_price_minor_units_for_graph():
    assert meta_price_minor_units("59") == 5900
    assert meta_price_minor_units("59.0") == 5900
    assert meta_price_minor_units("59.5") == 5950
    assert meta_price_minor_units("59.99") == 5999


def test_variant_payload_price_and_availability_from_variant():
    payload = build_meta_variant_payload(_parent(), _variant())
    assert payload["price"] == 5900
    assert isinstance(payload["price"], int)
    assert payload["currency"] == "SAR"
    assert payload["availability"] == "in stock"


def test_variant_image_falls_back_to_parent():
    url, source = resolve_variant_image_url(_parent(), _variant())
    assert url == "https://cdn.example/parent.jpg"
    assert source == "parent"


def test_variant_image_prefers_variant_when_present():
    variant = _variant(image_url="https://cdn.example/variant.jpg")
    url, source = resolve_variant_image_url(_parent(), variant)
    assert url == "https://cdn.example/variant.jpg"
    assert source == "variant"


def test_preview_warns_on_missing_image_when_parent_has_none():
    parent = _parent(extra_metadata={"product_url": "https://store.example/p"})
    report = preview_meta_variant_payload(parent, _variant())
    assert "missing_image_url" in report["warnings"]
    assert report["fatal"] is True


def test_preview_ok_when_parent_image_and_url_present():
    report = preview_meta_variant_payload(_parent(), _variant())
    assert report["fatal"] is False
    assert report["debug"]["sale_price"] == "59.0"
    assert report["debug"]["regular_price"] == "119.0"
    assert report["payload"]["name"] == "قميص قطني أزرق - M"


def test_preview_fatal_when_retailer_id_missing():
    report = preview_meta_variant_payload(_parent(), _variant(retailer_id=None))
    assert "missing_retailer_id" in report["warnings"]
    assert report["fatal"] is True


def test_display_name_uses_human_option_summary():
    parent = _parent(title="قميص قطني أزرق")
    variant = _variant(option_summary="مقاس 36", options=None)
    assert build_meta_variant_display_name(parent, variant) == "قميص قطني أزرق - مقاس 36"


def test_display_name_ignores_raw_option_summary_list_repr():
    parent = _parent(title="قميص قطني أزرق")
    variant = _variant(option_summary="['2019873167']", options=None)
    assert build_meta_variant_display_name(parent, variant) == "قميص قطني أزرق"


def test_display_name_ignores_raw_option_value_ids_only():
    parent = _parent(title="قميص قطني أزرق")
    variant = _variant(option_summary=None, options={"option_value_ids": ["2019873167"]})
    assert build_meta_variant_display_name(parent, variant) == "قميص قطني أزرق"
