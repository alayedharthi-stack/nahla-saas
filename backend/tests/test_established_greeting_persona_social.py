"""Established pure greetings → persona_social + persona_kind=greeting."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_GREET,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.intent.persona_interaction_classifier import (  # noqa: E402
    classify_persona_interaction,
)
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
    compose_persona_social_goal,
    is_established_greet_persona_compose_enabled,
)
from modules.ai.brain.state.stages import (  # noqa: E402
    STAGE_CHECKOUT,
    STAGE_DISCOVERY,
    STAGE_ORDERING,
)
from modules.ai.brain.suggestion.engine import DefaultSuggestionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_GREETING,
    Intent,
    MerchantConversationState,
)


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        has_active_integration=True,
        orderable=True,
        has_coupons=False,
        snapshot_fresh=True,
        store_name="متجر تجريبي",
        store_url="https://store.example.com",
    )


def _pure_greeting_intent(msg: str, *, slots: dict | None = None) -> Intent:
    """``INTENT_GREETING`` for decision-engine tests."""
    return Intent(
        name=INTENT_GREETING,
        confidence=0.95,
        slots=dict(slots or {}),
        raw_message=msg,
        extraction_method="test+pure_greeting",
    )


def _ctx(
    msg: str,
    *,
    greeted: bool = True,
    stage: str = STAGE_DISCOVERY,
    product: dict | None = None,
    checkout_url: str = "",
    slots: dict | None = None,
    intent: Intent | None = None,
) -> BrainContext:
    if intent is None:
        intent = _pure_greeting_intent(msg, slots=slots)
    elif slots:
        intent = Intent(
            name=intent.name,
            confidence=intent.confidence,
            slots={**(intent.slots or {}), **slots},
            raw_message=msg,
            extraction_method=intent.extraction_method,
        )
    state = MerchantConversationState()
    state.stage = stage
    state.greeted = greeted
    state.current_product_focus = product
    state.checkout_url = checkout_url or ""
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=msg,
        intent=intent,
        state=state,
        facts=_facts(),
    )


@pytest.mark.parametrize("msg", ["هلا", "مرحبا", "السلام عليكم ورحمة الله"])
def test_established_pure_greeting_routes_persona_social(msg: str) -> None:
    decision = DefaultDecisionEngine().decide(_ctx(msg, greeted=True))
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
    assert decision.args.get("block_commerce_escalation") is True
    assert "persona_social" in (decision.reason or "")


def test_first_turn_greeting_routes_persona_social_not_action_greet() -> None:
    """Contract (greeting persona unification): cold first turn → persona_social."""
    decision = DefaultDecisionEngine().decide(_ctx("هلا", greeted=False))
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
    assert decision.args.get("block_commerce_escalation") is True
    assert decision.action != ACTION_GREET


def test_greeting_locked_during_ordering() -> None:
    decision = DefaultDecisionEngine().decide(
        _ctx(
            "هلا",
            stage=STAGE_ORDERING,
            product={"id": 1, "title": "فستان", "price": 189},
        )
    )
    assert decision.action != ACTION_GREET
    assert decision.args.get("persona_kind") != PERSONA_KIND_GREETING
    assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


def test_greeting_locked_during_checkout() -> None:
    decision = DefaultDecisionEngine().decide(
        _ctx(
            "هلا",
            stage=STAGE_CHECKOUT,
            product={"id": 1, "title": "فستان"},
            checkout_url="https://pay.example.com/x",
        )
    )
    assert decision.action != ACTION_GREET
    assert decision.args.get("persona_kind") != PERSONA_KIND_GREETING


def test_embedded_greeting_skips_persona_re_greet_short_circuit() -> None:
    decision = DefaultDecisionEngine().decide(
        _ctx(
            "مساء الخير نحلة وش نشاطهم",
            greeted=True,
            intent=_pure_greeting_intent(
                "مساء الخير نحلة وش نشاطهم",
                slots={"embedded_greeting": True},
            ),
        )
    )
    assert decision.args.get("persona_kind") != PERSONA_KIND_GREETING
    assert decision.action != ACTION_GREET or not decision.args.get("re_greet")


def test_kill_switch_restores_legacy_re_greet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTABLISHED_GREET_PERSONA_COMPOSE_ENABLED", "false")
    assert is_established_greet_persona_compose_enabled() is False
    decision = DefaultDecisionEngine().decide(_ctx("هلا", greeted=True))
    assert decision.action == ACTION_GREET
    assert decision.args.get("re_greet") is True


def test_greeting_kind_goal_includes_phatic_and_no_reintro() -> None:
    goal = compose_persona_social_goal(PERSONA_KIND_GREETING)
    assert "persona_social" in goal
    assert "persona_kind=greeting" in goal
    assert "identity_already_introduced" in goal
    assert "Do NOT end with customer-service" in goal


def test_suggestion_suppresses_follow_up_on_greeting_kind() -> None:
    ctx = _ctx("هلا", greeted=True)
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": PERSONA_TOPIC_SOCIAL,
            "persona_kind": PERSONA_KIND_GREETING,
            "block_commerce_escalation": True,
        },
        reason="test",
    )
    snap = DefaultSuggestionEngine().suggest(ctx, decision, ActionResult(success=True))
    assert snap.needs_follow_up_question is False
    assert snap.suggested_next_step == "social_reciprocity"
    assert snap.coupon_logic_considered is False


def test_marhaba_classifies_as_greeting_not_persona_interaction() -> None:
    intent = rules.match("مرحبا")
    assert intent is not None
    assert intent.name == INTENT_GREETING
    assert classify_persona_interaction("مرحبا") is None


def test_pure_hala_intent_routes_persona_social_when_greeted() -> None:
    """Bare «هلا» classifies as GREETING and routes to persona compose."""
    ctx = _ctx("هلا", greeted=True)
    assert ctx.intent.name == INTENT_GREETING
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
