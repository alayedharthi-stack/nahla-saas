"""Regression: track_order compose must not crash on localized order totals."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.compose_amount import format_order_total_display, parse_compose_amount  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_TRACK_ORDER  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: E402
    evaluate_stale_checkout_suppression,
)

_GENERIC_ITEM = {
    "product_id": "sku-perfume-100",
    "product_name": "عطر ورد 100ml",
    "quantity": 1,
    "unit_price": 387.0,
}


class TestComposeAmountParsing:
    def test_numeric_total_parses(self) -> None:
        assert parse_compose_amount(741.0) == 741.0
        assert format_order_total_display(741.0, "SAR") == "741.00 SAR"

    def test_localized_string_total_parses(self) -> None:
        assert parse_compose_amount("387.00 ر.س") == 387.0
        assert format_order_total_display("387.00 ر.س", "SAR") == "387.00 SAR"

    def test_malformed_total_omitted_not_invented(self) -> None:
        assert parse_compose_amount("not-an-amount") is None
        assert format_order_total_display("not-an-amount", "SAR") is None


class TestTrackOrderTemplateTotals:
    def test_numeric_total_tenant1_style(self) -> None:
        body = T.order_status(
            reference="ORD-1001",
            status="payment_pending",
            status_label_ar="قيد إكمال الدفع",
            total=741.0,
            currency="SAR",
        )
        assert "741.00 SAR" in body
        assert "قيد إكمال الدفع" in body

    def test_localized_string_total_no_exception(self) -> None:
        body = T.order_status(
            reference="NHL-33-000012",
            status="payment_pending",
            status_label_ar="قيد إكمال الدفع",
            total="387.00 ر.س",
            currency="SAR",
            item_titles=["عطر ورد 100ml"],
        )
        assert "387.00" in body
        assert "قيد إكمال الدفع" in body
        assert "NHL-33-000012" in body

    def test_malformed_total_safe_reply(self) -> None:
        body = T.order_status(
            reference="ORD-MAL",
            status="under_review",
            status_label_ar="قيد المراجعة",
            total="???",
            currency="SAR",
        )
        assert "قيد المراجعة" in body
        assert "ORD-MAL" in body
        assert "الإجمالي" not in body


def _track_ctx(message: str = "وين طلبي؟") -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        message=message,
        intent=Intent(name="track_order", confidence=0.95, raw_message=message),
        state=MerchantConversationState(greeted=True),
        facts=CommerceFacts(has_products=True, store_name="متجر تجريبي عام"),
        history=[],
    )


def _track_result(*, total: object) -> ActionResult:
    return ActionResult(
        success=True,
        data={
            "reference": "NHL-33-000012",
            "status": "payment_pending",
            "status_label_ar": "قيد إكمال الدفع",
            "total": total,
            "currency": "SAR",
            "item_titles": ["عطر ورد 100ml"],
        },
    )


class TestTrackOrderComposeResponder:
    @pytest.mark.parametrize("total", [741.0, "387.00 ر.س", "???"])
    def test_compose_never_raises_on_total_shape(self, total: object) -> None:
        composer = DefaultComposer()
        decision = Decision(action=ACTION_TRACK_ORDER, args={})
        with patch(
            "modules.ai.brain.intent.link_disambiguation.should_use_generative_tracking_follow_up",
            return_value=False,
        ):
            reply = asyncio.run(
                composer.compose(decision, _track_result(total=total), _track_ctx()),
            )
        assert reply
        assert "NHL-33-000012" in reply
        assert "قيد إكمال الدفع" in reply

    def test_no_fake_tracking_number_in_reply(self) -> None:
        composer = DefaultComposer()
        decision = Decision(action=ACTION_TRACK_ORDER, args={})
        with patch(
            "modules.ai.brain.intent.link_disambiguation.should_use_generative_tracking_follow_up",
            return_value=False,
        ):
            reply = asyncio.run(
                composer.compose(
                    decision,
                    _track_result(total="387.00 ر.س"),
                    _track_ctx(),
                ),
            )
        assert "رقم التتبع" not in reply
        assert "tracking" not in reply.lower()
        assert "AWB" not in reply


class TestTrackOrderBypassesStaleCheckout:
    def test_active_draft_track_not_order_flow_v2(self) -> None:
        decision = evaluate_stale_checkout_suppression(
            message="وين طلبي؟",
            order_prep={"line_items": [dict(_GENERIC_ITEM)]},
            missing_fields=["payment_method"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is True
        assert decision.detected_intent == "track_order"
