"""Phase 1 contact escalation — detection, memory, telemetry."""
from __future__ import annotations

import logging

import pytest

from modules.ai.brain.commerce.contact_escalation import (
    append_staff_contact_sent,
    classify_employee_not_responding,
    classify_location_branch_failure,
    contact_already_sent,
    is_branch_list_request,
    is_branch_location_order_tail,
    log_contact_escalation,
    log_location_branch_failure,
    parse_staff_contacts_sent,
)
from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent.rules import match
from modules.ai.brain.types import (
    CommerceFacts,
    INTENT_ASK_LOCATION,
    INTENT_ASK_SHIPPING,
    INTENT_EMPLOYEE_NOT_RESPONDING,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    BrainContext,
    Intent,
    MerchantConversationState,
)


@pytest.mark.parametrize(
    "message",
    [
        "ما رد",
        "ما يرد",
        "ما جاوب",
        "ما يفتح",
        "ما رد علي",
        "اتصلت عليه وما رد",
        "كلمته وما يرد",
    ],
)
def test_employee_not_responding_detects_follow_up_phrases(message: str):
    assert classify_employee_not_responding(message) is not None


@pytest.mark.parametrize(
    "message",
    [
        "وين طلبي",
        "هل وصلت الشحنة",
        "ما وصلت الشحنة",
        "كم الشحن للرياض",
        "طريقة التوصيل",
        "عندي شكوى على المنتج",
        "كلموني",
        "حولني للموظف",
    ],
)
def test_employee_not_responding_does_not_steal_competing_intents(message: str):
    assert classify_employee_not_responding(message) is None


def test_intent_rules_prefer_track_order_over_employee_not_responding():
    intent = match("وين طلبي ما رد")
    assert intent is not None
    assert intent.name == INTENT_TRACK_ORDER


def test_intent_rules_classify_employee_not_responding():
    intent = match("ما رد علي")
    assert intent is not None
    assert intent.name == INTENT_EMPLOYEE_NOT_RESPONDING


def test_intent_rules_shipping_beats_employee_not_responding():
    intent = match("كم الشحن")
    assert intent is not None
    assert intent.name == INTENT_ASK_SHIPPING


def test_intent_rules_fresh_handoff_stays_talk_to_human():
    intent = match("كلموني")
    assert intent is not None
    assert intent.name == INTENT_TALK_HUMAN


def test_staff_contacts_sent_round_trip_in_brain_state():
    state = MerchantConversationState(
        staff_contacts_sent=[{"name": "أمين", "phone": "0501234567", "turn": 5}],
    )
    restored = MerchantConversationState.from_dict(state.to_dict())
    assert len(restored.staff_contacts_sent) == 1
    assert restored.staff_contacts_sent[0]["name"] == "أمين"
    assert restored.staff_contacts_sent[0]["turn"] == 5


def test_contact_already_sent_matches_name_or_phone():
    sent = parse_staff_contacts_sent([
        {"name": "أمين", "phone": "0501234567", "turn": 3},
    ])
    assert contact_already_sent(sent, name="أمين", phone="")
    assert contact_already_sent(sent, name="", phone="966501234567")
    assert not contact_already_sent(sent, name="هشام", phone="0509999999")


def test_append_staff_contact_sent_preserves_prior_entries():
    base = [{"name": "أمين", "phone": "0501111111", "turn": 2}]
    updated = append_staff_contact_sent(
        base, name="هشام", phone="0502222222", turn=4,
    )
    assert len(updated) == 2
    assert updated[1]["name"] == "هشام"
    assert updated[1]["turn"] == 4


def test_contact_escalation_log_line(caplog):
    caplog.set_level(logging.INFO, logger="nahla.brain.contact_escalation")
    log_contact_escalation(
        tenant_id=33,
        conversation_id=9063,
        trigger="employee_not_responding",
        context="post_location",
        name_source="history_bot",
        already_sent=True,
        selected_contact="أمين",
        contacts_sent_count=1,
    )
    line = next(
        r.message for r in caplog.records if "[CONTACT_ESCALATION]" in r.message
    )
    assert "trigger=employee_not_responding" in line
    assert "context=post_location" in line
    assert "name_source=history_bot" in line
    assert "already_sent=true" in line
    assert "selected_contact='أمين'" in line
    assert "contacts_sent_count=1" in line


class TestBranchRouting:
    def test_branch_list_request_intent(self):
        intent = match("أبغى الفروع")
        assert intent is not None
        assert intent.name == INTENT_ASK_LOCATION

    def test_branch_list_not_product_search(self):
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966500000001",
            message="أبغى الفروع",
            intent=match("أبغى الفروع") or Intent(
                name="ask_product", confidence=0.82, raw_message="أبغى الفروع",
            ),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "location_delivery"
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_branch_location_order_tail(self):
        assert is_branch_location_order_tail("الفروع")
        assert is_branch_list_request("أبغى الفروع")


@pytest.mark.parametrize(
    "message",
    [
        "الفرع مقفل",
        "الموقع غلط",
    ],
)
def test_location_branch_failure_standalone(message: str):
    verdict = classify_location_branch_failure(message)
    assert verdict is not None
    assert verdict.context == "standalone"


def test_location_branch_failure_ma_fatah_with_history():
    history = [{"direction": "in", "body": "وين موقعكم؟"}]
    verdict = classify_location_branch_failure("ما فتح", history=history)
    assert verdict is not None
    assert verdict.trigger == "location_failed"
    assert verdict.context == "post_location"


@pytest.mark.parametrize(
    "message",
    [
        "مقفل",
        "مبقفل",
    ],
)
def test_location_branch_failure_requires_context(message: str):
    assert classify_location_branch_failure(message) is None
    history = [{"direction": "in", "body": "وين موقعكم؟"}]
    verdict = classify_location_branch_failure(message, history=history)
    assert verdict is not None
    assert verdict.trigger == "branch_closed"
    assert verdict.context == "post_location"


def test_location_branch_failure_after_branch_ask():
    history = [
        {"direction": "in", "body": "وين موقعكم؟"},
        {"direction": "in", "body": "أبغى الفروع"},
    ]
    verdict = classify_location_branch_failure("مقفل", history=history)
    assert verdict is not None
    assert verdict.context == "post_branch_ask"


def test_location_branch_failure_log_line(caplog):
    caplog.set_level(logging.INFO, logger="nahla.brain.contact_escalation")
    log_location_branch_failure(
        tenant_id=33,
        conversation_id=9063,
        trigger="branch_closed",
        context="post_location",
        matched="مقفل",
        preview="مقفل",
    )
    line = next(
        r.message for r in caplog.records if "[LOCATION_BRANCH_FAILURE]" in r.message
    )
    assert "trigger=branch_closed" in line
    assert "context=post_location" in line
    assert "matched='مقفل'" in line
