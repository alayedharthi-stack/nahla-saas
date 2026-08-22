"""
tests/test_product_breadth_policy.py
────────────────────────────────────
Regression tests for LIMIT_RECOMMENDATION_BREADTH (May 2026).
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from modules.ai.brain.commerce.product_breadth_policy import (
    apply_display_slice,
    explicit_broad_browse_requested,
    explicit_hard_browse_requested,
    explicit_soft_browse_requested,
    limit_initial_product_options_enabled,
    limit_recommendation_breadth_enabled,
    next_catalog_browse_batch,
    resolve_product_breadth,
)
from modules.ai.brain.compose import templates as T


def _products(n: int):
    return [{"title": f"P{i}", "price": 100 + i, "external_id": f"e{i}"} for i in range(1, n + 1)]


class TestProductBreadthPolicy:
    def test_low_confidence_one_option(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            intent_name="general",
            intent_confidence=0.5,
            source="",
            stage="discovery",
            total_available=8,
            is_first_recommendation=True,
        )
        assert b.display_limit == 1
        assert b.catalog_card_limit == 1
        assert b.mode == "focused"
        assert b.confidence_tier == "low"

    def test_specific_product_ask_max_two(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            message="أبي عسل طلح",
            intent_name="ask_product",
            intent_confidence=0.9,
            query="عسل طلح",
            stage="exploring",
            total_available=8,
        )
        assert b.display_limit == 2
        assert b.catalog_card_limit == 1
        assert b.mode == "focused"
        assert b.confidence_tier == "high"

    def test_soft_browse_wsh_3ndkom_not_five(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            message="وش عندكم؟",
            intent_name="general",
            intent_confidence=0.6,
            source="top_products",
            stage="discovery",
            total_available=8,
        )
        assert b.display_limit == 3
        assert b.catalog_card_limit == 2
        assert b.mode == "browse"
        assert explicit_soft_browse_requested("وش عندكم؟")
        assert not explicit_hard_browse_requested("وش عندكم؟")

    def test_hard_browse_phrase_max_three(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            message="وريني كل المنتجات",
            intent_name="general",
            intent_confidence=0.6,
            total_available=8,
        )
        assert b.display_limit == 3
        assert b.mode == "broad"

    def test_top_products_source_alone_not_broad(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            intent_name="general",
            intent_confidence=0.6,
            source="top_products",
            stage="discovery",
            total_available=8,
            is_first_recommendation=True,
        )
        assert b.display_limit == 1
        assert b.mode == "focused"
        assert b.explicit_broad is False

    def test_single_match_one_option(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            intent_name="ask_product",
            intent_confidence=0.9,
            query="عسل",
            total_available=1,
        )
        assert b.display_limit == 1

    def test_apply_display_slice_hides_overflow(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            intent_name="general",
            intent_confidence=0.5,
            total_available=8,
            is_first_recommendation=True,
        )
        shown, meta = apply_display_slice(_products(8), b)
        assert len(shown) == 1
        assert meta["hidden_count"] == 7
        assert meta["show_more_hint"] is True

    def test_policy_disabled_shows_all(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "false")
        assert limit_recommendation_breadth_enabled() is False
        assert limit_initial_product_options_enabled() is False
        b = resolve_product_breadth(total_available=8)
        shown, meta = apply_display_slice(_products(8), b)
        assert len(shown) == 8
        assert meta["hidden_count"] == 0

    def test_show_more_batch_skips_already_shown(self):
        pool = _products(6)
        batch, nxt = next_catalog_browse_batch(
            pool,
            offset=0,
            exclude_keys=["e1", "e2"],
            limit=3,
        )
        assert [p["title"] for p in batch] == ["P3", "P4", "P5"]
        assert nxt == 5

    def test_show_more_continuation_not_broad(self, monkeypatch):
        monkeypatch.setenv("LIMIT_RECOMMENDATION_BREADTH", "true")
        b = resolve_product_breadth(
            message="وريني باقي الخيارات",
            intent_name="general",
            intent_confidence=0.6,
            source="show_more",
            stage="exploring",
            total_available=8,
        )
        assert b.display_limit == 3
        assert b.mode == "standard"
        assert explicit_broad_browse_requested("وريني باقي الخيارات") is False


class TestNarrowTemplates:
    def test_focused_header_two_options(self):
        text = T.narrow_choices(
            products=_products(2),
            variant=0,
            show_more_hint=False,
        )
        assert "خيارين" in text
        assert "1. *P1*" in text
        assert "2. *P2*" in text
        assert "3." not in text

    def test_show_more_hint_appended(self):
        text = T.narrow_choices(
            products=_products(2),
            variant=0,
            show_more_hint=True,
        )
        assert "باقي الخيارات" in text

    def test_single_option_header(self):
        text = T.narrow_choices(products=_products(1), variant=0)
        assert "1. *P1*" in text
        assert "2." not in text


class TestComposerBreadthIntegration:
    def test_search_products_compose_uses_decision_param(self):
        import asyncio
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS
        from modules.ai.brain.types import (
            ActionResult,
            BrainContext,
            CommerceFacts,
            Decision,
            Intent,
            MerchantConversationState,
        )

        async def _run():
            composer = DefaultComposer()
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "عسل"},
            )
            result = ActionResult(
                success=True,
                data={
                    "products": [
                        {
                            "title": "عسل طلح",
                            "price": 120,
                            "id": 1,
                            "external_id": "e1",
                            "can_checkout": True,
                        }
                    ],
                },
            )
            ctx = BrainContext(
                tenant_id=33,
                customer_phone="966500000000",
                message="بكم العسل؟",
                intent=Intent(name="ask_price", confidence=0.9, raw_message="بكم العسل؟"),
                state=MerchantConversationState(greeted=True),
                facts=CommerceFacts(has_products=True, orderable=True),
            )
            return await composer.compose(decision, result, ctx), result

        reply, composed = asyncio.run(_run())
        assert (reply or "").strip()
        assert composed.data.get("product_presentation_kind") != "single_resolved_rich"
        assert not composed.data.get("pending_product_cards")
        candidates = composed.data.get("pending_candidates") or composed.data.get("products") or []
        assert any(str((row or {}).get("title") or "") == "عسل طلح" for row in candidates)
