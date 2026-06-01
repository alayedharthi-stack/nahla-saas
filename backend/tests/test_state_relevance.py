"""
tests/test_state_relevance.py
─────────────────────────────
State relevance engine — blocks stale workflow resurrection.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.order_flow import context_aware_dedup_fallback
from modules.ai.brain.state.state_relevance import (
    should_block_workflow_resume,
    validate_state_relevance,
    validate_state_relevance_from_summary,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


class TestStateRelevanceValidator:
    def test_payment_not_relevant_for_all_sizes_query(self):
        op = OrderPreparationState(awaiting_payment_receipt=True, product_id="p1")
        state = MerchantConversationState(
            order_prep=op,
            current_product_focus={"title": "عسل", "price": "100"},
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="كل الحجام",
            intent=Intent(name="general", confidence=0.5, raw_message="كل الحجام"),
            state=state,
            facts=CommerceFacts(),
        )
        verdict = validate_state_relevance(ctx)
        assert verdict.detected_topic_shift is True
        assert verdict.payment_state_relevant is False
        assert should_block_workflow_resume("awaiting_payment_receipt", verdict)

    def test_payment_relevant_for_transfer_claim(self):
        verdict = validate_state_relevance_from_summary(
            message="حولت الآن",
            summary={"awaiting_payment_receipt": True, "selected_product": "عسل"},
        )
        assert verdict.payment_state_relevant is True
        assert not should_block_workflow_resume("payment_flow", verdict)

    def test_replay_blocked_without_explicit_request(self):
        state = MerchantConversationState(
            last_search_candidates=[{"title": "Bee Venom"}],
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="كل الحجام",
            intent=Intent(name="general", confidence=0.5, raw_message="كل الحجام"),
            state=state,
            facts=CommerceFacts(),
        )
        verdict = validate_state_relevance(ctx)
        assert should_block_workflow_resume("pending_candidates", verdict)

    def test_replay_allowed_with_show_more(self):
        state = MerchantConversationState(
            last_search_candidates=[{"title": "A"}],
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="وريني باقي الخيارات",
            intent=Intent(name="general", confidence=0.5, raw_message="وريني باقي الخيارات"),
            state=state,
            facts=CommerceFacts(),
        )
        verdict = validate_state_relevance(ctx)
        assert verdict.product_replay_relevant is True
        assert not should_block_workflow_resume("show_more", verdict)


class TestDedupFallbackPaymentBlock:
    def test_dedup_does_not_resurrect_payment_for_price_query(self):
        class _FakeDB:
            pass

        summary_state = {
            "awaiting_payment_receipt": True,
            "selected_product": "عسل سدر",
            "price": 120,
        }

        # Patch _load_brain_state / _focus_summary via monkeypatch on module
        import core.order_flow as of

        def _fake_load(db, tenant_id, phone):
            return None, {"order_prep": summary_state, "current_product_focus": {"title": "عسل سدر", "price": 120}}

        def _fake_focus(bs):
            return {
                "awaiting_payment_receipt": True,
                "selected_product": "عسل سدر",
                "price": 120,
            }

        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = _fake_load
            of._focus_summary = lambda bs: _fake_focus(bs)
            reply = context_aware_dedup_fallback(
                _FakeDB(),
                tenant_id=99,
                phone="966500000001",
                history=[],
                default_fallback="كيف أقدر أساعدك؟",
                inbound_text="كل الحجام",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert "بانتظار إيصال التحويل" not in reply
