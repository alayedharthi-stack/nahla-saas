# -*- coding: utf-8 -*-
"""Discovery pick_N → Product Selection (not Draft Order / Checkout)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.search import ProductSearchHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.stages import STAGE_EXPLORING, STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

JACKET = {
    "id": 28,
    "external_id": "1921568272",
    "title": "جاكيت",
    "can_checkout": True,
    "orderable": True,
    "in_stock": True,
    "product_url": "https://store.example/products/jacket",
    "image_url": "https://cdn.example/jacket.jpg",
}
SHOE = {
    "id": 11,
    "external_id": "shoe-1",
    "title": "حذاء رياضي أبيض",
    "can_checkout": True,
    "orderable": True,
    "in_stock": True,
    "product_url": "https://store.example/products/shoe",
    "image_url": "https://cdn.example/shoe.jpg",
}


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=2,
        in_stock_count=2,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
    )


def _ctx(
    message: str,
    *,
    candidates: list | None = None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    intent = rules.match(message) or Intent(
        name="pick_list_item",
        confidence=0.97,
        slots={"list_index": int(message.strip() or "1")},
        raw_message=message,
    )
    st = state or MerchantConversationState(stage=STAGE_EXPLORING, turn=2)
    if candidates is not None:
        st.last_search_candidates = list(candidates)
    return BrainContext(
        tenant_id=42,
        customer_phone="966555000001",
        message=message,
        intent=intent,
        state=st,
        facts=_facts(),
        profile={},
    )


class TestDiscoveryPickProductSelection:
    def test_single_product_pick_is_selection_not_draft_order(self):
        decision = DefaultDecisionEngine().decide(_ctx("1", candidates=[JACKET]))
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("source") == "product_selection_list_pick"
        assert decision.args.get("selected_product", {}).get("external_id") == JACKET["external_id"]
        assert "no checkout" in (decision.reason or "").lower()

    def test_multi_product_pick_resolves_correct_candidate(self):
        decision = DefaultDecisionEngine().decide(
            _ctx("2", candidates=[JACKET, SHOE]),
        )
        assert decision.action == ACTION_SEARCH_PRODUCTS
        selected = decision.args.get("selected_product") or {}
        assert selected.get("external_id") == SHOE["external_id"]
        assert selected.get("title") == SHOE["title"]
        assert decision.args.get("list_index") == 2

    def test_product_switch_pick_binds_new_candidate(self):
        state = MerchantConversationState(stage=STAGE_EXPLORING, turn=4)
        state.current_product_focus = dict(JACKET)
        state.last_search_candidates = [JACKET, SHOE]
        decision = DefaultDecisionEngine().decide(_ctx("2", state=state))
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("selected_product", {}).get("external_id") == SHOE["external_id"]

    def test_pending_options_still_continue_checkout(self):
        state = MerchantConversationState(stage=STAGE_ORDERING, turn=5)
        state.current_product_focus = dict(JACKET)
        state.pending_option_groups = ["size"]
        state.last_search_candidates = []
        decision = DefaultDecisionEngine().decide(_ctx("1", state=state))
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_active_checkout_digit_continues_order_without_list(self):
        state = MerchantConversationState(stage=STAGE_ORDERING, turn=6)
        state.current_product_focus = dict(JACKET)
        state.draft_order_id = "draft-99"
        prep = OrderPreparationState()
        prep.product_id = JACKET["external_id"]
        prep.missing_fields = ["address_location"]
        state.order_prep = prep
        state.last_search_candidates = []
        decision = DefaultDecisionEngine().decide(_ctx("1", state=state))
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_focus_alone_without_checkout_does_not_force_draft(self):
        state = MerchantConversationState(stage=STAGE_EXPLORING, turn=3)
        state.current_product_focus = dict(JACKET)
        state.last_search_candidates = []
        decision = DefaultDecisionEngine().decide(_ctx("1", state=state))
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER


class TestProductSelectionListPickExecutor:
    def test_executor_returns_product_without_browse_list(self):
        import asyncio

        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "source": "product_selection_list_pick",
                "selected_product": JACKET,
                "products": [JACKET],
                "query": JACKET["title"],
                "list_index": 1,
            },
            reason="test",
            confidence=0.95,
        )
        ctx = _ctx("1", candidates=[JACKET])
        result = asyncio.run(ProductSearchHandler().handle(decision, ctx))
        assert result.success is True
        assert result.data.get("product", {}).get("external_id") == JACKET["external_id"]
        assert result.data.get("products") == []
        assert result.data.get("product_selection") is True


class TestProductUrlProjection:
    def test_format_projects_product_url_from_metadata(self):
        from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

        product = SimpleNamespace(
            id=28,
            external_id="1921568272",
            title="جاكيت",
            sku="",
            description="",
            price=120,
            stock_quantity=3,
            in_stock=True,
            has_variants=False,
            default_variant_id=None,
            variants=[],
            extra_metadata={
                "status": "active",
                "image_url": "https://cdn.example/jacket.jpg",
                "product_url": "https://store.example/products/jacket",
                "stock_qty": 3,
                "in_stock": True,
            },
        )
        builder = CatalogContextBuilder.__new__(CatalogContextBuilder)
        formatted = CatalogContextBuilder._format(builder, product)
        assert formatted.get("product_url") == "https://store.example/products/jacket"
        assert formatted.get("image_url") == "https://cdn.example/jacket.jpg"

    def test_format_empty_product_url_when_missing(self):
        from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

        product = SimpleNamespace(
            id=7,
            external_id="x",
            title="قميص قطني أزرق",
            sku="",
            description="",
            price=80,
            stock_quantity=1,
            in_stock=True,
            has_variants=False,
            default_variant_id=None,
            variants=[],
            extra_metadata={"status": "active", "image_url": "", "in_stock": True},
        )
        builder = CatalogContextBuilder.__new__(CatalogContextBuilder)
        formatted = CatalogContextBuilder._format(builder, product)
        assert formatted.get("product_url") == ""
