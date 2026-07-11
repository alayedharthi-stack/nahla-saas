"""Order-reference support must beat stale draft/checkout continuation."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    should_block_order_draft_injection,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    try_order_reference_continuity_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_TRACK_ORDER  # noqa: E402

GENERIC_ORDER_REF = "284719365"
GENERIC_PRODUCT = "حذاء رياضي أبيض"


def _ctx(
    message: str,
    *,
    history: list | None = None,
    draft_order_id: str = "draft-16",
    order_prep: dict | None = None,
) -> SimpleNamespace:
    prep = {
        "draft_order_id": draft_order_id,
        "draft_order_reference": "NHL-33-000016",
        "order_creation_status": "created",
        "order_status": "pending_customer_info",
        "line_items": [{"name": GENERIC_PRODUCT, "qty": 1}],
        **(order_prep or {}),
    }
    state = SimpleNamespace(
        draft_order_id=draft_order_id,
        order_prep=SimpleNamespace(**prep),
    )
    state.to_dict = lambda: {  # type: ignore[attr-defined]
        "draft_order_id": draft_order_id,
        "order_prep": prep,
    }
    return SimpleNamespace(
        message=message,
        history=history or [],
        state=state,
        commerce_bundle={},
        profile={"inbound_metadata": {"type": "audio", "transcript": message}},
        tenant_id=1,
    )


class TestOrderReferenceBeatsStaleCheckout:
    def test_voice_shipping_follow_up_wins_over_stale_draft(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        ctx = _ctx("الطلب متأخر والشحن ما وصل", history=history)
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "existing_order_support"

    def test_product_clarification_stays_order_support(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        ctx = _ctx(f"الطلب فيه {GENERIC_PRODUCT}", history=history)
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "existing_order_support"

    def test_repeated_reference_not_suppressed(self) -> None:
        from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: PLC0415
            evaluate_duplicate_fragment_turn,
            reset_fragment_cache_for_tests,
        )

        reset_fragment_cache_for_tests()
        out = evaluate_duplicate_fragment_turn(
            tenant_id=1,
            customer_phone="966500000001",
            text=GENERIC_ORDER_REF,
        )
        assert out.process_turn is True

    def test_placed_order_statement_blocks_draft_injection(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        blocked = should_block_order_draft_injection(
            brain_state=_ctx("خلاص طلبت", history=history).state,
            customer_message="خلاص طلبت",
            history=history,
        )
        assert blocked is True

    def test_checkout_confirmation_without_order_ref_not_blocked(self) -> None:
        blocked = should_block_order_draft_injection(
            brain_state=_ctx("تمام").state,
            customer_message="تمام",
            history=[],
        )
        assert blocked is False

    def test_bare_reference_routes_to_track_order(self) -> None:
        ctx = _ctx(GENERIC_ORDER_REF)
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_TRACK_ORDER

    def test_draft_injection_blocked_for_order_support_follow_up(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        blocked = should_block_order_draft_injection(
            brain_state=_ctx("الطلب متأخر والشحن ما وصل", history=history).state,
            customer_message="الطلب متأخر والشحن ما وصل",
            history=history,
        )
        assert blocked is True

    def test_no_draft_reply_injected_when_order_support_active(self) -> None:
        from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: PLC0415

        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        state = _ctx("الطلب متأخر والشحن ما وصل", history=history).state
        reply = maybe_inject_draft_flow_reply(
            reply="",
            order_prep=state.order_prep,
            brain_state=state,
            customer_message="الطلب متأخر والشحن ما وصل",
            history=history,
        )
        assert reply == ""
