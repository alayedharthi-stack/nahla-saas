"""P1 — ledger intents must beat staff_contact_non_product social-NC gate."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import DEFAULT_PHONE_E164  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"

# Live RCA (T1): «رقم» in latest-order question must not lose to staff_contact_non_product.
LIVE_RCA_MESSAGE = "تعرف رقم اخر طلب لي؟"

_LEDGER_SIBLING_CASES = (
    (LIVE_RCA_MESSAGE, INTENT_LATEST_ORDER_SUMMARY),
    ("طلباتي السابقة كم؟", INTENT_ORDER_HISTORY_COUNT),
    ("أرسل أرقام الطلبات", INTENT_ORDER_REFERENCE_LIST),
)

_STAFF_CONTACT_MESSAGES = (
    "وش رقم المسؤول؟",
    "أرسل رقم الجوال",
)


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(
    message: str,
    *,
    intent: Intent | None = None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    if intent is None:
        intent = rules.match(message) or Intent(
            name=INTENT_GENERAL,
            confidence=0.5,
            raw_message=message,
        )
    return BrainContext(
        tenant_id=1,
        customer_phone=DEFAULT_PHONE_E164,
        message=message,
        intent=intent,
        state=state or MerchantConversationState(),
        facts=_facts(),
        history=[],
        commerce_bundle={},
        profile={"inbound_metadata": {}},
    )


def _decide(message: str, **kwargs) -> Decision:
    return DefaultDecisionEngine().decide(_ctx(message, **kwargs))


class TestLedgerIntentBeatsStaffContactNonCommerce:
    def test_live_rca_latest_order_summary_routes_to_ledger(self) -> None:
        matched = rules.match(LIVE_RCA_MESSAGE)
        assert matched is not None
        assert matched.name == INTENT_LATEST_ORDER_SUMMARY

        decision = _decide(LIVE_RCA_MESSAGE, intent=matched)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.args.get("ledger_topic") == INTENT_LATEST_ORDER_SUMMARY
        assert "staff_contact" not in (decision.reason or "")

    @pytest.mark.parametrize(
        ("message", "ledger_intent"),
        _LEDGER_SIBLING_CASES,
        ids=["latest_order_summary", "order_history_count", "order_reference_list"],
    )
    def test_ledger_sibling_intents_beat_staff_contact_nc(
        self,
        message: str,
        ledger_intent: str,
    ) -> None:
        matched = rules.match(message)
        intent = matched or Intent(
            name=ledger_intent,
            confidence=0.94,
            raw_message=message,
        )
        if matched is not None:
            assert matched.name == ledger_intent

        decision = _decide(message, intent=intent)
        if ledger_intent == INTENT_LATEST_ORDER_SUMMARY:
            assert decision.action == ACTION_LLM_REPLY
        else:
            assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.args.get("ledger_topic") == ledger_intent
        assert "staff_contact" not in (decision.reason or "")

    @pytest.mark.parametrize("message", _STAFF_CONTACT_MESSAGES)
    def test_staff_contact_questions_do_not_route_to_ledger(self, message: str) -> None:
        intent = Intent(name=INTENT_GENERAL, confidence=0.5, raw_message=message)
        decision = _decide(message, intent=intent)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.action == ACTION_LLM_REPLY
        assert "staff_contact" in (decision.reason or "")
