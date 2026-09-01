"""AGENT2-D1 — latest explicit catalog referent owns conversational product focus.

Generic Product A / Product B fixtures only. Asserts ownership and persistence,
not Arabic phrasing.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: E402
    apply_turn_catalog_referent_binding,
    current_turn_executor_catalog_referent,
    stamp_structured_presented_products,
    structured_product_from_turn,
    structured_selected_referent,
)
from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    bind_structured_catalog_referent,
    bind_variant_to_focus,
    canonical_product_referent,
    product_focus_identity,
    revert_to_previous_product_focus,
    search_results_are_new_customer_product_goal,
    should_keep_live_order_focus_after_product_list,
    should_preserve_focus_after_product_list_display,
)
from modules.ai.brain.types import (  # noqa: E402
    MerchantConversationState,
    OrderPreparationState,
)

PRODUCT_A = {
    "id": 801,
    "external_id": "sku-white-sneaker",
    "title": "حذاء رياضي أبيض",
    "price": 189,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
}
PRODUCT_B = {
    "id": 802,
    "external_id": "sku-blue-shirt",
    "title": "قميص قطني أزرق",
    "price": 120,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
}
PRODUCT_C = {
    "id": 803,
    "external_id": "sku-rose-100",
    "title": "عطر ورد 100ml",
    "price": 240,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
}


def _checkout_selected_state(product: dict, *, submitted: bool = False) -> MerchantConversationState:
    selected = dict(product)
    selected["customer_selected"] = True
    selected["from_catalog_order"] = True
    selected["provenance"] = "catalog_order_selected"
    prep = OrderPreparationState(
        product_id=str(product["external_id"]),
        missing_fields=["city"],
        order_status="awaiting_address",
    )
    if submitted:
        prep.order_creation_status = "created"
        prep.salla_order_id = "SALLA-ORDER-A-99"
        prep.order_status = "under_review"
    state = MerchantConversationState(
        stage="ordering",
        turn=6,
        current_product_focus=dict(selected),
        order_prep=prep,
        draft_order_id="draft-a-1",
        selected_variant={
            "variant_id": "var-a-42",
            "variant_label": "كبير",
            "price": product["price"],
            "product_id": product["id"],
        },
    )
    stamp_structured_presented_products(
        state,
        [selected],
        provenance="catalog_order_selected",
        customer_selected=True,
        turn=2,
    )
    bind_structured_catalog_referent(
        state,
        selected,
        reason="catalog_order_selected",
        turn=2,
        customer_selected=True,
    )
    return state


class TestAgent2D1CurrentTurnProductSupersedesStaleCheckout:
    def test_resolved_product_b_owns_focus_while_prep_stays(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        bound = bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="structured_turn_product",
            turn=9,
            current_turn_customer_referent=True,
        )
        assert bound is not None
        assert product_focus_identity(state.current_product_focus) == "sku-blue-shirt"
        assert canonical_product_referent(state)["id"] == 802
        assert product_focus_identity(state.previous_product_focus) == "sku-white-sneaker"
        assert state.order_prep.product_id == "sku-white-sneaker"
        assert state.draft_order_id == "draft-a-1"

    def test_apply_turn_binding_promotes_structured_product_b(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        apply_turn_catalog_referent_binding(
            state=state,
            reply="",
            catalog_candidates=[dict(PRODUCT_A), dict(PRODUCT_B)],
            turn=9,
            structured_product=dict(PRODUCT_B),
        )
        assert canonical_product_referent(state)["id"] == 802
        assert product_focus_identity(state.current_product_focus) == "sku-blue-shirt"

    def test_recommended_product_argument_does_not_steal_checkout_selected(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        apply_turn_catalog_referent_binding(
            state=state,
            reply="",
            catalog_candidates=[dict(PRODUCT_A), dict(PRODUCT_B)],
            turn=9,
            structured_product=dict(PRODUCT_B),
            current_turn_customer_referent=False,
        )
        assert product_focus_identity(state.current_product_focus) == "sku-white-sneaker"
        assert canonical_product_referent(state, checkout_active=True)["id"] == 801


class TestAgent2D1AssistantRecommendationDoesNotOutrankLaterCustomerGoal:
    def test_assistant_recommended_a_then_current_turn_b_wins(self) -> None:
        state = MerchantConversationState(turn=3)
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_A),
            reason="assistant_recommended_structured",
            turn=3,
        )
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="structured_turn_product",
            turn=4,
            current_turn_customer_referent=True,
        )
        assert product_focus_identity(state.current_product_focus) == "sku-blue-shirt"

    def test_family2_assistant_recommendation_does_not_steal_checkout_selected(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        bound = bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="assistant_recommended_structured",
            turn=5,
        )
        assert product_focus_identity(bound) == "sku-white-sneaker"
        assert product_focus_identity(state.current_product_focus) == "sku-white-sneaker"
        assert canonical_product_referent(state, checkout_active=True)["id"] == 801


class TestAgent2D1StaleVariantDoesNotHijackNewProduct:
    def test_variant_cleared_when_product_identity_changes(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        bind_variant_to_focus(
            state,
            {"variant_id": "var-a-42", "variant_label": "كبير", "price": 189},
        )
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="structured_turn_product",
            turn=9,
            current_turn_customer_referent=True,
        )
        assert product_focus_identity(state.current_product_focus) == "sku-blue-shirt"
        assert not state.selected_variant
        assert state.current_product_focus.get("variant_id") in (None, "")
        assert state.current_product_focus.get("variant_label") in (None, "")


class TestAgent2D1CheckoutContinuationKeepsProductA:
    def test_city_address_slots_do_not_switch_product(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        state.order_prep.city = "الرياض"
        state.order_prep.short_address_code = "RRRD1234"
        state.order_prep.missing_fields = ["payment_method"]
        assert product_focus_identity(state.current_product_focus) == "sku-white-sneaker"
        assert canonical_product_referent(state, checkout_active=True)["id"] == 801
        assert should_keep_live_order_focus_after_product_list(
            state.current_product_focus,
            [dict(PRODUCT_A)],
            has_live_order=True,
            state=state,
        ) is True

    def test_same_product_search_hit_preserves_checkout_selected(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        assert should_preserve_focus_after_product_list_display(
            state.current_product_focus,
            [dict(PRODUCT_A)],
            state=state,
        ) is True
        assert should_keep_live_order_focus_after_product_list(
            state.current_product_focus,
            [dict(PRODUCT_A)],
            has_live_order=True,
            state=state,
        ) is True


class TestAgent2D1SubmittedOrderPreserved:
    def test_committed_order_rows_survive_new_browse_focus(self) -> None:
        state = _checkout_selected_state(PRODUCT_A, submitted=True)
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="structured_turn_product",
            turn=12,
            current_turn_customer_referent=True,
        )
        assert product_focus_identity(state.current_product_focus) == "sku-blue-shirt"
        assert state.order_prep.salla_order_id == "SALLA-ORDER-A-99"
        assert state.order_prep.order_creation_status == "created"
        assert state.order_prep.product_id == "sku-white-sneaker"
        assert state.draft_order_id == "draft-a-1"


class TestAgent2D1ReturnToPreviousProduct:
    def test_explicit_return_restores_previous_focus(self) -> None:
        state = _checkout_selected_state(PRODUCT_A)
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="structured_turn_product",
            turn=9,
            current_turn_customer_referent=True,
        )
        assert revert_to_previous_product_focus(state, reason="user_return") is True
        assert product_focus_identity(state.current_product_focus) == "sku-white-sneaker"
        assert product_focus_identity(state.previous_product_focus) == "sku-blue-shirt"


class TestAgent2D1SearchListDoesNotKeepStaleOwner:
    def test_different_product_candidates_are_a_new_goal(self) -> None:
        assert search_results_are_new_customer_product_goal(
            dict(PRODUCT_A),
            [dict(PRODUCT_B), dict(PRODUCT_C)],
        ) is True
        assert should_keep_live_order_focus_after_product_list(
            dict(PRODUCT_A),
            [dict(PRODUCT_B), dict(PRODUCT_C)],
            has_live_order=True,
        ) is False

    def test_live_order_same_product_list_still_protected(self) -> None:
        focus = dict(PRODUCT_A)
        focus["customer_selected"] = True
        focus["provenance"] = "catalog_order_selected"
        assert should_keep_live_order_focus_after_product_list(
            focus,
            [dict(PRODUCT_A), dict(PRODUCT_A)],
            has_live_order=True,
        ) is True

    def test_family2_side_effect_list_helper_still_preserves_without_new_goal(self) -> None:
        focus = dict(PRODUCT_A)
        focus["customer_selected"] = True
        focus["from_catalog_order"] = True
        focus["provenance"] = "catalog_order_selected"
        assert should_preserve_focus_after_product_list_display(
            focus,
            [dict(PRODUCT_B), dict(PRODUCT_C)],
        ) is True


class TestAgent2D1SelectionAndVariantRegression:
    def test_explicit_customer_selection_still_binds(self) -> None:
        state = MerchantConversationState(turn=2)
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_A),
            reason="catalog_order_selected",
            turn=2,
            customer_selected=True,
        )
        assert structured_selected_referent(state)["id"] == 801
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="list_pick",
            turn=3,
            customer_selected=True,
        )
        assert product_focus_identity(state.current_product_focus) == "sku-blue-shirt"

    def test_variant_bind_stays_on_same_product(self) -> None:
        state = MerchantConversationState(turn=2)
        bind_structured_catalog_referent(
            state,
            dict(PRODUCT_B),
            reason="structured_turn_product",
            turn=2,
            current_turn_customer_referent=True,
        )
        bind_variant_to_focus(
            state,
            {"variant_id": "var-b-1", "variant_label": "M", "price": 130},
        )
        assert product_focus_identity(state.current_product_focus) == "sku-blue-shirt"
        assert state.selected_variant["variant_id"] == "var-b-1"
        assert state.current_product_focus["variant_label"] == "M"


class TestAgent2D1ExecutorProductOutranksRecommendedProduct:
    def test_executor_product_is_current_turn_goal(self) -> None:
        decision = type("D", (), {"args": {"recommended_product": dict(PRODUCT_A)}})()
        result = type("R", (), {"data": {"product": dict(PRODUCT_B)}})()
        assert structured_product_from_turn(decision, result)["id"] == 802
        assert current_turn_executor_catalog_referent(decision, result)["id"] == 802

    def test_recommended_only_is_not_executor_goal(self) -> None:
        decision = type("D", (), {"args": {"recommended_product": dict(PRODUCT_B)}})()
        result = type("R", (), {"data": {}})()
        assert structured_product_from_turn(decision, result)["id"] == 802
        assert current_turn_executor_catalog_referent(decision, result) is None
