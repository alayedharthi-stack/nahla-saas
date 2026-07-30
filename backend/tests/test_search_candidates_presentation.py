"""Regression: search candidate facts must appear in customer-facing browse replies."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose import templates as T  # noqa: E402

_DANGLING_CLOSING = "تفضل رقم المنتج أو اسمه وأكمل طلبك"

_FOUR_DRESSES = [
    {
        "id": 23,
        "title": "فستان سهرة مطرز",
        "price": 289.0,
        "can_checkout": True,
    },
    {
        "id": 39,
        "title": "فستان كاجوال خفيف",
        "price": 77.0,
        "can_checkout": True,
    },
    {
        "id": 38,
        "title": "فستان محتشم يومي",
        "price": 83.0,
        "can_checkout": True,
    },
    {
        "id": 37,
        "title": "فستان مناسبات أنيق",
        "price": 114.0,
        "can_checkout": True,
    },
]

_GENERIC_SHIRT = {
    "id": 501,
    "title": "قميص قطني أزرق",
    "price": 95.0,
    "can_checkout": True,
}


def _is_dangling_closing_only(reply: str, products: list[dict]) -> bool:
    body = str(reply or "").strip()
    if not body:
        return True
    if T.presentation_text_lists_products(body, products):
        return False
    return T.presentation_is_dangling_closing_only(body, products)


class TestSearchCandidatesPresentation:
    def test_four_dress_search_rebuilds_product_lines_from_facts(self) -> None:
        reply = T.search_products(
            products=_FOUR_DRESSES,
            query="فستان",
            discovery_presentation_text=_DANGLING_CLOSING,
        )
        assert "لا يوجد رقم تواصل" not in reply
        assert not _is_dangling_closing_only(reply, _FOUR_DRESSES)
        assert "289" in reply
        assert "114" in reply
        assert "فستان سهرة مطرز" in reply
        assert "فستان مناسبات أنيق" in reply
        assert "999" not in reply

    def test_zero_results_uses_no_products_template(self) -> None:
        reply = T.search_products(
            products=[],
            query="فستان",
            discovery_presentation_text=_DANGLING_CLOSING,
        )
        assert "لا يوجد رقم تواصل" not in reply
        assert _is_dangling_closing_only(reply, []) or "منتج" in reply
        assert "289" not in reply
        assert "114" not in reply

    def test_single_result_shows_one_candidate_not_silent_pick_among_many(self) -> None:
        single_reply = T.search_products(
            products=[_GENERIC_SHIRT],
            query="قميص",
        )
        assert "قميص قطني أزرق" in single_reply
        assert "1." in single_reply
        assert "2." not in single_reply

        multi_reply = T.search_products(
            products=_FOUR_DRESSES,
            query="فستان",
            discovery_presentation_text=_DANGLING_CLOSING,
        )
        assert "289" in multi_reply
        assert "114" in multi_reply
        assert multi_reply.count("1.") >= 1
        assert "2." in multi_reply

    def test_compose_facts_carry_all_eligible_candidates(self) -> None:
        facts = T.build_search_products_compose_facts(
            _FOUR_DRESSES,
            query="فستان",
            inbound_text="أبغى فستان",
        )
        assert facts["eligible_product_count"] == 4
        assert facts["search_result_count"] == 4
        prices = {
            row["price"]
            for row in facts["catalog_products"]
            if row.get("price") is not None
        }
        assert 289.0 in prices
        assert 114.0 in prices
        assert 999.0 not in prices

    def test_sale_price_constraint_keeps_candidate_on_display_list(self) -> None:
        product = {
            "id": 88,
            "title": "فستان صيفي",
            "sale_price": 114.0,
            "can_checkout": True,
        }
        reply = T.search_products(products=[product], query="فستان")
        assert "114" in reply
        assert "فستان صيفي" in reply

    def test_persona_compose_path_repairs_dangling_discovery_text(self) -> None:
        async def _run() -> None:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                new=AsyncMock(
                    return_value=(
                        _DANGLING_CLOSING,
                        None,
                        {"compose_source": "persona_llm", "llm_candidate_present": True},
                    ),
                ),
            ):
                reply, event = await T.compose_search_products_with_persona(
                    tenant_id=1,
                    customer_phone="966500000001",
                    inbound_text="أبغى فستان",
                    products=_FOUR_DRESSES,
                    catalog_search_query="فستان",
                )
            assert "لا يوجد رقم تواصل" not in reply
            assert not _is_dangling_closing_only(reply, _FOUR_DRESSES)
            assert "289" in reply
            assert "114" in reply
            assert event.get("search_products_facts", {}).get("eligible_product_count") == 4
            assert event.get("compose_source") == "fallback_deterministic"

        asyncio.run(_run())
