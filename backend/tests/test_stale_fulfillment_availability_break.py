"""PR-A — stale fulfillment/order replay must break on availability inquiry."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.dedup_order_state_gate import (  # noqa: E402
    inbound_is_commerce_inquiry_turn,
    should_suppress_dedup_order_templates,
)
from core.order_flow import context_aware_dedup_fallback  # noqa: E402
from modules.ai.brain.commerce.conversation_state_isolation import (  # noqa: E402
    inbound_breaks_fulfillment_ownership,
    should_replay_pending_question,
)


_AVAIL_MSG = "صباح الخير\nفي عندك طرود نحل ؟"
_FEE_MSG = "فيه عندك طرود نحل؟"


class TestStaleFulfillmentAvailabilityBreak:
    def test_commerce_inquiry_detects_availability_variants(self) -> None:
        assert inbound_is_commerce_inquiry_turn(_AVAIL_MSG)
        assert inbound_is_commerce_inquiry_turn(_FEE_MSG)
        assert inbound_is_commerce_inquiry_turn("هل عسل السمر متوفر؟") is False

    def test_inquiry_breaks_fulfillment_ownership(self) -> None:
        assert inbound_breaks_fulfillment_ownership(_AVAIL_MSG)
        assert inbound_breaks_fulfillment_ownership(_FEE_MSG)

    def test_order_tracking_does_not_break(self) -> None:
        for msg in (
            "وين طلبي؟",
            "متى يتجهز",
            "متى الشحن",
            "هل طلبي جاهز؟",
            "وصل الطلب؟",
            "رقم الشحنة",
        ):
            assert not inbound_is_commerce_inquiry_turn(msg)
            assert not inbound_breaks_fulfillment_ownership(msg)

    def test_dedup_suppresses_under_review_on_availability_inquiry(self) -> None:
        summary = {"payment_receipt_received": True, "selected_product": "عسل سمر"}
        suppress, reason = should_suppress_dedup_order_templates(
            message=_AVAIL_MSG,
            summary=summary,
        )
        assert suppress is True
        assert reason

    def test_dedup_does_not_suppress_on_explicit_order_question(self) -> None:
        summary = {"payment_receipt_received": True}
        suppress, _ = should_suppress_dedup_order_templates(
            message="وين طلبي؟",
            summary=summary,
        )
        assert suppress is False

    def test_context_aware_dedup_fallback_empty_on_availability_pivot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeDB:
            pass

        import core.order_flow as of

        def _fake_load(db, tenant_id, phone):
            return None, {
                "order_prep": {
                    "payment_receipt_received": True,
                    "selected_product": "عسل سمر",
                },
            }

        monkeypatch.setattr(of, "_load_brain_state", _fake_load)
        reply = context_aware_dedup_fallback(
            _FakeDB(),
            tenant_id=33,
            phone="966549815590",
            history=[],
            default_fallback="",
            inbound_text=_AVAIL_MSG,
        )
        assert "تحت المراجعة" not in (reply or "")

    def test_pending_question_not_replayed_on_availability_inquiry(self) -> None:
        assert not should_replay_pending_question(
            inbound_text=_AVAIL_MSG,
            last_question="What is your delivery address?",
        )
