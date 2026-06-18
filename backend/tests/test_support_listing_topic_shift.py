"""
tests/test_support_listing_topic_shift.py
──────────────────────────────────────────
Regression: Google Business Profile / listing support must escape stale
fulfillment lock and map-image delivery short-circuit.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.order_flow import maybe_handle_map_image_inbound  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_PROPOSE_DRAFT_ORDER  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import _MISSING_FIELD_PROMPTS_AR  # noqa: E402
from modules.ai.brain.order_context_gate import try_fulfillment_lock_continuation  # noqa: E402
from modules.ai.brain.state.state_relevance import (  # noqa: E402
    should_block_workflow_resume,
    validate_state_relevance,
)
from modules.ai.brain.state.support_listing_topic import (  # noqa: E402
    detect_support_listing_from_image_metadata,
    detect_support_listing_topic_shift,
)
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
    history: Optional[list] = None,
    orderable: bool = True,
) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966564725255",
        message=message,
        intent=Intent(name="general", confidence=0.5, raw_message=message),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(orderable=orderable),
        history=list(history or []),
    )


def _stale_order_state() -> MerchantConversationState:
    prep = OrderPreparationState(
        missing_fields=["city", "address_location"],
        product_id="27310682888555270",
    )
    return MerchantConversationState(
        stage="ordering",
        current_product_focus={
            "title": "عسل",
            "id": 27310682888555270,
            "external_id": "27310682888555270",
            "price": 120,
        },
        order_prep=prep,
    )


class TestSupportListingDetection:
    def test_listing_clarification_detected(self) -> None:
        assert detect_support_listing_topic_shift(
            "مكتوب متجر سلع منزلية والحين صار فيه محل",
        )

    def test_listing_opener_detected(self) -> None:
        assert detect_support_listing_topic_shift(
            "عمي تركي في ملاحظة في موقع قوقل ماب حق العسل",
        )

    def test_delivery_map_not_listing(self) -> None:
        assert not detect_support_listing_topic_shift("هذا موقع التوصيل")

    def test_gbp_ocr_metadata_detected(self) -> None:
        md = {
            "vision_text": (
                'نوع المحتوى: لقطة شاشة لمتجر عسل. "متجر سلع منزلية" '
                '"الاتجاهات" ayedhoney.com 055 590 6901'
            ),
        }
        assert detect_support_listing_from_image_metadata(
            md,
            ["عمي تركي في ملاحظة في موقع قوقل ماب حق العسل"],
        )


class TestStaleOrderListingEscape:
    def test_listing_text_blocks_fulfillment_lock(self) -> None:
        msg = "مكتوب متجر سلع منزلية والحين صار فيه محل"
        ctx = _ctx(msg, state=_stale_order_state())
        verdict = validate_state_relevance(ctx)
        assert verdict.support_listing_topic_shift is True
        assert should_block_workflow_resume("active_fulfillment", verdict)
        assert should_block_workflow_resume("awaiting_location", verdict)

    def test_no_propose_draft_order_on_listing_clarification(self) -> None:
        msg = "مكتوب متجر سلع منزلية والحين صار فيه محل"
        ctx = _ctx(msg, state=_stale_order_state())
        ctx.state_relevance = validate_state_relevance(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert _MISSING_FIELD_PROMPTS_AR["city"] not in (decision.args.get("reply") or "")

    def test_fulfillment_lock_continuation_blocked(self) -> None:
        msg = "مكتوب متجر سلع منزلية والحين صار فيه محل"
        ctx = _ctx(msg, state=_stale_order_state())
        ctx.state_relevance = validate_state_relevance(ctx)
        assert try_fulfillment_lock_continuation(ctx) is None


class TestListingOpenerRouting:
    def test_opener_not_checkout_continuation(self) -> None:
        msg = "عمي تركي في ملاحظة في موقع قوقل ماب حق العسل"
        ctx = _ctx(msg, state=_stale_order_state())
        ctx.state_relevance = validate_state_relevance(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action in {ACTION_LLM_REPLY, "platform_reply", "handoff"}


class TestMapImageShortCircuitGuard:
    def test_gbp_screenshot_not_delivery_short_circuit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.order_flow as order_flow

        brain_state = {
            "current_product_focus": {"title": "عسل", "price": 120},
            "order_prep": {"missing_fields": ["city"]},
        }

        def _fake_load(db: Any, *, tenant_id: int, phone: str):
            return MagicMock(), brain_state

        def _fake_history(db, phone, limit=10, tenant_id=None):
            return [
                {
                    "direction": "inbound",
                    "body": "عمي تركي في ملاحظة في موقع قوقل ماب حق العسل",
                },
            ]

        monkeypatch.setattr(order_flow, "_load_brain_state", _fake_load)
        monkeypatch.setattr(
            "core.conversation_engine.StateManager.load_history",
            staticmethod(_fake_history),
        )

        md = {
            "image_kind": "map_screenshot",
            "vision_text": (
                '"آل عايض للعسل البلدي" "متجر سلع منزلية" "الاتجاهات" '
                "ayedhoney.com 055 590 6901"
            ),
        }
        result = maybe_handle_map_image_inbound(
            db=MagicMock(),
            tenant_id=33,
            phone="966564725255",
            inbound_normalized_type="image",
            inbound_metadata=md,
        )
        assert result is None

    def test_real_delivery_map_still_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.order_flow as order_flow

        brain_state = {
            "current_product_focus": {"title": "عسل", "price": 120},
            "order_prep": {},
        }

        def _fake_load(db: Any, *, tenant_id: int, phone: str):
            return MagicMock(), brain_state

        def _fake_history(db, phone, limit=10, tenant_id=None):
            return [{"direction": "inbound", "body": "أبي توصيل لرياض"}]

        monkeypatch.setattr(order_flow, "_load_brain_state", _fake_load)
        monkeypatch.setattr(
            "core.conversation_engine.StateManager.load_history",
            staticmethod(_fake_history),
        )

        md = {
            "image_kind": "map_screenshot",
            "vision_text": "Google Maps dropped pin near customer home location",
        }
        result = maybe_handle_map_image_inbound(
            db=MagicMock(),
            tenant_id=33,
            phone="966500000001",
            inbound_normalized_type="image",
            inbound_metadata=md,
        )
        assert result is not None
        assert "وصلتنا لقطة الخريطة" in result["reply_text"]
        assert result["state_patch"].get("awaiting_location_text") is True
