"""Catalog Intelligence Phase 1 — data model, settings parser, router wiring."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.commerce.merchant_catalog_settings import (  # noqa: E402
    parse_merchant_catalog_settings,
)
from routers.catalog_intelligence import (  # noqa: E402
    CatalogSettingsIn,
    ProductGroupIn,
    router,
)
from services.catalog_intelligence_service import normalize_group_slug  # noqa: E402


class TestMerchantCatalogSettingsParser:
    def test_defaults(self) -> None:
        parsed = parse_merchant_catalog_settings({})
        assert parsed.best_seller_mode == "manual"
        assert parsed.max_relations_per_product == 8
        assert parsed.small_catalog_threshold == 5
        assert "sales_score" in parsed.scoring_weights

    def test_invalid_mode_falls_back(self) -> None:
        parsed = parse_merchant_catalog_settings({"best_seller_mode": "unknown"})
        assert parsed.best_seller_mode == "manual"

    def test_clamps_relation_limit(self) -> None:
        parsed = parse_merchant_catalog_settings({"max_relations_per_product": 999})
        assert parsed.max_relations_per_product == 50


class TestGroupSlugNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Men's Fragrances", "men-s-fragrances"),
            ("تيشيرتات", "تيشيرتات"),
            ("  honey-core  ", "honey-core"),
        ],
    )
    def test_normalize_group_slug(self, raw: str, expected: str) -> None:
        assert normalize_group_slug(raw) == expected


class TestCatalogIntelligenceRouterWiring:
    EXPECTED_PATHS = {
        "/settings/catalog-intelligence",
        "/catalog-intelligence/validation",
        "/catalog-intelligence/groups",
        "/catalog-intelligence/groups/reorder",
        "/catalog-intelligence/groups/{group_id}",
        "/catalog-intelligence/groups/{group_id}/items",
        "/catalog-intelligence/groups/{group_id}/items/{item_id}",
        "/catalog-intelligence/products/{product_id}/relations",
        "/catalog-intelligence/products/{product_id}/relations/{relation_id}",
        "/catalog-intelligence/products/{product_id}/ranking",
    }

    def test_routes_registered(self) -> None:
        paths = {route.path for route in router.routes}
        assert self.EXPECTED_PATHS.issubset(paths)

    def test_settings_body_has_no_tenant_id(self) -> None:
        fields = set(CatalogSettingsIn.model_fields)
        assert "tenant_id" not in fields

    def test_group_body_has_no_tenant_id(self) -> None:
        fields = set(ProductGroupIn.model_fields)
        assert "tenant_id" not in fields


class TestMainIncludesCatalogIntelligenceRouter:
    def test_main_registers_router(self) -> None:
        main_path = os.path.join(_BACKEND, "main.py")
        with open(main_path, encoding="utf-8") as fh:
            src = fh.read()
        assert "_catalog_intelligence_router" in src
        assert "app.include_router(_catalog_intelligence_router)" in src


class TestReadOnlyHelpersExported:
    def test_helpers_exist_without_pipeline_import(self) -> None:
        from services import catalog_intelligence_service as svc  # noqa: PLC0415

        for name in (
            "read_catalog_groups",
            "read_group_products",
            "read_product_relations",
            "read_best_sellers",
        ):
            assert callable(getattr(svc, name))
