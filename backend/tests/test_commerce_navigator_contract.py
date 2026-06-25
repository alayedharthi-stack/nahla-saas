"""Commerce navigator contract — facts only, no reply templates."""
from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_navigator import (  # noqa: E402
    CommerceNavigatorDecision,
    commerce_navigator_goal_directive,
    resolve_commerce_navigator,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402

_NAV_STORE = "https://shop.example"
_NAV_MAPS = "https://maps.example.com/showroom"

_REPLY_TEXT_KEYS = frozenset({
    "reply",
    "reply_text",
    "body",
    "message",
    "template",
    "customer_reply",
    "response_text",
})

_ARABIC_SENTENCE_RE = re.compile(
    r"[\u0600-\u06FF]{12,}.*(?:[.؟!]|$)",
    re.UNICODE,
)


def _assert_no_reply_payload(payload: dict) -> None:
    for key in _REPLY_TEXT_KEYS:
        assert key not in payload, f"navigator must not emit reply field {key!r}"
    for value in payload.values():
        if isinstance(value, str) and _ARABIC_SENTENCE_RE.search(value):
            if key := next((k for k, v in payload.items() if v is value), ""):
                if k not in {"reason", "next_goal", "customer_intent", "stage"}:
                    pytest.fail(f"unexpected Arabic prose in navigator field {key!r}")


class TestPurchaseChannelSelection:
    @pytest.mark.parametrize(
        "message",
        [
            "أبي أطلب",
            "كيف أشتري؟",
            "أبغى المنتج",
            "ودي آخذ",
            "كيف طريقة الطلب؟",
        ],
    )
    def test_purchase_intent_routes_to_channel_selection_without_checkout(
        self,
        message: str,
    ) -> None:
        decision = resolve_commerce_navigator(
            message=message,
            intent_name="start_order",
            store_url=_NAV_STORE,
            maps_url=_NAV_MAPS,
        )
        assert decision.stage == "purchase_channel_selection"
        assert decision.next_goal == "help_customer_choose_purchase_channel"
        assert decision.available_purchase_channels == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        assert "do_not_create_order_yet" in decision.forbidden_actions
        assert "do_not_ask_product_yet" in decision.forbidden_actions

    def test_channel_selection_does_not_ask_address_or_payment(self) -> None:
        decision = resolve_commerce_navigator(
            message="أبي أطلب",
            intent_name="start_order",
            store_url="https://shop.example",
            maps_url="https://maps.example.com/showroom",
        )
        forbidden = set(decision.forbidden_actions)
        assert "do_not_ask_payment" in forbidden
        assert "do_not_ask_address" in forbidden
        assert decision.missing_fields == []


class TestWhatsappQuickOrder:
    @pytest.mark.parametrize(
        "message",
        [
            "عن طريق واتساب",
            "طلب سريع",
            "جهز لي 50",
            "خذ طلبي",
            "أرسل لي الحساب",
            "أبي أكمل هنا",
        ],
    )
    def test_whatsapp_quick_order_only_after_explicit_channel_or_catalog_order(
        self,
        message: str,
    ) -> None:
        decision = resolve_commerce_navigator(
            message=message,
            intent_name="start_order",
            store_url=_NAV_STORE,
            maps_url=_NAV_MAPS,
        )
        assert decision.stage == "whatsapp_quick_order"
        assert decision.customer_intent == "whatsapp_quick_order"
        assert "do_not_ask_payment" in decision.forbidden_actions

    def test_catalog_order_event_enters_whatsapp_quick_order(self) -> None:
        decision = resolve_commerce_navigator(
            message="",
            inbound_metadata={"source_type": "catalog_order", "item_count": 1},
        )
        assert decision.stage == "whatsapp_quick_order"
        assert "product" in decision.missing_fields or decision.missing_fields


class TestPriceObjectionAndBrowse:
    def test_price_objection_not_routed_to_channel_selection_or_quantity(self) -> None:
        msg = "سعره غالي يقول 250"
        decision = resolve_commerce_navigator(
            message=msg,
            intent_name="ask_price",
            intent_slots={"price_objection": True},
            decision_topic="price_objection",
        )
        assert decision.stage == "price_objection"
        assert decision.stage != "purchase_channel_selection"
        assert "do_not_append_quantity_prompt" in decision.forbidden_actions
        assert "quantity" not in decision.missing_fields

    def test_browse_stays_browse_without_checkout(self) -> None:
        decision = resolve_commerce_navigator(
            message="وش عندكم متوفر؟",
            intent_name="ask_product",
        )
        assert decision.stage == "browse"
        assert "do_not_create_order_yet" in decision.forbidden_actions
        assert decision.stage != "purchase_channel_selection"


class TestNavigatorContractShape:
    def test_navigator_outputs_facts_not_reply_text(self) -> None:
        decision = resolve_commerce_navigator(
            message="أبي أطلب",
            store_url=_NAV_STORE,
            maps_url=_NAV_MAPS,
        )
        payload = decision.to_dict()
        _assert_no_reply_payload(payload)
        directive = commerce_navigator_goal_directive(decision)
        assert directive.startswith("commerce_navigator —")
        assert "next_goal=help_customer_choose_purchase_channel" in directive
        assert "تقدر تطلب" not in directive
        assert "المتجر" not in directive or "online_store" in directive

    def test_decision_is_immutable_dataclass(self) -> None:
        decision = resolve_commerce_navigator(
            message="أبي أطلب",
            store_url=_NAV_STORE,
            maps_url=_NAV_MAPS,
        )
        assert isinstance(decision, CommerceNavigatorDecision)
        with pytest.raises(Exception):
            decision.stage = "browse"  # type: ignore[misc]

    def test_active_checkout_with_prep_populates_missing_fields(self) -> None:
        prep = OrderPreparationState(
            product_id="ext-123",
            quantity=2,
            missing_fields=["city", "delivery_address"],
        )
        decision = resolve_commerce_navigator(
            message="الرياض",
            intent_name="start_order",
            stage="checkout",
            order_prep=prep,
        )
        assert decision.stage == "whatsapp_quick_order"
        assert "city" in decision.missing_fields or "delivery_address" in decision.missing_fields
