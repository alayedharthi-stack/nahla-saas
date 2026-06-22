"""Platform-wide commerce inquiry vs order boundary regression tests."""
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
from modules.ai.brain.commerce.commerce_conversation_guard import (  # noqa: E402
    prepare_commerce_inbound,
)
from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: E402
    classify_commerce_turn_kind,
    is_browse_availability_inquiry,
)
from modules.ai.brain.commerce.product_visual import (  # noqa: E402
    attachment_matches_turn_request,
    extract_visual_product_query,
    is_product_visual_request,
)
from modules.ai.brain.intent.cart_intent_extractor import extract_cart_intents  # noqa: E402


def _product(pid: int, title: str, *, category: str = "", quantity: int = 5) -> dict:
    return {
        "id": pid,
        "title": title,
        "category": category,
        "quantity": quantity,
    }


_HONEY_CATALOG = [
    _product(1, "عسل الطلح البلدي", category="عسل", quantity=10),
    _product(2, "عسل السدر البلدي", category="عسل", quantity=0),
    _product(3, "عسل بالعكبر", category="عسل", quantity=5),
]


class TestCommerceInquiryClassifier:
    @pytest.mark.parametrize(
        "message",
        [
            "فيه سدر",
            "متوفر السدر",
            "في طلح",
            "عندكم طلح؟",
            "وش الانواع المتوفره",
            "وش العطور الرجالية؟",
            "فيه تيشيرتات؟",
            "ابي اشوف صور للعسل",
        ],
    )
    def test_inquiry_phrases_classified_as_browse(self, message: str) -> None:
        assert is_browse_availability_inquiry(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "ابي كيلو طلح",
            "2 كيلو طلح",
            "أضف طلح",
            "سمر",
        ],
    )
    def test_order_phrases_not_classified_as_inquiry(self, message: str) -> None:
        assert is_browse_availability_inquiry(message) is False


class TestCartIntentInquiryBoundary:
    @pytest.mark.parametrize(
        "message",
        ["فيه سدر", "متوفر السدر", "في طلح", "طلح؟"],
    )
    def test_availability_does_not_add_cart_item(self, message: str) -> None:
        assert extract_cart_intents(message) == []

    def test_explicit_order_still_adds(self) -> None:
        intents = extract_cart_intents("ابي كيلو طلح")
        assert len(intents) == 1
        assert intents[0]["action"] == "add_item"
        assert "طلح" in intents[0]["product_name"]

    def test_quantity_order_still_adds(self) -> None:
        intents = extract_cart_intents("2 كيلo طلح")
        assert len(intents) == 1
        assert intents[0]["action"] == "add_item"


class TestCommerceGuardCheckoutBoundary:
    def test_availability_does_not_set_order_intent(self) -> None:
        state = SimpleNamespace(
            commerce_session={},
            stage="exploring",
            pending_action="",
            last_browse_query="",
        )
        prep = prepare_commerce_inbound(
            "فيه سدر",
            state=state,
            catalog=_HONEY_CATALOG,
        )
        assert prep.is_browse_inquiry is True
        assert prep.session.order_intent is False
        assert getattr(state, "stage", "") != "ordering"

    def test_explicit_order_still_sets_order_intent(self) -> None:
        state = SimpleNamespace(
            commerce_session={},
            stage="exploring",
            pending_action="",
        )
        prep = prepare_commerce_inbound(
            "أحتاج ربع كيلo من عسل الطلح",
            state=state,
            catalog=_HONEY_CATALOG,
        )
        assert prep.session.order_intent is True
        assert state.stage == "ordering"


class TestProductVisualScope:
    def test_honey_visual_query_extracts_group_not_stale_prefix(self) -> None:
        msg = "ابي اشوف صور للعسل"
        assert is_product_visual_request(msg) is True
        assert extract_visual_product_query(msg) == "عسل"

    def test_propolis_card_rejected_for_generic_honey_visual(self) -> None:
        allow, reason = attachment_matches_turn_request(
            inbound_message="ابي اشوف صور للعسل",
            attachment_title="عسل بالعكبر",
            brain_state={"current_product_focus": {"title": "عسل بالعكبر"}},
        )
        assert allow is False
        assert reason in {"explicit_scope_cross_form", "explicit_query_mismatch"}


class TestWaDraftInquirySafety:
    def test_browse_turn_does_not_inject_address(self) -> None:
        reply = compose_wa_order_flow_reply(
            order_prep={"cart_items": [], "delivery_address": ""},
            brain_state={},
            cart_changed=False,
            existing_reply="",
            customer_message="وش الانواع المتوفره",
        )
        assert reply is None

    def test_visual_turn_does_not_inject_price_confirmation(self) -> None:
        reply = compose_wa_order_flow_reply(
            order_prep={"cart_items": [], "delivery_address": ""},
            brain_state={},
            cart_changed=False,
            existing_reply="",
            customer_message="ابي اشوف صور للعسل",
        )
        assert reply is None
