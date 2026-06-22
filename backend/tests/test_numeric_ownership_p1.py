"""P1 numeric ownership — single source, hard guard, telemetry."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.numeric_ownership import (  # noqa: E402
    NUMERIC_OWNER_GROUP_PRODUCTS_PAGE,
    group_products_candidate_list,
    is_group_products_navigation_source,
    sync_group_products_single_source,
    try_group_products_numeric_guard_decision,
)
from modules.ai.brain.catalog.navigation import try_catalog_navigation_decision  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_CLARIFY, ACTION_PROPOSE_DRAFT_ORDER  # noqa: E402
from modules.ai.brain.catalog.product_pick import try_catalog_navigation_product_pick_decision  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

COLLECTIONS = [
    {"group_id": "g1", "group_name": "Cat A"},
    {"group_id": "g2", "group_name": "Cat B"},
]
PRODUCTS = [
    {"id": "p1", "external_id": "p1", "title": "Item A"},
    {"id": "p2", "external_id": "p2", "title": "Item B"},
]


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
    )


def _ctx(msg: str, *, state: MerchantConversationState, button_id: str = "") -> BrainContext:
    intent = rules.match(msg) or Intent(name="pick_list_item", confidence=0.97, raw_message=msg)
    profile = {"inbound_metadata": {"button_id": button_id, "button_provenance": button_id}} if button_id else {}
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000011",
        message=msg,
        intent=intent,
        state=state,
        facts=_facts(),
        profile=profile,
    )


class TestGroupProductsSingleSource:
    def test_sync_aligns_all_product_lists(self):
        state = MerchantConversationState(
            catalog_navigation_source="group_products",
            last_presented_group_products=list(PRODUCTS),
            last_search_candidates=[{"id": "stale", "title": "Stale"}],
            last_presented_products=[{"id": "stale2", "title": "Stale2"}],
        )
        sync_group_products_single_source(state)
        assert group_products_candidate_list(state) == PRODUCTS
        assert state.last_search_candidates == PRODUCTS
        assert state.last_presented_products == PRODUCTS


class TestGroupProductsHardGuard:
    def test_numeric_blocked_when_products_state_lost(self):
        state = MerchantConversationState(
            catalog_navigation_source="group_products",
            current_catalog_group={"group_id": "g1", "group_name": "Cat A"},
            last_presented_collections=list(COLLECTIONS),
            last_presented_group_products=[],
        )
        decision = try_group_products_numeric_guard_decision(_ctx("1", state=state))
        assert decision is not None
        assert decision.action == ACTION_CLARIFY

    def test_legacy_navigation_does_not_pick_collection(self):
        state = MerchantConversationState(
            catalog_navigation_source="group_products",
            current_catalog_group={"group_id": "g1", "group_name": "Cat A"},
            last_presented_collections=list(COLLECTIONS),
            last_presented_group_products=[],
            selection_context_turn=1,
            turn=2,
        )
        nav = try_catalog_navigation_decision(_ctx("1", state=state))
        assert nav is None

    def test_guard_before_pick_list_not_collection(self):
        state = MerchantConversationState(
            catalog_navigation_source="group_products",
            current_catalog_group={"group_id": "g1"},
            last_presented_collections=list(COLLECTIONS),
            last_presented_group_products=[],
        )
        guard = try_group_products_numeric_guard_decision(_ctx("2", state=state))
        assert guard is not None
        assert guard.action != ACTION_PROPOSE_DRAFT_ORDER


class TestProductPickStillWorks:
    def test_valid_group_product_pick_not_guarded(self):
        state = MerchantConversationState(
            catalog_navigation_source="group_products",
            current_catalog_group={"group_id": "g1", "group_name": "Cat A"},
            last_presented_group_products=list(PRODUCTS),
        )
        pick = try_catalog_navigation_product_pick_decision(_ctx("1", state=state, button_id="coll_1"))
        assert pick is not None
        assert pick.action == ACTION_PROPOSE_DRAFT_ORDER
        guard = try_group_products_numeric_guard_decision(_ctx("1", state=state))
        assert guard is None


class TestNumericOwnerResolution:
    def test_group_products_source_detected(self):
        state = MerchantConversationState(catalog_navigation_source="group_products")
        assert is_group_products_navigation_source(state) is True
