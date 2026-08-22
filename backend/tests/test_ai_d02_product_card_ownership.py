"""AI-D02 — product card ownership requires authoritative referent grounding."""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    PRESENTATION_MULTI_CHOICES,
    PRESENTATION_NONE,
    PRESENTATION_SINGLE_RICH,
    apply_search_product_presentation,
    authoritative_card_grounding,
    build_standard_pick_buttons,
    clear_incompatible_product_cards,
    resolve_product_presentation,
    stamp_presentation_observability,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    ProductAvailabilityTruthGuardResult,
)
from modules.ai.brain.commerce.commerce_focus_owner import set_product_focus  # noqa: E402
from modules.ai.brain.types import CommerceFacts, MerchantConversationState  # noqa: E402

SHOE = {
    "id": 501,
    "external_id": "shoe-white-501",
    "title": "حذاء رياضي أبيض",
    "price": 249,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/shoe.jpg",
    "product_url": "https://shop.example/shoe",
}

PERFUME = {
    "id": 602,
    "external_id": "perfume-rose-602",
    "title": "عطر ورد 100ml",
    "price": 320,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
}

SHIRT = {
    "id": 703,
    "external_id": "shirt-blue-703",
    "title": "قميص قطني أزرق",
    "price": 149,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
}


def _catalog_facts(*products: dict[str, Any]) -> CommerceFacts:
    rows = [dict(p) for p in products]
    return CommerceFacts(
        has_products=True,
        orderable=True,
        product_count=len(rows),
        discovery_products=rows,
        top_products=rows,
    )


def _selected_state(product: dict[str, Any]) -> MerchantConversationState:
    row = {**product, "customer_selected": True, "provenance": "catalog_order_selected"}
    return MerchantConversationState(
        greeted=True,
        stage="checkout",
        current_product_focus=dict(row),
        last_presented_products=[dict(row)],
    )


def _confirmed_focus_state(product: dict[str, Any]) -> MerchantConversationState:
    return MerchantConversationState(
        greeted=True,
        stage="discovery",
        current_product_focus=dict(product),
    )


class TestBroadInquiryNoCard:
    def test_broad_category_singleton_not_single_rich(self) -> None:
        decision = resolve_product_presentation([SHOE])
        assert decision.kind == PRESENTATION_NONE
        assert decision.reason == "ranked_singleton_not_referent"

        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[SHOE],
            build_buttons=build_standard_pick_buttons,
        )
        assert not data.get("pending_product_cards")
        assert data.get("pending_candidates")


class TestUnresolvedGiftNoCard:
    def test_unresolved_recommendation_context_no_card(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            last_recommended_products=[dict(PERFUME)],
        )
        assert authoritative_card_grounding(PERFUME, state=state) is False
        decision = resolve_product_presentation(
            [PERFUME],
            state=state,
            resolved_product=PERFUME,
        )
        assert decision.kind == PRESENTATION_NONE


class TestRankedSingletonCentral:
    def test_ranked_singleton_without_referent(self) -> None:
        data: dict[str, Any] = {}
        decision = apply_search_product_presentation(
            data,
            candidates=[SHOE],
            build_buttons=build_standard_pick_buttons,
        )
        assert decision.kind == PRESENTATION_NONE
        assert decision.reason == "ranked_singleton_not_referent"
        assert not data.get("pending_product_cards")
        assert not data.get("pending_buttons")

    def test_search_unique_hit_plus_catalog_facts_does_not_ground(self) -> None:
        """Production path: unique orderable is copied to data['product'] and facts."""
        facts = _catalog_facts(SHOE)
        merchant = {"products": [dict(SHOE)]}
        decision = resolve_product_presentation(
            [SHOE],
            resolved_product=SHOE,
            facts=facts,
            merchant_context=merchant,
            discovery_entry_type="product_specific",
        )
        assert decision.kind == PRESENTATION_NONE
        assert decision.reason == "ranked_singleton_not_referent"

        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[SHOE],
            resolved_product=SHOE,
            facts=facts,
            merchant_context=merchant,
            discovery_entry_type="product_specific",
            build_buttons=build_standard_pick_buttons,
        )
        assert not data.get("pending_product_cards")

    def test_same_turn_rank_pin_does_not_ground(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            turn=5,
            product_focus_turn=5,
            current_product_focus=dict(SHOE),
        )
        decision = resolve_product_presentation(
            [SHOE],
            state=state,
            resolved_product=SHOE,
            facts=_catalog_facts(SHOE),
            merchant_context={"products": [dict(SHOE)]},
        )
        assert decision.kind == PRESENTATION_NONE
        assert not authoritative_card_grounding(
            SHOE,
            state=state,
            resolved_product=SHOE,
            facts=_catalog_facts(SHOE),
            merchant_context={"products": [dict(SHOE)]},
        )


