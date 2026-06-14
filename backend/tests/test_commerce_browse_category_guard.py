"""Smoke — category-scoped commerce browse stays inside requested category."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: E402
    extract_browse_category_scope,
    filter_products_to_browse_category,
    is_category_scoped_browse,
    should_exclude_cross_category_product,
)


def _product(
    pid: int,
    title: str,
    *,
    category: str = "",
    description: str = "",
) -> dict:
    payload = {"id": pid, "title": title, "category": category}
    if description:
        payload["description"] = description
    return payload


# Generic catalog — category nouns are placeholders, not merchant SKUs.
_ALPHA = "alpha"
_BETA = "beta"
_GAMMA = "gamma"

_MIXED_CATALOG = [
    _product(1, f"Season {_ALPHA} reserve", category=_ALPHA),
    _product(2, f"Premium {_ALPHA} batch", category=_ALPHA),
    _product(3, f"Skin {_BETA} formula", category=_BETA),
    _product(4, f"Natural {_GAMMA} blend", category=_GAMMA),
    _product(5, f"{_BETA} with {_ALPHA} extract", category=_BETA),
]


class TestScopeExtraction:
    @pytest.mark.parametrize(
        "message,expected_scope",
        [
            ("what alpha do you have", _ALPHA),
            ("show alpha collection", _ALPHA),
            ("display the alpha items", _ALPHA),
            (f"want {_ALPHA} seasonal batch", _ALPHA),
        ],
    )
    def test_generic_browse_phrases_extract_scope(self, message: str, expected_scope: str) -> None:
        assert extract_browse_category_scope(message) == expected_scope

    @pytest.mark.parametrize(
        "message",
        [
            "what do you have",
            "show all products",
            "what is available",
            "top products",
        ],
    )
    def test_global_browse_has_no_scope(self, message: str) -> None:
        assert extract_browse_category_scope(message) is None
        assert is_category_scoped_browse(message) is False


class TestCategoryFilter:
    @pytest.mark.parametrize(
        "message",
        [
            f"what {_ALPHA} do you have",
            f"show {_ALPHA} options",
            f"display {_ALPHA} line",
            f"want {_ALPHA} seasonal",
        ],
    )
    def test_category_browse_excludes_other_families(self, message: str) -> None:
        filtered = filter_products_to_browse_category(
            _MIXED_CATALOG,
            message=message,
            query=_ALPHA,
        )
        titles = {p["title"] for p in filtered}
        assert titles == {
            f"Season {_ALPHA} reserve",
            f"Premium {_ALPHA} batch",
            f"{_BETA} with {_ALPHA} extract",
        }
        assert f"Skin {_BETA} formula" not in titles
        assert f"Natural {_GAMMA} blend" not in titles

    def test_derivative_form_kept_when_customer_asks_for_it(self) -> None:
        catalog = [
            _product(1, f"Pure {_ALPHA} line", category=_ALPHA),
            _product(2, f"Therapeutic {_BETA} blend", category=_BETA),
        ]
        filtered = filter_products_to_browse_category(
            catalog,
            message=f"show {_BETA} options",
            query=_BETA,
        )
        assert [p["id"] for p in filtered] == [2]

    def test_no_filter_when_browse_is_store_wide(self) -> None:
        filtered = filter_products_to_browse_category(
            _MIXED_CATALOG,
            message="what do you have",
            query="",
            source="top_products",
        )
        assert len(filtered) == len(_MIXED_CATALOG)

    def test_empty_input_is_safe(self) -> None:
        assert filter_products_to_browse_category([], message=f"show {_ALPHA}") == []


class TestSmokeRegression:
    """Production-shaped leaks — description/hive bleed must not widen scope."""

    _MSG = "وش عندكم عسل"

    _CATALOG = [
        _product(1, "عسل سدر", category="عسل"),
        _product(
            2,
            "زيت سم النحل",
            description="منتج طبيعي من العسل والنحل",
        ),
        _product(
            3,
            "كريم سم النحل",
            description="مشتقات العسل الطبيعية",
        ),
    ]

    def test_description_honey_copy_does_not_keep_oil_or_cream(self) -> None:
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message=self._MSG,
            query="",
            source="top_products",
        )
        assert [p["id"] for p in filtered] == [1]

    def test_hive_title_without_honey_is_excluded(self) -> None:
        assert should_exclude_cross_category_product(
            _product(4, "سم النحل"),
            scope="عسل",
            message=self._MSG,
        )

    @pytest.mark.parametrize(
        "message",
        [
            "وش عندكم عسل",
            "ابي عسل الموسم",
            "اعرض الأعسال",
        ],
    )
    def test_top_products_path_still_scopes_honey(self, message: str) -> None:
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message=message,
            query="",
            source="top_products",
        )
        assert [p["id"] for p in filtered] == [1]

    def test_cream_browse_allows_cream(self) -> None:
        catalog = [
            _product(10, "كريم سم النحل"),
            _product(11, "عسل سدر", category="عسل"),
        ]
        filtered = filter_products_to_browse_category(
            catalog,
            message="اعرض كريم",
            query="كريم",
        )
        assert [p["id"] for p in filtered] == [10]


class TestArabicBrowseSmoke:
    """Representative Arabic browse shapes — category noun only, no tenant SKU."""

    _CAT = "nectar"

    _AR_CATALOG = [
        _product(10, f"{_CAT} season reserve"),
        _product(11, f"premium {_CAT}"),
        _product(12, "skin cream formula"),
        _product(13, "natural oil blend"),
    ]

    @pytest.mark.parametrize(
        "message",
        [
            "وش عندكم nectar",
            "ابي nectar الموسم",
            "اعرض nectar",
        ],
    )
    def test_arabic_category_browse_stays_in_category(self, message: str) -> None:
        filtered = filter_products_to_browse_category(
            self._AR_CATALOG,
            message=message,
            query=self._CAT,
        )
        ids = {p["id"] for p in filtered}
        assert ids == {10, 11}
        assert 12 not in ids
        assert 13 not in ids


class TestHoneyCategoryShapes:
    """User-facing browse shapes — generic titles, no merchant SKU."""

    _CAT = "عسل"

    _CATALOG = [
        _product(20, "عسل موسم أ"),
        _product(21, "عسل احتياطي"),
        _product(22, "كريم مرطب"),
        _product(23, "زيت طبيعي"),
    ]

    @pytest.mark.parametrize(
        "message",
        [
            "وش عندكم عسل",
            "ابي عسل الموسم",
            "اعرض الأعسال",
        ],
    )
    def test_honey_browse_excludes_cross_category(self, message: str) -> None:
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message=message,
            query=self._CAT,
        )
        ids = {p["id"] for p in filtered}
        assert ids == {20, 21}
        assert 22 not in ids
        assert 23 not in ids
