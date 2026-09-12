"""P0 regression — product attribute questions must not enter order flow."""
from __future__ import annotations

import os
import sys
from typing import Optional

import pytest

pytestmark = pytest.mark.governance_contract

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.commerce.product_media import detect_product_media_turn  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import _MISSING_FIELD_PROMPTS_AR  # noqa: E402
from modules.ai.brain.state.product_information_topic import (  # noqa: E402
    TOPIC_PRODUCT_ATTRIBUTE_INFORMATION,
    detect_customer_owned_product_reference,
    detect_product_attribute_question,
    detect_product_information_topic_shift,
    should_suppress_product_focus_pin,
)
from modules.ai.brain.state.state_relevance import validate_state_relevance  # noqa: E402
from modules.ai.brain.turn.arbiter import arbitrate_turn  # noqa: E402
from modules.ai.brain.turn.contract import OWNER_ORDERING, OWNER_PERSONA_SOCIAL  # noqa: E402
from modules.ai.brain.turn.understanding import synthesize_turn_understanding  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(
    message: str,
    *,
    state: Optional[MerchantConversationState] = None,
    intent_name: str = "general",
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000000",
        message=message,
        intent=Intent(name=intent_name, confidence=0.85, raw_message=message),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(orderable=True, has_products=True),
    )


def _ordering_state(*, title: str = "Product Alpha") -> MerchantConversationState:
    prep = OrderPreparationState(
        missing_fields=["customer_first_name"],
        product_id="sku-alpha",
    )
    return MerchantConversationState(
        stage="ordering",
        turn=2,
        product_focus_turn=1,
        current_product_focus={
            "title": title,
            "id": "sku-alpha",
            "external_id": "sku-alpha",
            "price": 99,
        },
        order_prep=prep,
        last_question_asked="customer_first_name",
        last_question_answered=False,
    )


class TestProductAttributeDetection:
    @pytest.mark.parametrize(
        "msg",
        [
            "هل المنتج مبستر؟",
            "هل هذا المنتج خام؟",
            "هل هو طبيعي؟",
            "هل هو معالج؟",
            "هل يحتوي المنتج على نكهة؟",
            "ما مكونات المنتج؟",
            "is this product pasteurized?",
            "what are the ingredients?",
        ],
    )
    def test_attribute_questions_detected(self, msg: str) -> None:
        assert detect_product_attribute_question(msg)
        assert detect_product_information_topic_shift(msg)

    def test_customer_owned_reference(self) -> None:
        msg = "المنتج اللي عندي ألفا هل هو مبستر؟"
        assert detect_customer_owned_product_reference(msg)
        assert detect_product_information_topic_shift(msg)

    def test_customer_owned_not_order_intent(self) -> None:
        msg = "عندي ألفا هل هو خام؟"
        assert detect_customer_owned_product_reference(msg)
        assert detect_product_information_topic_shift(msg)

    def test_availability_fieh_not_attribute_question(self) -> None:
        msg = "صباح الخير\nفيه عندك طرود نحل؟"
        assert not detect_product_attribute_question(msg)
        assert not detect_product_information_topic_shift(msg)


class TestAttributeQuestionDecisionRouting:
    @pytest.mark.parametrize(
        "msg",
        [
            "هل المنتج مبستر؟",
            "هل المادة الغذائية مبستر؟",
            "المنتج اللي عندي ألفا هل هو مبستر؟",
            "عندي ألفا هل هو خام؟",
            "هل يحتوي المنتج على نكهة؟",
            "ما مكونات المنتج؟",
            "طريقة استخدام هذا المنتج؟",
        ],
    )
    def test_informational_not_order_or_search(self, msg: str) -> None:
        ctx = _ctx(msg, state=_ordering_state(), intent_name="general")
        ctx.state_relevance = validate_state_relevance(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("suppress_checkout") is True

    def test_attribute_topic_on_pasteurized_question(self) -> None:
        msg = "هل المنتج مبستر؟"
        ctx = _ctx(msg, state=_ordering_state(), intent_name="general")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.args.get("topic") == TOPIC_PRODUCT_ATTRIBUTE_INFORMATION

    def test_during_ordering_attribute_does_not_ask_name(self) -> None:
        msg = "هل المنتج مبستر؟"
        ctx = _ctx(msg, state=_ordering_state(), intent_name="general")
        decision = DefaultDecisionEngine().decide(ctx)
        reply = str(decision.args.get("reply") or "")
        assert _MISSING_FIELD_PROMPTS_AR["customer_first_name"] not in reply

    def test_continue_order_after_attribute_question(self) -> None:
        state = _ordering_state()
        ctx = _ctx("تمام كمل الطلب", state=state, intent_name="general")
        ctx.state_relevance = validate_state_relevance(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_explicit_order_intent_still_works(self) -> None:
        state = _ordering_state()
        ctx = _ctx("أبي أطلبه", state=state, intent_name="start_order")
        ctx.state_relevance = validate_state_relevance(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


class TestArbiterAndFocusSuppression:
    def test_attribute_question_blocks_slot_replay(self) -> None:
        msg = "المنتج اللي عندي ألفا هل هو مبستر؟"
        ctx = _ctx(msg, state=_ordering_state(), intent_name="general")
        ctx.state_relevance = validate_state_relevance(ctx)
        understanding = synthesize_turn_understanding(ctx)
        arbitration = arbitrate_turn(understanding, ctx)
        assert ctx.state_relevance.product_information_topic_shift is True
        assert arbitration.slot_replay_approved is False
        assert arbitration.turn_owner != OWNER_ORDERING

    def test_product_media_skipped_for_attribute_question(self) -> None:
        verdict = detect_product_media_turn(
            "هل المنتج مبستر؟",
            intent_name="general",
        )
        assert verdict.matched is False
        assert verdict.reason == "product_attribute_or_usage_question"

    def test_contains_question_still_detected(self) -> None:
        assert detect_product_attribute_question("هل يحتوي المنتج على سكر؟")
        assert detect_product_attribute_question("هل فيه سكر؟")

    def test_post_order_shipping_question_not_attribute(self) -> None:
        msg = "اي فرع ارسلتو طلبي في سمسا"
        assert detect_product_attribute_question(msg) is False
        assert detect_product_information_topic_shift(msg) is False
