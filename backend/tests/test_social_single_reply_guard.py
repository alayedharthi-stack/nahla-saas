"""
tests/test_social_single_reply_guard.py
────────────────────────────────────────
P0 — duplicate social/greeting reply regression coverage.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.persona_template_engine import (  # noqa: E402
    pick_persona_social_reply,
    PERSONA_SOCIAL_WARM_BY_CATEGORY,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_GREET,
    ACTION_LLM_REPLY,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.postprocess.social_single_reply_guard import (  # noqa: E402
    SocialReplySelection,
    claim_social_reply_selection,
    is_morning_greeting_time,
    is_social_greeting_decision,
    resolve_time_aware_social_category,
    should_defer_layer0_for_brain_social,
    should_suppress_competing_social_outbound,
)
from modules.ai.brain.types import BrainContext, Decision, Intent  # noqa: E402
from modules.ai.routing.layer0_router import evaluate_layer0_route  # noqa: E402
from services.turn_trace import TurnTrace  # noqa: E402


def _ctx(message: str = "") -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966561110307",
        message=message,
        intent=Intent(name="greeting", confidence=0.9, slots={}),
        state=SimpleNamespace(greeted=True, turn=3, stage="idle"),
        facts=SimpleNamespace(store_name="Test", assistant_name="نحلة"),
        history=[],
    )


class TestTimeAwareMorningGreeting:
    def test_evening_does_not_use_morning_greeting_template(self) -> None:
        evening = datetime(2026, 6, 19, 21, 50, tzinfo=timezone.utc)
        cat = resolve_time_aware_social_category(
            "morning_greeting",
            inbound_text="كيف حالك",
            now=evening,
        )
        assert cat != "morning_greeting"
        assert cat == "general_courtesy"

    def test_morning_window_keeps_morning_greeting(self) -> None:
        morning = datetime(2026, 6, 19, 7, 0, tzinfo=timezone.utc)
        assert is_morning_greeting_time(now=morning) is True
        cat = resolve_time_aware_social_category(
            "morning_greeting",
            inbound_text="صباح الخير",
            now=morning,
        )
        assert cat == "morning_greeting"

    def test_pick_persona_social_no_sabah_at_night(self, monkeypatch: pytest.MonkeyPatch) -> None:
        evening = datetime(2026, 6, 19, 21, 50, tzinfo=timezone.utc)

        def _resolve(category: str, *, inbound_text: str = "", now=None) -> str:
            return resolve_time_aware_social_category(
                category,
                inbound_text=inbound_text,
                now=evening,
            )

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.social_single_reply_guard.resolve_time_aware_social_category",
            _resolve,
        )
        reply = pick_persona_social_reply(
            _ctx("كيف حالك"),
            "morning_greeting",
            inbound_text="كيف حالك",
        )
        assert "صباح الخير" not in reply
        assert reply in PERSONA_SOCIAL_WARM_BY_CATEGORY["general_courtesy"]


class TestLayer0DeferToBrain:
    def test_kayf_halak_defers_layer0(self) -> None:
        assert should_defer_layer0_for_brain_social("كيف حالك") is True

    def test_absher_bkhair_defers_layer0(self) -> None:
        assert should_defer_layer0_for_brain_social("ابشرك والله بخير") is True

    def test_layer0_returns_none_for_wellbeing_phrase(self) -> None:
        db = MagicMock()
        decision = evaluate_layer0_route(
            db,
            tenant_id=1,
            customer_phone="966561110307",
            message="كيف حالك",
            history=[],
            conversation_id=99,
        )
        assert decision is None


class TestSingleReplyGuard:
    def test_social_greeting_decision_detection(self) -> None:
        assert is_social_greeting_decision(
            Decision(action=ACTION_GREET, args={"re_greet": True}, reason=""),
        )
        assert is_social_greeting_decision(
            Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "social_persona_ack",
                    "social_category": "wellbeing_check",
                    "block_commerce_escalation": True,
                },
                reason="",
            ),
        )
        assert not is_social_greeting_decision(
            Decision(action=ACTION_LLM_REPLY, args={"topic": "ask_price"}, reason=""),
        )

    def test_suppress_second_social_outbound_after_first_sent(self) -> None:
        trace = TurnTrace(tenant_id=1, phone="*0307", inbound_text="كيف حالك")
        claim_social_reply_selection(
            trace,
            selection=SocialReplySelection(
                action=ACTION_LLM_REPLY,
                source="brain",
                category="wellbeing_check",
            ),
        )
        trace.mark_outbound_sent(source="brain", length=42)
        assert should_suppress_competing_social_outbound(
            trace,
            source="brain_wire_send",
            action=ACTION_SOCIAL_REPLY,
            inbound_text="كيف حالك",
        ) is True

    def test_no_suppress_when_nothing_sent_yet(self) -> None:
        trace = TurnTrace(tenant_id=1, phone="*0307", inbound_text="كيف حالك")
        claim_social_reply_selection(
            trace,
            selection=SocialReplySelection(
                action=ACTION_GREET,
                source="layer0_router",
                category="greeting",
            ),
        )
        assert should_suppress_competing_social_outbound(
            trace,
            source="brain_wire_send",
            inbound_text="كيف حالك",
        ) is False
