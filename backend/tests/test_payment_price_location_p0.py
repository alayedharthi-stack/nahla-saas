"""
tests/test_payment_price_location_p0.py
P0 regression: payment receipt gating, price objection, explicit location.
"""
from __future__ import annotations

import os
import sys
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.payment_intent import (  # noqa: E402
    detect_payment_confirmation_text,
    has_explicit_payment_receipt_evidence,
    is_generic_payment_acknowledgement_only,
    maybe_handle_payment_claim,
)
from core.wa_order_linking import MSG_WA_PAYMENT_UNLINKED  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_FAQ_REPLY,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.faq import TOPIC_LOCATION  # noqa: E402
from modules.ai.brain.intent.link_disambiguation import (  # noqa: E402
    looks_like_physical_location_request,
)
from modules.ai.brain.intent.rules import match  # noqa: E402
from modules.ai.brain.state.price_objection_topic import (  # noqa: E402
    detect_price_objection_topic_shift,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_ASK_LOCATION,
    MerchantConversationState,
)


_MAPS = "https://maps.app.goo.gl/test-branch"
_PRICE_MSG = (
    "صباحكم سعيد\n"
    "الكيلو عند منافسيكم جوده ولون وطعم ٢٠٠ ريال.\n"
    "لماذا عندكم بهذا السعر العالي\n"
    "نحن عملاء شبه جمله\n"
    "ليsh؟"
).replace("ليsh", "ليش")


def _ctx(
    message: str,
    *,
    state: Optional[MerchantConversationState] = None,
    maps_url: str = "",
    intent_name: str = "general",
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.5, raw_message=message),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(orderable=True, maps_url=maps_url or _MAPS),
        history=[],
    )


class TestPaymentReceiptGating:
    def test_tamam_is_not_payment_evidence(self) -> None:
        assert is_generic_payment_acknowledgement_only("تمام")
        assert not detect_payment_confirmation_text("تمام")
        assert not has_explicit_payment_receipt_evidence("تمام")

    def test_tamam_does_not_trigger_payment_claim_short_circuit(self) -> None:
        with patch("core.order_flow._load_brain_state", return_value=(MagicMock(), {})):
            result = maybe_handle_payment_claim(
                MagicMock(),
                tenant_id=1,
                phone="966500000001",
                inbound_text="تمام",
                has_attached_media=False,
            )
        assert result is None

    def test_transfer_claim_still_detected(self) -> None:
        assert detect_payment_confirmation_text("تم التحويل")
        assert has_explicit_payment_receipt_evidence("تم التحويل")

    def test_unlinked_transfer_still_replies_when_no_order(self) -> None:
        with patch("core.order_flow._load_brain_state", return_value=(MagicMock(), {})):
            with patch(
                "core.wa_order_linking.find_linkable_wa_order",
                return_value=None,
            ):
                result = maybe_handle_payment_claim(
                    MagicMock(),
                    tenant_id=1,
                    phone="966500000001",
                    inbound_text="تم الدفع",
                    has_attached_media=False,
                )
        assert result is not None
        assert MSG_WA_PAYMENT_UNLINKED in (result.get("reply_text") or "")


class TestPriceObjectionRouting:
    def test_price_objection_detected(self) -> None:
        assert detect_price_objection_topic_shift(_PRICE_MSG)

    def test_price_objection_routes_to_llm_not_stub(self) -> None:
        ctx = _ctx(_PRICE_MSG)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "price_objection"
        assert "وصلت رسالتك" not in str(decision.args.get("reply") or "")


class TestExplicitLocationRequest:
    @pytest.mark.parametrize("message", ["ارسل موقعه", "أرسل موقعه", "وين الموقع"])
    def test_location_phrases_detected(self, message: str) -> None:
        assert looks_like_physical_location_request(message)

    def test_arcel_mawqe_routes_to_faq_with_maps(self) -> None:
        msg = "ارسل موقعه"
        assert match(msg).name == INTENT_ASK_LOCATION
        ctx = _ctx(msg, maps_url=_MAPS, intent_name=INTENT_ASK_LOCATION)
        ctx.intent = Intent(name=INTENT_ASK_LOCATION, confidence=0.93, raw_message=msg)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_FAQ_REPLY
        assert decision.args.get("topic") == TOPIC_LOCATION
