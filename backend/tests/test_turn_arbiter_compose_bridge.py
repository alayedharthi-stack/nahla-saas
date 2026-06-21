"""
tests/test_turn_arbiter_compose_bridge.py
─────────────────────────────────────────
Phase 3A — OwnerBrief native compose bridge tests.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRICE_INQUIRY, IntentPriorityVerdict  # noqa: E402
from modules.ai.brain.turn.compose_bridge import (  # noqa: E402
    maybe_attach_owner_brief_for_compose,
    resolve_owner_brief_dict,
)
from modules.ai.brain.turn.flags import is_owner_brief_native_compose_enabled  # noqa: E402
from modules.ai.brain.turn.shadow import prepare_turn_arbitration  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _ctx(msg: str) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=msg,
        raw_message=msg,
        intent=Intent(name=INTENT_ASK_PRICE, confidence=0.9, slots={}),
        state=MerchantConversationState(turn=2),
        facts=CommerceFacts(has_products=True),
        history=[],
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )


def test_native_compose_flag_default_off():
    assert is_owner_brief_native_compose_enabled() is False


def test_attach_skipped_when_flag_off():
    ctx = _ctx("ما عندكم كود خصم")
    prepare_turn_arbitration(ctx)
    decision = Decision(action=ACTION_SEARCH_PRODUCTS, reason="catalog")
    new_decision, attached = maybe_attach_owner_brief_for_compose(decision, ctx)
    assert attached is False
    assert "owner_brief" not in (new_decision.args or {})


def test_attach_owner_brief_when_flag_on(monkeypatch):
    monkeypatch.setenv("TURN_ARBITER_OWNER_BRIEF_COMPOSE_ENABLED", "true")
    ctx = _ctx("ما عندكم كود خصم")
    prepare_turn_arbitration(ctx)
    decision = Decision(action=ACTION_SEARCH_PRODUCTS, reason="catalog")
    new_decision, attached = maybe_attach_owner_brief_for_compose(decision, ctx)

    assert attached is True
    brief = new_decision.args.get("owner_brief") or {}
    assert brief.get("compose_mode") == "persona"
    assert "answer_discount_or_product_question_first" in str(brief.get("reply_goal") or "")
    assert new_decision.args.get("owner_brief_native_compose") is True


def test_resolve_prefers_enforce_brief_over_ctx():
    ctx = _ctx("test")
    prepare_turn_arbitration(ctx)
    enforced_brief = {"owner": "support", "reply_goal": "from_enforce", "compose_mode": "persona"}
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={"owner_brief": enforced_brief},
    )
    resolved = resolve_owner_brief_dict(decision, ctx)
    assert resolved == enforced_brief


def test_resolve_from_arbitration_when_flag_on(monkeypatch):
    monkeypatch.setenv("TURN_ARBITER_OWNER_BRIEF_COMPOSE_ENABLED", "true")
    ctx = _ctx("ما عندكم كود خصم")
    prepare_turn_arbitration(ctx)
    decision = Decision(action=ACTION_SEARCH_PRODUCTS, reason="catalog")
    resolved = resolve_owner_brief_dict(decision, ctx)
    assert resolved is not None
    assert resolved.get("owner") == "discovery"
