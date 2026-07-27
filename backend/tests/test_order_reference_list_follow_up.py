"""Issue #709 — order-reference list follow-up routing and safety regressions."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.fallback_guard import RECENT_TOPIC_TTL_TURNS  # noqa: E402
from modules.ai.brain.commerce.ledger_follow_up import (  # noqa: E402
    _LEDGER_RECENT_TOPIC,
    is_ledger_context_active,
    is_order_reference_follow_up,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.store import DefaultStateStore  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import DEFAULT_PHONE_E164  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"

TURN1_LEDGER = "طلباتي السابقة عندكم"
TURN2_REFERENCE = "تعرف أرقامها؟"
TURN2_TYPO = "عتعرف ارقامها ؟"

# EM review (2026-07-27): plural «أرقام» + noun contact phrases that the first
# detector revision wrongly hijacked; earlier tests only covered singular «رقم».
PLURAL_CONTACT_PHRASES = (
    "وش أرقام التواصل معكم؟",
    "أرقام خدمة العملاء",
    "أرقام الجوالات عندكم؟",
    "أرقام الحساب البنكي",
    "وش أرقام الفروع؟",
    "أرقام المسؤولين",
)

PLURAL_CONTACT_ENGINE_BASELINES = (
    ("وش أرقام التواصل معكم؟", ACTION_LLM_REPLY, "identity_collaboration"),
    ("أرقام خدمة العملاء", ACTION_HANDOFF, None),
    ("أرقام الجوالات عندكم؟", ACTION_LLM_REPLY, "non_sales_ambiguous"),
    ("أرقام الحساب البنكي", ACTION_LLM_REPLY, "payment_info"),
    ("وش أرقام الفروع؟", ACTION_LLM_REPLY, "location_delivery"),
    ("أرقام المسؤولين", ACTION_LLM_REPLY, "non_sales_ambiguous"),
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
    history: list | None = None,
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
        history=history or [],
        commerce_bundle={},
        profile={"inbound_metadata": {}},
    )


def _decide(message: str, **kwargs) -> Decision:
    return DefaultDecisionEngine().decide(_ctx(message, **kwargs))


def _ledger_state_after_turn1() -> MerchantConversationState:
    turn1_intent = rules.match(TURN1_LEDGER)
    assert turn1_intent is not None
    turn1_decision = _decide(TURN1_LEDGER, intent=turn1_intent)
    return DefaultStateStore().transition(
        MerchantConversationState(),
        turn1_intent,
        turn1_decision,
    )


class TestOrderReferenceFollowUpHelpers:
    @pytest.mark.parametrize(
        "message",
        (
            TURN2_REFERENCE,
            TURN2_TYPO,
            "وش أرقامها؟",
            "أرقامها",
            "أرسل أرقام الطلبات",
            "وش أرقام الطلبات",
        ),
    )
    def test_reference_follow_up_phrasings_match(self, message: str) -> None:
        assert is_order_reference_follow_up(message) is True

    @pytest.mark.parametrize(
        "message",
        (
            "وش رقم المسؤول؟",
            "أرسل رقم الجوال",
            "ابي رقم خدمة العملاء",
        ),
    )
    def test_staff_contact_phrasings_do_not_match(self, message: str) -> None:
        assert is_order_reference_follow_up(message) is False


class TestPluralContactPhraseWhitelist:
    # EM review (2026-07-27): first revision only tested singular «رقم» forms.

    @pytest.mark.parametrize("message", PLURAL_CONTACT_PHRASES)
    def test_plural_contact_phrases_do_not_match_detector(self, message: str) -> None:
        assert is_order_reference_follow_up(message) is False

    @pytest.mark.parametrize(
        "message",
        (
            TURN2_REFERENCE,
            TURN2_TYPO,
            "وش أرقامها؟",
            "أرقامها",
            "أرسل أرقام الطلبات",
            "وش أرقام الطلبات",
            "ارقام طلبي",
        ),
    )
    def test_pronoun_and_order_bound_phrases_still_match(self, message: str) -> None:
        assert is_order_reference_follow_up(message) is True

    @pytest.mark.parametrize(
        "message,expected_action,expected_topic",
        PLURAL_CONTACT_ENGINE_BASELINES,
    )
    def test_plural_contact_phrases_not_hijacked_by_engine(
        self,
        message: str,
        expected_action: str,
        expected_topic: str | None,
    ) -> None:
        state = _ledger_state_after_turn1()
        intent = rules.match(message) or Intent(
            name=INTENT_GENERAL,
            confidence=0.5,
            raw_message=message,
        )
        decision = _decide(message, intent=intent, state=state)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.action == expected_action
        assert decision.args.get("topic") == expected_topic


class TestSequentialLedgerFollowUp:
    def test_two_turn_history_then_reference_list(self) -> None:
        turn1_intent = rules.match(TURN1_LEDGER)
        assert turn1_intent is not None
        assert turn1_intent.name == INTENT_ORDER_HISTORY_COUNT

        turn1_decision = _decide(TURN1_LEDGER, intent=turn1_intent)
        assert turn1_decision.action == ACTION_CUSTOMER_LEDGER_REPLY

        state = DefaultStateStore().transition(
            MerchantConversationState(),
            turn1_intent,
            turn1_decision,
        )
        assert state.last_intent == INTENT_ORDER_HISTORY_COUNT

        turn2_intent = rules.match(TURN2_REFERENCE) or Intent(
            name=INTENT_GENERAL,
            confidence=0.5,
            raw_message=TURN2_REFERENCE,
        )
        turn2_decision = _decide(TURN2_REFERENCE, intent=turn2_intent, state=state)
        assert turn2_decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert turn2_decision.args.get("ledger_topic") == INTENT_ORDER_REFERENCE_LIST

    def test_production_typo_reference_follow_up(self) -> None:
        state = MerchantConversationState()
        state.last_intent = INTENT_ORDER_HISTORY_COUNT
        state.last_action = ACTION_CUSTOMER_LEDGER_REPLY
        state.recent_topic = _LEDGER_RECENT_TOPIC
        state.recent_topic_turn = 0
        state.turn = 1

        turn2_intent = Intent(
            name=INTENT_GENERAL,
            confidence=0.5,
            raw_message=TURN2_TYPO,
        )
        decision = _decide(TURN2_TYPO, intent=turn2_intent, state=state)
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.args.get("ledger_topic") == INTENT_ORDER_REFERENCE_LIST


class TestSafetyNoLedgerContext:
    def test_reference_question_without_context_stays_staff_contact(self) -> None:
        """Pin today's behaviour: no ledger context → not ledger; staff_contact LLM."""
        decision = _decide(TURN2_REFERENCE)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "non_sales_ambiguous"
        assert decision.args.get("block_commerce_escalation") is True
        assert "staff_contact" in (decision.reason or "")


