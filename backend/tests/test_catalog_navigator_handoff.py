"""CatalogNavigator product handoff + group pagination regression tests."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.navigation import (  # noqa: E402
    PATH_GROUPS,
    PATH_GROUP_PRODUCTS,
    try_catalog_navigation_decision,
)
from modules.ai.brain.catalog.product_pick import (  # noqa: E402
    CANDIDATE_SOURCE,
    build_rich_forced_product,
    try_catalog_navigation_product_pick_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.catalog_navigate import CatalogNavigateHandler  # noqa: E402
from modules.ai.brain.execution.orders import (  # noqa: E402
    DraftOrderHandler,
    _maybe_prefill_navigator_product_options,
    _missing_product_options,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

MSG_MORE = "\u0627\u0644\u0645\u0632\u064a\u062f"
MSG_BACK = "\u0631\u062c\u0639\u0646\u064a \u0644\u0644\u0623\u0642\u0633\u0627\u0645"

COLLECTIONS = [
    {"group_id": "grp-a", "group_name": "Category A", "browse_rank": 1},
    {"group_id": "grp-b", "group_name": "Category B", "browse_rank": 2},
]

GROUP_A = {"group_id": "grp-a", "group_slug": "grp-a", "group_name": "Category A"}

PAGE_ONE = [
    {
        "id": "101",
        "external_id": "101",
        "title": "Product Alpha 1kg",
        "display_label": "Product Alpha 1kg",
        "variant_id": "v-101",
        "size": "1kg",
        "weight": "1",
        "unit": "kg",
        "price": 100,
        "sale_price": 90,
        "list_index": 1,
    },
    {
        "id": "102",
        "external_id": "102",
        "title": "Product Beta 500g",
        "display_label": "Product Beta 500g",
        "variant_id": "v-102",
        "size": "500g",
        "price": 80,
        "list_index": 2,
    },
]

PAGE_TWO = [
    {
        "id": "103",
        "external_id": "103",
        "title": "Product Gamma 2kg",
        "display_label": "Product Gamma 2kg",
        "variant_id": "v-103",
        "size": "2kg",
        "price": 150,
        "list_index": 1,
    },
]

FULL_POOL = PAGE_ONE + PAGE_TWO + [
    {"id": "104", "external_id": "104", "title": "Product Delta", "price": 60},
]


def _facts(*, product_count: int = 20) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=product_count,
        in_stock_count=product_count,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
        top_products=PAGE_ONE,
    )


def _ctx(msg: str, *, state: MerchantConversationState | None = None, db: Any = None) -> BrainContext:
    intent = rules.match(msg) or Intent(name="general", confidence=0.5, raw_message=msg)
    ctx = BrainContext(
        tenant_id=11,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery", turn=4),
        facts=_facts(),
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _group_products_state(**overrides: Any) -> MerchantConversationState:
    state = MerchantConversationState(
        greeted=True,
        stage="discovery",
        turn=6,
        last_presented_collections=list(COLLECTIONS),
        current_catalog_group=dict(GROUP_A),
        last_presented_group_products=list(PAGE_ONE),
        group_products_pool=list(FULL_POOL),
        group_products_offset=0,
        group_products_page_size=2,
        next_page_available=True,
        selected_collection="grp-a",
        catalog_navigation_source="group_products",
        selection_context_turn=5,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class TestCatalogNavigationProductPick:
    def test_numeric_pick_uses_catalog_navigation_source(self):
        decision = try_catalog_navigation_product_pick_decision(_ctx("1", state=_group_products_state()))
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args["candidate_source"] == CANDIDATE_SOURCE
        assert decision.args["source"] == "catalog_navigation_product_pick"
        assert decision.args["forced_product"]["candidate_source"] == CANDIDATE_SOURCE

    def test_rich_forced_product_carries_variant_size_price_group(self):
        forced = build_rich_forced_product(
            PAGE_ONE[0],
            selected_index=1,
            source_group=GROUP_A,
        )
        assert forced["external_id"] == "101"
        assert forced["variant_id"] == "v-101"
        assert forced["size"] == "1kg"
        assert forced["price"] == 100
        assert forced["sale_price"] == 90
        assert forced["source_group"]["group_id"] == "grp-a"
        assert forced["selected_index"] == 1

    def test_out_of_page_index_does_not_pick(self):
        decision = try_catalog_navigation_product_pick_decision(_ctx("9", state=_group_products_state()))
        assert decision is None

    def test_engine_prefers_navigator_pick_over_list_pick(self):
        state = _group_products_state()
        state.last_search_candidates = [{"id": "x", "external_id": "x", "title": "Wrong Product"}]
        with patch("modules.ai.brain.catalog.navigation.try_catalog_navigation_decision", return_value=None):
            decision = DefaultDecisionEngine().decide(_ctx("1", state=state))
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("candidate_source") == CANDIDATE_SOURCE
        assert decision.args.get("source") == "catalog_navigation_product_pick"


class TestDraftOrderNavigatorPrefill:
    def test_does_not_ask_size_when_variant_known(self):
        prep = OrderPreparationState(
            product_options_loaded=True,
            product_has_required_options=True,
            product_options_meta=[
                {
                    "id": 1,
                    "name": "Size",
                    "values": [{"id": 11, "name": "1kg"}, {"id": 12, "name": "500g"}],
                }
            ],
            product_variants_raw=[
                {
                    "id": "v-101",
                    "related_options": [1],
                    "related_option_values": [11],
                }
            ],
        )
        product = build_rich_forced_product(PAGE_ONE[0], selected_index=1, source_group=GROUP_A)
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={"source": "catalog_navigation_product_pick", "forced_product": product},
            reason="test",
            confidence=0.9,
        )
        _maybe_prefill_navigator_product_options(prep, product, decision)
        assert _missing_product_options(prep) == []


class TestCatalogNavigationPagination:
    def test_more_shows_next_slice_same_group(self):
        state = _group_products_state()
        decision = try_catalog_navigation_decision(_ctx(MSG_MORE, state=state))
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args["chosen_path"] == PATH_GROUP_PRODUCTS
        assert decision.args["group_products_offset"] == 2
        assert decision.args["reuse_group_pool"] is True

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_more_at_end_returns_groups(self, _mock_groups):
        state = _group_products_state(
            group_products_offset=2,
            group_products_page_size=2,
            next_page_available=False,
            last_presented_group_products=PAGE_TWO,
        )
        decision = try_catalog_navigation_decision(_ctx(MSG_MORE, state=state))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUPS

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_back_to_groups_does_not_touch_checkout(self, _mock_groups):
        state = _group_products_state()
        state.order_prep = OrderPreparationState(
            customer_first_name="Ali",
            city="Riyadh",
            product_id="101",
        )
        checkout_before = state.order_prep.to_dict()
        decision = try_catalog_navigation_decision(_ctx(MSG_BACK, state=state))
        assert decision is None
        assert state.order_prep.to_dict() == checkout_before

    def test_reselect_group_resets_offset(self):
        collections_state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            turn=7,
            last_presented_collections=list(COLLECTIONS),
            group_products_offset=2,
            group_products_page_size=2,
            catalog_navigation_source="groups",
        )
        decision = try_catalog_navigation_decision(_ctx("1", state=collections_state))
        assert decision is not None
        patch = decision.args.get("navigation_state_patch") or {}
        assert patch.get("group_products_offset") == 0
        assert patch.get("group_products_pool") == []

    def test_handler_persists_pool_and_offset(self):
        handler = CatalogNavigateHandler()
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "show_group_products",
                "chosen_path": PATH_GROUP_PRODUCTS,
                "owner_step": "group_products",
                "query": "Category A",
                "catalog_group_id": "grp-a",
                "group_products_offset": 0,
                "navigation_state_patch": {"current_catalog_group": GROUP_A},
            },
            reason="test",
            confidence=0.9,
        )
        ctx = _ctx("1", db=MagicMock())
        payload = {
            "discovery_presentation_text": "products page",
            "product_lines": "products page",
            "discovery_output_kind": "products",
            "products": PAGE_ONE,
            "chosen_path": PATH_GROUP_PRODUCTS,
            "next_page_available": True,
            "navigation_state_patch": {
                "group_products_pool": FULL_POOL,
                "group_products_offset": 0,
                "group_products_page_size": 2,
                "next_page_available": True,
                "last_presented_group_products": PAGE_ONE,
                "current_catalog_group": GROUP_A,
                "catalog_navigation_source": "group_products",
            },
        }
        with patch.object(handler, "_render_group_products", new=AsyncMock(return_value=payload)):
            result = asyncio.run(handler.handle(decision, ctx))
        nav_patch = result.data.get("navigation_state_patch") or {}
        assert len(nav_patch.get("group_products_pool") or []) == len(FULL_POOL)
        assert nav_patch.get("group_products_offset") == 0
        assert nav_patch.get("next_page_available") is True
