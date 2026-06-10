"""
tests/test_p1c1_order_flow_truth.py
───────────────────────────────────
P1-C-1: dedup order-state gate, order creation evidence, track contradiction.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.dedup_order_state_gate import (
    inbound_is_short_product_inquiry,
    inbound_is_visual_pivot,
    should_suppress_dedup_order_templates,
)
from core.order_creation_evidence import (
    OrderCreationEvidence,
    OrderCreationStatus,
    outbound_contains_unsupported_creation_claim,
    recent_outbound_claims_order_creating,
    resolve_order_creation_evidence,
    resolve_track_order_fallback,
)
from core.order_flow import context_aware_dedup_fallback
from modules.ai.brain.compose import templates as T
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _patch_dedup_state(summary_state: dict):
    import core.order_flow as of

    def _fake_load(db, tenant_id, phone):
        return None, {
            "order_prep": summary_state,
            "current_product_focus": {
                "title": summary_state.get("selected_product", ""),
                "price": summary_state.get("price"),
            },
        }

    def _fake_focus(bs):
        return dict(summary_state)

    return of, _fake_load, _fake_focus


class TestDedupOrderStateGate:
    def test_price_query_blocks_under_review_template(self):
        summary = {
            "payment_receipt_received": True,
            "selected_product": "عسل طلح نجد",
            "price": 120,
        }
        suppress, reason = should_suppress_dedup_order_templates(
            message="بكم يطلع الكيلو",
            summary=summary,
        )
        assert suppress is True
        assert "commerce" in reason or "payment_receipt" in reason

    def test_product_name_blocks_under_review(self):
        summary = {
            "payment_receipt_received": True,
            "selected_product": "عسل طلح",
        }
        suppress, _ = should_suppress_dedup_order_templates(
            message="طلح",
            summary=summary,
        )
        assert suppress is True

    def test_visual_blocks_under_review(self):
        summary = {
            "payment_receipt_received": True,
            "selected_product": "عسل",
        }
        suppress, _ = should_suppress_dedup_order_templates(
            message="الصوره هنا مافيه؟",
            summary=summary,
        )
        assert suppress is True

    def test_track_order_question_not_suppressed_as_commerce_pivot(self):
        assert inbound_is_short_product_inquiry("وين رقم الطلب؟") is False

    def test_dedup_fallback_no_under_review_on_price(self):
        of, fake_load, fake_focus = _patch_dedup_state({
            "payment_receipt_received": True,
            "selected_product": "عسل طلح نجد",
            "price": 120,
        })
        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = fake_load
            of._focus_summary = fake_focus
            reply = context_aware_dedup_fallback(
                object(),
                tenant_id=1,
                phone="966500000001",
                history=[],
                default_fallback="",
                inbound_text="بكم يطلع الكيلو",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert "تحت المراجعة" not in reply
        assert reply == ""

    def test_dedup_still_shows_under_review_for_neutral_followup(self):
        of, fake_load, fake_focus = _patch_dedup_state({
            "payment_receipt_received": True,
            "selected_product": "عسل طلح",
        })
        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = fake_load
            of._focus_summary = fake_focus
            reply = context_aware_dedup_fallback(
                object(),
                tenant_id=1,
                phone="966500000001",
                history=[],
                default_fallback="fallback",
                inbound_text="تمام",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert "تحت المراجعة" in reply


class TestOrderCreationEvidence:
    def test_created_requires_reference(self):
        ev = OrderCreationEvidence(
            status=OrderCreationStatus.CREATED,
            draft_order_id="",
            salla_order_id="",
        )
        assert ev.can_claim_created() is False

    def test_created_with_salla_id(self):
        ev = OrderCreationEvidence(
            status=OrderCreationStatus.CREATED,
            salla_order_id="ORD-99",
            reference="ORD-99",
        )
        assert ev.can_claim_created() is True

    def test_resolve_creating_from_handler_data(self):
        state = MerchantConversationState(
            current_product_focus={"title": "عسل"},
            order_prep=OrderPreparationState(order_creation_status="creating"),
        )
        ev = resolve_order_creation_evidence(
            state=state,
            handler_data={"salla_retry": True},
        )
        assert ev.status == OrderCreationStatus.CREATING
        assert ev.can_claim_creating() is True

    def test_unsupported_created_claim_detected(self):
        ev = OrderCreationEvidence(status=OrderCreationStatus.CREATING)
        assert outbound_contains_unsupported_creation_claim(
            "تم إنشاء طلبك بنجاح", ev,
        ) is True

    def test_recent_outbound_creating_marker(self):
        history = [
            {"direction": "out", "body": "جارٍ إنشاء طلب *عسل* — لحظة من فضلك."},
        ]
        assert recent_outbound_claims_order_creating(history) is True


class TestTrackOrderContradictionGuard:
    def test_no_orders_replaced_when_creating(self):
        state = MerchantConversationState(
            current_product_focus={"title": "عسل طلح"},
            order_prep=OrderPreparationState(order_creation_status="creating"),
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="وين رقم الطلب؟",
            intent=Intent(name="track_order", confidence=0.9, raw_message=""),
            state=state,
            facts=CommerceFacts(),
            history=[
                {"direction": "out", "body": "جارٍ إنشاء طلب *عسل* — لحظة."},
            ],
        )
        reply = resolve_track_order_fallback(
            state=ctx.state,
            history=ctx.history,
        )
        assert reply is not None
        assert "لم أجد" not in reply
        assert "قيد الإنشاء" in reply

    def test_no_orders_when_no_evidence(self):
        state = MerchantConversationState()
        reply = resolve_track_order_fallback(state=state, history=[])
        assert reply is None
        assert T.no_orders() == "لم أجد أي طلبات مسجّلة لرقمك. هل تريد إنشاء طلب جديد؟"

    def test_failed_status_honest_reply(self):
        state = MerchantConversationState(
            current_product_focus={"title": "عسل"},
            order_prep=OrderPreparationState(order_creation_status="failed"),
        )
        reply = resolve_track_order_fallback(state=state, history=[])
        assert reply is not None
        assert "تعذّر إنشاء" in reply