class TestExplicitSkuGrounded:
    def test_customer_selected_allows_single_rich(self) -> None:
        state = _selected_state(SHOE)
        decision = resolve_product_presentation(
            [SHOE],
            state=state,
            merchant_context={"products": [dict(SHOE)]},
        )
        assert decision.kind == PRESENTATION_SINGLE_RICH

    def test_same_turn_customer_selected_allows_card(self) -> None:
        state = _selected_state(SHOE)
        state.turn = 3
        state.product_focus_turn = 3
        decision = resolve_product_presentation(
            [SHOE],
            state=state,
            resolved_product=SHOE,
            facts=_catalog_facts(SHOE),
            merchant_context={"products": [dict(SHOE)]},
        )
        assert decision.kind == PRESENTATION_SINGLE_RICH


class TestValidReferentD03:
    def test_catalog_confirmed_focus_allows_card(self) -> None:
        state = _confirmed_focus_state(SHOE)
        facts = _catalog_facts(SHOE)
        assert authoritative_card_grounding(
            SHOE,
            state=state,
            facts=facts,
            merchant_context={"products": [dict(SHOE)]},
        )
        decision = resolve_product_presentation(
            [SHOE],
            state=state,
            facts=facts,
            merchant_context={"products": [dict(SHOE)]},
        )
        assert decision.kind == PRESENTATION_SINGLE_RICH

    def test_prior_turn_focus_survives_same_identity_search(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            turn=8,
            product_focus_turn=7,
            current_product_focus=dict(SHOE),
        )
        decision = resolve_product_presentation(
            [SHOE],
            state=state,
            resolved_product=SHOE,
            facts=_catalog_facts(SHOE),
            merchant_context={"products": [dict(SHOE)]},
        )
        assert decision.kind == PRESENTATION_SINGLE_RICH

    def test_same_identity_rebind_preserves_prior_focus_turn(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            turn=8,
            product_focus_turn=7,
            current_product_focus={**SHOE, "customer_selected": True},
        )
        set_product_focus(state, dict(SHOE), reason="executor_product_search_products", turn=8)
        assert state.product_focus_turn == 7
        assert state.current_product_focus.get("customer_selected") is True
        decision = resolve_product_presentation(
            [SHOE],
            state=state,
            resolved_product=SHOE,
            facts=_catalog_facts(SHOE),
            merchant_context={"products": [dict(SHOE)]},
        )
        assert decision.kind == PRESENTATION_SINGLE_RICH

    def test_cross_namespace_identity_collision_does_not_preserve_turn(self) -> None:
        prior = {
            "id": 501,
            "title": "قميص قطني أزرق",
            "customer_selected": True,
            "provenance": "catalog_order_selected",
        }
        ranked = {
            "id": 802,
            "external_id": "501",
            "title": "حذاء رياضي أبيض",
            "price": 249,
            "in_stock": True,
        }
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            turn=8,
            product_focus_turn=7,
            current_product_focus=dict(prior),
        )
        set_product_focus(
            state,
            dict(ranked),
            reason="executor_product_search_products",
            turn=8,
        )
        assert state.product_focus_turn == 8
        assert not state.current_product_focus.get("customer_selected")
        decision = resolve_product_presentation(
            [ranked],
            state=state,
            resolved_product=ranked,
            facts=_catalog_facts(ranked),
            merchant_context={"products": [dict(ranked)]},
        )
        assert decision.kind == PRESENTATION_NONE

    def test_cross_namespace_resolved_product_does_not_merge_selection(self) -> None:
        selected_shirt = {
            "id": 501,
            "title": "قميص قطني أزرق",
            "customer_selected": True,
            "provenance": "catalog_order_selected",
        }
        ranked_shoe = {
            "id": 802,
            "external_id": "501",
            "title": "حذاء رياضي أبيض",
        }
        decision = resolve_product_presentation(
            [ranked_shoe],
            resolved_product=selected_shirt,
        )
        assert decision.kind == PRESENTATION_NONE
        assert not (decision.resolved_product or {}).get("customer_selected")


class TestStaleReferentNoCard:
    def test_title_only_focus_does_not_ground(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            current_product_focus={"title": "حذاء رياضي أبيض"},
        )
        assert authoritative_card_grounding(SHOE, state=state) is False

    def test_deleted_id_focus_not_catalog_confirmed(self) -> None:
        stale = {"id": 999, "external_id": "deleted", "title": "حذاء رياضي أبيض"}
        state = _confirmed_focus_state(stale)
        facts = _catalog_facts(SHOE)
        assert authoritative_card_grounding(
            SHOE,
            state=state,
            facts=facts,
            merchant_context={"products": [dict(SHOE)]},
        ) is False


