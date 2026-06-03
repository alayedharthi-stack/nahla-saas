"""
tests/test_payment_transfer_promise.py
──────────────────────────────────────
Future-transfer promise routing — deterministic decision path while
awaiting payment receipt (no LLM fallback, no payment_reply_guard).
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.payment_intent import (
    PAYMENT_TRANSFER_PROMISE_REPLY_AR,
    detect_future_transfer_promise_text,
)
from modules.ai.brain.decision.actions import (
    ACTION_LLM_REPLY,
    ACTION_PAYMENT_TRANSFER_PROMISE,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.execution.executor import DefaultActionExecutor
from modules.ai.brain.compose.responder import DefaultComposer
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_EXPECTED_REPLY = (
    "تمام، بعد التحويل أرسل الإيصال هنا عشان نراجعه ونكمل الطلب 🌷"
)


def _awaiting_receipt_ctx(
    message: str,
    *,
    intent_name: str = "general",
) -> BrainContext:
    op = OrderPreparationState(
        awaiting_payment_receipt=True,
        product_id="p1",
        order_status="awaiting_receipt",
    )
    state = MerchantConversationState(
        greeted=True,
        stage="checkout",
        order_prep=op,
        current_product_focus={"title": "عسل سدر", "price": 120},
    )
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.5,
            raw_message=message,
            extraction_method="llm",
        ),
        state=state,
        facts=CommerceFacts(has_products=True, store_name="Test"),
    )


class TestDetectFutureTransferPromiseText:
    @pytest.mark.parametrize(
        "phrase",
        [
            "بحول لك",
            "بعد شوي احول لك",
            "أنا أحول الآن",
            "بحول وأرسل الإيصال",
        ],
    )
    def test_detects_future_promise_phrases(self, phrase: str) -> None:
        assert detect_future_transfer_promise_text(phrase) is True

    def test_past_claim_is_not_future_promise(self) -> None:
        assert detect_future_transfer_promise_text("تم التحويل") is False
        assert detect_future_transfer_promise_text("حولت لك") is False


class TestPaymentTransferPromiseDecision:
    @pytest.mark.parametrize(
        "phrase",
        [
            "بحول لك",
            "بعد شوي احول لك",
            "أنا أحول الآن",
            "بحول وأرسل الإيصال",
        ],
    )
    def test_routes_deterministically_with_general_intent(self, phrase: str) -> None:
        ctx = _awaiting_receipt_ctx(phrase, intent_name="general")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PAYMENT_TRANSFER_PROMISE
        assert decision.action != ACTION_LLM_REPLY
        assert "future transfer" in decision.reason.lower()

    def test_does_not_fire_without_awaiting_receipt(self) -> None:
        ctx = _awaiting_receipt_ctx("بحول لك")
        ctx.state.order_prep.awaiting_payment_receipt = False
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY

    def test_past_claim_does_not_use_transfer_promise_action(self) -> None:
        ctx = _awaiting_receipt_ctx("تم التحويل")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PAYMENT_TRANSFER_PROMISE


class TestPaymentTransferPromiseCompose:
    @pytest.mark.parametrize(
        "phrase",
        [
            "بحول لك",
            "بعد شوي احول لك",
            "أنا أحول الآن",
            "بحول وأرسل الإيصال",
        ],
    )
    def test_end_to_end_deterministic_reply(self, phrase: str) -> None:
        async def _run() -> None:
            ctx = _awaiting_receipt_ctx(phrase, intent_name="general")
            decision = DefaultDecisionEngine().decide(ctx)
            assert decision.action == ACTION_PAYMENT_TRANSFER_PROMISE

            result = await DefaultActionExecutor().execute(decision, ctx)
            reply = await DefaultComposer().compose(decision, result, ctx)

            assert reply == _EXPECTED_REPLY
            assert reply == PAYMENT_TRANSFER_PROMISE_REPLY_AR
            assert result.data.get("chosen_path") == "payment_transfer_promise"

        asyncio.run(_run())
