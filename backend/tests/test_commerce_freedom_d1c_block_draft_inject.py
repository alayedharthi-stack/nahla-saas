"""LIVE-COMMERCE-FREEDOM-D1C — honor decision block before WA draft injection.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
Customer wording appears as TEST INPUT only. Tests assert ownership/yield,
not exact Arabic customer-facing sentences.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_cart_line_items import ITEM_STATUS_CONFIRMED  # noqa: E402
from core.wa_draft_confirmation import (  # noqa: E402
    compose_wa_order_flow_reply,
    maybe_inject_draft_flow_reply,
)
from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    should_block_order_draft_injection,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.types import Decision  # noqa: E402

# Fixture evidence only — live inbound from T33 conv 26 / message 58191.
_LIVE_SHAPED_INBOUND = "هل هذا العسل من نحل بلدي ؟"
_COMPOSED_KNOWLEDGE_REPLY = (
    "نعم، القميص القطني الأزرق مصنوع من قطن محلي موثق في معرفة التاجر."
)
_GENERIC_PRODUCT = "قميص قطني أزرق"


def _stale_unknown_price_prep() -> Dict[str, Any]:
    return {
        "line_items": [
            {
                "product_id": "sku-generic-shirt",
                "product_name": _GENERIC_PRODUCT,
                "match_status": ITEM_STATUS_CONFIRMED,
                "quantity": 2,
            }
        ],
        "cart_deltas": [],
    }


def _knowledge_decision() -> Decision:
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "product_knowledge_facts",
            "block_commerce_escalation": True,
            "customer_action": "knowledge",
        },
        reason="product_knowledge — attribute",
        confidence=0.93,
    )


def _pipeline_post_compose_draft_inject(
    *,
    reply: str,
    order_prep: Any,
    brain_state: Any,
    decision: Any,
    customer_message: str = "",
    cart_changed: bool = False,
    history: Optional[List[Any]] = None,
) -> str:
    """Mirror pipeline.py post-compose WA draft inject ownership gate."""
    if should_block_order_draft_injection(
        brain_state=brain_state,
        customer_message=customer_message or "",
        decision=decision,
        history=list(history or []),
    ):
        return reply or ""
    return maybe_inject_draft_flow_reply(
        reply=reply or "",
        order_prep=order_prep,
        brain_state=brain_state,
        cart_changed=cart_changed,
        customer_message=customer_message or "",
        history=list(history or []),
    )


class TestAExplicitBlock:
    def test_block_commerce_escalation_true_blocks_draft_injection(self) -> None:
        blocked = should_block_order_draft_injection(
            brain_state={},
            customer_message="generic product question",
            decision=_knowledge_decision(),
            history=[],
        )
        assert blocked is True

    def test_composed_reply_preserved_when_block_signal_present(self) -> None:
        out = _pipeline_post_compose_draft_inject(
            reply=_COMPOSED_KNOWLEDGE_REPLY,
            order_prep=_stale_unknown_price_prep(),
            brain_state={"order_prep": _stale_unknown_price_prep()},
            decision=_knowledge_decision(),
            customer_message="generic product question",
            cart_changed=False,
        )
        assert out == _COMPOSED_KNOWLEDGE_REPLY


class TestBLiveShapedStaleUnknownPrice:
    def test_stale_unknown_price_does_not_replace_composed_reply_when_blocked(self) -> None:
        prep = _stale_unknown_price_prep()
        out = _pipeline_post_compose_draft_inject(
            reply=_COMPOSED_KNOWLEDGE_REPLY,
            order_prep=prep,
            brain_state={"order_prep": prep},
            decision=_knowledge_decision(),
            customer_message=_LIVE_SHAPED_INBOUND,
            cart_changed=False,
        )
        assert out == _COMPOSED_KNOWLEDGE_REPLY

    def test_rule_f_can_fire_without_block_signal_on_same_stale_cart(self) -> None:
        """Prove the live overwrite shape still exists when Decision does not block."""
        prep = _stale_unknown_price_prep()
        injected = compose_wa_order_flow_reply(
            order_prep=prep,
            brain_state={},
            cart_changed=False,
            existing_reply=_COMPOSED_KNOWLEDGE_REPLY,
            customer_message=_LIVE_SHAPED_INBOUND,
        )
        assert injected is not None
        assert injected != _COMPOSED_KNOWLEDGE_REPLY


class TestCGenuineUnblockedOrder:
    def test_rule_f_remains_available_without_block_signal(self) -> None:
        prep = {
            "line_items": [
                {
                    "product_id": "sku-generic-shirt",
                    "product_name": _GENERIC_PRODUCT,
                    "match_status": ITEM_STATUS_CONFIRMED,
                    "quantity": 1,
                }
            ],
            "cart_deltas": [{"op": "add"}],
        }
        decision = Decision(action=ACTION_LLM_REPLY, args={}, reason="order_flow")
        assert should_block_order_draft_injection(
            brain_state={"order_prep": prep},
            customer_message="أضف القميص",
            decision=decision,
            history=[],
        ) is False
        out = _pipeline_post_compose_draft_inject(
            reply="",
            order_prep=prep,
            brain_state={"order_prep": prep},
            decision=decision,
            customer_message="أضف القميص",
            cart_changed=True,
        )
        assert out
        assert out != _COMPOSED_KNOWLEDGE_REPLY


class TestDExistingBlockOrderFlow:
    def test_block_order_flow_still_blocks_injection(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"block_order_flow": True, "topic": "support_complaint_refund"},
            reason="complaint",
        )
        assert should_block_order_draft_injection(
            brain_state={},
            customer_message="تمام",
            decision=decision,
            history=[],
        ) is True


class TestEUnrelatedDecisions:
    def test_unrelated_decision_without_block_signal_does_not_block(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "general"},
            reason="unrelated",
        )
        assert should_block_order_draft_injection(
            brain_state={},
            customer_message="تمام",
            decision=decision,
            history=[],
        ) is False

    def test_block_commerce_escalation_false_does_not_block(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"block_commerce_escalation": False, "topic": "general"},
            reason="unrelated",
        )
        assert should_block_order_draft_injection(
            brain_state={},
            customer_message="تمام",
            decision=decision,
            history=[],
        ) is False


class TestFPipelinePreservation:
    def test_pipeline_hook_yields_without_overwriting_composed_body(self) -> None:
        prep = _stale_unknown_price_prep()
        out = _pipeline_post_compose_draft_inject(
            reply=_COMPOSED_KNOWLEDGE_REPLY,
            order_prep=prep,
            brain_state={"order_prep": prep},
            decision=_knowledge_decision(),
            customer_message=_LIVE_SHAPED_INBOUND,
            cart_changed=False,
        )
        assert out == _COMPOSED_KNOWLEDGE_REPLY
