"""Discovery product_specific focus export — source-contract regression."""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.catalog_intelligence import DiscoveryPlan  # noqa: E402
from modules.ai.brain.catalog.discovery_presenter import (  # noqa: E402
    DiscoveryPresentationResult,
)
from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    product_focus_identity,
    set_product_focus,
)
from modules.ai.brain.commerce.discovery_strategy import (  # noqa: E402
    DiscoveryMode,
    DiscoveryStrategyResult,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.discovery.entry import (  # noqa: E402
    CATEGORY_BROWSE,
    PRODUCT_SPECIFIC,
)
from modules.ai.brain.execution.search import (  # noqa: E402
    _apply_discovery_strategy,
    resolve_confirmed_discovery_product,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _shoe_white() -> dict[str, Any]:
    return {
        "id": "shoe-white-1",
        "external_id": "SKU-WHITE",
        "title": "حذاء رياضي أبيض",
        "price": 199,
    }


def _perfume_rose() -> dict[str, Any]:
    return {
        "id": "perf-rose-1",
        "external_id": "SKU-PERF-ROSE",
        "title": "عطر ورد 100ml",
        "price": 149,
    }


def _shirt_blue() -> dict[str, Any]:
    return {
        "product_id": "shirt-blue-9",
        "title": "قميص قطني أزرق",
        "price": 89,
    }


def _ctx() -> BrainContext:
    ctx = BrainContext(
        tenant_id=42,
        customer_phone="966500000001",
        message="أبغى حذاء رياضي أبيض",
        intent=Intent(name="start_order", confidence=0.9, raw_message="أبغى حذاء رياضي أبيض"),
        state=MerchantConversationState(greeted=True, stage="discovery", turn=3),
        facts=CommerceFacts(has_products=True, product_count=10),
        history=[],
    )
    ctx._db = MagicMock()  # type: ignore[attr-defined]
    return ctx


class TestResolveConfirmedDiscoveryProduct:
    def test_product_specific_single_product_exports_unchanged(self) -> None:
        product = _shoe_white()
        resolved = resolve_confirmed_discovery_product(
            entry_type=PRODUCT_SPECIFIC,
            discovery_output_kind="products",
            presented_products=[product],
        )
        assert resolved is product
        assert product_focus_identity(resolved) == "SKU-WHITE"

    def test_product_specific_perfume_category_exports_by_id(self) -> None:
        product = _perfume_rose()
        resolved = resolve_confirmed_discovery_product(
            entry_type=PRODUCT_SPECIFIC,
            discovery_output_kind="products",
            presented_products=[product],
        )
        assert resolved is product
        assert product_focus_identity(resolved) == "SKU-PERF-ROSE"

    def test_product_specific_shirt_with_product_id_only(self) -> None:
        product = _shirt_blue()
        resolved = resolve_confirmed_discovery_product(
            entry_type=PRODUCT_SPECIFIC,
            discovery_output_kind="products",
            presented_products=[product],
        )
        assert resolved is product
        assert product_focus_identity(resolved) == "shirt-blue-9"

    def test_multiple_products_returns_none(self) -> None:
        assert (
            resolve_confirmed_discovery_product(
                entry_type=PRODUCT_SPECIFIC,
                discovery_output_kind="products",
                presented_products=[_shoe_white(), _perfume_rose()],
            )
            is None
        )

    def test_category_browse_single_product_returns_none(self) -> None:
        assert (
            resolve_confirmed_discovery_product(
                entry_type=CATEGORY_BROWSE,
                discovery_output_kind="products",
                presented_products=[_shoe_white()],
            )
            is None
        )

    def test_collections_output_returns_none(self) -> None:
        assert (
            resolve_confirmed_discovery_product(
                entry_type=PRODUCT_SPECIFIC,
                discovery_output_kind="collections",
                presented_products=[_shoe_white()],
            )
            is None
        )

    def test_title_only_without_canonical_id_returns_none(self) -> None:
        assert (
            resolve_confirmed_discovery_product(
                entry_type=PRODUCT_SPECIFIC,
                discovery_output_kind="products",
                presented_products=[{"title": "حذاء رياضي أبيض", "price": 199}],
            )
            is None
        )


class TestApplyDiscoveryStrategyProductExport:
    def _run_discovery(
        self,
        *,
        entry_type: str,
        products: list[dict[str, Any]],
        presentation: DiscoveryPresentationResult,
    ):
        ctx = _ctx()
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": "query",
                "source": "order_product_query",
                "discovery_mode": DiscoveryMode.DIRECT_CATALOG.value,
                "discovery_entry_type": entry_type,
            },
            reason="test",
            confidence=0.9,
        )
        strategy = DiscoveryStrategyResult(mode=DiscoveryMode.DIRECT_CATALOG, initial_count=3)
        plan = DiscoveryPlan(output_kind="products", products=products)

        with (
            patch(
                "modules.ai.brain.commerce.discovery_strategy.strategy_from_decision_args",
                return_value=strategy,
            ),
            patch(
                "modules.ai.brain.catalog.catalog_provider.get_catalog_provider",
                return_value=MagicMock(),
            ),
            patch(
                "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
                return_value=[],
            ),
            patch(
                "modules.ai.brain.catalog.catalog_intelligence.CatalogIntelligence",
            ) as mock_intel_cls,
            patch(
                "modules.ai.brain.catalog.catalog_intelligence.attach_discovery_signals_from_db",
                side_effect=lambda rows, **_kw: list(rows),
            ),
            patch(
                "modules.ai.brain.catalog.presentation_contract.validate_discovery_products",
                side_effect=lambda rows: list(rows),
            ),
            patch(
                "modules.ai.brain.catalog.discovery_presenter.DiscoveryPresentationComposer",
            ) as mock_composer_cls,
        ):
            intel = mock_intel_cls.return_value
            intel.build_discovery_plan.return_value = plan
            intel.rank_products.side_effect = lambda rows, **_kw: list(rows)
            mock_composer_cls.return_value.compose.return_value = presentation

            return _apply_discovery_strategy(
                products,
                decision=decision,
                ctx=ctx,
                query="query",
                source="order_product_query",
            )

    def test_product_specific_discovery_payload_includes_product(self) -> None:
        product = _shoe_white()
        presentation = DiscoveryPresentationResult(
            text="list",
            output_kind="products",
            products=[product],
        )
        result = self._run_discovery(
            entry_type=PRODUCT_SPECIFIC,
            products=[product],
            presentation=presentation,
        )
        assert result is not None
        assert result.success is True
        assert result.data.get("product") is product
        assert product_focus_identity(result.data["product"]) == "SKU-WHITE"

    def test_multiple_products_payload_product_is_none(self) -> None:
        products = [_shoe_white(), _perfume_rose()]
        presentation = DiscoveryPresentationResult(
            text="list",
            output_kind="products",
            products=products,
        )
        result = self._run_discovery(
            entry_type=PRODUCT_SPECIFIC,
            products=products,
            presentation=presentation,
        )
        assert result is not None
        assert "product" in result.data
        assert result.data["product"] is None

    def test_category_browse_payload_product_is_none(self) -> None:
        product = _shirt_blue()
        presentation = DiscoveryPresentationResult(
            text="list",
            output_kind="products",
            products=[product],
        )
        result = self._run_discovery(
            entry_type=CATEGORY_BROWSE,
            products=[product],
            presentation=presentation,
        )
        assert result is not None
        assert result.data.get("product") is None

    def test_collections_payload_product_is_none(self) -> None:
        presentation = DiscoveryPresentationResult(
            text="collections",
            output_kind="collections",
            collections=[{"group_name": "أحذية", "product_count": 5}],
        )
        result = self._run_discovery(
            entry_type=PRODUCT_SPECIFIC,
            products=[],
            presentation=presentation,
        )
        assert result is not None
        assert result.data.get("product") is None


class TestPipelineFocusOwnerConsumesDiscoveryProduct:
    """Mirror pipeline focus-pin guard without duplicating ownership logic."""

    def test_search_products_result_pins_focus_via_owner(self) -> None:
        state = MerchantConversationState(greeted=True, stage="discovery", turn=4)
        product = _perfume_rose()
        result_data = {
            "products": [product],
            "product": product,
            "discovery_output_kind": "products",
        }
        decision_action = ACTION_SEARCH_PRODUCTS

        if result_data.get("product") and decision_action == ACTION_SEARCH_PRODUCTS:
            set_product_focus(
                state,
                result_data["product"],
                reason=f"executor_product_{decision_action}",
                turn=int(state.turn or 0),
            )

        assert state.current_product_focus is not None
        assert product_focus_identity(state.current_product_focus) == "SKU-PERF-ROSE"
        assert state.current_product_focus["id"] == "perf-rose-1"
