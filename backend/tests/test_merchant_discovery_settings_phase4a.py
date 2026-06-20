"""Phase 4A — merchant-configurable discovery settings."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.catalog_intelligence import (  # noqa: E402
    CatalogIntelligence,
    compute_discovery_score,
)
from modules.ai.brain.catalog.catalog_provider import CatalogProvider  # noqa: E402
from modules.ai.brain.catalog.discovery_presenter import (  # noqa: E402
    compose_collection_products,
    compose_merchant_collections,
)
from modules.ai.brain.commerce.discovery_strategy import (  # noqa: E402
    CatalogContextSnapshot,
    DiscoveryMode,
    resolve_discovery_strategy,
)
from modules.ai.brain.commerce.merchant_discovery_settings import (  # noqa: E402
    DiscoveryCollectionConfig,
    FeaturedProductConfig,
    MerchantDiscoverySettings,
    parse_merchant_discovery_settings,
)
from modules.ai.brain.discovery.entry import (  # noqa: E402
    CATEGORY_BROWSE,
    GLOBAL_BROWSE,
)
from services.merchant_discovery_settings_service import sanitize_discovery_settings  # noqa: E402


class _FakeProvider(CatalogProvider):
    def __init__(self, products: List[Dict[str, Any]]) -> None:
        self._products = list(products)

    def search_products(self, query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        q = str(query or "").strip().lower()
        if not q:
            return self._products[:limit]
        return [
            p for p in self._products
            if q in str(p.get("title") or "").lower()
            or q in str(p.get("category") or "").lower()
        ][:limit]

    def get_top_products(self, *, limit: int = 12) -> List[Dict[str, Any]]:
        return self._products[:limit]

    def list_collections(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        return []

    def get_collection_products(self, collection_name: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        return self.search_products(collection_name, limit=limit)


_CLOTHING_CATALOG = [
    {"id": 1, "title": "Talh 1kg", "category": "Honey", "price": "220", "in_stock": True},
    {"id": 2, "title": "Sidr 1kg", "category": "Honey", "price": "250", "in_stock": True},
    {"id": 3, "title": "Samar 1kg", "category": "Honey", "price": "230", "in_stock": True},
    {"id": 4, "title": "Talh 500g", "category": "Honey", "price": "120", "in_stock": True},
    {"id": 5, "title": "Men Jacket", "category": "Men", "price": "300", "in_stock": True},
    {"id": 6, "title": "Women Dress", "category": "Women", "price": "280", "in_stock": True},
]


def _merchant_settings(**overrides: Any) -> MerchantDiscoverySettings:
    base = {
        "default_mode": "collections_first",
        "collections": [
            {
                "id": "honey",
                "label": "العسل",
                "priority": 1,
                "enabled": True,
                "catalog_match": "Honey",
                "featured_products": [
                    {"product_id": "1", "priority": 1, "label_override": "الطلح البري 1 كيلو"},
                    {"product_id": "2", "priority": 2},
                    {"product_id": "3", "priority": 3},
                ],
            },
            {
                "id": "other_products",
                "label": "منتجات أخرى",
                "priority": 2,
                "enabled": True,
            },
            {
                "id": "wholesale",
                "label": "المبيعات بالجملة",
                "priority": 3,
                "enabled": False,
            },
        ],
    }
    base.update(overrides)
    return parse_merchant_discovery_settings(base)


class TestPlatformDefaults:
    def test_no_settings_uses_platform_defaults(self) -> None:
        settings = parse_merchant_discovery_settings({})
        assert settings.default_mode == ""
        assert settings.collections == []
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=30, collection_count=4),
            merchant_settings=settings,
        )
        assert strategy.mode == DiscoveryMode.COLLECTIONS_FIRST


class TestDiscoveryStrategyMerchantMode:
    def test_collections_first_setting_controls_global_browse(self) -> None:
        settings = _merchant_settings(default_mode="collections_first")
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=10, collection_count=0),
            merchant_settings=settings,
        )
        assert strategy.mode == DiscoveryMode.COLLECTIONS_FIRST

    def test_featured_first_setting_controls_global_browse(self) -> None:
        settings = _merchant_settings(default_mode="featured_first")
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=10, collection_count=4),
            merchant_settings=settings,
        )
        assert strategy.mode == DiscoveryMode.FEATURED_FIRST

    def test_selection_bypasses_merchant_default_mode(self) -> None:
        settings = _merchant_settings(default_mode="collections_first")
        strategy = resolve_discovery_strategy(
            commerce_objective="selection",
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=10, collection_count=4),
            merchant_settings=settings,
        )
        assert strategy.mode == DiscoveryMode.DIRECT_CATALOG


class TestCatalogIntelligenceMerchantSettings:
    def test_collection_priority_controls_display_order(self) -> None:
        settings = _merchant_settings()
        intel = CatalogIntelligence(_FakeProvider(_CLOTHING_CATALOG))
        plan = intel.build_discovery_plan(
            strategy=resolve_discovery_strategy(
                commerce_objective="discovery",
                entry_type=GLOBAL_BROWSE,
                catalog_context=CatalogContextSnapshot(product_count=6, collection_count=2),
                merchant_settings=settings,
            ),
            merchant_settings=settings,
        )
        labels = [c.group_name for c in plan.collections]
        assert labels[0] == "العسل"
        assert "منتجات أخرى" in labels
        assert "المبيعات بالجملة" not in labels

    def test_disabled_collections_are_hidden(self) -> None:
        settings = _merchant_settings()
        intel = CatalogIntelligence(_FakeProvider(_CLOTHING_CATALOG))
        groups = intel.list_collections(merchant_settings=settings)
        names = [g.group_name for g in groups]
        assert "المبيعات بالجملة" not in names

    def test_featured_products_inside_collection_appear_first(self) -> None:
        settings = _merchant_settings()
        collection = settings.enabled_collections()[0]
        intel = CatalogIntelligence(_FakeProvider(_CLOTHING_CATALOG))
        ranked = intel.rank_products(
            _CLOTHING_CATALOG,
            strategy=resolve_discovery_strategy(
                commerce_objective="discovery",
                entry_type=CATEGORY_BROWSE,
                catalog_context=CatalogContextSnapshot(product_count=6),
                merchant_settings=settings,
            ),
            merchant_settings=settings,
            collection=collection,
        )
        assert [p["id"] for p in ranked[:3]] == [1, 2, 3]

    def test_merchant_priority_scores_higher(self) -> None:
        low = {"id": 99, "title": "Generic", "price": 100, "in_stock": True}
        high = {"id": 1, "title": "Featured", "price": 100, "in_stock": True}
        settings = _merchant_settings()
        score_low = compute_discovery_score(
            low,
            merchant_priority_map=settings.merchant_priority_map(),
        )
        score_high = compute_discovery_score(
            high,
            merchant_priority_map=settings.merchant_priority_map(),
        )
        assert score_high > score_low


class TestDiscoveryPresenter:
    def test_compose_merchant_collections(self) -> None:
        settings = _merchant_settings()
        intel = CatalogIntelligence(_FakeProvider(_CLOTHING_CATALOG))
        groups = intel.list_collections(merchant_settings=settings)
        text = compose_merchant_collections(groups, merchant_settings=settings)
        assert "اختر القسم اللي يناسبك" in text
        assert "1. العسل" in text
        assert "2. منتجات أخرى" in text

    def test_compose_collection_products_with_label_override(self) -> None:
        settings = _merchant_settings()
        collection = settings.enabled_collections()[0]
        products = [
            {"id": 1, "title": "Talh 1kg", "price": "220"},
            {"id": 2, "title": "Sidr 1kg", "price": "250"},
        ]
        text = compose_collection_products(
            products,
            collection=collection,
            merchant_settings=settings,
            collection_label=collection.label,
        )
        assert "من العسل المتوفر" in text
        assert "الطلح البري 1 كيلو" in text
        assert "220 ريال" in text
        assert "اكتب رقم المنتج أو اسمه" in text


class TestSettingsSanitizer:
    def test_invalid_product_id_is_ignored(self) -> None:
        class _Db:
            def query(self, *_args: Any, **_kwargs: Any) -> "_Db":
                return self

            def filter(self, *_args: Any, **_kwargs: Any) -> "_Db":
                return self

            def all(self) -> List[Any]:
                return []

        raw = _merchant_settings().to_dict()
        raw["collections"][0]["featured_products"].append({"product_id": "99999", "priority": 9})
        sanitized = sanitize_discovery_settings(raw, db=_Db(), tenant_id=1)
        featured = sanitized["collections"][0]["featured_products"]
        assert all(fp["product_id"] != "99999" for fp in featured)


class TestGenericMerchantCategories:
    def test_clothing_merchant_structure(self) -> None:
        settings = parse_merchant_discovery_settings(
            {
                "default_mode": "collections_first",
                "collections": [
                    {"id": "men", "label": "رجالي", "priority": 1, "enabled": True, "catalog_match": "Men"},
                    {"id": "women", "label": "نسائي", "priority": 2, "enabled": True, "catalog_match": "Women"},
                    {"id": "sale", "label": "تخفيضات", "priority": 3, "enabled": True, "catalog_match": "Sale"},
                ],
            }
        )
        assert "عسل" not in settings.to_dict()["collections"][0]["label"]
        intel = CatalogIntelligence(_FakeProvider(_CLOTHING_CATALOG))
        plan = intel.build_discovery_plan(
            strategy=resolve_discovery_strategy(
                commerce_objective="discovery",
                entry_type=GLOBAL_BROWSE,
                catalog_context=CatalogContextSnapshot(product_count=6, collection_count=2),
                merchant_settings=settings,
            ),
            merchant_settings=settings,
        )
        labels = [c.group_name for c in plan.collections]
        assert labels[0] == "رجالي"

    def test_variant_specific_featured_preferred_in_presenter(self) -> None:
        settings = parse_merchant_discovery_settings(
            {
                "collections": [
                    {
                        "id": "honey",
                        "label": "Honey",
                        "priority": 1,
                        "enabled": True,
                        "featured_products": [
                            {
                                "product_id": "4",
                                "variant_id": "44",
                                "priority": 1,
                                "label_override": "Talh 500g",
                            }
                        ],
                    }
                ]
            }
        )
        collection = settings.enabled_collections()[0]
        fp = settings.featured_for_collection(collection)[0]
        product = {
            "id": 4,
            "title": "Talh 500g",
            "price": "999",
            "variants": [{"id": "44", "price": "120"}],
        }
        text = compose_collection_products(
            [product],
            collection=collection,
            merchant_settings=settings,
            collection_label="Honey",
        )
        assert "Talh 500g" in text
        assert "120 ريال" in text
        assert "999" not in text
        assert fp.variant_id == "44"
