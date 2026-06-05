"""
Platform-wide bare salaam routing — classifier, clarify gate, slim eligibility.

Locks the May 2026 fix: pure greeting without commerce residue must not
become ``start_order``, must not trigger contextual clarify, and must be
slim-eligible on cold general turns.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.clarification.router import (  # noqa: E402
    try_contextual_clarification_fallback,
)
from modules.ai.brain.clarification.types import COMPOSE_TOPIC_CONTEXTUAL_CLARIFY  # noqa: E402
from modules.ai.brain.compose.brain_state_slim import (  # noqa: E402
    should_slim_general_brain_state,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    clarify_instead_of_top_products,
    product_discovery_block_reason,
)
from modules.ai.brain.state.stages import STAGE_DISCOVERY  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_GREETING,
    INTENT_START_ORDER,
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
        snapshot_fresh=True,
        store_name="متجر تجريبي",
        store_url="https://store.example.com",
    )


def _ctx(
    msg: str,
    *,
    greeted: bool = True,
    stage: str = STAGE_DISCOVERY,
    intent: Intent | None = None,
) -> BrainContext:
    if intent is None:
        matched = rules.match(msg)
        assert matched is not None
        intent = matched
    state = MerchantConversationState()
    state.greeted = greeted
    state.stage = stage
    state.assistant_identity_introduced = True
    return BrainContext(
        tenant_id=7,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state,
        facts=_facts(),
    )


def _reset_state() -> MerchantConversationState:
    """Minimum safe reset shape for cold general turn testing."""
    state = MerchantConversationState()
    state.greeted = True
    state.stage = STAGE_DISCOVERY
    state.assistant_identity_introduced = True
    state.recent_messages = []
    state.current_product_focus = None
    return state


# ── 1–3. Pure salaam classification ─────────────────────────────────────────

@pytest.mark.parametrize(
    "msg",
    [
        "هلا",
        "مرحبا",
        "السلام عليكم",
        "حياك الله",
    ],
)
def test_pure_salaam_classifies_as_greeting(msg: str) -> None:
    intent = rules.match(msg)
    assert intent is not None
    assert intent.name == INTENT_GREETING
    assert not (intent.slots or {}).get("embedded_greeting")


def test_ahleen_classifies_non_commerce() -> None:
    """``اهلين`` may classify as ``social`` (courtesy layer) — still not commerce."""
    intent = rules.match("اهلين")
    assert intent is not None
    assert intent.name in {INTENT_GREETING, "social"}
    assert intent.name != INTENT_START_ORDER


# ── 4–5. Mixed salaam + commerce ────────────────────────────────────────────

def test_hala_order_request_is_commerce() -> None:
    intent = rules.match("هلا أبي أطلب")
    assert intent is not None
    assert intent.name == INTENT_START_ORDER
    assert (intent.slots or {}).get("embedded_greeting") is True


def test_hala_price_inquiry_is_commerce() -> None:
    intent = rules.match("هلا كم سعر السمر؟")
    assert intent is not None
    assert intent.name in {INTENT_ASK_PRICE, INTENT_START_ORDER, "ask_price"}
    assert intent.name != INTENT_GREETING or (intent.slots or {}).get("embedded_greeting")


def test_urgent_continuations_still_start_order() -> None:
    """Post-selection urgency tokens must remain commerce continuations."""
    intent = rules.match("الحين")
    assert intent is not None
    assert intent.name == INTENT_START_ORDER


# ── 6–7. Clarify / discovery gates on pure greeting ─────────────────────────

@pytest.mark.parametrize("msg", ["هلا", "مرحبا", "حياك الله"])
def test_pure_greeting_blocks_product_discovery(msg: str) -> None:
    ctx = _ctx(msg)
    reason = product_discovery_block_reason(ctx, source="top_products_start_order")
    assert reason == "pure_greeting"


@pytest.mark.parametrize("msg", ["هلا", "مرحبا"])
def test_pure_greeting_skips_contextual_clarify(msg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXTUAL_CLARIFY_ENABLED", "true")
    ctx = _ctx(msg)
    dec = try_contextual_clarification_fallback(
        ctx, trigger="weak_or_unknown_intent",
    )
    assert dec is None


def test_pure_greeting_clarify_fallback_routes_persona_social() -> None:
    ctx = _ctx("هلا")
    dec = clarify_instead_of_top_products(ctx, reason="weak_or_unknown_intent")
    assert dec.action == ACTION_LLM_REPLY
    assert dec.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert dec.args.get("persona_kind") == PERSONA_KIND_GREETING
    assert dec.args.get("topic") != COMPOSE_TOPIC_CONTEXTUAL_CLARIFY


# ── 8. Slim eligibility after reset-shaped state ────────────────────────────

def test_pure_hala_slim_eligible_after_reset() -> None:
    intent = rules.match("هلا")
    assert intent is not None
    assert intent.name == INTENT_GREETING

    state = BrainReplyState(
        intent_name=intent.name,
        stage=STAGE_DISCOVERY,
        contextual_clarify_mode=False,
        persona_expression_mode=False,
    )
    eligible, reason = should_slim_general_brain_state(state)
    assert eligible is True
    assert reason == "non_commerce_intent:greeting"


# ── 9. Established turn routes persona — not loop-prone clarify path ────────

def test_reset_shaped_hala_avoids_contextual_clarify_decision() -> None:
    """Wrong routing (start_order → clarify) produced short LLM replies that
    loop_guard replaced; pure greeting must take persona_social instead."""
    state = _reset_state()
    intent = rules.match("هلا")
    assert intent is not None
    ctx = BrainContext(
        tenant_id=7,
        customer_phone="966500000001",
        message="هلا",
        intent=intent,
        state=state,
        facts=_facts(),
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
    assert decision.args.get("topic") != COMPOSE_TOPIC_CONTEXTUAL_CLARIFY
    assert "persona_social" in (decision.reason or "")


# ── 10. Commerce paths preserved ────────────────────────────────────────────

def test_commerce_price_with_greeting_still_actionable() -> None:
    intent = rules.match("السلام عليكم أبي سعر العسل")
    assert intent is not None
    assert intent.name == INTENT_ASK_PRICE
    assert (intent.slots or {}).get("embedded_greeting") is True


def test_commerce_product_browse_with_greeting() -> None:
    intent = rules.match("هلا، عندكم عسل سدر؟")
    assert intent is not None
    assert intent.name != INTENT_GREETING


def test_want_product_phrase_routes_commerce() -> None:
    intent = rules.match("أبغى المنتج")
    assert intent is not None
    assert intent.name not in {INTENT_GREETING, "social", "general"}
