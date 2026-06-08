"""
Greeting Persona Unification — contract tests (TDD).

Target: all pure greetings (including cold first-turn) route through
persona_social compose — never PRE_BRAIN_FAST_PATH, never
render_identity_reply, never ACTION_GREET templates.

Production implementation is intentionally NOT included yet; these
tests define the contract and should fail until PR-1/PR-2 land.

Run:
    cd backend
    python -m pytest tests/test_greeting_persona_unification.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_GREET,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
)
from modules.ai.brain.state.store import DefaultStateStore  # noqa: E402
from modules.ai.brain.state.stages import STAGE_DISCOVERY  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_GREETING,
    INTENT_ASK_PRICE,
    Intent,
    MerchantConversationState,
)
from modules.ai.routing.conversation_mode import detect_identity_topic  # noqa: E402

# Deterministic CS-desk phrases the unified persona path must never emit
# on the first-greeting routing path (templates / fast-path residuals).
_BANNED_FIRST_GREETING_PHRASES = (
    "كيف أقدر أخدمك",
    "كيف أقدر أساعدك",
    "بماذا أخدمك",
    "تحت أمرك",
)

_COLD_PURE_GREETING = "السلام عليكم"
_MIXED_GREETING_PRICE = "السلام عليكم كم سعر السمر؟"


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
        assistant_name="نحلة",
    )


def _greeting_intent(msg: str = _COLD_PURE_GREETING) -> Intent:
    intent = rules.match(msg)
    assert intent is not None
    assert intent.name == INTENT_GREETING, (
        f"expected INTENT_GREETING for pure greeting fixture, got {intent.name!r}"
    )
    return intent


def _cold_greeting_ctx(
    msg: str = _COLD_PURE_GREETING,
    *,
    intent: Intent | None = None,
) -> BrainContext:
    state = MerchantConversationState()
    state.stage = STAGE_DISCOVERY
    state.greeted = False
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=msg,
        intent=intent or _greeting_intent(msg),
        state=state,
        facts=_facts(),
    )


def _persona_greeting_decision(*, re_greet: bool = False) -> Decision:
    args = {
        "topic": PERSONA_TOPIC_SOCIAL,
        "persona_kind": PERSONA_KIND_GREETING,
        "block_commerce_escalation": True,
    }
    if re_greet:
        args["re_greet"] = True
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason="persona_social greeting — contract fixture",
    )


def _webhook_greeting_fast_path_block() -> str:
    """Return the webhook source slice for the greeting fast-path gate."""
    webhook_src = (
        Path(_BACKEND) / "routers" / "whatsapp_webhook.py"
    ).read_text(encoding="utf-8")
    marker = 'identity_topic == "greeting"'
    start = webhook_src.index(marker)
    return webhook_src[start : start + 1600]


# ── 1. Cold first-turn pure greeting → persona_social ─────────────────────


@pytest.mark.parametrize("msg", [_COLD_PURE_GREETING, "هلا", "مرحبا"])
def test_cold_first_turn_pure_greeting_routes_persona_social(msg: str) -> None:
    """First inbound pure greeting must use persona compose, not ACTION_GREET."""
    decision = DefaultDecisionEngine().decide(_cold_greeting_ctx(msg))
    assert decision.action == ACTION_LLM_REPLY, (
        f"expected ACTION_LLM_REPLY for cold pure greeting, got {decision.action!r} "
        f"(reason={decision.reason!r})"
    )
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
    assert decision.args.get("block_commerce_escalation") is True
    assert decision.action != ACTION_GREET


def test_cold_first_turn_salaam_intent_is_greeting_not_commerce() -> None:
    assert detect_identity_topic(_COLD_PURE_GREETING) == "greeting"
    intent = rules.match(_COLD_PURE_GREETING)
    assert intent is not None
    assert intent.name == INTENT_GREETING


# ── 2. Established re-greeting — preserve persona_social ──────────────────


@pytest.mark.parametrize("msg", ["هلا", "مرحبا", "السلام عليكم ورحمة الله"])
def test_established_pure_greeting_still_routes_persona_social(msg: str) -> None:
    """Regression: established re-greetings keep persona_social behavior."""
    state = MerchantConversationState()
    state.greeted = True
    ctx = BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=msg,
        intent=Intent(
            name=INTENT_GREETING,
            confidence=0.95,
            slots={},
            raw_message=msg,
            extraction_method="test",
        ),
        state=state,
        facts=_facts(),
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
    assert decision.args.get("block_commerce_escalation") is True


# ── 3. Mixed greeting + commerce — no persona greeting short-circuit ──────


def test_mixed_greeting_price_classifies_as_commerce_not_pure_greeting() -> None:
    intent = rules.match(_MIXED_GREETING_PRICE)
    assert intent is not None
    assert intent.name == INTENT_ASK_PRICE
    assert (intent.slots or {}).get("embedded_greeting") is True


def test_mixed_greeting_price_not_persona_greeting_route() -> None:
    """Salaam + price ask must answer commerce — never persona_kind=greeting."""
    intent = rules.match(_MIXED_GREETING_PRICE)
    assert intent is not None
    ctx = _cold_greeting_ctx(_MIXED_GREETING_PRICE, intent=intent)
    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action != ACTION_GREET, (
        "mixed greeting+commerce must not emit welcome-card ACTION_GREET"
    )
    assert decision.args.get("persona_kind") != PERSONA_KIND_GREETING, (
        f"mixed turn must not persona-greet; got action={decision.action!r} "
        f"args={decision.args!r} reason={decision.reason!r}"
    )
    if decision.action == ACTION_LLM_REPLY:
        assert (
            decision.args.get("topic") != PERSONA_TOPIC_SOCIAL
            or decision.args.get("persona_kind") != PERSONA_KIND_GREETING
        )


# ── 4. State store — persona greeting stamps greeted=True ─────────────────


def test_persona_greeting_transition_marks_greeted_true() -> None:
    """ACTION_LLM_REPLY persona_social+greeting must persist greeted=True."""
    state = MerchantConversationState()
    state.greeted = False
    intent = _greeting_intent()
    decision = _persona_greeting_decision()

    new_state = DefaultStateStore().transition(state, intent, decision)

    assert new_state.greeted is True, (
        "persona_social greeting must stamp greeted=True on first contact"
    )


def test_persona_greeting_transition_marks_greeted_idempotent() -> None:
    state = MerchantConversationState()
    state.greeted = True
    intent = _greeting_intent()
    decision = _persona_greeting_decision(re_greet=True)

    new_state = DefaultStateStore().transition(state, intent, decision)

    assert new_state.greeted is True


# ── 5. Webhook — no PRE_BRAIN_FAST_PATH for cold greetings ────────────────


def test_webhook_cold_greeting_must_not_use_pre_brain_fast_path() -> None:
    """Contract: webhook must not early-return via render_identity_reply."""
    block = _webhook_greeting_fast_path_block()
    assert "render_identity_reply" not in block, (
        "greeting fast path must be removed — cold greetings reach Brain"
    )
    assert "PRE_BRAIN_FAST_PATH" not in block


def test_render_identity_greeting_variants_contain_banned_cs_phrases() -> None:
    """Document why PRE_BRAIN_FAST_PATH must be deleted (residual source)."""
    from modules.ai.routing.conversation_mode import _greeting_variants  # noqa: PLC0415

    variants = _greeting_variants(assistant_name="", store_name="متجر تجريبي")
    assert variants, "expected greeting variants for regression documentation"
    hits = [
        v for v in variants
        if any(phrase in v for phrase in _BANNED_FIRST_GREETING_PHRASES)
    ]
    assert hits, (
        "render_identity_reply greeting pool must still contain banned CS "
        "phrases until the fast path is removed"
    )


# ── 6. Regression — first-greeting path must not use template greet ───────


def test_cold_first_turn_not_action_greet_template_path() -> None:
    decision = DefaultDecisionEngine().decide(_cold_greeting_ctx())
    assert decision.action != ACTION_GREET


def test_greeting_templates_pool_still_contains_banned_phrases_until_deprecated() -> None:
    """Sanity: templates.greeting is the residual we are retiring from routing."""
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    texts = [
        T.greeting(store_name="متجر تجريبي", assistant_name="", variant=v)
        for v in range(3)
    ]
    assert any(
        any(phrase in text for phrase in _BANNED_FIRST_GREETING_PHRASES)
        for text in texts
    ), "at least one greeting template variant still carries banned CS phrasing"


def test_unified_path_must_not_select_template_greeting_for_cold_turn() -> None:
    """Routing contract: cold pure greeting never selects templates.greeting."""
    decision = DefaultDecisionEngine().decide(_cold_greeting_ctx())
    assert decision.action != ACTION_GREET
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
