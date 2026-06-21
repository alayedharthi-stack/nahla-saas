"""P0 — external outbound context + post-purchase product feedback routing."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_objective import (  # noqa: E402
    COMMERCE_OBJECTIVE_ORDERING,
    COMMERCE_OBJECTIVE_POST_PURCHASE,
    COMMERCE_OBJECTIVE_SUPPORT,
    get_commerce_objective,
)
from modules.ai.brain.commerce.external_outbound_context import (  # noqa: E402
    CONTEXT_DELIVERY_REVIEW,
    ExternalOutboundContext,
    apply_external_outbound_context,
    classify_external_outbound,
    resolve_external_outbound_context,
)
from modules.ai.brain.commerce.post_purchase_feedback_guard import (  # noqa: E402
    classify_product_quality_feedback,
    try_post_purchase_feedback_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from core.wa_draft_confirmation import compose_wa_order_flow_reply  # noqa: E402


_EXTERNAL_REVIEW = (
    "تم توصيل طلبك رقم 266982457 … ونود أن نعرف رأيك في العسل"
)
_CUSTOMER_FEEDBACK = (
    "يا هلا ابو هشام العسل خفيف والله مو زي دايم وزايد حلاه "
    "مو زي السمره اللي دايم"
)
_PRICE_CONFIRM_STUB = (
    "هذا الخيار يحتاج تأكيد السعر. أرفعه لك للتأكيد "
    "أو تختار الحجم المتوفر بالسعر الظاهر؟"
)


def _ctx(
    msg: str,
    *,
    history: list | None = None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=msg,
        intent=Intent(name="general", confidence=0.5, raw_message=msg),
        state=state or MerchantConversationState(greeted=True, stage="complete"),
        facts=CommerceFacts(
            has_products=True,
            product_count=5,
            orderable=True,
            has_active_integration=True,
            store_name="test",
        ),
        history=history or [],
    )


def _delivered_state_with_stale_cart() -> MerchantConversationState:
    """Simulates stale line_items that previously caused price-confirm injection."""
    state = MerchantConversationState(
        greeted=True,
        stage="complete",
        commerce_objective=COMMERCE_OBJECTIVE_ORDERING,
        current_product_focus={"title": "عسل سمر", "product_id": 55},
    )
    state.order_prep = OrderPreparationState.from_dict({
        "order_status": "delivered",
        "product_name": "عسل سمر",
        "line_items": [{
            "product_id": 55,
            "title": "عسل سمر",
            "match_status": "confirmed",
            "unit_price": None,
        }],
        "cart_deltas": ["stale"],
    })
    return state


class TestExternalOutboundClassification:
    def test_delivery_review_request_detected(self) -> None:
        ctx = classify_external_outbound(_EXTERNAL_REVIEW)
        assert ctx is not None
        assert ctx.context_type == CONTEXT_DELIVERY_REVIEW
        assert ctx.order_reference == "266982457"

    def test_resolves_from_history_regardless_of_origin(self) -> None:
        history = [
            {"direction": "out", "body": _EXTERNAL_REVIEW, "source": "external"},
            {"direction": "in", "body": "شكراً"},
        ]
        resolved = resolve_external_outbound_context(history)
        assert resolved is not None
        assert resolved.context_type == CONTEXT_DELIVERY_REVIEW


class TestProductQualityFeedbackClassification:
    def test_production_customer_message_is_quality_feedback(self) -> None:
        assert classify_product_quality_feedback(_CUSTOMER_FEEDBACK) is True

    def test_buy_intent_during_ordering_not_feedback(self) -> None:
        assert classify_product_quality_feedback("أبغى عسل خفيف") is False


class TestProductionConversationTrace:
    """Trace the exact reported production failure."""

    def test_full_trace_routes_support_not_price_confirm(self) -> None:
        state = _delivered_state_with_stale_cart()
        history = [
            {"direction": "out", "body": _EXTERNAL_REVIEW, "source": "crm"},
            {"direction": "in", "body": _CUSTOMER_FEEDBACK},
        ]
        ctx = _ctx(_CUSTOMER_FEEDBACK, history=history[:-1], state=state)

        apply_external_outbound_context(ctx)
        assert get_commerce_objective(ctx.state) == COMMERCE_OBJECTIVE_SUPPORT

        dec = try_post_purchase_feedback_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "support_product_feedback"
        assert dec.args.get("block_order_flow") is True

        engine_dec = DefaultDecisionEngine().decide(ctx)
        assert engine_dec.action == ACTION_LLM_REPLY
        assert engine_dec.args.get("topic") == "support_product_feedback"

        apply_external_outbound_context(ctx)
        injected = compose_wa_order_flow_reply(
            order_prep=state.order_prep,
            brain_state=state.to_dict(),
            cart_changed=True,
            customer_message=_CUSTOMER_FEEDBACK,
            history=history[:-1],
        )
        assert injected is None
        assert injected != _PRICE_CONFIRM_STUB

    def test_without_external_outbound_ordering_not_hijacked(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            commerce_objective=COMMERCE_OBJECTIVE_ORDERING,
        )
        ctx = _ctx("عسل خفيف", state=state, history=[])
        assert try_post_purchase_feedback_decision(ctx) is None


class TestPostPurchaseObjectiveTransition:
    def test_feedback_shifts_ordering_to_support_then_support_on_complaint(self) -> None:
        from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
            try_complaint_refund_decision,
        )

        state = _delivered_state_with_stale_cart()
        history = [{"direction": "out", "body": _EXTERNAL_REVIEW}]
        ctx = _ctx("العسل مو عسل أبغى فلوسي", history=history, state=state)
        apply_external_outbound_context(ctx)

        dec = try_complaint_refund_decision(ctx)
        assert dec is not None
        assert dec.args.get("topic") == "support_complaint_refund"

        from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
            apply_complaint_refund_session_flags,
        )

        apply_complaint_refund_session_flags(state, ctx.message, dec)
        assert get_commerce_objective(state) == COMMERCE_OBJECTIVE_SUPPORT
