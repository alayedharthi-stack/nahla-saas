"""Tests for memory selection evidence + fresh social context hotfix."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.responder import _as_ai_history  # noqa: E402
from modules.ai.brain.context.fresh_social_context import (  # noqa: E402
    FRESH_SOCIAL_GAP_DAYS,
    should_apply_fresh_social_context,
)
from modules.ai.brain.intent_priority.types import GOAL_SOCIAL_ONLY  # noqa: E402
from modules.ai.brain.observability.memory_selection_evidence import (  # noqa: E402
    build_memory_candidates,
    MEMORY_EPHEMERAL_SOCIAL,
)
from modules.ai.brain.types import MerchantConversationState, OrderPreparationState  # noqa: E402


def _stale_state(*, days: float = 30.0) -> MerchantConversationState:
    ts = datetime.now(timezone.utc) - timedelta(days=days)
    return MerchantConversationState(
        updated_at=ts.isoformat(),
        stage="discovery",
    )


class TestFreshSocialContextPolicy:
    def test_emoji_after_long_gap_applies(self) -> None:
        apply, reason = should_apply_fresh_social_context(
            inbound_text="❤️",
            state=_stale_state(days=30),
            intent_name="social",
            primary_customer_goal=GOAL_SOCIAL_ONLY,
        )
        assert apply is True
        assert reason == "stale_gap_fresh_social"

    def test_emoji_within_gap_does_not_apply(self) -> None:
        apply, _ = should_apply_fresh_social_context(
            inbound_text="❤️",
            state=_stale_state(days=2),
            intent_name="social",
            primary_customer_goal=GOAL_SOCIAL_ONLY,
        )
        assert apply is False

    def test_active_order_blocks_fresh_social(self) -> None:
        state = _stale_state(days=30)
        state.order_prep = OrderPreparationState(
            line_items=[{"product_name": "منتج", "quantity": 1}],
            missing_fields=["customer_first_name"],
        )
        apply, reason = should_apply_fresh_social_context(
            inbound_text="❤️",
            state=state,
            intent_name="social",
            primary_customer_goal=GOAL_SOCIAL_ONLY,
        )
        assert apply is False
        assert reason == "active_order"

    def test_support_stage_blocks_fresh_social(self) -> None:
        state = _stale_state(days=30)
        state.stage = "support"
        apply, reason = should_apply_fresh_social_context(
            inbound_text="😊",
            state=state,
            intent_name="social",
            primary_customer_goal=GOAL_SOCIAL_ONLY,
        )
        assert apply is False
        assert reason == "open_support_case"

    def test_bare_naim_not_lightweight_social(self) -> None:
        apply, _ = should_apply_fresh_social_context(
            inbound_text="نعم",
            state=_stale_state(days=30),
            intent_name="general",
            primary_customer_goal="",
        )
        assert apply is False


class TestFreshSocialHistoryFilter:
    def test_as_ai_history_drops_tail_when_fresh(self) -> None:
        history = [
            {"direction": "in", "body": "اقتراحي إنكم تضيفون منتج جديد"},
            {"direction": "out", "body": "فكرة حلوة"},
        ]
        msgs = _as_ai_history(history, "❤️", fresh_social_context=True)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "❤️"
        assert "اقتراح" not in msgs[0]["content"]


class TestMemorySelectionEvidence:
    def test_stale_summary_excluded_when_fresh_social(self) -> None:
        state = _stale_state(days=20)
        history = [{"direction": "in", "body": "اقتراحي ممتاز"}]
        cands = build_memory_candidates(
            state=state,
            history=history,
            conversation_summary="العميل اقترح فكرة قبل شهر",
            fresh_social_context=True,
            fresh_social_reason="stale_gap_fresh_social",
        )
        summary = next(c for c in cands if c.candidate_id == "conversation_summary")
        tail = next(c for c in cands if c.candidate_id == "history_tail")
        assert summary.selected is False
        assert tail.selected is False
        assert tail.memory_class == MEMORY_EPHEMERAL_SOCIAL

    def test_gap_threshold_constant(self) -> None:
        assert FRESH_SOCIAL_GAP_DAYS == 7
