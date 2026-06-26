"""WhatsApp catalog_order must beat browse/search/discovery."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    is_current_catalog_order_submitted,
    maybe_enforce_catalog_order_continue_checkout,
    try_catalog_order_continue_decision,
)
from modules.ai.brain.commerce.catalog_product_grounding import (  # noqa: E402
    build_catalog_grounded_list_reply,
)
from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_bare_start_order_guard_reply,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import resolve_discovery_entry  # noqa: E402
from modules.ai.brain.product_discovery_gate import try_category_price_browse_decision  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _catalog_meta(**overrides: Any) -> Dict[str, Any]:
    base = {
        "source_type": "catalog_order",
        "product_items": [
            {
                "product_retailer_id": "86bqzca62a",
                "quantity": 2,
                "item_price": 159.5,
                "currency": "SAR",
            },
        ],
        "product_names": ["1 كيلو عسل سمر"],
        "total_price": 319.0,
        "currency": "SAR",
        "item_count": 1,
        "total_quantity": 2,
    }
    base.update(overrides)
    return base


def _catalog_message() -> str:
    return (
        "[طلب كتالوج من العميل]\n"
        "عدد أسطر الطلب: 2\n"
        "إجمالي الكمية: 2\n"
        "الإجمالي: 319 SAR\n"
        "رمز المنتج (SKU): 86bqzca62a\n"
        "ملاحظة: العميل أرسل طلبًا من كتالوج واتساب."
    )


def _catalog_ctx(*, prep: OrderPreparationState | None = None) -> BrainContext:
    state = MerchantConversationState(stage="ordering", turn=2)
    if prep is not None:
        state.order_prep = prep
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        message=_catalog_message(),
        intent=Intent(name="start_order", confidence=0.9, raw_message=_catalog_message()),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": _catalog_meta()},
    )


class TestCatalogOrderPriority:
    def test_catalog_order_takes_priority_over_category_browse(self) -> None:
        ctx = _catalog_ctx()
        with patch(
            "modules.ai.brain.commerce.commerce_browse_category_guard"
            ".extract_browse_category_scope",
            return_value="عسل",
        ):
            assert try_category_price_browse_decision(ctx) is None

    def test_catalog_order_takes_priority_over_product_discovery(self) -> None:
        ctx = _catalog_ctx()
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is False
        assert entry.reason == "catalog_order"

    def test_catalog_order_after_category_browse_patch_asks_missing_city_not_products(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        prep = OrderPreparationState(
            catalog_line_items_authoritative=True,
            catalog_checkout_total=319.0,
            catalog_checkout_currency="SAR",
            missing_fields=["city"],
            line_items=[
                {
                    "product_retailer_id": "86bqzca62a",
                    "quantity": 2,
                    "from_native_catalog_order": True,
                },
            ],
        )
        ctx = _catalog_ctx(prep=prep)
        decision = try_catalog_order_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("continue_checkout") is True
        assert decision.args.get("skip_product_discovery") is True

    def test_catalog_order_does_not_emit_available_products_list(self) -> None:
        ctx = _catalog_ctx()
        decision = try_catalog_order_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        list_reply = build_catalog_grounded_list_reply(
            ["عكبر سائل", "1 كيلو عسل سمر", "كريم سم النحل"],
            greeting="حاضر، أي نوع يناسبك؟",
        )
        assert "المتوفر حاليًا عندنا" in list_reply
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_quick_whatsapp_order_prompts_catalog_selection_not_free_text_product(self) -> None:
        reply = build_bare_start_order_guard_reply("ابي اطلب")
        assert "كتالوج واتساب" in reply
        assert "وش المنتج" not in reply

    def test_catalog_order_preserves_line_items_total_and_routes_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx()
        enforced = maybe_enforce_catalog_order_continue_checkout(
            ctx,
            Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "", "source": "top_products"}),
        )
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER
        product = enforced.args.get("product") or {}
        assert product.get("line_items_count") == 1
        assert product.get("price") == 319.0
        assert product.get("product_retailer_id") == "86bqzca62a"

    @patch(
        "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
        return_value=False,
    )
    @patch(
        "modules.ai.brain.commerce.commerce_browse_category_guard"
        ".is_category_price_or_availability_message",
        return_value=True,
    )
    @patch(
        "modules.ai.brain.commerce.commerce_browse_category_guard"
        ".extract_browse_category_scope",
        return_value="عسل",
    )
    @patch(
        "modules.ai.brain.catalog.catalog_browse_scope_resolver.resolve_catalog_category_scope",
    )
    def test_category_price_browse_still_works_without_catalog_order(
        self,
        mock_scope: MagicMock,
        _mock_extract: MagicMock,
        _mock_shape: MagicMock,
        _mock_catalog: MagicMock,
    ) -> None:
        from modules.ai.brain.catalog.catalog_browse_scope_resolver import (  # noqa: PLC0415
            CatalogCategoryScope,
        )

        mock_scope.return_value = CatalogCategoryScope(
            intent="category_price_browse",
            matched_category="عسل",
            query_subject="عسل",
            must_filter_by_category=True,
            specific_product=False,
        )
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message="اسعار العسل",
            intent=Intent(name="ask_price", confidence=0.9, raw_message="اسعار العسل"),
            state=MerchantConversationState(),
            facts=CommerceFacts(has_products=True, orderable=True),
            profile={"inbound_metadata": {}},
        )
        decision = try_category_price_browse_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == "category_browse"

    def test_is_current_catalog_order_submitted_from_message_marker(self) -> None:
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message=_catalog_message(),
            intent=Intent(name="general", confidence=0.5, raw_message=_catalog_message()),
            state=MerchantConversationState(),
            facts=CommerceFacts(),
            profile={"inbound_metadata": _catalog_meta()},
        )
        assert is_current_catalog_order_submitted(ctx) is True

    def test_decision_engine_prefers_catalog_order_before_discovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx(
            prep=OrderPreparationState(
                missing_fields=["city"],
                catalog_checkout_total=319.0,
                line_items=[{"product_retailer_id": "86bqzca62a", "quantity": 2}],
            ),
        )
        eng = DefaultDecisionEngine()
        decision = eng.decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("catalog_order_submitted") is True
