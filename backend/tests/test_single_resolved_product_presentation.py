"""Single resolved product → rich presentation; multi → pick_N."""
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
    PRESENTATION_SINGLE_RICH,
    apply_search_product_presentation,
    build_standard_pick_buttons,
    resolve_product_presentation,
)


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


class TestResolveProductPresentation:
    def test_single_resolved_uses_rich_presentation(self) -> None:
        d = resolve_product_presentation([JACKET])
        assert d.kind == PRESENTATION_SINGLE_RICH
        assert d.candidate_count == 1
        assert d.resolved_product is not None
        assert d.resolved_product["external_id"] == "1921568272"

    def test_multi_candidates_use_choices(self) -> None:
        d = resolve_product_presentation([JACKET, SHOE])
        assert d.kind == PRESENTATION_MULTI_CHOICES
        assert d.candidate_count == 2

    def test_singleton_without_identity_falls_back_to_choices(self) -> None:
        d = resolve_product_presentation([{"title": "شيء"}])
        assert d.kind == PRESENTATION_MULTI_CHOICES

    def test_apply_stamps_card_and_clears_buttons(self) -> None:
        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[JACKET],
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

    def test_tenant_agnostic_non_salla_url(self) -> None:
        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[SHOE],
            build_buttons=build_standard_pick_buttons,
        )
        assert data["pending_product_cards"][0]["product_url"].endswith("white-sneaker")

    def test_generic_commerce_perfume_singleton(self) -> None:
        data: dict[str, Any] = {}
        apply_search_product_presentation(
            data,
            candidates=[PERFUME],
            build_buttons=build_standard_pick_buttons,
        )
        assert data.get("pending_buttons") == []
        assert data["pending_product_cards"][0]["external_id"] == "perfume-rose"
