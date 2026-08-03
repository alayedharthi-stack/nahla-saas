"""P0-C — Honey type-first browse ladder."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.honey_browse_strategy import (  # noqa: E402
    apply_honey_browse_strategy,
    collapse_products_to_honey_types,
    should_collapse_to_honey_types,
)


def _product(pid: int, title: str, *, category: str = "عسل", quantity: int = 5) -> dict:
    return {"id": pid, "title": title, "category": category, "quantity": quantity, "in_stock": quantity > 0}


_CATALOG = [
    _product(1, "عسل طلح نجد 250 جرام"),
    _product(2, "عسل طلح نجد 5 كيلو"),
    _product(3, "عسل سمر الحجاز 500 جرام"),
    _product(4, "عسل سدر 1 كيلو"),
    _product(5, "كريم سم النحل"),
]


class TestHoneyTypeCollapse:
    def test_generic_options_collapse_to_one_per_type(self) -> None:
        honey_only = [_p for _p in _CATALOG if _p["id"] != 5]
        collapsed = collapse_products_to_honey_types(honey_only)
        titles = {p["title"] for p in collapsed}
        assert len(collapsed) == 3
        assert any("طلح" in t for t in titles)
        assert any("سمر" in t for t in titles)
        assert any("سدر" in t for t in titles)
        assert sum("طلح" in t for t in titles) == 1

    def test_specific_type_skips_collapse(self) -> None:
        assert should_collapse_to_honey_types(
            "ابي طلح",
            query="طلح",
            active_category="عسل",
            source="category_browse",
        ) is False

    def test_session_locked_generic_options_browse(self) -> None:
        assert should_collapse_to_honey_types(
            "وش الخيارات؟",
            active_category="عسل",
            source="top_products",
        ) is True

    def test_apply_strategy_filters_then_collapses(self) -> None:
        result = apply_honey_browse_strategy(
            _CATALOG,
            message="وش الخيارات؟",
            active_category="عسل",
            source="top_products",
        )
        titles = {p["title"] for p in result}
        assert "كريم سم النحل" not in titles
        assert len(result) == 3

    def test_message_with_honey_word_but_shoe_catalog_skips_honey_collapse(self) -> None:
        assert should_collapse_to_honey_types(
            "ابي عسل",
            active_category="عسل",
            source="top_products",
            products=[
                _product(1, "حذاء رياضي أبيض", category="أحذية"),
                _product(2, "حذاء كاجوال بني", category="أحذية"),
            ],
        ) is False

    def test_generic_shoe_browse_collapses_representatives(self) -> None:
        shoe_catalog = [
            _product(1, "حذاء رياضي أبيض مقاس 40", category="أحذية"),
            _product(2, "حذاء رياضي أبيض مقاس 42", category="أحذية"),
            _product(3, "حذاء كاجوال بني مقاس 41", category="أحذية"),
            _product(4, "صندل صيفي مقاس 40", category="أحذية"),
        ]
        result = apply_honey_browse_strategy(
            shoe_catalog,
            message="وش الخيارات؟",
            active_category="أحذية",
            source="top_products",
        )
        assert len(result) <= 4
        assert all("حذاء" in p["title"] or "صندل" in p["title"] for p in result)

    def test_unavailable_type_excluded_from_type_overview(self) -> None:
        catalog = [
            _product(1, "عسل طلح 250 جرام", quantity=5),
            _product(2, "عسل سمر 500 جرام", quantity=3),
            _product(3, "عسل سدر 1 كيلo", quantity=0),
        ]
        collapsed = collapse_products_to_honey_types(catalog)
        titles = " ".join(p["title"] for p in collapsed)
        assert "طلح" in titles
        assert "سمر" in titles
        assert "سدر" not in titles
        assert len(collapsed) == 2