class TestSafetyStaffContactWithLedgerContext:
    @pytest.mark.parametrize("message", ("وش رقم المسؤول؟", "أرسل رقم الجوال"))
    def test_staff_number_ask_not_hijacked_by_ledger(self, message: str) -> None:
        state = MerchantConversationState()
        state.last_intent = INTENT_ORDER_HISTORY_COUNT
        state.last_action = ACTION_CUSTOMER_LEDGER_REPLY
        state.recent_topic = _LEDGER_RECENT_TOPIC
        state.recent_topic_turn = 0
        state.turn = 1

        intent = Intent(name=INTENT_GENERAL, confidence=0.5, raw_message=message)
        decision = _decide(message, intent=intent, state=state)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.action == ACTION_LLM_REPLY
        assert "staff_contact" in (decision.reason or "")


class TestLedgerContextRecency:
    def test_expired_stamped_context_does_not_activate(self) -> None:
        state = MerchantConversationState()
        state.recent_topic = _LEDGER_RECENT_TOPIC
        state.recent_topic_turn = 0
        state.turn = RECENT_TOPIC_TTL_TURNS + 2

        assert is_ledger_context_active(state) is False

        intent = Intent(name=INTENT_GENERAL, confidence=0.5, raw_message=TURN2_REFERENCE)
        decision = _decide(TURN2_REFERENCE, intent=intent, state=state)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY

    def test_stamped_context_active_within_ttl_window(self) -> None:
        state = MerchantConversationState()
        state.recent_topic = _LEDGER_RECENT_TOPIC
        state.recent_topic_turn = 0
        state.turn = RECENT_TOPIC_TTL_TURNS

        assert is_ledger_context_active(state) is True

    def test_last_intent_survives_one_turn_not_ttl_bounded(self) -> None:
        """``last_intent`` is overwritten each transition — not a multi-turn TTL."""
        state = MerchantConversationState()
        state.last_intent = INTENT_ORDER_HISTORY_COUNT
        state.turn = 99

        assert is_ledger_context_active(state) is True

    def test_last_intent_cleared_when_replaced(self) -> None:
        state = MerchantConversationState()
        state.last_intent = INTENT_GENERAL
        state.recent_topic = ""
        state.turn = 1

        assert is_ledger_context_active(state) is False


class TestExplicitIntentLedgerReferenceList:
    def test_explicit_reference_list_intent_routes_to_ledger(self) -> None:
        message = "أرسل أرقام الطلبات"
        matched = rules.match(message)
        intent = matched or Intent(
            name=INTENT_ORDER_REFERENCE_LIST,
            confidence=0.94,
            raw_message=message,
        )
        if matched is None or matched.name != INTENT_ORDER_REFERENCE_LIST:
            intent = Intent(
                name=INTENT_ORDER_REFERENCE_LIST,
                confidence=0.94,
                raw_message=message,
            )

        decision = _decide(message, intent=intent)
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.args.get("ledger_topic") == INTENT_ORDER_REFERENCE_LIST
