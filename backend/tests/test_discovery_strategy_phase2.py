"""Phase 2 — discovery strategy resolver and catalog intelligence."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.catalog_intelligence import compute_discovery_score  # noqa: E402
from modules.ai.brain.catalog.presentation_contract import (  # noqa: E402
    discovery_has_catalog_evidence,
    reply_contains_ungrounded_discovery_claim,
    validate_discovery_products,
)
from modules.ai.brain.commerce.discovery_strategy import (  # noqa: E402
    CatalogContextSnapshot,
    DiscoveryMode,
    resolve_discovery_strategy,
)
from modules.ai.brain.commerce.merchant_discovery_settings import (  # noqa: E402
    MerchantDiscoverySettings,
)
from modules.ai.brain.discovery.entry import (  # noqa: E402
    CATEGORY_BROWSE,
    GLOBAL_BROWSE,
    TOP_PRODUCTS,
)


class TestDiscoveryStrategyResolver:
    def test_global_browse_collections_first(self) -> None:
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(
                product_count=30,
                collection_count=4,
            ),
        )
        assert strategy.mode == DiscoveryMode.COLLECTIONS_FIRST

    def test_top_products_featured_first(self) -> None:
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=TOP_PRODUCTS,
            catalog_context=CatalogContextSnapshot(product_count=30),
        )
        assert strategy.mode == DiscoveryMode.FEATURED_FIRST

    def test_category_browse_direct_catalog(self) -> None:
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=CATEGORY_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=30),
        )
        assert strategy.mode == DiscoveryMode.DIRECT_CATALOG

    def test_large_global_browse_guided(self) -> None:
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(
                product_count=50,
                collection_count=0,
            ),
        )
        assert strategy.mode == DiscoveryMode.GUIDED_DISCOVERY
        assert strategy.guided_question

    def test_merchant_mode_override(self) -> None:
        strategy = resolve_discovery_strategy(
            commerce_objective="discovery",
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=10, collection_count=3),
            merchant_settings=MerchantDiscoverySettings(mode_override="direct_catalog"),
        )
        assert strategy.mode == DiscoveryMode.DIRECT_CATALOG


class TestDiscoveryScore:
    def test_featured_product_scores_higher(self) -> None:
        base = {
            "external_id": "b",
            "title": "Product B",
            "in_stock": True,
            "price": 100,
            "discovery_signals": {"featured_rank": 0, "sales_score": 1},
        }
        featured = {
            "external_id": "a",
            "title": "Product A",
            "in_stock": True,
            "price": 100,
            "discovery_signals": {"featured_rank": 0, "sales_score": 0},
        }
        score_base = compute_discovery_score(base, featured_product_ids=["b"])
        score_feat = compute_discovery_score(featured, featured_product_ids=["b"])
        assert score_base > score_feat


class TestPresentationContract:
    def test_validate_requires_title_and_price_or_image(self) -> None:
        valid = validate_discovery_products([
            {"title": "SKU One", "price": 50},
            {"title": ""},
        ])
        assert len(valid) == 1

    def test_collections_count_as_evidence(self) -> None:
        assert discovery_has_catalog_evidence(
            collections=[{"group_name": "Men", "product_count": 3}],
        )

    def test_generic_claim_detected(self) -> None:
        assert reply_contains_ungrounded_discovery_claim("عندنا أنواع مميزة")
