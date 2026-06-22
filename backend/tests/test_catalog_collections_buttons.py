"""CatalogNavigator collections buttons + pagination regression tests."""
from __future__ import annotations

import os
import sys
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.collections_pagination import (  # noqa: E402
    BUTTON_MORE_COLLECTIONS,
    BUTTON_MORE_PRODUCTS,
    BUTTON_START_COLLECTIONS,
    build_collection_quick_buttons,
    normalize_collections_page,
)
from modules.ai.brain.catalog.navigation import (  # noqa: E402
    PATH_GROUPS,
    PATH_GROUP_PRODUCTS,
    try_catalog_navigation_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

MSG_MORE = "\u0627\u0644\u0645\u0632\u064a\u062f"
MSG_START = "\u0627\u0644\u0628\u062f\u0627\u064a\u0629"

FIVE_COLLECTIONS = [
    {"group_id": f"grp-{i}", "group_name": f"Category {i}", "browse_rank": i}
    for i in range(1, 6)
]


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=20,
        in_stock_count=20,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
    )


def _ctx(msg: str, *, state: MerchantConversationState) -> BrainContext:
    intent = rules.match(msg) or Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=3,
        customer_phone="966500000099",
        message=msg,
        intent=intent,
        state=state,
        facts=_facts(),
    )


def _collections_state(
    *,
    offset: int = 0,
    page: list[dict[str, Any]] | None = None,
) -> MerchantConversationState:
    shown = page if page is not None else normalize_collections_page(
        FIVE_COLLECTIONS[offset: offset + 2],
        offset=offset,
    )
    return MerchantConversationState(
        greeted=True,
        stage="discovery",
        turn=4,
        catalog_navigation_source="groups",
        collections_pool=list(FIVE_COLLECTIONS),
        collections_offset=offset,
        collections_page_size=2,
        collections_next_available=(offset + 2) < len(FIVE_COLLECTIONS),
        last_presented_collections=shown,
        selection_context_turn=3,
    )


class TestCollectionButtons:
    def test_five_collections_buttons_are_top_two_plus_more(self):
        page = normalize_collections_page(FIVE_COLLECTIONS[:2], offset=0)
        buttons = build_collection_quick_buttons(
            page,
            collections_next_available=True,
        )
        assert len(buttons) == 3
        assert buttons[0]["reply"]["id"] == "coll_1"
        assert buttons[1]["reply"]["id"] == "coll_2"
        assert buttons[2]["reply"]["id"] == BUTTON_MORE_COLLECTIONS

    def test_two_collections_only_two_buttons(self):
        page = normalize_collections_page(FIVE_COLLECTIONS[:2], offset=0)
        buttons = build_collection_quick_buttons(
            page,
            collections_next_available=False,
        )
        assert len(buttons) == 2
        assert buttons[0]["reply"]["id"] == "coll_1"
        assert buttons[1]["reply"]["id"] == "coll_2"


class TestCollectionsPaginationRouting:
    def test_more_in_groups_shows_next_collections_page(self):
        decision = try_catalog_navigation_decision(
            _ctx(MSG_MORE, state=_collections_state(offset=0)),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args["chosen_path"] == PATH_GROUPS
        assert decision.args["navigator_step"] == "show_groups"
        assert decision.args["collections_offset"] == 2
        assert decision.args.get("reuse_collections_pool") is True

    def test_more_in_groups_at_end_shows_start_button_policy(self):
        state = _collections_state(offset=4, page=normalize_collections_page(FIVE_COLLECTIONS[4:5], offset=4))
        decision = try_catalog_navigation_decision(_ctx(MSG_MORE, state=state))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUPS
        assert decision.args.get("collections_at_end") is True

        page = normalize_collections_page(FIVE_COLLECTIONS[4:5], offset=4)
        buttons = build_collection_quick_buttons(
            page,
            collections_next_available=False,
            collections_at_end=True,
        )
        assert any(btn["reply"]["id"] == BUTTON_START_COLLECTIONS for btn in buttons)

    def test_page_two_pick_one_opens_third_collection(self):
        page = normalize_collections_page(FIVE_COLLECTIONS[2:4], offset=2)
        decision = try_catalog_navigation_decision(
            _ctx("1", state=_collections_state(offset=2, page=page)),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args["chosen_path"] == PATH_GROUP_PRODUCTS
        assert decision.args["catalog_group_id"] == "grp-3"

    def test_page_two_numeric_one_matches_current_page_not_first_global(self):
        page = normalize_collections_page(FIVE_COLLECTIONS[2:4], offset=2)
        decision = try_catalog_navigation_decision(
            _ctx("1", state=_collections_state(offset=2, page=page)),
        )
        assert decision is not None
        assert decision.args["catalog_group_id"] != "grp-1"
        assert decision.args["catalog_group_id"] == "grp-3"

    def test_more_in_group_products_not_groups(self):
        from modules.ai.brain.catalog.product_pick import has_active_group_products_context  # noqa: E402

        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            turn=8,
            catalog_navigation_source="group_products",
            current_catalog_group={"group_id": "grp-1", "group_name": "Category 1"},
            last_presented_group_products=[{"id": "p1", "title": "Item 1"}],
            group_products_pool=[{"id": "p1", "title": "Item 1"}, {"id": "p2", "title": "Item 2"}],
            group_products_offset=0,
            group_products_page_size=1,
            next_page_available=True,
            last_presented_collections=FIVE_COLLECTIONS[:2],
            collections_pool=list(FIVE_COLLECTIONS),
        )
        assert has_active_group_products_context(state)
        decision = try_catalog_navigation_decision(_ctx(MSG_MORE, state=state))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUP_PRODUCTS
        assert decision.args["navigator_step"] == "show_group_products"

    def test_more_in_groups_does_not_open_products(self):
        decision = try_catalog_navigation_decision(
            _ctx(MSG_MORE, state=_collections_state(offset=0)),
        )
        assert decision is not None
        assert decision.args["navigator_step"] == "show_groups"
        assert decision.args["chosen_path"] != PATH_GROUP_PRODUCTS

    def test_collection_button_pick_does_not_start_order(self):
        page = normalize_collections_page(FIVE_COLLECTIONS[:2], offset=0)
        decision = try_catalog_navigation_decision(
            _ctx("1", state=_collections_state(offset=0, page=page)),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_start_over_resets_collections_offset(self):
        decision = try_catalog_navigation_decision(
            _ctx(MSG_START, state=_collections_state(offset=2)),
        )
        assert decision is not None
        assert decision.args["collections_offset"] == 0

    def test_group_product_more_button_id_is_products_not_collections(self):
        page = normalize_collections_page(FIVE_COLLECTIONS[:2], offset=0)
        col_buttons = build_collection_quick_buttons(page, collections_next_available=True)
        assert BUTTON_MORE_PRODUCTS not in {b["reply"]["id"] for b in col_buttons}
        prod_buttons = [
            {"type": "reply", "reply": {"id": BUTTON_MORE_PRODUCTS, "title": "المزيد"}},
        ]
        assert prod_buttons[0]["reply"]["id"] == BUTTON_MORE_PRODUCTS
        assert prod_buttons[0]["reply"]["id"] != BUTTON_MORE_COLLECTIONS
