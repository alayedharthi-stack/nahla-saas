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

from services.meta_catalog_export import build_meta_variant_display_name  # noqa: E402
from services.store_sync import _variant_option_summary  # noqa: E402
from store_adapters.salla_adapter import _extract_variant_options  # noqa: E402


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
