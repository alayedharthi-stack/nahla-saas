"""
tests/test_order_context_gate.py
────────────────────────────────
Regression: active-order location/map messages must not trigger catalog search.
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
    ACTION_FAQ_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.order_context_gate import (
    FULFILLMENT_DELIVERY_SWITCH,
    detect_fulfillment_update,
    has_active_order_context,
    has_explicit_commerce_topic_change,
    is_fulfillment_session_locked,
    is_order_fulfillment_product_query,
    should_block_product_discovery,
    should_skip_catalog_preload,
    should_suppress_product_escalation,
    try_fulfillment_lock_continuation,
    try_order_context_update_decision,
)
from modules.ai.brain.state.stages import STAGE_ORDERING
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_MAPS = "https://maps.app.goo.gl/abc123test"
_PRODUCT = {
    "title": "عسل سدر",
    "external_id": "ext-honey-1",
    "id": 101,
    "can_checkout": True,
}


def _active_order_ctx(
    message: str,
    *,
    intent_name: str = "general",
    intent_confidence: float = 0.55,
    slots: dict | None = None,
    with_focus: bool = True,
) -> BrainContext:
    prep = OrderPreparationState(
        product_id="ext-honey-1",
        customer_first_name="محمد",
        customer_phone="966500000001",
        city="الرياض",
        order_status="awaiting_address",
    )
    state = MerchantConversationState(
        stage=STAGE_ORDERING,
        greeted=True,
        order_prep=prep,
        current_product_focus=dict(_PRODUCT) if with_focus else None,
        last_search_candidates=[dict(_PRODUCT)],
    )
    return BrainContext(
        tenant_id=99,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=intent_confidence,
            slots=dict(slots or {}),
            raw_message=message,
        ),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
    )


class TestOrderContextGateDetection:
    def test_maps_link_during_active_order(self):
        msg = f"{_MAPS}\nأبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg, slots={"google_maps_url": _MAPS})
        assert has_active_order_context(ctx)
        assert detect_fulfillment_update(msg, ctx.intent.slots) is not None
        assert should_block_product_discovery(ctx)

    def test_delivery_phrase_without_maps(self):
        msg = "أبغى الطلبية تجي الموقع ذا"
        assert detect_fulfillment_update(msg, {}) is not None
        assert is_order_fulfillment_product_query("الطلبية تجي الموقع ذا")

    def test_pickup_to_delivery_switch(self):
        msg = "\u063a\u064a\u0631\u062a \u0625\u0644\u0649 \u062a\u0648\u0635\u064a\u0644"
        assert detect_fulfillment_update(msg, {}) == FULFILLMENT_DELIVERY_SWITCH

    def test_no_active_order_map_is_not_blocked(self):
        msg = _MAPS
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.5, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, store_name="Test"),
        )
        assert not has_active_order_context(ctx)
        assert not should_block_product_discovery(ctx)


class TestOrderContextDecisionEngine:
    def test_active_order_maps_routes_order_context_update(self):
        msg = f"{_MAPS}\nأبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg, slots={"google_maps_url": _MAPS})
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_ORDER_CONTEXT_UPDATE
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("google_maps_url") == _MAPS

    def test_active_order_delivery_phrase_not_search(self):
        msg = "أبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg, intent_name="start_order", intent_confidence=0.85)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {
            ACTION_ORDER_CONTEXT_UPDATE,
            ACTION_PROPOSE_DRAFT_ORDER,
        }
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_pickup_to_delivery_continues_order_flow(self):
        msg = "\u063a\u064a\u0631\u062a \u0625\u0644\u0649 \u062a\u0648\u0635\u064a\u0644"
        ctx = _active_order_ctx(msg)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_ORDER_CONTEXT_UPDATE
        assert decision.args.get("fulfillment_kind") == FULFILLMENT_DELIVERY_SWITCH

    def test_no_active_order_map_routes_store_location_faq(self):
        msg = "وين موقعكم"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(
                name="ask_location",
                confidence=0.92,
                raw_message=msg,
            ),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(
                has_products=True,
                store_name="Test",
                maps_url="https://maps.app.goo.gl/store",
            ),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_FAQ_REPLY
        assert decision.args.get("topic") == "location"

    def test_completed_order_product_ask_still_allowed(self):
        msg = "عسل سدر بكم؟"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="ask_price", confidence=0.9, raw_message=msg),
            state=MerchantConversationState(greeted=True, stage="discovery"),
            facts=CommerceFacts(has_products=True, orderable=True),
            commerce_bundle={},
        )
        assert not has_active_order_context(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS


class TestOrderContextPipelineSkip:
    def test_skip_catalog_preload_during_fulfillment(self):
        msg = f"{_MAPS} أبغى الطلبية تجي الموقع ذا"
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            customer_first_name="محمد",
        )
        state = MerchantConversationState(
            stage=STAGE_ORDERING,
            order_prep=prep,
            current_product_focus=dict(_PRODUCT),
        )
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
        assert should_skip_catalog_preload(
            message=msg,
            state=state,
            intent=intent,
        )

    def test_try_order_context_update_includes_product(self):
        msg = f"{_MAPS}"
        ctx = _active_order_ctx(msg, slots={"google_maps_url": _MAPS})
        decision = try_order_context_update_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_ORDER_CONTEXT_UPDATE
        assert decision.args.get("product", {}).get("external_id") == "ext-honey-1"


class TestFulfillmentSessionLock:
    def test_generic_message_blocks_product_discovery(self):
        msg = "تمام"
        ctx = _active_order_ctx(msg, intent_name="general")
        assert is_fulfillment_session_locked(ctx)
        assert should_block_product_discovery(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_explicit_commerce_topic_change_unlocks_discovery(self):
        msg = "أبي منتج ثاني"
        ctx = _active_order_ctx(msg, intent_name="ask_product", intent_confidence=0.9)
        assert has_explicit_commerce_topic_change(msg)
        assert not should_block_product_discovery(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_quiet_gap_order_prep_only_still_locked(self):
        """Persisted order_prep survives stage=discovery after a quiet gap."""
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            customer_first_name="محمد",
            order_status="awaiting_payment",
        )
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
        )
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message="أيوه",
            intent=Intent(name="general", confidence=0.5, raw_message="أيوه"),
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_fulfillment_session_locked(ctx)
        assert should_block_product_discovery(ctx)
        assert should_skip_catalog_preload(
            message="أيوه",
            state=state,
            intent=ctx.intent,
        )

    def test_fulfillment_lock_continuation_on_generic_turn(self):
        msg = "حسنا"
        ctx = _active_order_ctx(msg, intent_name="general")
        decision = try_fulfillment_lock_continuation(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("fulfillment_lock") is True

    def test_webhook_suppress_helper_uses_persisted_state(self):
        prep = {
            "product_id": "ext-honey-1",
            "customer_first_name": "محمد",
            "order_status": "awaiting_address",
        }
        assert should_suppress_product_escalation(
            message="مرحبا",
            brain_state={"order_prep": prep, "stage": "discovery"},
        )
        assert not should_suppress_product_escalation(
            message="ورني العروض",
            brain_state={"order_prep": prep, "stage": "discovery"},
        )
