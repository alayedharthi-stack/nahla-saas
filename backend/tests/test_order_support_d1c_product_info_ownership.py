"""ORDER-SUPPORT-D1C — product-information ownership requires current-turn product scope.

Uses the real rule classifier for the live count phrasing. Does not inject
Layer-2 track_order. Does not enumerate production phrases.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.order_support_ownership import (  # noqa: E402
    has_authoritative_order_support_ownership,
    should_stamp_ledger_context,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.product_information_topic import (  # noqa: E402
    TOPIC_PRODUCT_USAGE_INFORMATION,
    detect_customer_owned_product_reference,
    detect_product_information_topic_shift,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_ASK_PRODUCT,
    INTENT_LATEST_ORDER_SUMMARY,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_PERFUME = "عطر ورد 100ml"
GENERIC_SHOE = "حذاء رياضي أبيض"
LIVE_COUNT = "ابي اشوف كم مرة طلبت منكم سابقا"
COUNT_PARAPHRASES = (
    LIVE_COUNT,
    "كم طلب سويت عندكم من قبل؟",
    "كم مرة اشتريت منكم؟",
    "كم توصيلة سابقة لي؟",
)


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _stale_checkout_state() -> MerchantConversationState:
    """Live-shaped leftover checkout focus — not current-turn product scope."""
    return MerchantConversationState(
        stage="ordering",
        turn=736,
        product_focus_turn=729,
        current_product_focus={
            "id": 28,
            "title": "جاكت",
            "external_id": "1921568272",
            "price": "169.0",
        },
        order_prep=OrderPreparationState(
            product_id="1921568272",
            missing_fields=["city", "delivery_address"],
        ),
    )


def _scoped_perfume_state() -> MerchantConversationState:
    return MerchantConversationState(
        turn=3,
        product_focus_turn=2,
        current_product_focus={
            "id": 11,
            "title": GENERIC_PERFUME,
            "external_id": "perfume-11",
        },
    )


def _rule_intent(message: str) -> Intent:
    matched = rules.match(message)
    assert matched is not None
    return matched


def _ctx(
    message: str,
    *,
    intent: Intent | None = None,
    state: MerchantConversationState | None = None,
    tenant_id: int = 1,
) -> BrainContext:
    if intent is None:
        intent = rules.match(message) or Intent(
            name="general",
            confidence=0.5,
            raw_message=message,
        )
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000000",
        customer_id=1,
        message=message,
        intent=intent,
        state=state or MerchantConversationState(),
        facts=_facts(),
        history=[],
        commerce_bundle={},
        profile={"inbound_metadata": {}},
    )


def _decide(message: str, **kwargs):
    return DefaultDecisionEngine().decide(_ctx(message, **kwargs))


class TestLiveCountRealClassifier:
    def test_rule_classifier_is_ask_product_not_injected_os(self) -> None:
        intent = _rule_intent(LIVE_COUNT)
        assert intent.name == INTENT_ASK_PRODUCT
        assert abs(float(intent.confidence) - 0.82) < 0.001
        assert str(getattr(intent, "extraction_method", "") or "") == "rules"
        assert has_authoritative_order_support_ownership(intent) is False

    def test_stale_focus_count_yields_product_information(self) -> None:
        intent = _rule_intent(LIVE_COUNT)
        state = _stale_checkout_state()
        assert detect_product_information_topic_shift(
            LIVE_COUNT,
            state=state,
            intent=intent,
        ) is False

    def test_engine_after_yield_is_not_product_usage(self) -> None:
        intent = _rule_intent(LIVE_COUNT)
        state = _stale_checkout_state()
        decision = _decide(LIVE_COUNT, intent=intent, state=state)
        args = decision.args or {}
        assert args.get("topic") != TOPIC_PRODUCT_USAGE_INFORMATION
        assert "product_information_topic_shift" not in (decision.reason or "")
        assert has_authoritative_order_support_ownership(intent, state=state) is False
        assert should_stamp_ledger_context(intent, state=state) is False
        # Next owner after product-info yield — observed, not repaired here.
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "rule_based_checkout" in (decision.reason or "")


class TestCountParaphrasesLackProductScope:
    @pytest.mark.parametrize("message", COUNT_PARAPHRASES)
    def test_count_paraphrases_yield_without_product_scope(self, message: str) -> None:
        state = _stale_checkout_state()
        assert detect_product_information_topic_shift(message, state=state) is False


class TestGenuineProductUsagePreserved:
    def test_deictic_pronoun_usage_with_fresh_canonical_product(self) -> None:
        message = "كم مرة استخدم هذا باليوم؟"
        state = _scoped_perfume_state()
        assert detect_product_information_topic_shift(message, state=state) is True
        decision = _decide(message, state=state)
        assert decision.action == ACTION_LLM_REPLY
        assert (decision.args or {}).get("topic") == TOPIC_PRODUCT_USAGE_INFORMATION

    def test_named_referent_dosage_question(self) -> None:
        message = "كم جرعة باليوم لعطر ورد؟"
        state = _scoped_perfume_state()
        assert detect_product_information_topic_shift(message, state=state) is True
        decision = _decide(message, state=state)
        assert (decision.args or {}).get("topic") == TOPIC_PRODUCT_USAGE_INFORMATION

    def test_customer_owned_product_usage(self) -> None:
        message = "المنتج اللي عندي كيف استخدمه؟"
        assert detect_customer_owned_product_reference(message) is True
        assert detect_product_information_topic_shift(message) is True
        decision = _decide(message)
        assert (decision.args or {}).get("topic") == TOPIC_PRODUCT_USAGE_INFORMATION

    def test_how_to_use_this_with_fresh_focus(self) -> None:
        message = "كيف استخدم هذا؟"
        state = _scoped_perfume_state()
        assert detect_product_information_topic_shift(message, state=state) is True


class TestStaleFocusIsNotProductScope:
    def test_stale_jacket_focus_does_not_own_frequency(self) -> None:
        state = _stale_checkout_state()
        assert detect_product_information_topic_shift(
            "كم مرة طلبت منكم سابقا",
            state=state,
        ) is False

    def test_generic_non_product_frequency_not_usage(self) -> None:
        state = _stale_checkout_state()
        assert detect_product_information_topic_shift(
            "كم مرة بالاسبوع",
            state=state,
        ) is False

    def test_ask_product_intent_alone_is_not_scope(self) -> None:
        intent = Intent(
            name=INTENT_ASK_PRODUCT,
            confidence=0.82,
            raw_message=LIVE_COUNT,
            extraction_method="rules",
        )
        assert detect_product_information_topic_shift(
            LIVE_COUNT,
            intent=intent,
        ) is False


class TestOperationalBoundaries:
    def test_staff_contact_not_product_usage(self) -> None:
        decision = _decide("وش رقم المسؤول؟")
        assert (decision.args or {}).get("topic") != TOPIC_PRODUCT_USAGE_INFORMATION
        assert "staff_contact" in (decision.reason or "") or decision.action == ACTION_HANDOFF

    def test_latest_order_not_product_usage(self) -> None:
        decision = _decide("ما آخر طلب لي؟")
        assert (decision.args or {}).get("topic") != TOPIC_PRODUCT_USAGE_INFORMATION
        assert (decision.args or {}).get("ledger_topic") == INTENT_LATEST_ORDER_SUMMARY

    def test_track_order_not_product_usage(self) -> None:
        decision = _decide("وين طلبي؟")
        assert decision.action == ACTION_TRACK_ORDER

    def test_checkout_confirm_still_continues(self) -> None:
        state = _stale_checkout_state()
        decision = _decide(
            "تمام كمل الطلب",
            intent=Intent(name="general", confidence=0.5, raw_message="تمام كمل الطلب"),
            state=state,
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_tenant_isolation_on_decide_path(self) -> None:
        intent = _rule_intent(LIVE_COUNT)
        state = _stale_checkout_state()
        a = DefaultDecisionEngine().decide(
            _ctx(LIVE_COUNT, intent=intent, state=state, tenant_id=11)
        )
        b = DefaultDecisionEngine().decide(
            _ctx(LIVE_COUNT, intent=intent, state=state, tenant_id=22)
        )
        assert a.action == b.action
        assert (a.args or {}).get("topic") != TOPIC_PRODUCT_USAGE_INFORMATION
        assert (b.args or {}).get("topic") != TOPIC_PRODUCT_USAGE_INFORMATION