class TestGuardClearsCards:
    def test_rewrite_unknown_clears_pending_cards(self) -> None:
        data: dict[str, Any] = {
            "product_presentation_kind": PRESENTATION_SINGLE_RICH,
            "pending_product_cards": [{"kind": "product_card", "id": 501}],
        }
        stamp_presentation_observability(data)
        clear_incompatible_product_cards(data, reason="availability_truth_unresolved")
        assert not data.get("pending_product_cards")
        assert data.get("product_presentation_kind") == PRESENTATION_NONE
        assert data.get("cards_cleared_reason") == "availability_truth_unresolved"
        assert data.get("pending_product_card_count") == 0

    @pytest.mark.parametrize("action", ["rewrite_conflict", "rewrite_unknown", "rewrite_false_positive"])
    def test_guard_structural_rewrite_actions_clear_cards(self, action: str) -> None:
        data: dict[str, Any] = {
            "pending_product_cards": [{"kind": "product_card", "id": 501}],
        }
        result = ProductAvailabilityTruthGuardResult(
            reply="rewritten",
            action=action,
            replaced=True,
            availability_claim_blocked=True,
        )
        if result.action in (
            "rewrite_conflict",
            "rewrite_unknown",
            "rewrite_false_positive",
        ) and (result.replaced or result.availability_claim_blocked):
            clear_incompatible_product_cards(data, reason="availability_truth_unresolved")
        assert not data.get("pending_product_cards")


class TestGroundedAvailableKeepsCard:
    def test_allowed_guard_action_does_not_clear_cards(self) -> None:
        data: dict[str, Any] = {
            "pending_product_cards": [{"kind": "product_card", "id": 501}],
        }
        result = ProductAvailabilityTruthGuardResult(
            reply="متوفر",
            action="allowed",
            replaced=False,
            availability_claim_blocked=False,
        )
        if result.action in (
            "rewrite_conflict",
            "rewrite_unknown",
            "rewrite_false_positive",
        ) and (result.replaced or result.availability_claim_blocked):
            clear_incompatible_product_cards(data, reason="availability_truth_unresolved")
        assert data.get("pending_product_cards")

    def test_rewrite_false_negative_keeps_cards(self) -> None:
        data: dict[str, Any] = {
            "pending_product_cards": [{"kind": "product_card", "id": 501}],
        }
        result = ProductAvailabilityTruthGuardResult(
            reply="متوفر",
            action="rewrite_false_negative",
            replaced=True,
            availability_claim_blocked=False,
        )
        if result.action in (
            "rewrite_conflict",
            "rewrite_unknown",
            "rewrite_false_positive",
        ) and (result.replaced or result.availability_claim_blocked):
            clear_incompatible_product_cards(data, reason="availability_truth_unresolved")
        assert data.get("pending_product_cards")


class TestGenericCommerce:
    def test_shoes_and_perfume_ungrounded(self) -> None:
        for product in (SHOE, PERFUME, SHIRT):
            decision = resolve_product_presentation([product])
            assert decision.kind == PRESENTATION_NONE


class TestMultiChoicesControl:
    def test_two_candidates_multi_choices(self) -> None:
        decision = resolve_product_presentation([SHOE, PERFUME])
        assert decision.kind == PRESENTATION_MULTI_CHOICES
        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[SHOE, PERFUME],
            build_buttons=build_standard_pick_buttons,
        )
        assert len(data.get("pending_buttons") or []) == 2
        assert not data.get("pending_product_cards")


class TestTenantIsolation:
    def test_two_tenants_no_cross_card_ids(self) -> None:
        tenant_a = {"products": [dict(SHOE)]}
        tenant_b = {"products": [dict(PERFUME)]}
        state_a = _selected_state(SHOE)
        state_b = _selected_state(PERFUME)

        data_a: dict[str, Any] = {}
        data_b: dict[str, Any] = {}
        apply_search_product_presentation(
            data_a,
            candidates=[SHOE],
            state=state_a,
            merchant_context=tenant_a,
        )
        apply_search_product_presentation(
            data_b,
            candidates=[PERFUME],
            state=state_b,
            merchant_context=tenant_b,
        )
        ids_a = {c.get("id") for c in (data_a.get("pending_product_cards") or [])}
        ids_b = {c.get("id") for c in (data_b.get("pending_product_cards") or [])}
        assert ids_a == {501}
        assert ids_b == {602}
        assert ids_a.isdisjoint(ids_b)
