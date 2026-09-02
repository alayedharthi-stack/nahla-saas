"""ORDER-SUPPORT-D1 — structural ownership across natural order phrasing.

Asserts owner, action, facts, and isolation. Does not pin customer-facing wording.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.customer_commerce_ledger import resolve_customer_commerce_profile  # noqa: E402
from modules.ai.brain.commerce.customer_order_evidence import (  # noqa: E402
    collect_customer_order_evidence,
)
from modules.ai.brain.commerce.ledger_follow_up import (  # noqa: E402
    is_ledger_context_active,
    is_order_reference_follow_up,
)
from modules.ai.brain.commerce.order_support_ownership import (  # noqa: E402
    has_authoritative_order_support_ownership,
    should_stamp_ledger_context,
)
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.product_information_topic import (  # noqa: E402
    TOPIC_PRODUCT_USAGE_INFORMATION,
    detect_product_information_topic_shift,
)
from modules.ai.brain.state.store import DefaultStateStore  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_GENERAL,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
    INTENT_TRACK_ORDER,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_SHOE = "حذاء رياضي أبيض"
GENERIC_SHIRT = "قميص قطني أزرق"
GENERIC_PERFUME = "عطر ورد 100ml"
LATEST_REF = "284719628"
OLDER_REF = "284719100"
COUNT_FAMILY = (
    "ابي اشوف كم مرة طلبت منكم سابقا",
    "كم طلب سويت عندكم من قبل؟",
)
NUMBER_FOLLOW_FAMILY = (
    "ابي ارقامها",
    "عطني أرقامهم",
)
LATEST_FAMILY = (
    "رقم آخر طلب",
    "ما آخر طلب لي؟",
)
PRODUCT_USAGE_FAMILY = (
    "كم مرة استخدمه باليوم؟",
    "كم جرعة باليوم؟",
)


def _layer2_track_order(message: str) -> Intent:
    return Intent(
        name=INTENT_TRACK_ORDER,
        confidence=0.72,
        slots={
            "semantic_owner": "brain_classifier",
            "classification_provenance": "LAYER2_SEMANTIC_OVERRIDE",
            "precedence_winner": "layer2",
            "layer2_result": INTENT_TRACK_ORDER,
            "semantic_relation": "authoritative_override",
        },
        raw_message=message,
        extraction_method="llm",
    )


def _noisy_track_order(message: str) -> Intent:
    return Intent(
        name=INTENT_TRACK_ORDER,
        confidence=0.72,
        slots={},
        raw_message=message,
        extraction_method="llm",
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
    tenant_id: int = 1,
    phone: str = DEFAULT_PHONE_E164,
    customer_id: int | None = None,
) -> BrainContext:
    if intent is None:
        intent = rules.match(message) or Intent(
            name=INTENT_GENERAL,
            confidence=0.5,
            raw_message=message,
        )
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        customer_id=customer_id,
        message=message,
        intent=intent,
        state=state or MerchantConversationState(),
        facts=_facts(),
        history=history or [],
        commerce_bundle={},
        profile={"inbound_metadata": {}},
    )


def _decide(message: str, **kwargs):
    return DefaultDecisionEngine().decide(_ctx(message, **kwargs))


def _is_order_support(decision) -> bool:
    args = decision.args or {}
    if decision.action == ACTION_CUSTOMER_LEDGER_REPLY:
        return True
    if decision.action == ACTION_TRACK_ORDER:
        return True
    if decision.action == ACTION_LLM_REPLY and (
        args.get("topic") in {"order_history", "latest_order_summary"}
        or args.get("ledger_topic")
    ):
        return True
    return False


def _commerce_blocked(decision) -> bool:
    return bool((decision.args or {}).get("block_commerce_escalation"))


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def world(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    visible_statuses = ("in_progress", "in_progress", "in_progress", "delivered")
    visible_refs = (LATEST_REF, "284719293", "284719477", "284719659")
    for idx, (status, ref) in enumerate(zip(visible_statuses, visible_refs)):
        row = seed_order(
            db,
            tenant.id,
            status=status,
            source="salla",
            external_id=ref,
            external_order_number=ref,
            customer_info={"phone": DEFAULT_PHONE_E164, "mobile": DEFAULT_PHONE_E164},
            line_items=[{"title": GENERIC_SHOE if idx == 0 else GENERIC_SHIRT, "quantity": 1}],
            extra_metadata={"created_at": (base + timedelta(days=30 - idx)).isoformat()},
        )
        row.customer_id = None
    for idx, ref in enumerate(("284719315", "284719976", "284719245")):
        row = seed_order(
            db,
            tenant.id,
            status="cancelled",
            source="salla",
            external_id=ref,
            external_order_number=ref,
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[{"title": GENERIC_PERFUME, "quantity": 1}],
            extra_metadata={"created_at": (base + timedelta(days=idx)).isoformat()},
        )
        row.customer_id = None
    abandoned = seed_order(
        db,
        tenant.id,
        status="abandoned",
        source="salla",
        external_id="284719136",
        external_order_number="284719136",
        customer_info={"phone": DEFAULT_PHONE_E164},
        extra_metadata={"created_at": base.isoformat()},
    )
    abandoned.customer_id = None
    db.commit()
    other = seed_tenant(db, name="متجر تجريبي آخر")
    seed_order(
        db,
        other.id,
        status="delivered",
        source="salla",
        external_id="999888777",
        external_order_number="999888777",
        customer_info={"phone": DEFAULT_PHONE_E164},
        extra_metadata={"created_at": base.isoformat()},
    )
    return SimpleNamespace(
        db=db,
        tenant_id=tenant.id,
        other_tenant_id=other.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


class TestPrimarySocialNcBoundary:
    @pytest.mark.parametrize("message", NUMBER_FOLLOW_FAMILY)
    def test_layer2_track_order_yields_to_order_support(self, message: str) -> None:
        intent = _layer2_track_order(message)
        social = resolve_current_turn_social_non_commerce(message, intent=intent)
        assert social.matched is False
        decision = _decide(message, intent=intent)
        assert _is_order_support(decision)
        assert (decision.args or {}).get("topic") != "non_sales_ambiguous"
        assert _commerce_blocked(decision) is False

    def test_noisy_072_without_provenance_does_not_become_order_support(self) -> None:
        message = "وش رقم المسؤول؟"
        intent = _noisy_track_order(message)
        social = resolve_current_turn_social_non_commerce(message, intent=intent)
        assert social.matched is True
        assert social.category == "staff_contact"
        decision = _decide(message, intent=intent)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert _is_order_support(decision) is False
        assert "staff_contact" in (decision.reason or "")


class TestCanonicalOwnershipContract:
    def test_layer2_provenance_is_authoritative(self) -> None:
        intent = _layer2_track_order("placeholder")
        assert has_authoritative_order_support_ownership(intent) is True
        assert should_stamp_ledger_context(intent) is True

    def test_high_confidence_order_intent_is_authoritative(self) -> None:
        intent = Intent(name=INTENT_ORDER_HISTORY_COUNT, confidence=0.94)
        assert has_authoritative_order_support_ownership(intent) is True

    def test_noisy_low_confidence_does_not_own_or_stamp(self) -> None:
        intent = _noisy_track_order("وش رقم المسؤول؟")
        state = MerchantConversationState()
        state.last_intent = INTENT_ORDER_HISTORY_COUNT
        state.last_action = ACTION_CUSTOMER_LEDGER_REPLY
        state.recent_topic = "customer_ledger"
        state.recent_topic_turn = 0
        state.turn = 1
        assert has_authoritative_order_support_ownership(intent, state=state) is False
        assert should_stamp_ledger_context(intent, state=state) is False
        decision = _decide("وش رقم المسؤول؟", intent=intent, state=state)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert _is_order_support(decision) is False
        assert is_ledger_context_active(state) is True


class TestProductInformationBoundary:
    @pytest.mark.parametrize("message", COUNT_FAMILY)
    def test_order_history_count_not_product_usage(self, message: str) -> None:
        intent = _layer2_track_order(message)
        assert detect_product_information_topic_shift(message, intent=intent) is False
        decision = _decide(message, intent=intent)
        assert (decision.args or {}).get("topic") != TOPIC_PRODUCT_USAGE_INFORMATION
        assert "product_information_topic_shift" not in (decision.reason or "")
        assert _is_order_support(decision)

    @pytest.mark.parametrize("message", PRODUCT_USAGE_FAMILY)
    def test_real_product_usage_still_owns(self, message: str) -> None:
        state = MerchantConversationState(
            current_product_focus={"id": 11, "title": GENERIC_PERFUME},
        )
        assert detect_product_information_topic_shift(message, state=state) is True
        decision = _decide(message, state=state)
        assert decision.action == ACTION_LLM_REPLY
        assert (decision.args or {}).get("topic") == TOPIC_PRODUCT_USAGE_INFORMATION


class TestContractedPhrasesUnchanged:
    def test_previous_orders_count(self) -> None:
        decision = _decide("طلباتي السابقة عندكم")
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert (decision.args or {}).get("ledger_topic") == INTENT_ORDER_HISTORY_COUNT

    def test_know_the_numbers_after_ledger_context(self) -> None:
        state = MerchantConversationState()
        state.last_intent = INTENT_ORDER_HISTORY_COUNT
        state.last_action = ACTION_CUSTOMER_LEDGER_REPLY
        state.recent_topic = "customer_ledger"
        state.recent_topic_turn = 0
        state.turn = 1
        decision = _decide("تعرف أرقامها؟", state=state)
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert (decision.args or {}).get("ledger_topic") == INTENT_ORDER_REFERENCE_LIST

    def test_send_order_numbers(self) -> None:
        decision = _decide("أرسل أرقام الطلبات")
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert (decision.args or {}).get("ledger_topic") == INTENT_ORDER_REFERENCE_LIST

    def test_latest_order(self) -> None:
        decision = _decide("ما آخر طلب لي؟")
        assert decision.action == ACTION_LLM_REPLY
        assert (decision.args or {}).get("ledger_topic") == INTENT_LATEST_ORDER_SUMMARY

    def test_where_is_my_order(self) -> None:
        decision = _decide("وين طلبي؟")
        assert decision.action == ACTION_TRACK_ORDER

    def test_explicit_known_reference_continuity(self) -> None:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            try_order_reference_continuity_decision,
        )

        history = [{"direction": "in", "body": LATEST_REF}]
        ctx = _ctx(
            "الطلب متأخر والشحن ما وصل",
            history=history,
            intent=Intent(name=INTENT_GENERAL, confidence=0.5, raw_message="الطلب متأخر والشحن ما وصل"),
        )
        ctx.state.draft_order_id = "draft-16"
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert (dec.args or {}).get("topic") == "existing_order_support"


class TestStaffAndBankBoundaries:
    def test_customer_service_numbers_not_ledger(self) -> None:
        decision = _decide("أرقام خدمة العملاء")
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.action == ACTION_HANDOFF

    def test_manager_contact_not_ledger(self) -> None:
        decision = _decide("وش رقم المسؤول؟")
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert "staff_contact" in (decision.reason or "")

    def test_bank_account_numbers_not_order_reference(self) -> None:
        state = MerchantConversationState()
        state.last_intent = INTENT_ORDER_HISTORY_COUNT
        state.last_action = ACTION_CUSTOMER_LEDGER_REPLY
        state.recent_topic = "customer_ledger"
        state.recent_topic_turn = 0
        state.turn = 1
        assert is_order_reference_follow_up("أرقام الحساب البنكي") is False
        decision = _decide("أرقام الحساب البنكي", state=state)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert (decision.args or {}).get("ledger_topic") != INTENT_ORDER_REFERENCE_LIST


class TestLiveFixtureReplay:
    def test_before_repair_shapes_no_longer_own(self) -> None:
        count_msg = COUNT_FAMILY[0]
        numbers_msg = NUMBER_FOLLOW_FAMILY[0]
        assert detect_product_information_topic_shift(
            count_msg,
            intent=_layer2_track_order(count_msg),
        ) is False
        social = resolve_current_turn_social_non_commerce(
            numbers_msg,
            intent=_layer2_track_order(numbers_msg),
        )
        assert social.matched is False

    def test_conversation_journey_owners_and_local_truth(self, world) -> None:
        store = DefaultStateStore()
        state = MerchantConversationState(turn=1)
        turn1_msg = COUNT_FAMILY[0]
        turn1_intent = _layer2_track_order(turn1_msg)
        turn1 = _decide(turn1_msg, intent=turn1_intent, state=state)
        assert _is_order_support(turn1)
        assert (turn1.args or {}).get("topic") != TOPIC_PRODUCT_USAGE_INFORMATION
        assert _commerce_blocked(turn1) is False
        assert is_ledger_context_active(state) is True

        state = store.transition(state, turn1_intent, turn1)
        state.last_action = turn1.action
        turn2_msg = NUMBER_FOLLOW_FAMILY[0]
        turn2_intent = _layer2_track_order(turn2_msg)
        turn2 = _decide(turn2_msg, intent=turn2_intent, state=state)
        assert turn2.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert (turn2.args or {}).get("ledger_topic") == INTENT_ORDER_REFERENCE_LIST
        assert _commerce_blocked(turn2) is False

        state = store.transition(state, turn2_intent, turn2)
        state.last_action = turn2.action
        turn3_msg = LATEST_FAMILY[0]
        turn3_intent = _layer2_track_order(turn3_msg)
        turn3 = _decide(turn3_msg, intent=turn3_intent, state=state)
        assert _is_order_support(turn3)
        assert (turn3.args or {}).get("topic") != "non_sales_ambiguous"
        assert _commerce_blocked(turn3) is False

        profile = resolve_customer_commerce_profile(
            world.db,
            tenant_id=world.tenant_id,
            customer_id=world.customer_id,
            phone=world.phone,
            include_abandoned=False,
            include_cancelled=True,
        )
        evidence = collect_customer_order_evidence(
            db=world.db,
            tenant_id=world.tenant_id,
            phone=world.phone,
            customer_id=world.customer_id,
            conversation_id=world.conversation_id,
        )
        assert int(profile.order_counts.total_orders or 0) == 7
        assert evidence is not None
        assert int(evidence.get("order_count") or 0) == 7
        latest = evidence.get("latest_order") or {}
        assert str(latest.get("display_reference") or LATEST_REF).endswith("628")
        refs = {
            str(row.get("display_reference") or "")
            for row in (evidence.get("orders") or [])
        }
        assert LATEST_REF in refs or any(item.endswith("628") for item in refs)
        assert "999888777" not in refs

    def test_second_count_paraphrase_same_owner(self) -> None:
        decision = _decide(COUNT_FAMILY[1], intent=_layer2_track_order(COUNT_FAMILY[1]))
        assert _is_order_support(decision)

    def test_second_number_follow_up_same_owner(self) -> None:
        state = MerchantConversationState()
        state.last_intent = INTENT_ORDER_HISTORY_COUNT
        state.last_action = ACTION_CUSTOMER_LEDGER_REPLY
        state.recent_topic = "customer_ledger"
        state.recent_topic_turn = 0
        state.turn = 1
        decision = _decide(
            NUMBER_FOLLOW_FAMILY[1],
            intent=_layer2_track_order(NUMBER_FOLLOW_FAMILY[1]),
            state=state,
        )
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert (decision.args or {}).get("ledger_topic") == INTENT_ORDER_REFERENCE_LIST


class TestIsolationAndEmpty:
    def test_same_phone_other_tenant_does_not_leak(self, world) -> None:
        payload = collect_customer_order_evidence(
            db=world.db,
            tenant_id=world.other_tenant_id,
            phone=world.phone,
            customer_id=None,
        )
        refs = {
            str(row.get("display_reference") or "")
            for row in ((payload or {}).get("orders") or [])
        }
        assert LATEST_REF not in refs
        assert "284719293" not in refs
        other_count = int((payload or {}).get("order_count") or 0)
        assert other_count == 1

    def test_no_local_orders_is_honest(self, db) -> None:
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
        payload = collect_customer_order_evidence(
            db=db,
            tenant_id=tenant.id,
            phone=DEFAULT_PHONE_E164,
            customer_id=customer.id,
        )
        assert payload is not None
        assert int(payload.get("order_count") or 0) == 0
        assert list(payload.get("orders") or []) == []

    def test_stale_checkout_does_not_steal_order_support(self) -> None:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            try_order_reference_continuity_decision,
        )

        history = [{"direction": "in", "body": LATEST_REF}]
        state = MerchantConversationState(draft_order_id="draft-16")
        ctx = _ctx(
            "الطلب فيه حذاء رياضي أبيض",
            history=history,
            state=state,
            intent=Intent(
                name=INTENT_GENERAL,
                confidence=0.5,
                raw_message="الطلب فيه حذاء رياضي أبيض",
            ),
        )
        ctx.state.order_prep.awaiting_checkout_channel = True
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert (dec.args or {}).get("topic") == "existing_order_support"
        count_decision = _decide(
            COUNT_FAMILY[0],
            intent=_layer2_track_order(COUNT_FAMILY[0]),
            state=state,
        )
        assert _is_order_support(count_decision)
        assert (count_decision.args or {}).get("topic") != "purchase_channel_selection"
