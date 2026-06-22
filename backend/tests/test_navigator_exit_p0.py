"""P0 — Navigator exit on order handoff regression tests."""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.navigation import try_catalog_navigation_decision  # noqa: E402
from modules.ai.brain.catalog.navigator_exit import (  # noqa: E402
    EXIT_REASON_ORDER_HANDOFF,
    clear_navigator_state_for_order_handoff,
    is_catalog_navigation_order_handoff_decision,
    navigator_should_yield_to_order_flow,
)
from modules.ai.brain.catalog.product_pick import (  # noqa: E402
    try_catalog_navigation_product_pick_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
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

GROUP_A = {"group_id": "grp-a", "group_slug": "grp-a", "group_name": "Category A"}

PAGE_ONE = [
    {
        "id": "101",
        "external_id": "101",
        "title": "Product Alpha 1kg",
        "display_label": "Product Alpha 1kg",
        "variant_id": "v-101",
        "price": 100,
        "list_index": 1,
    },
    {
        "id": "102",
        "external_id": "102",
        "title": "Product Beta 500g",
        "display_label": "Product Beta 500g",
        "variant_id": "v-102",
        "price": 80,
        "list_index": 2,
    },
]

FOCUS = {
    "id": "101",
    "external_id": "101",
    "title": "Product Alpha 1kg",
    "variant_id": "v-101",
}


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=20,
        in_stock_count=20,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
        top_products=PAGE_ONE,
    )


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState,
    intent_name: str = "",
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None and intent_name:
        intent = Intent(name=intent_name, confidence=0.9, raw_message=msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=11,
        customer_phone="966500000099",
        message=msg,
        intent=intent,
        state=state,
        facts=_facts(),
    )


def _stale_navigator_order_state(**overrides: Any) -> MerchantConversationState:
    """Simulates post-navigator-pick state before exit clears browse fields."""
    state = MerchantConversationState(
        greeted=True,
        stage="ordering",
        turn=9,
        current_product_focus=dict(FOCUS),
        order_prep=OrderPreparationState(
            product_id="101",
            quantity=1,
            product_options_loaded=True,
            product_has_required_options=True,
        ),
        catalog_navigation_source="group_products",
        current_catalog_group=dict(GROUP_A),
        last_presented_group_products=list(PAGE_ONE),
        group_products_pool=list(PAGE_ONE),
        group_products_offset=0,
        group_products_page_size=2,
        next_page_available=True,
        selected_collection="grp-a",
        last_search_candidates=list(PAGE_ONE),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class TestNavigatorExitOnHandoff:
    def test_handoff_decision_detection(self):
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={"source": "catalog_navigation_product_pick"},
            reason="test",
            confidence=0.9,
        )
        assert is_catalog_navigation_order_handoff_decision(decision) is True

    def test_clear_navigator_state_on_handoff(self):
        state = _stale_navigator_order_state()
        clear_navigator_state_for_order_handoff(state, tenant_id=11)
        assert state.catalog_navigation_source == ""
        assert state.current_catalog_group is None
        assert state.last_presented_group_products == []
        assert state.last_search_candidates == []
        assert navigator_should_yield_to_order_flow(state) is True


class TestNavigatorYieldDuringOrderFlow:
    def _assert_not_navigator(self, decision: Decision) -> None:
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("source") != "catalog_navigation_product_pick"

    def test_quantity_after_navigator_pick_reaches_order_flow(self):
        state = _stale_navigator_order_state()
        with patch(
            "modules.ai.brain.catalog.navigation.try_catalog_navigation_decision",
            side_effect=AssertionError("navigator must yield during ordering"),
        ):
            decision = DefaultDecisionEngine().decide(_ctx("2", state=state))
        self._assert_not_navigator(decision)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_option_selection_after_navigator_pick_reaches_order_flow(self):
        state = _stale_navigator_order_state(
            pending_option_groups=["Size"],
            order_prep=OrderPreparationState(
                product_id="101",
                quantity=1,
                product_options_loaded=True,
                product_has_required_options=True,
                product_options_meta=[
                    {
                        "id": 1,
                        "name": "Size",
                        "values": [{"id": 11, "name": "1kg"}, {"id": 12, "name": "500g"}],
                    }
                ],
            ),
        )
        decision = DefaultDecisionEngine().decide(_ctx("1", state=state, intent_name="pick_list_item"))
        self._assert_not_navigator(decision)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_numeric_variant_choice_after_navigator_pick_reaches_order_flow(self):
        state = _stale_navigator_order_state(
            order_prep=OrderPreparationState(
                product_id="101",
                quantity=1,
                awaiting_variant_choice=True,
                pending_variant_product_id="101",
            ),
        )
        decision = DefaultDecisionEngine().decide(_ctx("2", state=state))
        self._assert_not_navigator(decision)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("variant_pick") is not None

    def test_more_during_ordering_does_not_paginate(self):
        state = _stale_navigator_order_state()
        nav = try_catalog_navigation_decision(_ctx(MSG_MORE, state=state))
        assert nav is None
        decision = DefaultDecisionEngine().decide(_ctx(MSG_MORE, state=state))
        assert decision.action != ACTION_CATALOG_NAVIGATE

    def test_more_during_checkout_does_not_paginate(self):
        state = _stale_navigator_order_state(
            stage="checkout",
            checkout_url="https://checkout.example/order/1",
            order_prep=OrderPreparationState(
                product_id="101",
                quantity=2,
                customer_first_name="Ali",
                city="Riyadh",
            ),
        )
        pick = try_catalog_navigation_product_pick_decision(_ctx("1", state=state))
        assert pick is None
        nav = try_catalog_navigation_decision(_ctx(MSG_MORE, state=state))
        assert nav is None
        decision = DefaultDecisionEngine().decide(_ctx(MSG_MORE, state=state))
        assert decision.action != ACTION_CATALOG_NAVIGATE


class TestNavigatorExitTelemetryConstant:
    def test_exit_reason_constant(self):
        assert EXIT_REASON_ORDER_HANDOFF == "order_handoff"
