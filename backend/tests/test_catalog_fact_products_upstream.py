"""Upstream catalog_fact_products side channel for price/availability Q&A."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.store_knowledge import (  # noqa: E402
    CatalogContextBuilder,
    CatalogSearchProductsResult,
)


def _product_row(
    *,
    pid: int,
    title: str,
    price: float = 387.0,
    in_stock: bool = True,
    external_id: str = "ext-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=pid,
        title=title,
        external_id=external_id,
        sku="",
        description="",
        price=price,
        in_stock=in_stock,
        stock_quantity=None,
        extra_metadata={},
        catalog_status="active",
        merchant_hidden_at=None,
        has_variants=False,
        default_variant_id=None,
        variants=[],
    )


class TestCatalogFactProductsFilter:
    def test_filter_orderable_collects_non_orderable_facts(self) -> None:
        builder = CatalogContextBuilder(None, tenant_id=33)  # type: ignore[arg-type]
        talh = _product_row(
            pid=501,
            title="عسل الطلح",
            price=387,
            in_stock=False,
            external_id="27310682888555270",
        )
        orderable = _product_row(pid=502, title="عسل سدر", price=400, in_stock=True)
        orderable_rows, fact_rows = builder._filter_orderable(  # type: ignore[misc]
            [talh, orderable],
            source="search",
            collect_non_orderable_facts=True,
        )
        assert len(orderable_rows) == 1
        assert orderable_rows[0]["id"] == 502
        assert len(fact_rows) == 1
        assert fact_rows[0]["id"] == 501
        assert fact_rows[0]["price"] == 387
        assert fact_rows[0]["can_checkout"] is False
        assert fact_rows[0]["in_stock"] is False

    def test_filter_orderable_without_collect_drops_facts(self) -> None:
        builder = CatalogContextBuilder(None, tenant_id=33)  # type: ignore[arg-type]
        talh = _product_row(pid=501, title="عسل الطلح", in_stock=False)
        rows = builder._filter_orderable([talh], source="search")  # type: ignore[list-item]
        assert rows == []


class TestCatalogSearchProductsResult:
    def test_search_result_dataclass_shape(self) -> None:
        talh = {"id": 501, "title": "عسل الطلح", "price": 387, "can_checkout": False}
        orderable = {"id": 502, "title": "عسل سدر", "can_checkout": True}
        result = CatalogSearchProductsResult(
            products=[orderable],
            catalog_fact_products=[talh],
        )
        assert result.products[0]["id"] == 502
        assert result.catalog_fact_products[0]["id"] == 501


class TestRuntimeCatalogFactProducts:
    def test_tool_search_products_includes_fact_side_channel(self) -> None:
        from modules.ai.commerce.runtime import CommerceToolRuntime  # noqa: PLC0415

        talh = {"id": 501, "title": "عسل الطلح", "price": 387, "can_checkout": False}
        runtime = CommerceToolRuntime.__new__(CommerceToolRuntime)
        runtime.catalog = MagicMock()
        runtime.catalog.search_products.return_value = CatalogSearchProductsResult(
            products=[],
            catalog_fact_products=[talh],
        )

        async def _run():
            return await CommerceToolRuntime._tool_search_products(
                runtime,
                {
                    "query": "طلح",
                    "limit": 8,
                    "include_non_orderable_facts": True,
                },
            )

        result = asyncio.run(_run())
        assert result.ok is True
        assert result.payload["products"] == []
        assert result.payload["catalog_fact_products"][0]["id"] == 501
        runtime.catalog.search_products.assert_called_once_with(
            "طلح",
            limit=8,
            include_non_orderable_facts=True,
        )


class TestResponderCatalogFactProductsMerge:
    def test_compose_products_uses_fact_side_channel_for_price(self) -> None:
        from modules.ai.brain.compose.responder import (  # noqa: PLC0415
            catalog_compose_products_for_search_turn,
        )

        talh = {
            "id": 501,
            "title": "عسل الطلح",
            "price": 387,
            "can_checkout": False,
            "in_stock": False,
        }
        orderable = {"id": 502, "title": "عسل سدر", "can_checkout": True}
        selected = catalog_compose_products_for_search_turn(
            question_kind="price",
            category_filtered_facts=[talh],
            display_candidates=[orderable],
        )
        assert selected == [talh]
        assert 501 not in {p["id"] for p in [orderable]}

    def test_browse_ignores_fact_side_channel_selection(self) -> None:
        from modules.ai.brain.compose.responder import (  # noqa: PLC0415
            catalog_compose_products_for_search_turn,
        )

        candidates = [{"id": 101, "title": "عسل سدر", "can_checkout": True}]
        selected = catalog_compose_products_for_search_turn(
            question_kind="browse",
            category_filtered_facts=[{"id": 999, "can_checkout": False}],
            display_candidates=candidates,
        )
        assert selected == candidates
