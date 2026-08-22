"""Single resolved product → rich presentation only with referent grounding."""
from __future__ import annotations

import os
import sys
from typing import Any

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
    resolve_product_presentation,
)
from modules.ai.brain.types import CommerceFacts, MerchantConversationState  # noqa: E402


JACKET = {
    "id": 28,
    "external_id": "1921568272",
    "title": "جاكيت",
    "price": 199,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/jacket.jpg",
    "product_url": "https://shop.example/products/jacket",
}

SHOE = {
    "id": 55,
    "external_id": "shoe-1",
    "title": "حذاء رياضي أبيض",
    "price": 249,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/shoe.jpg",
    "product_url": "https://shop.example/products/white-sneaker",
}

PERFUME = {
    "id": 77,
    "external_id": "perfume-rose",
    "title": "عطر ورد 100ml",
    "price": 320,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/perfume.jpg",
    "product_url": "https://merchant.example/p/rose-100",
}


def _grounded_state(product: dict[str, Any]) -> MerchantConversationState:
    return MerchantConversationState(
        greeted=True,
        stage="discovery",
        current_product_focus={
            **product,
            "customer_selected": True,
            "provenance": "catalog_order_selected",
        },
        last_presented_products=[
            {
                **product,
                "customer_selected": True,
                "provenance": "catalog_order_selected",
            }
        ],
    )


class TestResolveProductPresentation:
    def test_ungrounded_singleton_is_not_single_rich(self) -> None:
        d = resolve_product_presentation([JACKET])
        assert d.kind == PRESENTATION_NONE
        assert d.candidate_count == 1
        assert d.reason == "ranked_singleton_not_referent"

    def test_grounded_singleton_uses_rich_presentation(self) -> None:
        state = _grounded_state(JACKET)
        d = resolve_product_presentation(
            [JACKET],
            state=state,
            merchant_context={"products": [dict(JACKET)]},
        )
        assert d.kind == PRESENTATION_SINGLE_RICH
        assert d.candidate_count == 1
        assert d.resolved_product is not None
        assert d.resolved_product["external_id"] == "1921568272"
        assert d.reason == "authoritative_referent_grounded"

    def test_identity_grounded_flag_allows_single_rich(self) -> None:
        d = resolve_product_presentation([JACKET], identity_grounded=True)
        assert d.kind == PRESENTATION_SINGLE_RICH

    def test_multi_candidates_use_choices(self) -> None:
        d = resolve_product_presentation([JACKET, SHOE])
        assert d.kind == PRESENTATION_MULTI_CHOICES
        assert d.candidate_count == 2

    def test_singleton_without_identity_falls_back_to_choices(self) -> None:
        d = resolve_product_presentation([{"title": "شيء"}])
        assert d.kind == PRESENTATION_MULTI_CHOICES

    def test_apply_grounded_stamps_card_and_clears_buttons(self) -> None:
        data: dict[str, Any] = {}
        state = _grounded_state(JACKET)
        apply_search_product_presentation(
            data,
            candidates=[JACKET],
            state=state,
            merchant_context={"products": [dict(JACKET)]},
            build_buttons=build_standard_pick_buttons,
        )
        assert data.get("pending_buttons") == []
        cards = data.get("pending_product_cards") or []
        assert len(cards) == 1
        assert cards[0]["kind"] == "product_card"
        assert cards[0]["product_url"] == "https://shop.example/products/jacket"
        assert cards[0]["file_url"] == "https://cdn.example/jacket.jpg"
        assert cards[0]["dispatch_source"] == "single_resolved_presentation"
        assert cards[0]["title"] == "جاكيت"

    def test_apply_ungrounded_singleton_emits_no_card(self) -> None:
        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[JACKET],
            build_buttons=build_standard_pick_buttons,
        )
        assert not data.get("pending_product_cards")
        assert data.get("product_presentation_kind") == PRESENTATION_NONE
        assert data.get("product_presentation_reason") == "ranked_singleton_not_referent"

    def test_apply_multi_builds_pick_buttons(self) -> None:
        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[JACKET, SHOE, PERFUME],
            build_buttons=build_standard_pick_buttons,
        )
        buttons = data.get("pending_buttons") or []
        assert len(buttons) == 3
        assert buttons[0]["reply"]["id"] == "pick_1"
        assert buttons[1]["reply"]["id"] == "pick_2"
        assert not data.get("pending_product_cards")

    def test_tenant_agnostic_non_salla_url_when_grounded(self) -> None:
        data: dict[str, Any] = {}
        state = _grounded_state(SHOE)
        apply_search_product_presentation(
            data,
            candidates=[SHOE],
            state=state,
            merchant_context={"products": [dict(SHOE)]},
            build_buttons=build_standard_pick_buttons,
        )
        assert data["pending_product_cards"][0]["product_url"].endswith("white-sneaker")

    def test_generic_commerce_perfume_singleton_requires_grounding(self) -> None:
        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[PERFUME],
            build_buttons=build_standard_pick_buttons,
        )
        assert not data.get("pending_product_cards")

        grounded = _grounded_state(PERFUME)
        apply_search_product_presentation(
            data,
            candidates=[PERFUME],
            state=grounded,
            merchant_context={"products": [dict(PERFUME)]},
            build_buttons=build_standard_pick_buttons,
        )
        assert data["pending_product_cards"][0]["external_id"] == "perfume-rose"

    def test_last_recommended_unique_row_does_not_ground_card(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            last_recommended_products=[dict(JACKET)],
        )
        assert authoritative_card_grounding(JACKET, state=state) is False

    def test_unique_search_hit_with_facts_is_not_a_referent(self) -> None:
        facts = CommerceFacts(
            has_products=True,
            orderable=True,
            product_count=1,
            discovery_products=[dict(JACKET)],
            top_products=[dict(JACKET)],
        )
        d = resolve_product_presentation(
            [JACKET],
            resolved_product=JACKET,
            facts=facts,
            merchant_context={"products": [dict(JACKET)]},
        )
        assert d.kind == PRESENTATION_NONE
        assert d.reason == "ranked_singleton_not_referent"
