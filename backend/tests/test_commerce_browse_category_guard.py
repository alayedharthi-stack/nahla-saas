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
    extract_browse_category_scopes,
    filter_products_for_browse_turn,
    filter_products_to_browse_category,
    is_category_scoped_browse,
    resolve_browse_category_scope,
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

    def test_feminine_plural_scope_keeps_singular_title(self) -> None:
        catalog = [
            _product(1, "ساعة يد فضية", category=""),
            _product(2, "حقيبة يد جلد", category=""),
        ]
        filtered = filter_products_to_browse_category(
            catalog,
            message="عندكم ساعات؟",
            query="ساعات",
        )
        assert [p["id"] for p in filtered] == [1]


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


class TestHoneySessionLockedBrowse:
    """Generic options browse inherits locked honey session (P0-B)."""

    _CATALOG = [
        _product(1, "عسل طلح"),
        _product(2, "كريم سم النحل"),
        _product(3, "زيت سم النحل"),
    ]

    @pytest.mark.parametrize(
        "message",
        [
            "وش الخيارات؟",
            "وين الخيارات",
            "وش المتوفر",
        ],
    )
    def test_generic_options_respects_active_honey_category(self, message: str) -> None:
        assert resolve_browse_category_scope(
            message,
            active_category="عسل",
            source="top_products",
        ) == "عسل"
        filtered = filter_products_for_browse_turn(
            self._CATALOG,
            message=message,
            source="top_products",
            active_category="عسل",
        )
        assert [p["id"] for p in filtered] == [1]

    def test_honey_subtype_without_explicit_honey_word(self) -> None:
        assert resolve_browse_category_scope("ابي طلح") == "عسل"


class TestArabicPluralAndMultiScopeResolution:
    """RCA fix — plural morphology + multi-category OR/AND union.

    Uses generic apparel nouns only; no merchant/SKU hardcoding in runtime.
    """

    _CATALOG = [
        _product(1, "جاكيت"),
        _product(2, "فستان"),
        _product(3, "بنطلون"),
        _product(4, "بلوزة"),
    ]

    def test_sound_plural_scope_matches_singular_title(self) -> None:
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message="وش عندكم جاكيتات؟",
            query="",
            source="top_products",
        )
        assert [p["id"] for p in filtered] == [1]

    def test_broken_plural_scope_matches_singular_title(self) -> None:
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message="وش عندكم فساتين؟",
            query="",
            source="top_products",
        )
        assert [p["id"] for p in filtered] == [2]

    @pytest.mark.parametrize(
        "message",
        [
            "وش عندكم جاكيتات أو فساتين ؟",
            "وش عندكم جاكيتات وفساتين ؟",
            "جاكيتات و فساتين",
        ],
    )
    def test_multi_category_union_keeps_both_families(self, message: str) -> None:
        scopes = extract_browse_category_scopes(message)
        assert set(scopes) == {"جاكيتات", "فساتين"}
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message=message,
            query="",
            source="top_products",
        )
        assert {p["id"] for p in filtered} == {1, 2}
        assert 3 not in {p["id"] for p in filtered}

    def test_unmatched_scope_stays_empty(self) -> None:
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message="وش عندكم قبعات؟",
            query="",
            source="top_products",
        )
        assert filtered == []

    def test_global_browse_without_scope_is_untouched(self) -> None:
        message = "وش عندكم؟"
        assert extract_browse_category_scope(message) is None
        assert is_category_scoped_browse(message, source="top_products") is False
        filtered = filter_products_to_browse_category(
            self._CATALOG,
            message=message,
            query="",
            source="top_products",
        )
        assert len(filtered) == len(self._CATALOG)

    def test_multi_match_stays_multi_candidate(self) -> None:
        catalog = [
            _product(10, "جاكيت خفيف"),
            _product(11, "جاكيت شتوي"),
            _product(12, "فستان"),
        ]
        filtered = filter_products_to_browse_category(
            catalog,
            message="وش عندكم جاكيتات؟",
            source="top_products",
        )
        assert [p["id"] for p in filtered] == [10, 11]
        assert len(filtered) >= 2

    def test_single_resolved_candidate_ready_for_presentation_contract(self) -> None:
        catalog = [
            _product(20, "جاكيت", category="جاكيت"),
            _product(21, "فستان"),
        ]
        filtered = filter_products_to_browse_category(
            catalog,
            message="اعرض الجاكيت",
            source="top_products",
        )
        assert len(filtered) == 1
        assert filtered[0]["id"] == 20
        # Presentation (#787) requires candidate_count==1 + catalog identity —
        # scope resolution must surface exactly one catalog row here.
        assert filtered[0].get("title") and (
            filtered[0].get("id") is not None or filtered[0].get("category")
        )

    def test_tenant_isolation_filter_is_list_local(self) -> None:
        tenant_a = [_product(101, "جاكيت"), _product(102, "فستان")]
        tenant_b = [_product(201, "جاكيت"), _product(202, "فستان")]
        out_a = filter_products_for_browse_turn(
            tenant_a,
            message="وش عندكم جاكيتات أو فساتين ؟",
            source="top_products",
            tenant_id=1,
        )
        out_b = filter_products_for_browse_turn(
            tenant_b,
            message="وش عندكم جاكيتات أو فساتين ؟",
            source="top_products",
            tenant_id=2,
        )
        assert {p["id"] for p in out_a} == {101, 102}
        assert {p["id"] for p in out_b} == {201, 202}
        assert not ({p["id"] for p in out_a} & {p["id"] for p in out_b})

    def test_primary_scope_api_keeps_first_for_compat(self) -> None:
        assert extract_browse_category_scope("جاكيتات أو فساتين") == "جاكيتات"
        assert resolve_browse_category_scope("جاكيتات أو فساتين") == "جاكيتات"
