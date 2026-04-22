"""
tests/test_platform_brain_state_driven.py
─────────────────────────────────────────
State-driven contract tests for the Platform Brain
(`backend/core/conversation_engine.py`).

Background
──────────
The Platform sales bot (`PLATFORM_TENANT_ID`) used to re-fire the
`SHOW_WELCOME_MENU` deterministic action on every "هلا" / "مرحبا"
because `ConversationState` had no greeting lock. This is the same
class of bug that the MerchantBrain composer fixed via `state.greeted`
+ a defense-in-depth guard.

These tests freeze the new contract:

  1. A brand-new conversation greets ONCE (`SHOW_WELCOME_MENU`).
  2. After the welcome menu fires, `state.greeted=True` must be
     persisted, and the next greeting is downgraded to
     `GENERATE_AI_REPLY` (LLM with full context) — never the menu again.
  3. A greeting received past the `discovery` stage is also downgraded
     so the bot doesn't restart the funnel mid-checkout.
  4. The system injection block surfaces intent + action + decision
     reason + response goal so Claude has the same context the rule
     layer used.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from core.conversation_engine import (  # noqa: E402
    ContextBuilder,
    ConversationState,
    DecisionEngine,
    GENERATE_AI_REPLY,
    SHOW_WELCOME_MENU,
    S_CHECKOUT,
    S_DISCOVERY,
    S_QUALIFICATION,
)


# ── 1. First greeting still wins ─────────────────────────────────────────────

def test_first_greeting_fires_welcome_menu():
    state = ConversationState(phone="+966500000001")
    assert state.greeted is False
    assert state.stage == S_DISCOVERY

    action, reason = DecisionEngine.decide("greeting", state)

    assert action == SHOW_WELCOME_MENU
    assert "first_turn" in reason


# ── 2. Re-greet after greeted=True is downgraded to LLM ──────────────────────

def test_greeting_after_greeted_routes_to_llm():
    state = ConversationState(phone="+966500000002", greeted=True)

    action, reason = DecisionEngine.decide("greeting", state)

    assert action == GENERATE_AI_REPLY, (
        "Once greeted, a second 'هلا' must NOT replay SHOW_WELCOME_MENU"
    )
    assert "greeting_after_first_turn" in reason
    assert "greeted=True" in reason


# ── 3. Mid-funnel greeting (any stage past discovery) → LLM ──────────────────

def test_greeting_past_discovery_stage_routes_to_llm():
    # Even if greeted is False (e.g. legacy state row), being mid-funnel
    # means the bot should NOT restart with a welcome menu.
    state = ConversationState(
        phone="+966500000003",
        stage=S_QUALIFICATION,
        greeted=False,
    )

    action, reason = DecisionEngine.decide("greeting", state)

    assert action == GENERATE_AI_REPLY
    assert "stage=qualification" in reason


def test_greeting_in_checkout_routes_to_llm_not_menu():
    state = ConversationState(
        phone="+966500000004",
        stage=S_CHECKOUT,
        greeted=True,
    )

    # NOTE: Tier 2 (`stage=checkout`) wins over greeting handling — the
    # decision engine pushes the checkout link rather than a re-greet,
    # which is the desired behaviour. The important contract here is
    # simply: NO `SHOW_WELCOME_MENU` is ever produced for a non-virgin
    # conversation.
    action, _reason = DecisionEngine.decide("greeting", state)

    assert action != SHOW_WELCOME_MENU


# ── 4. System injection surfaces decision context for Claude ─────────────────

def test_system_injection_includes_decision_context_block():
    state = ConversationState(
        phone="+966500000005",
        stage=S_QUALIFICATION,
        greeted=True,
        turn=3,
    )

    block = ContextBuilder.build_system_injection(
        state,
        next_action=GENERATE_AI_REPLY,
        decision_reason="greeting_after_first_turn:greeted=True:stage=qualification",
        intent="greeting",
    )

    # The block must clearly tell the LLM:
    #   • intent name
    #   • action
    #   • decision reason
    #   • response goal
    #   • greeted flag (so it doesn't say "هلا" again)
    assert "نية التاجر" in block
    assert "greeting" in block
    assert "GENERATE_AI_REPLY" in block
    assert "greeting_after_first_turn" in block
    assert "هدف الرد" in block
    assert "سبق الترحيب بالتاجر" in block
    assert "ممنوع تكرار الترحيب" in block


# ── 5. State serialization round-trips greeted ──────────────────────────────

def test_greeted_round_trips_through_to_dict_from_dict():
    state = ConversationState(phone="+966500000006", greeted=True, turn=4)

    payload = state.to_dict()
    assert payload["greeted"] is True

    restored = ConversationState.from_dict(payload)
    assert restored.greeted is True
    assert restored.phone == "+966500000006"
    assert restored.turn == 4


def test_greeted_defaults_false_when_missing_from_legacy_row():
    # Older persisted rows did not store this key.
    legacy = {
        "phone": "+966500000007",
        "stage": "discovery",
        "turn": 1,
        "slots": {},
    }
    restored = ConversationState.from_dict(legacy)
    assert restored.greeted is False


# ── 6. Buy-intent priorities are unaffected by the greeting guard ───────────

def test_subscribe_intent_still_wins_over_greeting_guard():
    state = ConversationState(phone="+966500000008", greeted=True)
    action, reason = DecisionEngine.decide("subscribe_now", state)
    # Tier 1 buy-intent must short-circuit BEFORE the greeting branch.
    assert action != GENERATE_AI_REPLY
    assert action != SHOW_WELCOME_MENU
    assert "subscribe" in reason
