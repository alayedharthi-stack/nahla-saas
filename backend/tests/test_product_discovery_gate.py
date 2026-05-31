"""
tests/test_product_discovery_gate.py
────────────────────────────────────
Regression: weak/ambiguous turns must not trigger top_products or
unrelated catalog recommendations.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.order_context_gate import should_block_product_discovery
from modules.ai.brain.product_discovery_gate import (
    allows_top_products_decision,
    has_explicit_broad_browse_request,
    is_price_without_product_context,
    product_discovery_block_reason,
    should_suppress_recommendation_escalation,
    try_price_query_decision,
)
from modules.ai.brain.state.stages import STAGE_ORDERING
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_PRODUCT = {
    "title": "عسل سدر",
    "external_id": "ext-honey-1",
    "id": 101,
    "can_checkout": True,
}
_MAPS = "https://maps.app.goo.gl/abc123test"


def _active_order_ctx(message: str, *, intent_name: str = "general") -> BrainContext:
    prep = OrderPreparationState(
        product_id="ext-honey-1",
        customer_first_name="محمد",
        city="الرياض",
        order_status="awaiting_address",
    )
    state = MerchantConversationState(
        stage=STAGE_ORDERING,
        greeted=True,
        order_prep=prep,
        current_product_focus=dict(_PRODUCT),
    )
    return BrainContext(
        tenant_id=99,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.55, raw_message=message),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
    )


class TestProductDiscoveryGate:
    def test_active_order_location_blocks_top_products(self):
        msg = f"{_MAPS}\nأبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg)
        assert product_discovery_block_reason(ctx) == "active_fulfillment"
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_kilo_price_without_product_clarifies(self):
        msg = "كم سعر الكيلو؟"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="ask_price", confidence=0.9, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_price_without_product_context(ctx)
        decision = try_price_query_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_CLARIFY
        full = DefaultDecisionEngine().decide(ctx)
        assert full.action == ACTION_CLARIFY
        assert full.action != ACTION_SEARCH_PRODUCTS

    def test_kilo_price_with_active_product_uses_focus(self):
        msg = "كم سعر الكيلو؟"
        ctx = _active_order_ctx(msg, intent_name="ask_price")
        assert not is_price_without_product_context(ctx)
        decision = try_price_query_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "price"

    def test_unknown_message_suppresses_recommendations(self):
        assert should_suppress_recommendation_escalation(
            message="تمام",
            brain_state={"stage": "discovery"},
            intent_name="general",
        )

    def test_explicit_browse_allows_top_products(self):
        msg = "وش عندكم؟"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.6, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert has_explicit_broad_browse_request(msg)
        assert allows_top_products_decision(ctx, source="top_products", message=msg)

    def test_show_more_requires_prior_browse_context(self):
        msg = "وريني باقي الخيارات"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.7, raw_message=msg),
            state=MerchantConversationState(
                greeted=True,
                last_search_candidates=[dict(_PRODUCT)],
            ),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert product_discovery_block_reason(ctx, source="show_more") is None

    def test_show_more_without_pool_blocked(self):
        msg = "وريني باقي الخيارات"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.7, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert product_discovery_block_reason(ctx, source="show_more") == (
            "weak_or_unknown_intent"
        )
