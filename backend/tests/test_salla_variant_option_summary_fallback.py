"""Regression tests for human Salla variant option_summary fallbacks."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.meta_catalog_export import (  # noqa: E402
    build_meta_variant_display_name,
    build_meta_variant_payload,
)
from services.store_sync import _variant_option_summary  # noqa: E402
from store_adapters.salla_adapter import (  # noqa: E402
    _canonicalize_option_entry,
    _extract_variant_options,
)


def test_orphan_related_option_values_falls_back_to_raw_variant_name():
    raw_variant = {
        "id": 591539870,
        "name": "40 - M",
        "related_option_values": [2019873167],
    }
    product_options = [
        {
            "id": 1099872859,
            "name": "المقاس",
            "values": [
                {"id": 1980448394, "name": "44 - XL"},
                {"id": 1137728907, "name": "42 - L"},
            ],
        }
    ]

    options, summary = _extract_variant_options(raw_variant, product_options)

    assert summary == "40 - M"
    assert options == {"المقاس": "40 - M"}


def test_variant_option_summary_ignores_option_value_ids_only():
    assert _variant_option_summary(
        {"options": {"option_value_ids": ["2019873167"]}}
    ) == ""


def test_variant_option_summary_keeps_mapped_human_options():
    assert _variant_option_summary(
        {
            "options": {"المقاس": "38 - S"},
        }
    ) == "38 - S"


def test_meta_display_name_uses_human_option_summary_for_blouse_variant():
    parent = SimpleNamespace(title="بلوزة")
    variant = SimpleNamespace(option_summary="40 - M", options=None)
    assert build_meta_variant_display_name(parent, variant) == "بلوزة - 40 - M"


def test_salla_canonicalizes_color_key_with_size_value_to_size():
    assert _canonicalize_option_entry("اللون", "40 - M") == ("المقاس", "40 - M")


def test_salla_keeps_real_color_as_color():
    options, summary = _extract_variant_options(
        {"options": {"اللون": "أسود"}},
        [],
    )
    assert options == {"اللون": "أسود"}
    assert summary == "أسود"


def test_salla_canonicalizes_pass_through_dict():
    options, summary = _extract_variant_options(
        {"options": {"اللون": "42 - L"}},
        [],
    )
    assert options == {"المقاس": "42 - L"}
    assert summary == "42 - L"


def test_salla_value_index_canonicalizes_group_name():
    raw_variant = {
        "id": 1,
        "related_option_values": [90001],
    }
    product_options = [
        {
            "id": 10,
            "name": "اللون",
            "values": [{"id": 90001, "name": "36 - XS"}],
        }
    ]
    options, summary = _extract_variant_options(raw_variant, product_options)
    assert options == {"المقاس": "36 - XS"}
    assert summary == "36 - XS"


def test_salla_orphan_fallback_canonicalizes_variant_name():
    raw_variant = {
        "id": 591539870,
        "name": "40 - M",
        "related_option_values": [2019873167],
    }
    product_options = [
        {
            "id": 1099872859,
            "name": "اللون",
            "values": [
                {"id": 1980448394, "name": "44 - XL"},
            ],
        }
    ]
    options, summary = _extract_variant_options(raw_variant, product_options)
    assert options == {"المقاس": "40 - M"}
    assert summary == "40 - M"


def test_salla_option_value_ids_only_stays_safe_when_unresolvable():
    options, summary = _extract_variant_options(
        {
            "id": 316,
            "related_option_values": ["1064266980", "1837256091"],
        },
        [
            {"id": 1, "name": "اللون", "values": [{"id": 1, "name": "أسود"}]},
            {"id": 2, "name": "المقاس", "values": [{"id": 2, "name": "44 - XL"}]},
        ],
    )
    assert options == {
        "option_value_ids": ["1064266980", "1837256091"],
    }
    assert summary is None


def test_salla_does_not_false_positive_non_apparel():
    options, _ = _extract_variant_options({"options": {"اللون": "عود فاخر"}}, [])
    assert options == {"اللون": "عود فاخر"}

    options2, _ = _extract_variant_options({"options": {"النوع": "كلاسيك"}}, [])
    assert options2 == {"النوع": "كلاسيك"}


def test_meta_payload_gets_size_after_canonicalized_options():
    options, _ = _extract_variant_options({"options": {"اللون": "40 - M"}}, [])
    parent = SimpleNamespace(
        title="فستان",
        external_id="299542287",
        description="وصف",
        extra_metadata={
            "image_url": "https://cdn.example/p.jpg",
            "product_url": "https://store.example/p",
            "currency": "SAR",
        },
    )
    variant = SimpleNamespace(
        id=318,
        product_id=37,
        salla_variant_id="1568058297",
        retailer_id="299542287-1568058297",
        price="114",
        currency="SAR",
        stock_quantity=1,
        in_stock=True,
        option_summary="40 - M",
        options=options,
        image_url=None,
        extra_metadata={},
    )
    payload = build_meta_variant_payload(parent, variant)
    assert payload["size"] == "40 - M"
    assert "color" not in payload
    assert payload["name"] == "فستان - 40 - M"
