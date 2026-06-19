"""Tests for Phase 1 order-flow evidence telemetry."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.observability.order_flow_evidence import (  # noqa: E402
    detect_input_types,
    infer_why_focus_changed,
    is_generic_ack_stub,
    reply_acknowledges_important_input,
    snapshot_focus,
)
from modules.ai.brain.types import MerchantConversationState, OrderPreparationState  # noqa: E402


class TestGenericAckRule:
    def test_stub_detected(self) -> None:
        assert is_generic_ack_stub("تمام 🌷 وصلت رسالتك.")

    def test_important_name_with_stub_not_acknowledged(self) -> None:
        types = detect_input_types(message="فايز الصبحي أبو نايف")
        assert "name" in types
        assert not reply_acknowledges_important_input(
            reply="تمام 🌷 وصلت رسالتك.",
            input_types=types,
        )

    def test_name_with_order_continue_is_acknowledged(self) -> None:
        types = detect_input_types(message="فايز الصبحي")
        assert reply_acknowledges_important_input(
            reply="الله يحييك يا فايز، أرسل الرمز المختصر للعنوان.",
            input_types=types,
            state=MerchantConversationState(
                order_prep=OrderPreparationState(customer_first_name="فايز"),
            ),
        )


class TestConversationFocus:
    def test_focus_cleared_detected(self) -> None:
        before = snapshot_focus(
            MerchantConversationState(
                current_product_focus={"title": "عسل طلح"},
                order_prep=OrderPreparationState(quantity=2),
            )
        )
        after = snapshot_focus(MerchantConversationState())
        assert infer_why_focus_changed(before, after) == "focus_cleared"

    def test_line_items_change_detected(self) -> None:
        before = snapshot_focus(
            MerchantConversationState(
                order_prep=OrderPreparationState(
                    line_items=[{"product_name": "عسل", "quantity": 1}],
                ),
            )
        )
        after = snapshot_focus(
            MerchantConversationState(
                order_prep=OrderPreparationState(
                    line_items=[{"product_name": "عسل", "variant": "1kg", "quantity": 2}],
                ),
            )
        )
        assert infer_why_focus_changed(before, after) == "line_items_changed"


class TestInputTypeDetection:
    def test_quantity_message(self) -> None:
        assert "quantity" in detect_input_types(message="نص كيلo ونص كيلo")

    def test_address_image_metadata(self) -> None:
        types = detect_input_types(
            message="",
            inbound_metadata={"image_kind": "national_address_card", "normalized_type": "image"},
        )
        assert "address" in types
        assert "location" in types
