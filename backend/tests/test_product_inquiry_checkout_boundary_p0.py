"""P0 — product inquiry vs checkout boundary (price / availability / multi-type)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from core.wa_draft_confirmation import compose_wa_order_flow_reply  # noqa: E402
from modules.ai.brain.commerce.checkout_route_owner import has_checkout_route_intent  # noqa: E402
from modules.ai.brain.commerce.commerce_conversation_guard import prepare_commerce_inbound  # noqa: E402
from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: E402
    CommerceTurnKind,
    classify_commerce_turn_kind,
    has_explicit_order_select_signal,
    has_price_inquiry_signal,
    is_commerce_inquiry_turn,
)
from modules.ai.brain.commerce.honey_browse_strategy import (  # noqa: E402
    apply_honey_browse_strategy,
    customer_specified_honey_types,
)
from modules.ai.brain.intent.cart_intent_extractor import extract_cart_intents  # noqa: E402

_AKBAR_PRICE = "\u0639\u0643\u0628\u0631 \u0643\u0645 \u0633\u0639\u0631\u0647"
_BKM_AKBAR = "\u0628\u0643\u0645 \u0627\u0644\u0639\u0643\u0628\u0631\u061f"


def _product(pid: int, title: str, *, quantity: int = 5) -> dict:
    return {"id": pid, "title": title, "category": "عسل", "quantity": quantity}


_HONEY_CATALOG = [
    _product(1, "عسل سمر الحجاز", quantity=10),
    _product(2, "عسل طلح نجد", quantity=8),
    _product(3, "عسل بالعكبر", quantity=5),
    _product(4, "سطل 5 كيلو", quantity=3),
    _product(5, "عسل الصيفي", quantity=0),
    _product(6, "عسل سدر بلدي", quantity=0),
]


class TestPriceInquiryNotCheckout:
    @pytest.mark.parametrize(
        "message",
        [_AKBAR_PRICE, _BKM_AKBAR],
        ids=["akbar_price", "bkm_akbar"],
    )
    def test_price_question_stays_inquiry(self, message: str) -> None:
        assert has_price_inquiry_signal(message) is True
        assert is_commerce_inquiry_turn(message) is True
        assert has_explicit_order_select_signal(message) is False
        assert extract_cart_intents(message) == []
        assert has_checkout_route_intent(message) is False
        assert classify_commerce_turn_kind(message) == CommerceTurnKind.PRICE_INQUIRY

    @pytest.mark.parametrize(
        "message",
        [_AKBAR_PRICE, _BKM_AKBAR],
        ids=["akbar_price", "bkm_akbar"],
    )
    def test_price_inquiry_does_not_inject_checkout_draft(self, message: str) -> None:
        reply = compose_wa_order_flow_reply(
            order_prep={"cart_items": [], "delivery_address": ""},
            brain_state={},
            cart_changed=False,
            existing_reply="",
            customer_message=message,
        )
        assert reply is None


class TestPackagedAvailabilityInquiry:
    def test_six_40g_jars_availability_is_inquiry_not_checkout(self) -> None:
        message = "6 عبوات 40جرام\nمتوفر؟"
        assert is_commerce_inquiry_turn(message) is True
        assert has_explicit_order_select_signal(message) is False
        assert extract_cart_intents(message) == []
        assert has_checkout_route_intent(message) is False
        assert classify_commerce_turn_kind(message) == CommerceTurnKind.AVAILABILITY

        state = SimpleNamespace(
            commerce_session={},
            stage="exploring",
            pending_action="",
            last_browse_query="",
        )
        prep = prepare_commerce_inbound(message, state=state, catalog=_HONEY_CATALOG)
        assert prep.is_browse_inquiry is True
        assert prep.session.order_intent is False


class TestMultiTypeInquiry:
    def test_three_types_request_is_inquiry_not_checkout(self) -> None:
        message = "ابي 3 انواع ذي"
        assert is_commerce_inquiry_turn(message) is True
        assert has_explicit_order_select_signal(message) is False
        assert extract_cart_intents(message) == []
        assert has_checkout_route_intent(message) is False

    def test_prior_type_context_extracts_requested_types(self) -> None:
        prior = "سمر وسدر وطلح"
        assert customer_specified_honey_types(prior) == ["طلح", "سمر", "سدر"]


class TestHoneyListingScope:
    def test_samar_and_talh_listing_excludes_unrequested_skus(self) -> None:
        message = "عسل السمر والطلح"
        assert customer_specified_honey_types(message) == ["طلح", "سمر"]

        scoped = apply_honey_browse_strategy(
            _HONEY_CATALOG,
            message=message,
            query=message,
            active_category="عسل",
            source="category_browse",
        )
        titles = {p["title"] for p in scoped}
        assert "عسل سمر الحجاز" in titles
        assert "عسل طلح نجد" in titles
        assert "عسل بالعكبر" not in titles
        assert "سطل 5 كيلو" not in titles
        assert "عسل الصيفي" not in titles


class TestExplicitAddEntersCheckout:
    def test_add_propolis_after_price_inquiry_is_order(self) -> None:
        message = "أضف العكبر"
        assert has_explicit_order_select_signal(message) is True
        assert has_price_inquiry_signal(message) is False
        assert is_commerce_inquiry_turn(message) is False
        assert has_checkout_route_intent(message) is True
