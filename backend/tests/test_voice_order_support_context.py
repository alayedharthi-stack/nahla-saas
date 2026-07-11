"""Voice + history-aware order-support ownership over stale checkout."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: E402
from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    should_block_order_draft_injection,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    try_order_reference_continuity_decision,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_ASK_SHIPPING,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.media.routing_guard import (  # noqa: E402
    is_audio_without_trusted_transcript,
    resolve_semantic_customer_message,
)
from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: E402
    EXISTING_ORDER_SUPPORT,
    evaluate_stale_checkout_suppression,
)

GENERIC_ORDER_REF = "284719365"
GENERIC_PRODUCT = "حذاء رياضي أبيض"
VOICE_SHIPPING = "الطلب متأخر والشحن ما وصل"


def _pending_history() -> list[dict]:
    return [{"direction": "in", "body": GENERIC_ORDER_REF}]


def _stale_prep() -> dict:
    return {
        "draft_order_id": "draft-16",
        "draft_order_reference": "NHL-1-000016",
        "order_creation_status": "created",
        "order_status": "pending_customer_info",
        "line_items": [{"name": GENERIC_PRODUCT, "qty": 1}],
    }


def _suppress(
    message: str,
    *,
    history: list | None = None,
    inbound_metadata: dict | None = None,
) -> bool:
    decision = evaluate_stale_checkout_suppression(
        message=message,
        inbound_metadata=inbound_metadata,
        order_prep=_stale_prep(),
        brain_state={"order_prep": _stale_prep()},
        history=history or [],
        checkout_active=True,
        draft_active=True,
    )
    return decision.suppress


class TestSemanticCustomerMessage:
    def test_t1_audio_transcript_resolves_when_body_empty(self) -> None:
        transcript = VOICE_SHIPPING
        semantic = resolve_semantic_customer_message(
            brain_text="",
            inbound_metadata={"type": "audio", "transcript_text": transcript},
            inbound_normalized_type="audio",
        )
        assert semantic == transcript

    def test_t2_text_body_unchanged(self) -> None:
        msg = f"الطلب فيه {GENERIC_PRODUCT}"
        assert resolve_semantic_customer_message(
            brain_text=msg,
            inbound_metadata={"normalized_type": "text"},
            inbound_normalized_type="text",
        ) == msg


class TestOrderFlowV2HistoryAwareSuppression:
    def test_t3_pending_ref_voice_shipping_suppresses_checkout(self) -> None:
        assert _suppress(
            VOICE_SHIPPING,
            history=_pending_history(),
            inbound_metadata={"type": "audio", "transcript": VOICE_SHIPPING},
        )

    def test_t4_pending_ref_text_shipping_suppresses_checkout(self) -> None:
        assert _suppress(VOICE_SHIPPING, history=_pending_history())

    def test_t5_pending_ref_product_clarification_suppresses_checkout(self) -> None:
        msg = f"الطلب فيه {GENERIC_PRODUCT}"
        assert _suppress(msg, history=_pending_history())

    def test_t6_pending_ref_placed_order_statement_suppresses_checkout(self) -> None:
        assert _suppress("خلاص طلبت", history=_pending_history())

    def test_t7_missing_transcript_audio_with_pending_ref_suppresses_checkout(self) -> None:
        assert is_audio_without_trusted_transcript(
            {"type": "audio", "transcript_status": "empty"},
            semantic_message="",
            inbound_normalized_type="audio",
        )
        assert _suppress(
            "",
            history=_pending_history(),
            inbound_metadata={"type": "audio", "transcript_status": "empty"},
        )

    def test_t8_no_ref_social_voice_does_not_claim_order_support(self) -> None:
        decision = evaluate_stale_checkout_suppression(
            message="هلا كيفك",
            inbound_metadata={"type": "audio", "transcript": "هلا كيفك"},
            order_prep=_stale_prep(),
            history=[],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.detected_intent != EXISTING_ORDER_SUPPORT

    def test_t10_stale_draft_no_ref_tamam_continues_checkout(self) -> None:
        assert not _suppress("تمام", history=[])


class TestBrainAndDraftGuards:
    def test_t9_paid_order_shipping_post_order_preserved(self) -> None:
        op = OrderPreparationState()
        op.payment_receipt_received = True
        op.order_status = "processing"
        st = MerchantConversationState()
        st.order_prep = op
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000099",
            message="اي فرع ارسلتو طلبي في سمسا",
            intent=Intent(
                name=INTENT_ASK_SHIPPING,
                confidence=0.9,
                slots={},
                raw_message="اي فرع ارسلتو طلبي في سمسا",
                extraction_method="rules",
            ),
            state=st,
            facts=CommerceFacts(),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.args.get("topic") == "shipping_post_order"

    def test_t11_draft_injection_blocked_after_order_support(self) -> None:
        history = _pending_history()
        prep = _stale_prep()
        state = SimpleNamespace(
            draft_order_id="draft-16",
            order_prep=SimpleNamespace(**prep),
        )
        state.to_dict = lambda: {  # type: ignore[attr-defined]
            "draft_order_id": "draft-16",
            "order_prep": prep,
        }
        blocked = should_block_order_draft_injection(
            brain_state=state,
            customer_message=VOICE_SHIPPING,
            history=history,
        )
        assert blocked is True
        reply = maybe_inject_draft_flow_reply(
            reply="",
            order_prep=state.order_prep,
            brain_state=state,
            customer_message=VOICE_SHIPPING,
            history=history,
        )
        assert reply == ""

    def test_brain_continuity_routes_voice_shipping_to_order_support(self) -> None:
        history = _pending_history()
        ctx = SimpleNamespace(
            message=VOICE_SHIPPING,
            history=history,
            state=SimpleNamespace(
                draft_order_id="draft-16",
                order_prep=SimpleNamespace(**_stale_prep()),
            ),
            commerce_bundle={},
            profile={"inbound_metadata": {"type": "audio", "transcript": VOICE_SHIPPING}},
            tenant_id=1,
        )
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "existing_order_support"


class TestSuppressionIntentLabel:
    def test_detected_intent_is_existing_order_support(self) -> None:
        decision = evaluate_stale_checkout_suppression(
            message=VOICE_SHIPPING,
            history=_pending_history(),
            order_prep=_stale_prep(),
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is True
        assert decision.detected_intent == EXISTING_ORDER_SUPPORT
