"""Compose-policy: preserve order-support response_goal in prompt state."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    build_order_support_follow_up_args,
    compose_order_support_response_goal_for_decision,
)
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.pipeline import _compose_base_response_goal, _compose_response_goal  # noqa: E402
from modules.ai.brain.types import BrainReplyState, Decision, SuggestionSnapshot  # noqa: E402

GENERIC_ORDER_REF = "284719365"
GENERIC_DELAY_MSG = "الطلب متأخر والشحن ما وصل"


def _support_decision(*, verified: bool = False, status: str = "") -> Decision:
    args = build_order_support_follow_up_args(
        message=GENERIC_DELAY_MSG,
        history=[{"direction": "in", "body": GENERIC_ORDER_REF}],
        order_verified=verified,
    )
    if status:
        args["order_status"] = status
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason="existing_order_support_ownership:test",
        confidence=0.93,
    )


class TestOrderSupportResponseGoalPreserved:
    def test_existing_order_support_goal_reaches_prompt_state(self) -> None:
        """A — structured response_goal survives into BrainReplyState / prompt."""
        decision = _support_decision()
        goal = _compose_response_goal(decision, SuggestionSnapshot())
        assert goal.startswith("existing_order_support —")
        assert "in_channel_only" in goal
        assert f"order_reference={GENERIC_ORDER_REF}" in goal
        assert "order_verified=False" in goal

        state = BrainReplyState(
            store_name="متجر تجريبي عام",
            tone="friendly",
            stage="ordering",
            response_goal=goal,
            intent_name="ask_product",
        )
        prompt = build_brain_reply_prompt(state)
        assert "existing_order_support —" in prompt
        assert "in_channel_only" in prompt
        assert decision.args["response_goal"] in prompt

    def test_unverified_shipping_delay_goal_constraints(self) -> None:
        """B — in-channel support; no off-channel redirect policy in goal."""
        goal = _compose_response_goal(_support_decision(), SuggestionSnapshot())
        assert "Do NOT redirect them generically to phone" in goal
        assert "Do NOT invent a contact team" in goal
        assert "Do NOT fabricate tracking" in goal
        assert "Do NOT open catalog or restart checkout" in goal
        assert goal != "existing_order_support_ownership:test"
        assert "advance the conversation toward the next sales step" not in goal

    def test_verified_order_with_status_grounding_hints(self) -> None:
        """C — verified support goal keeps structured facts for grounded replies."""
        decision = _support_decision(verified=True, status="shipped")
        goal = _compose_response_goal(decision, SuggestionSnapshot())
        assert "order_verified=True" in goal
        assert "order_status=shipped" in goal
        assert "existing_order_support —" in goal

    def test_shipping_post_order_topic_gets_in_channel_goal(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "shipping_post_order"},
            reason="ASK_SHIPPING matched, paid/processing/shipped order present",
            confidence=0.9,
        )
        goal = _compose_base_response_goal(decision, SuggestionSnapshot())
        assert goal.startswith("shipping_post_order —")
        assert "in_channel_only" in goal

    def test_human_escalation_stays_in_channel_in_goal(self) -> None:
        """D — escalation policy is in-channel only; no invented contact details."""
        goal = compose_order_support_response_goal_for_decision(
            build_order_support_follow_up_args(
                message="أبغى أحد من الفريق",
                history=[{"direction": "in", "body": GENERIC_ORDER_REF}],
            )
        )
        assert "official staff handoff may be triggered by the platform" in goal
        assert "do not supply phone numbers or email addresses unless they appear in Facts" in goal

    def test_category_discovery_goal_unchanged(self) -> None:
        """E — generic sales/discovery compose path is not altered."""
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "category_discovery", "category_hint": "أحذية"},
            reason="category browse",
            confidence=0.8,
        )
        goal = _compose_base_response_goal(decision, SuggestionSnapshot())
        assert "in_channel_only" not in goal
        assert "existing_order_support —" not in goal
        assert "category_discovery" in goal
