"""Catalog inquiry subject resolution + OOS fact focus export regressions."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.catalog_search_evidence import (  # noqa: E402
    apply_catalog_search_evidence_gate,
    has_catalog_search_evidence,
)
from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    product_focus_identity,
    set_product_focus,
)
from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: E402
    extract_inquiry_subject,
)
from modules.ai.brain.commerce.discovery_strategy import DiscoveryMode  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import (  # noqa: E402
    PRODUCT_SPECIFIC,
    extract_order_product_query,
    resolve_discovery_entry,
    route_discovery_entry,
)
from modules.ai.brain.execution.search import (  # noqa: E402
    ProductSearchHandler,
    resolve_search_result_product_for_focus,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    _normalize_ar,
    has_explicit_product_browse_intent,
    product_discovery_block_reason,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _facts(*, has_products: bool = True) -> CommerceFacts:
    return CommerceFacts(
        has_products=has_products,
        product_count=10 if has_products else 0,
        in_stock_count=10 if has_products else 0,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
    )


def _ctx(
    message: str,
    *,
    intent_name: str = "ask_product",
    slots: dict[str, Any] | None = None,
    tenant_id: int = 1,
    customer_phone: str = "966500000001",
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.9,
            raw_message=message,
            slots=dict(slots if slots is not None else {}),
            extraction_method="llm",
        ),
        state=MerchantConversationState(greeted=True, stage="exploring"),
        history=[],
        facts=_facts(),
    )


def _route_discovery_realistic(ctx: BrainContext, entry):
    return route_discovery_entry(
        ctx,
        entry,
        facts=ctx.facts,
        product_discovery_blocked=lambda source: (
            product_discovery_block_reason(ctx, source=source) is not None
        ),
        fulfillment_locked_fallback=lambda: None,
        block_stale_resume=lambda _wf: False,
        is_commerce_blocked=lambda _ctx: False,
    )


def _perfume_rose_fact() -> dict[str, Any]:
    return {
        "id": "perf-rose-oos-1",
        "external_id": "SKU-PERF-ROSE-OOS",
        "title": "عطر ورد 100ml",
        "price": 149,
        "in_stock": False,
        "can_checkout": False,
    }


def _shirt_blue_oos_fact() -> dict[str, Any]:
    return {
        "external_id": "sku-shirt-blue",
        "title": "قميص قطني أزرق",
        "price": 89,
        "in_stock": False,
        "can_checkout": False,
    }


def _shirt_blue_fact() -> dict[str, Any]:
    return {
        "product_id": "shirt-blue-oos-9",
        "title": "قميص قطني أزرق",
        "price": 89,
        "in_stock": False,
        "can_checkout": False,
    }


def _watch_silver_orderable() -> dict[str, Any]:
    return {
        "id": "watch-silver-1",
        "external_id": "SKU-WATCH-SILVER",
        "title": "ساعة فضية",
        "price": 299,
        "in_stock": True,
        "can_checkout": True,
    }


class TestDiscoveryEntryInquirySubjectPath:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
            ("عندكم عطر ورد 100ml؟", "عطر ورد 100ml"),
        ],
    )
    def test_extract_inquiry_subject_full_names(self, message: str, expected: str) -> None:
        assert _normalize_ar(extract_inquiry_subject(message) or "") == _normalize_ar(expected)

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
            ("عندكم عطر ورد 100ml؟", "عطر ورد 100ml"),
        ],
    )
    def test_extract_order_product_query_empty_slots(self, message: str, expected: str) -> None:
        ctx = _ctx(message)
        assert _normalize_ar(extract_order_product_query(ctx)) == _normalize_ar(expected)

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
        ],
    )
    def test_resolve_discovery_entry_product_specific(self, message: str, expected: str) -> None:
        ctx = _ctx(message)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        assert entry.entry_type == PRODUCT_SPECIFIC
        assert _normalize_ar(entry.query or "") == _normalize_ar(expected)

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
        ],
    )
    def test_product_discovery_not_blocked_for_order_product_query(
        self,
        message: str,
        expected: str,
    ) -> None:
        ctx = _ctx(message)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        assert has_explicit_product_browse_intent(ctx, source=entry.source) is True
        assert product_discovery_block_reason(ctx, source=entry.source) is None

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
        ],
    )
    def test_route_discovery_entry_search_with_full_query(
        self,
        message: str,
        expected: str,
    ) -> None:
        ctx = _ctx(message)
        entry = resolve_discovery_entry(ctx)
        decision = _route_discovery_realistic(ctx, entry)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert _normalize_ar(str(decision.args.get("query") or "")) == _normalize_ar(expected)

        gated = apply_catalog_search_evidence_gate(ctx, decision)
        assert gated.action == ACTION_SEARCH_PRODUCTS
        assert gated.args.get("topic") != "commerce_ambiguous"
        assert _normalize_ar(str(gated.args.get("query") or "")) == _normalize_ar(expected)

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
        ],
    )
    def test_decision_engine_routes_search_not_category_browse(
        self,
        message: str,
        expected: str,
    ) -> None:
        ctx = _ctx(message)
        decision = DefaultDecisionEngine().decide(ctx)
        gated = apply_catalog_search_evidence_gate(ctx, decision)
        assert gated.action == ACTION_SEARCH_PRODUCTS
        assert _normalize_ar(str(gated.args.get("query") or "")) == _normalize_ar(expected)
        assert gated.args.get("topic") != "commerce_ambiguous"
        assert "category price browse" not in str(decision.reason or "").lower()

    @pytest.mark.parametrize(
        "message,expect_browse_intent",
        [
            ("عندكم توصيل الرياض؟", False),
            ("أبغى الفروع", True),
        ],
    )
    def test_non_product_inquiry_empty_slots_not_product_specific(
        self,
        message: str,
        expect_browse_intent: bool,
    ) -> None:
        ctx = _ctx(message)
        assert extract_order_product_query(ctx) == ""
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is False
        assert entry.entry_type != PRODUCT_SPECIFIC
        assert has_explicit_product_browse_intent(ctx) is expect_browse_intent
        assert _route_discovery_realistic(ctx, entry) is None

    def test_store_wide_offers_not_catalog_product_query(self) -> None:
        ctx = _ctx("عندكم عروض؟")
        assert extract_order_product_query(ctx) == ""
        entry = resolve_discovery_entry(ctx)
        assert entry.entry_type != PRODUCT_SPECIFIC
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert (decision.args or {}).get("query") != "عروض"


class TestNonProductTopicsDoNotBecomeCatalogSearch:
    def test_shipping_geo_blocked_by_discovery_gate(self) -> None:
        ctx = _ctx("كم رسوم الشحن للرياض؟", intent_name="general")
        assert product_discovery_block_reason(ctx) in {"non_commerce", "logistics_context"}

    def test_logistics_context_blocked_without_product_browse(self) -> None:
        ctx = _ctx("معك مندوب سمسا", intent_name="general")
        assert product_discovery_block_reason(ctx) == "logistics_context"

    def test_geo_query_rejected_by_catalog_search_evidence_gate(self) -> None:
        ctx = _ctx("عندكم توصيل الرياض؟")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "رياض", "source": "order_product_query"},
            reason="test",
        )
        assert has_catalog_search_evidence(ctx, "رياض", decision) is False
        out = apply_catalog_search_evidence_gate(ctx, decision)
        assert out.action != ACTION_SEARCH_PRODUCTS


class TestResolveSearchResultProductForFocus:
    def test_singular_orderable_result_still_wins(self) -> None:
        product = _watch_silver_orderable()
        resolved = resolve_search_result_product_for_focus(
            products=[product],
            catalog_fact_products=[],
            query="ساعة فضية",
        )
        assert resolved is product
        assert product_focus_identity(resolved) == "SKU-WATCH-SILVER"

    def test_mixed_orderable_and_fact_returns_none(self) -> None:
        assert (
            resolve_search_result_product_for_focus(
                products=[_watch_silver_orderable()],
                catalog_fact_products=[_perfume_rose_fact()],
                query="ساعة فضية",
            )
            is None
        )

    def test_singular_oos_fact_with_specific_query_exports_product(self) -> None:
        fact = _shirt_blue_fact()
        resolved = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact],
            query="قميص قطني أزرق",
        )
        assert resolved is fact
        assert product_focus_identity(resolved) == "shirt-blue-oos-9"

    def test_singular_oos_fact_perfume_specific_query(self) -> None:
        fact = _perfume_rose_fact()
        resolved = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact],
            query="عطر ورد 100ml",
        )
        assert resolved is fact
        assert product_focus_identity(resolved) == "SKU-PERF-ROSE-OOS"

    def test_generic_one_token_category_with_singular_fact_returns_none(self) -> None:
        fact = {
            "id": "cat-perfume-1",
            "title": "عطر",
            "price": 99,
        }
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[fact],
                query="عطر",
            )
            is None
        )

    def test_generic_accessory_category_with_singular_fact_returns_none(self) -> None:
        fact = {
            "external_id": "SKU-BAG-1",
            "title": "حقيبة",
            "price": 120,
        }
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[fact],
                query="حقيبة",
            )
            is None
        )

    def test_multiple_facts_returns_none(self) -> None:
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[_perfume_rose_fact(), _shirt_blue_fact()],
                query="عطر ورد 100ml",
            )
            is None
        )

    def test_title_only_fact_without_canonical_id_returns_none(self) -> None:
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[{"title": "عطر ورد 100ml", "price": 149}],
                query="عطر ورد 100ml",
            )
            is None
        )

    def test_multiple_orderable_products_returns_none(self) -> None:
        assert (
            resolve_search_result_product_for_focus(
                products=[_watch_silver_orderable(), _watch_silver_orderable()],
                catalog_fact_products=[],
                query="ساعة",
            )
            is None
        )


class TestPipelineFocusOwnerIsolation:
    """Mirror pipeline focus-pin guard — tenant state stays isolated."""

    def test_search_result_product_pins_focus_per_tenant_state(self) -> None:
        tenant_a = MerchantConversationState(greeted=True, stage="discovery", turn=2)
        tenant_b = MerchantConversationState(greeted=True, stage="discovery", turn=2)
        fact_a = _perfume_rose_fact()
        fact_b = {
            "id": "tenant-b-perf",
            "external_id": "SKU-B-PERF",
            "title": "عطر ورد 100ml",
            "price": 159,
        }
        result_a = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact_a],
            query="عطر ورد 100ml",
        )
        result_b = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact_b],
            query="عطر ورد 100ml",
        )
        assert result_a is not None and result_b is not None
        set_product_focus(tenant_a, result_a, reason="executor_product_search", turn=2)
        set_product_focus(tenant_b, result_b, reason="executor_product_search", turn=2)
        assert product_focus_identity(tenant_a.current_product_focus) == "SKU-PERF-ROSE-OOS"
        assert product_focus_identity(tenant_b.current_product_focus) == "SKU-B-PERF"
        assert tenant_a.current_product_focus is not tenant_b.current_product_focus


class TestProductSearchHandlerOosFactFocus:
    def _availability_decision(self, *, query: str) -> Decision:
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": query,
                "source": "order_product_query",
                "discovery_mode": DiscoveryMode.DIRECT_CATALOG.value,
                "discovery_entry_type": PRODUCT_SPECIFIC,
                "question_kind": "availability",
            },
            reason="availability inquiry",
            confidence=0.9,
        )

    async def _run_handler(
        self,
        *,
        decision: Decision,
        ctx: BrainContext,
        runtime_payload: dict[str, Any],
    ):
        mock_runtime = MagicMock()
        mock_runtime.execute = AsyncMock(
            return_value=MagicMock(payload=runtime_payload),
        )

        with patch(
            "modules.ai.brain.execution.runtime_factory.build_commerce_runtime",
            return_value=mock_runtime,
        ):
            return await ProductSearchHandler().handle(decision, ctx)

    def test_oos_fact_skips_discovery_strategy_and_exports_focus(self) -> None:
        fact = _shirt_blue_oos_fact()
        ctx = _ctx("عندكم قميص قطني أزرق؟")
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = self._availability_decision(query="قميص قطني ازرق")

        with patch(
            "modules.ai.brain.execution.search._apply_discovery_strategy",
        ) as mock_strategy:
            mock_strategy.return_value = MagicMock(
                success=True,
                data={"discovery_output_kind": "empty", "products": []},
            )
            result = asyncio.run(
                self._run_handler(
                    decision=decision,
                    ctx=ctx,
                    runtime_payload={
                        "products": [],
                        "catalog_fact_products": [fact],
                    },
                )
            )

        mock_strategy.assert_not_called()
        assert result.success is True
        assert result.data.get("products") == []
        assert result.data.get("count") == 0
        assert result.data.get("catalog_fact_products") == [fact]
        assert result.data.get("product") is not None
        assert product_focus_identity(result.data["product"]) == "sku-shirt-blue"
        assert result.data.get("discovery_output_kind") is None
        assert result.data.get("product_lines") == ""

    def test_category_filter_drops_fact_orderable_invokes_discovery(self) -> None:
        fact = {
            "external_id": "sku-watch-matte-oos",
            "title": "ساعة فضية مطفية",
            "price": 279,
            "in_stock": False,
            "can_checkout": False,
        }
        orderable = {
            "id": "watch-silver-1",
            "external_id": "SKU-WATCH-SILVER",
            "title": "ساعة فضية كلاسيك",
            "price": 299,
            "in_stock": True,
            "can_checkout": True,
        }
        ctx = _ctx("عندكم ساعة فضية كلاسيك؟")
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = self._availability_decision(query="ساعة فضية كلاسيك")
        strategy_payload = {
            "products": [orderable],
            "product_lines": "presented",
            "count": 1,
            "query": "ساعة فضية كلاسيك",
            "discovery_output_kind": "products",
            "product": orderable,
        }

        def _scope_products(products, **_kw):
            rows = [dict(p) for p in products]
            if any(p.get("external_id") == "sku-watch-matte-oos" for p in rows):
                return []
            return rows

        with (
            patch(
                "modules.ai.brain.execution.search._apply_discovery_strategy",
                return_value=MagicMock(success=True, data=strategy_payload),
            ) as mock_strategy,
            patch(
                "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                side_effect=_scope_products,
            ),
        ):
            result = asyncio.run(
                self._run_handler(
                    decision=decision,
                    ctx=ctx,
                    runtime_payload={
                        "products": [orderable],
                        "catalog_fact_products": [fact],
                    },
                )
            )

        mock_strategy.assert_called_once()
        assert result.success is True
        assert result.data.get("discovery_output_kind") == "products"
        assert result.data.get("catalog_fact_products") is None
        assert result.data.get("product") is orderable

    def test_mixed_orderable_and_fact_preserves_facts_without_pin(self) -> None:
        fact = {
            "external_id": "sku-watch-matte-oos",
            "title": "ساعة فضية مطفية",
            "price": 279,
            "in_stock": False,
            "can_checkout": False,
        }
        orderable = {
            "id": "watch-silver-1",
            "external_id": "SKU-WATCH-SILVER",
            "title": "ساعة فضية كلاسيك",
            "price": 299,
            "in_stock": True,
            "can_checkout": True,
        }
        ctx = _ctx("عندكم ساعة فضية كلاسيك؟")
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = self._availability_decision(query="ساعة فضية كلاسيك")

        with (
            patch(
                "modules.ai.brain.execution.search._apply_discovery_strategy",
            ) as mock_strategy,
            patch(
                "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                side_effect=lambda products, **_kw: [dict(p) for p in products],
            ),
        ):
            result = asyncio.run(
                self._run_handler(
                    decision=decision,
                    ctx=ctx,
                    runtime_payload={
                        "products": [orderable],
                        "catalog_fact_products": [fact],
                    },
                )
            )

        mock_strategy.assert_not_called()
        assert result.success is True
        assert len(result.data.get("products") or []) == 1
        assert len(result.data.get("catalog_fact_products") or []) == 1
        assert result.data.get("product") is None

    def test_orderable_only_still_invokes_discovery_strategy(self) -> None:
        orderable = _watch_silver_orderable()
        ctx = _ctx("عندكم ساعة فضية؟")
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": "ساعة فضية",
                "source": "order_product_query",
                "discovery_mode": DiscoveryMode.DIRECT_CATALOG.value,
                "discovery_entry_type": PRODUCT_SPECIFIC,
            },
            reason="product search",
            confidence=0.9,
        )
        strategy_payload = {
            "products": [orderable],
            "product_lines": "presented",
            "count": 1,
            "query": "ساعة فضية",
            "discovery_output_kind": "products",
            "product": orderable,
        }

        with patch(
            "modules.ai.brain.execution.search._apply_discovery_strategy",
            return_value=MagicMock(success=True, data=strategy_payload),
        ) as mock_strategy:
            result = asyncio.run(
                self._run_handler(
                    decision=decision,
                    ctx=ctx,
                    runtime_payload={"products": [orderable]},
                )
            )

        mock_strategy.assert_called_once()
        assert result.success is True
        assert result.data.get("discovery_output_kind") == "products"
        assert result.data.get("product") is orderable


class TestPipelineOosFactFocusOwner:
    def test_singular_oos_fact_pins_sku_shirt_blue(self) -> None:
        state = MerchantConversationState(greeted=True, stage="discovery", turn=5)
        fact = _shirt_blue_oos_fact()
        result_data = {
            "products": [],
            "catalog_fact_products": [fact],
            "product": fact,
        }

        if result_data.get("product"):
            set_product_focus(
                state,
                result_data["product"],
                reason="executor_product_search",
                turn=int(state.turn or 0),
            )

        assert state.current_product_focus is not None
        assert product_focus_identity(state.current_product_focus) == "sku-shirt-blue"
        assert state.current_product_focus["in_stock"] is False
        assert state.current_product_focus["can_checkout"] is False
