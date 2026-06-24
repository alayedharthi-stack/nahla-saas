"""Regression tests — catalog body echo + current order amount routing."""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.outbound_text_policy import OutboundTextTracker  # noqa: E402
from core.wa_native_catalog_order import (  # noqa: E402
    apply_native_order_to_state,
    build_line_items_from_payload,
    parse_native_catalog_order,
)
from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: E402
    FORBIDDEN_CATALOG_INTRO_MARKERS,
    TECHNICAL_CATALOG_BODY,
    resolve_native_catalog_body_text,
)
from modules.ai.brain.commerce.current_order_amount import (  # noqa: E402
    has_active_current_order,
    is_current_order_amount_question,
    resolve_current_order_amount,
    should_route_current_order_amount_over_tracking,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    boost_track_order_intent,
    is_explicit_order_tracking_request,
    is_order_tracking_follow_up,
)
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.catalog_navigate import CatalogNavigateHandler  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


CUSTOMER_BROWSE = "وش عندكم منتجات"
CUSTOMER_AMOUNT = "عارف كم قيمة طلبي؟"


def _catalog_order_meta(*, items: int = 2, total: float = 1614.0) -> dict:
    return {
        "source_type": "catalog_order",
        "product_items": [
            {
                "product_retailer_id": f"sku-{i}",
                "quantity": 1,
                "item_price": total / items,
                "currency": "SAR",
                "name": f"Product {i}",
            }
            for i in range(1, items + 1)
        ],
        "item_count": items,
        "total_price": total,
        "currency": "SAR",
    }


def _state_with_catalog_order(*, items: int = 2, total: float = 1614.0) -> MerchantConversationState:
    state = MerchantConversationState(stage="ordering", greeted=True)
    prep = OrderPreparationState(
        product_id="101",
        order_status="awaiting_address",
        line_items=[
            {
                "product_id": str(100 + i),
                "product_name": f"Item {i}",
                "quantity": 1,
                "unit_price": total / items,
                "currency": "SAR",
                "source": "whatsapp_native_catalog_order",
            }
            for i in range(1, items + 1)
        ],
        catalog_checkout_total=total,
        catalog_checkout_currency="SAR",
    )
    state.order_prep = prep
    state.cart_items = list(prep.line_items)
    state.current_product_focus = {
        "from_catalog_order": True,
        "from_native_catalog_order": True,
        "line_items_count": items,
    }
    return state


@dataclass
class _FakeMatch:
    product_retailer_id: str

    matched: bool = True
    match_field: str = "product.external_id"
    product_id: int = 101
    variant_id: int | None = None
    product_title: str = "Honey"
    catalog_price: float = 807.0


class TestNativeCatalogBodyPolicy:
    def test_native_catalog_body_does_not_echo_customer_message(self):
        body = resolve_native_catalog_body_text(
            context_reply="",
            inbound_customer_message=CUSTOMER_BROWSE,
        )
        assert body != CUSTOMER_BROWSE
        assert body == TECHNICAL_CATALOG_BODY
        for marker in FORBIDDEN_CATALOG_INTRO_MARKERS:
            assert marker not in body

    def test_native_catalog_body_uses_llm_context_or_minimal_body(self):
        llm_ctx = "المنتجات متاحة في الكتالوج"
        body = resolve_native_catalog_body_text(
            context_reply=llm_ctx,
            inbound_customer_message=CUSTOMER_BROWSE,
        )
        assert body == llm_ctx

        body2 = resolve_native_catalog_body_text(
            context_reply=CUSTOMER_BROWSE,
            inbound_customer_message=CUSTOMER_BROWSE,
        )
        assert body2 == TECHNICAL_CATALOG_BODY

    def test_catalog_navigate_does_not_echo_customer_message(self):
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message=CUSTOMER_BROWSE,
            intent=Intent(name="ask_product", confidence=0.9, raw_message=CUSTOMER_BROWSE),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True),
            history=[],
        )
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "native_catalog_entry",
                "native_catalog_entry": {"thumbnail_product_retailer_id": "rid-1"},
            },
        )
        result = asyncio.run(CatalogNavigateHandler().handle(decision, ctx))
        body = result.data["native_catalog_entry"]["body_text"]
        assert body != CUSTOMER_BROWSE
        assert body == TECHNICAL_CATALOG_BODY

    def test_native_catalog_minimal_body_metadata(self):
        tracker = OutboundTextTracker()
        tracker.set_native_catalog(body=".")
        meta = tracker.to_metadata()
        assert meta["final_delivery_type"] == "native_catalog"
        assert meta["text_source"] == "technical"
        assert "native_catalog_minimal_body" in meta["audit_notes"]


