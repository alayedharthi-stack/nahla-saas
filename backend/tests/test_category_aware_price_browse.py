"""Category-aware price/availability browse — platform-wide catalog scope."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.catalog.catalog_browse_scope_resolver import (  # noqa: E402
    CatalogCategoryScope,
    filter_products_by_category_metadata,
    resolve_catalog_category_scope,
)
from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: E402
    extract_browse_category_scope,
    filter_products_for_browse_turn,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    _detect_violations,
    _filter_violations_for_category_browse,
    _is_general_category_browse_turn,
    apply_product_claim_grounding_guard,
)


def _product(
    pid: int,
    title: str,
    *,
    category: str = "",
    price: float = 100.0,
) -> Dict[str, Any]:
    return {
        "id": pid,
        "title": title,
        "category": category,
        "price": price,
        "can_checkout": True,
    }


HONEY_GROUP = {
    "id": 11,
    "slug": "honey",
    "label": "عسل",
    "catalog_match": "عسل, honey",
    "is_active": True,
    "priority": 10,
}

OILS_GROUP = {
    "id": 12,
    "slug": "oils",
    "label": "زيوت",
    "catalog_match": "زيوت, oils",
    "is_active": True,
    "priority": 20,
}

MIXED_CATALOG = [
    _product(1, "عسل سدر", category="عسل", price=300),
    _product(2, "عسل طلح", category="عسل", price=250),
    _product(3, "عكبر", category="مكملات", price=180),
    _product(4, "كريم سم النحل", category="كريمات", price=120),
    _product(5, "زيت زيتون", category="زيوت", price=90),
]


class TestPriceStopwordsNotCategoryScope:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("اسعار العسل", "عسل"),
            ("أسعار العسل", "عسل"),
            ("كم سعر التمور", "تمور"),
            ("عندكم زيوت؟", "زيت"),
            ("وش المتوفر من الكريمات؟", "كريم"),
        ],
    )
    def test_price_stopwords_are_not_used_as_category_scope(
        self,
        message: str,
        expected: str,
    ) -> None:
        scope = extract_browse_category_scope(message)
        assert scope == expected
        assert scope not in {"اسعار", "أسعار", "سعر", "كم", "من", "المتوفر", "عندكم"}


class TestResolveCatalogCategoryScope:
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
        return_value=[HONEY_GROUP, OILS_GROUP],
    )
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
        return_value=(1, 2),
    )
    def test_category_price_request_matches_catalog_category(
        self,
        _mock_ids: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        scope = resolve_catalog_category_scope(MagicMock(), 33, "اسعار العسل")
        assert scope.intent == "category_price_browse"
        assert scope.matched_category == "عسل"
        assert scope.catalog_group_id == 11
        assert scope.query_subject == "عسل"
        assert scope.must_filter_by_category is True
        assert scope.use_catalog_prices_only is True
        assert scope.specific_product is False

    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
        return_value=[HONEY_GROUP, OILS_GROUP],
    )
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
        return_value=(1, 2),
    )
    def test_honey_prices_match_honey_category_without_hardcoding_honey(
        self,
        _mock_ids: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        scope = resolve_catalog_category_scope(MagicMock(), 33, "أسعار العسل")
        assert scope.matched_category == "عسل"
        assert scope.product_ids == (1, 2)

    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
        return_value=[HONEY_GROUP, OILS_GROUP],
    )
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
        return_value=(5,),
    )
    def test_oils_prices_match_oils_category_without_hardcoding_oils(
        self,
        _mock_ids: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        scope = resolve_catalog_category_scope(MagicMock(), 33, "عندكم زيوت؟")
        assert scope.matched_category == "زيوت"
        assert scope.catalog_group_id == 12


class TestCategoryBrowseFiltering:
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
        return_value=[HONEY_GROUP],
    )
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
        return_value=(1, 2),
    )
    def test_category_browse_uses_product_group_filter(
        self,
        _mock_ids: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        filtered = filter_products_for_browse_turn(
            MIXED_CATALOG,
            message="اسعار العسل",
            db=MagicMock(),
            tenant_id=33,
        )
        assert [p["id"] for p in filtered] == [1, 2]

    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
        return_value=[],
    )
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver._load_snapshot_categories",
        return_value=["كريمات"],
    )
    def test_category_browse_falls_back_to_product_metadata_category(
        self,
        _mock_snapshot: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        filtered = filter_products_by_category_metadata(
            MIXED_CATALOG,
            category="كريمات",
        )
        assert [p["id"] for p in filtered] == [4]

        scoped = filter_products_for_browse_turn(
            MIXED_CATALOG,
            message="وش المتوفر من الكريمات؟",
            db=MagicMock(),
            tenant_id=33,
        )
        assert [p["id"] for p in scoped] == [4]

    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
        return_value=[HONEY_GROUP],
    )
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
        return_value=(1, 2),
    )
    def test_category_browse_excludes_products_from_other_categories(
        self,
        _mock_ids: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        filtered = filter_products_for_browse_turn(
            MIXED_CATALOG,
            message="اسعار العسل",
            db=MagicMock(),
            tenant_id=33,
        )
        ids = {p["id"] for p in filtered}
        assert ids == {1, 2}
        assert 3 not in ids
        assert 4 not in ids
        assert 5 not in ids


class TestProductClaimGroundingGuardCategoryBrowse:
    def _evidence(self, **overrides: Any) -> ProductClaimGroundingEvidence:
        base = dict(
            grounded_prices=frozenset({300, 250}),
            grounded_text_corpus="",
            available_products=(
                {"id": 1, "title": "عسل سدر", "can_checkout": True},
                {"id": 2, "title": "عسل طلح", "can_checkout": True},
            ),
            unavailable_products=(
                {"id": 3, "title": "عسل سدر", "can_checkout": False},
            ),
            catalog_products_this_turn=False,
            catalog_miss_this_turn=False,
            recent_catalog_miss=False,
            recent_no_synced=False,
            has_checkout_catalog=True,
            executor_product_ids=frozenset(),
            kb_section_ids=frozenset(),
        )
        base.update(overrides)
        return ProductClaimGroundingEvidence(**base)

    def test_general_category_browse_does_not_emit_no_grounded_price_fallback(self) -> None:
        reply = "عسل سدر: 350 ريال وعسل طلح: 280 ريال"
        violations = _detect_violations(reply, self._evidence(grounded_prices=frozenset()))
        filtered = _filter_violations_for_category_browse(violations, category_browse=True)
        assert not any(v[0] == "ungrounded_price" for v in filtered)

        guarded = apply_product_claim_grounding_guard(
            reply=reply,
            inbound_metadata={
                "inbound_text": "اسعار العسل",
                "category_browse": True,
                "specific_product": False,
            },
        )
        assert "ما ظهر عندي سعر مؤكد" not in guarded.reply
        assert guarded.replaced is False

    def test_general_category_browse_does_not_emit_unavailable_product_message(self) -> None:
        reply = "السدر الخيار الأفضل لك. تبي أرسل لك تفاصيل السدر؟"
        violations = _detect_violations(reply, self._evidence())
        filtered = _filter_violations_for_category_browse(violations, category_browse=True)
        assert not any(v[0] == "unavailable_promoted" for v in filtered)

        guarded = apply_product_claim_grounding_guard(
            reply=reply,
            inbound_metadata={
                "inbound_text": "اسعار العسل",
                "category_browse": True,
                "specific_product": False,
            },
        )
        assert "هذا المنتج غير متوفر حالياً" not in guarded.reply
        assert guarded.replaced is False

    @patch(
        "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
    )
    def test_product_unavailable_message_only_for_specific_product(
        self,
        mock_evidence: MagicMock,
    ) -> None:
        mock_evidence.return_value = self._evidence()
        reply = "تبي أرسل لك تفاصيل عسل سدر؟"
        guarded = apply_product_claim_grounding_guard(
            reply=reply,
            inbound_metadata={
                "inbound_text": "كم سعر عسل سدر 500 جرام",
                "specific_product": True,
            },
        )
        assert guarded.replaced is True
        assert guarded.stripped is True
        assert "تبي أرسل" not in guarded.reply
        assert "هذا المنتج غير متوفر حالياً" not in guarded.reply
        assert guarded.scrubbed_empty is True
        assert guarded.requires_grounded_recompose is True

    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.resolve_catalog_category_scope",
        return_value=CatalogCategoryScope(
            intent="category_price_browse",
            matched_category="عسل",
            query_subject="عسل",
            must_filter_by_category=True,
            specific_product=False,
        ),
    )
    @patch(
        "modules.ai.brain.commerce.commerce_browse_category_guard.extract_browse_category_scope",
        return_value="عسل",
    )
    def test_is_general_category_browse_turn_from_inbound_text(
        self,
        _mock_extract: MagicMock,
        _mock_scope: MagicMock,
    ) -> None:
        assert _is_general_category_browse_turn(
            {"inbound_text": "اسعار العسل"},
            db=MagicMock(),
            tenant_id=33,
        ) is True
