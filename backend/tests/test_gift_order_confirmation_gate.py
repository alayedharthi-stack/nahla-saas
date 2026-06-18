"""P0 — Gift order confirmation gate regression tests."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.gift_order_gate import (  # noqa: E402
    clear_pending_delivery_location,
    consume_pending_delivery_location,
    extract_gift_recipient_name,
    get_pending_cart_confirmation,
    get_pending_delivery_location,
    is_bare_cart_confirmation,
    is_bare_cart_rejection,
    is_order_shaped_message,
    maybe_clear_pending_cart_confirmation,
    run_pre_decide_order_extraction,
    set_pending_cart_confirmation,
    try_pending_cart_confirmation_decision,
    try_ready_for_order_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.types import Decision  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.intent.cart_intent_extractor import extract_cart_intents  # noqa: E402
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    INTENT_START_ORDER,
)


GIFT_ORDER_MSG = (
    "طلب توصيل عسل لهذا الشخص\n"
    "محمد بدر الرشيدي\n"
    "نص كيلo طلح\n"
    "ونص صيفي"
)


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_START_ORDER,
    prep: OrderPreparationState | None = None,
    cart_items: list | None = None,
    pending_location: dict | None = None,
) -> BrainContext:
    prep = prep or OrderPreparationState()
    state = MerchantConversationState(
        stage="ordering",
        greeted=True,
        order_prep=prep,
        cart_items=list(cart_items or []),
        pending_delivery_location=dict(pending_location or {}),
        turn=5,
    )
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000099",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.93,
            raw_message=message,
            extraction_method="rules",
        ),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
    )


class TestGiftOrderIntent:
    def test_delivery_request_is_start_order_not_ask_product(self) -> None:
        msg = "طلب توصيل عسل لهذا الشخص"
        matched = intent_rules.match(msg)
        assert matched is not None
        assert matched.name == INTENT_START_ORDER
        assert matched.name != "ask_product"


class TestMultiLineCartExtraction:
    def test_two_honey_lines_including_summer(self) -> None:
        msg = "نص كيلo طلح\nونص صيفi"
        intents = extract_cart_intents(msg)
        assert len(intents) == 2
        names = {i["product_name"] for i in intents}
        assert "عسل طلح" in names
        assert "عسل صيفي" in names
        variants = {i.get("variant") for i in intents}
        assert "500g" in variants


class TestGiftRecipientExtraction:
    def test_recipient_from_line_after_gift_marker(self) -> None:
        name = extract_gift_recipient_name(GIFT_ORDER_MSG)
        assert name == "محمد بدر الرشيدي"


class TestLocationStashAndConsume:
    def test_pending_location_consumed_on_order_shaped_turn(self) -> None:
        prep = OrderPreparationState()
        pending = {
            "latitude": 24.7136,
            "longitude": 46.6753,
            "google_maps_url": "https://maps.app.goo.gl/testpin",
        }
        ctx = _ctx(GIFT_ORDER_MSG, prep=prep, pending_location=pending)
        summary = run_pre_decide_order_extraction(ctx)
        assert summary.get("applied") is True
        assert summary.get("location_consumed") is True
        assert prep.latitude == 24.7136
        assert prep.longitude == 46.6753
        assert get_pending_delivery_location(ctx.state) == {}


class TestLocationThenGiftOrder:
    def test_pre_decide_builds_cart_recipient_and_ready(self) -> None:
        prep = OrderPreparationState()
        pending = {
            "latitude": 24.71,
            "longitude": 46.67,
            "google_maps_url": "https://maps.app.goo.gl/giftloc",
        }
        ctx = _ctx(GIFT_ORDER_MSG, prep=prep, pending_location=pending)
        summary = run_pre_decide_order_extraction(ctx)

        assert summary.get("cart_size") == 2
        assert prep.recipient_name == "محمد بدر الرشيدي"
        assert prep.fulfillment_kind == "gift_delivery"
        assert prep.google_maps_url or prep.latitude
        assert len(ctx.state.cart_items) == 2
        assert summary.get("ready_for_order_creation") is True

        decision = try_ready_for_order_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "ready_for_order_creation" in (decision.reason or "")

        engine_decision = DefaultDecisionEngine().decide(ctx)
        assert engine_decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert engine_decision.action != ACTION_LLM_REPLY
        assert "order_recovery" not in str(engine_decision.args.get("topic") or "")

        staff = apply_staff_escalation_truth_guard(
            reply="تم تحويلك للدعم",
            inbound_text=GIFT_ORDER_MSG,
            state=ctx.state,
            conversation_flags={},
        )
        assert staff.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR


class TestPendingCartConfirmation:
    def test_bare_naim_consumes_pending_cart_confirmation(self) -> None:
        items = [
            {"product_name": "عسل طلح", "variant": "500g", "quantity": 1},
            {"product_name": "عسل صيفي", "variant": "500g", "quantity": 1},
        ]
        prep = OrderPreparationState(
            google_maps_url="https://maps.app.goo.gl/x",
            customer_first_name="Test",
            customer_last_name="User",
            city="الرياض",
        )
        set_pending_cart_confirmation(
            prep,
            items=items,
            source="cart_summary",
            turn=4,
        )
        ctx = _ctx("نعم", intent_name="general", prep=prep, cart_items=items)
        ctx.intent = Intent(
            name="general",
            confidence=0.55,
            raw_message="نعم",
            extraction_method="rules",
        )
        decision = try_pending_cart_confirmation_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("source") == "pending_cart_confirmation"

        set_pending_cart_confirmation(
            prep,
            items=items,
            source="cart_summary",
            turn=4,
        )
        engine_decision = DefaultDecisionEngine().decide(ctx)
        assert engine_decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert engine_decision.action != ACTION_LLM_REPLY

    def test_bare_affirmative_detector(self) -> None:
        assert is_bare_cart_confirmation("نعم")
        assert is_bare_cart_confirmation("توكل")
        assert not is_bare_cart_confirmation("نعم ابغى عسل")


class TestOrderShapedGate:
    def test_order_shaped_detects_delivery_phrases(self) -> None:
        assert is_order_shaped_message("طلب توصيل عسل")
        assert is_order_shaped_message("ابغى ربع كيلo عسل")
        assert not is_order_shaped_message("وش أخبارك")


class TestPendingCartConfirmationLifecycle:
    def test_cleared_after_draft_proposal_decision(self) -> None:
        prep = OrderPreparationState()
        set_pending_cart_confirmation(prep, items=[{"product_name": "x"}], source="t", turn=1)
        decision = Decision(action=ACTION_PROPOSE_DRAFT_ORDER, args={}, reason="test")
        assert maybe_clear_pending_cart_confirmation(prep=prep, decision=decision)
        assert get_pending_cart_confirmation(prep) == {}

    def test_cleared_on_bare_rejection(self) -> None:
        prep = OrderPreparationState()
        set_pending_cart_confirmation(prep, items=[{"product_name": "x"}], source="t", turn=1)
        decision = Decision(action=ACTION_LLM_REPLY, args={}, reason="test")
        assert maybe_clear_pending_cart_confirmation(prep=prep, decision=decision, message="لا")
        assert get_pending_cart_confirmation(prep) == {}

    def test_cleared_on_handoff_intent(self) -> None:
        prep = OrderPreparationState()
        set_pending_cart_confirmation(prep, items=[{"product_name": "x"}], source="t", turn=1)
        decision = Decision(action=ACTION_HANDOFF, args={}, reason="test")
        assert maybe_clear_pending_cart_confirmation(prep=prep, decision=decision)
        assert get_pending_cart_confirmation(prep) == {}

    def test_rejection_detector(self) -> None:
        assert is_bare_cart_rejection("لا")
        assert is_bare_cart_rejection("الغي")
        assert not is_bare_cart_rejection("لا ابغى")


class TestPendingDeliveryLocationSingleUse:
    def test_consumed_once_not_reused(self) -> None:
        prep = OrderPreparationState()
        state = MerchantConversationState(
            pending_delivery_location={
                "latitude": 24.1,
                "longitude": 46.2,
            },
            order_prep=prep,
        )
        assert consume_pending_delivery_location(state, prep, gift=True) is True
        assert prep.latitude == 24.1
        assert get_pending_delivery_location(state) == {}

        prep.latitude = None
        prep.longitude = None
        assert consume_pending_delivery_location(state, prep) is False
        assert prep.latitude is None

    def test_stash_overwrites_previous_pin(self) -> None:
        prep = OrderPreparationState()
        state = MerchantConversationState(
            pending_delivery_location={"latitude": 1.0, "longitude": 2.0},
            order_prep=prep,
        )
        clear_pending_delivery_location(state, prep)
        state.pending_delivery_location = {"latitude": 24.5, "longitude": 46.6}
        assert state.pending_delivery_location["latitude"] == 24.5
