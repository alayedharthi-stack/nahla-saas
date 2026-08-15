"""
tests/test_p1a_fulfillment_lock_regression.py
──────────────────────────────────────────────
P1a production smoke regression — fulfillment lock must not block broad
browse or explicit product visual requests.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: E402
    global_availability_browse_requested,
)
from modules.ai.brain.commerce.product_visual import (  # noqa: E402
    is_product_visual_request,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.order_context_gate import (  # noqa: E402
    is_fulfillment_discovery_unlock,
    is_fulfillment_session_locked,
    should_block_product_discovery,
    should_suppress_product_escalation,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from routers.whatsapp_webhook import _should_suppress_empty_outbound_reply  # noqa: E402
from services.final_dispatch_guard import (  # noqa: E402
    should_allow_product_attachment_dispatch,
)


def _payment_receipt_locked_state(*, focus_id: int = 109) -> MerchantConversationState:
    prep = OrderPreparationState(
        product_id="ext-talh-1",
        awaiting_payment_receipt=True,
        payment_receipt_received=True,
        order_status="awaiting_payment",
    )
    return MerchantConversationState(
        stage="exploring",
        greeted=True,
        order_prep=prep,
        current_product_focus={
            "id": focus_id,
            "title": "عسل طلح",
            "external_id": "ext-talh-1",
        },
    )


def _locked_ctx(
    message: str,
    *,
    intent_name: str = "general",
    intent_confidence: float = 0.5,
) -> BrainContext:
    state = _payment_receipt_locked_state()
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000099",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=intent_confidence,
            raw_message=message,
        ),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
    )


class TestFulfillmentLockBrowseUnlock:
    def test_global_browse_phrase_detected(self):
        assert global_availability_browse_requested("وش المتوفر")

    def test_unlocks_broad_browse_during_payment_receipt_wait(self):
        msg = "وش المتوفر"
        ctx = _locked_ctx(msg)
        assert is_fulfillment_session_locked(ctx)
        assert is_fulfillment_discovery_unlock(msg)
        assert not should_block_product_discovery(ctx)

    def test_top_products_routes_when_fulfillment_locked(self):
        msg = "وش المتوفر"
        ctx = _locked_ctx(msg)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}
        if decision.action == ACTION_SEARCH_PRODUCTS:
            assert decision.args.get("source") == "top_products"

    def test_generic_ack_still_blocked(self):
        msg = "تمام"
        ctx = _locked_ctx(msg)
        assert is_fulfillment_session_locked(ctx)
        assert not is_fulfillment_discovery_unlock(msg)
        assert should_block_product_discovery(ctx)

    def test_webhook_suppress_helper_unlocks_global_browse(self):
        prep = {
            "product_id": "ext-talh-1",
            "awaiting_payment_receipt": True,
            "payment_receipt_received": True,
        }
        assert not should_suppress_product_escalation(
            message="وش المتوفر",
            brain_state={
                "order_prep": prep,
                "current_product_focus": {"id": 109, "title": "عسل طلح"},
            },
        )
        assert should_suppress_product_escalation(
            message="تمام",
            brain_state={"order_prep": prep},
        )


class TestFulfillmentLockVisualUnlock:
    def test_visual_phrase_detected(self):
        assert is_product_visual_request("ابي صورة الطلح")

    def test_visual_unlocks_during_payment_receipt_wait(self):
        msg = "ابي صورة الطلح"
        ctx = _locked_ctx(msg, intent_name="product_visual_request", intent_confidence=0.93)
        assert is_fulfillment_discovery_unlock(msg, intent_name="product_visual_request")
        assert not should_block_product_discovery(ctx)

    def test_dispatch_allows_visual_under_fulfillment_lock(self):
        prep = {
            "product_id": "ext-talh-1",
            "awaiting_payment_receipt": True,
            "payment_receipt_received": True,
        }
        brain_state = {
            "order_prep": prep,
            "current_product_focus": {"id": 109, "title": "عسل طلح"},
            "turn": 118,
            "product_focus_turn": 117,
        }
        decision = should_allow_product_attachment_dispatch(
            brain_action="llm_reply",
            intent_name="general",
            inbound_message="ابي صورة الطلح",
            reply_text="",
            fulfillment_discovery_blocked=True,
            brain_state=brain_state,
            active_order_state=prep,
        )
        assert decision.allow is True
        assert decision.reason in {
            "visual_product_intent",
            "product_visual_request",
            "positive_commerce_intent",
        }

    def test_marker_only_visual_does_not_suppress_wire_send(self):
        card = {"kind": "product_card", "id": 109, "title": "عسل طلح"}
        assert not _should_suppress_empty_outbound_reply(
            "",
            pending_attachments=[card],
        )

    def test_maps_during_fulfillment_still_blocks_cards(self):
        msg = "https://maps.app.goo.gl/abc — أبغى الطلبية تجي هنا"
        decision = should_allow_product_attachment_dispatch(
            brain_action="llm_reply",
            intent_name="general",
            inbound_message=msg,
            reply_text="تمام وصل الموقع",
            fulfillment_discovery_blocked=True,
            brain_state={
                "stage": "ordering",
                "order_prep": {
                    "product_id": "ext-1",
                    "order_status": "awaiting_address",
                },
                "current_product_focus": {"title": "عسل سدر", "id": 101},
            },
            active_order_state={"product_id": "ext-1"},
        )
        assert decision.allow is False
        assert decision.reason == "fulfillment_lock"