class TestCatalogOrderState:
    def test_catalog_order_with_two_items_preserves_all_line_items(self):
        payload = parse_native_catalog_order(
            {"product_items": _catalog_order_meta()["product_items"]},
        )
        db = MagicMock()

        def _match(_db: Any, _t: int, rid: str) -> _FakeMatch:
            return _FakeMatch(product_retailer_id=rid, product_id=100 + int(rid.split("-")[-1]))

        with patch("core.wa_native_catalog_order.match_retailer_id", side_effect=_match):
            resolution = build_line_items_from_payload(db, 1, payload)
        assert len(resolution.line_items) == 2

        state = MerchantConversationState()
        with patch("core.wa_native_catalog_order.match_retailer_id", side_effect=_match):
            apply_native_order_to_state(db=db, tenant_id=1, state=state, payload=payload)
        assert len(state.order_prep.line_items) == 2

    def test_catalog_order_total_available_before_address_completion(self):
        state = _state_with_catalog_order()
        snap = resolve_current_order_amount(state=state)
        assert snap.has_active_current_order is True
        assert snap.line_items_count == 2
        assert snap.total_amount == 1614.0
        assert snap.currency == "SAR"


class TestCurrentOrderAmountRouting:
    def test_current_order_amount_question_detected(self):
        assert is_current_order_amount_question(CUSTOMER_AMOUNT) is True
        assert is_current_order_amount_question("كم المجموع؟") is True

    def test_tracking_still_works_for_real_tracking_question(self):
        assert is_order_tracking_follow_up("وين طلبي؟") is True
        assert is_current_order_amount_question("وين طلبي؟") is False
        assert is_explicit_order_tracking_request("وين طلبي؟") is True
        assert is_explicit_order_tracking_request("متى يوصل طلبي؟") is True

    def test_current_order_amount_question_uses_active_catalog_order(self):
        state = _state_with_catalog_order()
        assert should_route_current_order_amount_over_tracking(CUSTOMER_AMOUNT, state=state)
        assert is_explicit_order_tracking_request(CUSTOMER_AMOUNT, state=state) is False
        assert boost_track_order_intent(CUSTOMER_AMOUNT, state=state) is None

    def test_order_tracking_lookup_does_not_override_active_checkout_amount_question(self):
        state = _state_with_catalog_order()
        assert has_active_current_order(state=state) is True
        assert should_route_current_order_amount_over_tracking(
            "عارف كم قيمة طلبي؟",
            state=state,
        )

    def test_decision_engine_routes_amount_to_llm_not_track_order(self):
        state = _state_with_catalog_order()
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message=CUSTOMER_AMOUNT,
            intent=Intent(name="track_order", confidence=0.96, raw_message=CUSTOMER_AMOUNT),
            state=state,
            facts=CommerceFacts(),
            history=[],
            profile={"inbound_metadata": {}},
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "current_order_amount"
        facts = decision.args.get("current_order_amount_facts") or {}
        assert facts.get("has_active_current_order") is True
        assert facts.get("total_amount") == 1614.0

    def test_track_order_compose_does_not_use_no_orders_when_amount_routed(self):
        state = _state_with_catalog_order()
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message=CUSTOMER_AMOUNT,
            intent=Intent(name="general", confidence=0.5, raw_message=CUSTOMER_AMOUNT),
            state=state,
            facts=CommerceFacts(),
            history=[],
        )
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "current_order_amount",
                "current_order_amount_facts": {
                    "has_active_current_order": True,
                    "total_amount": 1614.0,
                    "currency": "SAR",
                },
            },
        )
        composer = DefaultComposer()
        with patch.object(composer, "_llm_compose", return_value="") as mock_llm:
            asyncio.run(
                composer.compose(decision, ActionResult(success=True, data={}), ctx),
            )
        assert mock_llm.called

    def test_track_order_still_routes_without_active_checkout(self):
        assert is_explicit_order_tracking_request("وين طلبي؟", state=None) is True


class TestFallbackMetadata:
    def test_online_store_exception_fallback_metadata_present(self):
        tracker = OutboundTextTracker()
        tracker.mark_fallback(
            reason="brain_exception",
            kind="neutral_retry",
            intent="online_store_inquiry",
            decision_action="faq_reply",
        )
        meta = tracker.to_metadata()
        assert meta["fallback_reason"] == "brain_exception"
        assert meta["fallback_kind"] == "neutral_retry"
        assert meta["intent"] == "online_store_inquiry"
        assert meta["decision_action"] == "faq_reply"
