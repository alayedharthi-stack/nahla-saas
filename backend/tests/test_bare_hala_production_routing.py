"""
Production-shaped routing — bare salaam vs persona greeting (Phase 3).

Pure salaam (no commerce residue) must classify as ``INTENT_GREETING`` and
reach persona LLM compose by default. Template ``ACTION_GREET`` remains
available only when routine LLM avoid is explicitly enabled.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import ACTION_GREET, ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
    is_established_greet_persona_compose_enabled,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_GREETING,
    INTENT_START_ORDER,
    MerchantConversationState,
)


def _production_facts() -> CommerceFacts:
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


def _established_ctx(msg: str) -> BrainContext:
    """Established relationship — mirrors post-onboarding Brain turn."""
    intent = rules.match(msg)
    assert intent is not None
    state = MerchantConversationState()
    state.greeted = True
    state.stage = "discovery"
    state.assistant_identity_introduced = True
    return BrainContext(
        tenant_id=7,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state,
        facts=_production_facts(),
    )


def _assert_persona_greeting_llm(decision) -> None:
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING


def test_production_bare_hala_routes_persona_llm_re_greet() -> None:
    assert is_established_greet_persona_compose_enabled()
    ctx = _established_ctx("هلا")
    decision = DefaultDecisionEngine().decide(ctx)

    assert ctx.intent.name == INTENT_GREETING
    assert not (ctx.intent.slots or {}).get("embedded_greeting")
    _assert_persona_greeting_llm(decision)


def test_production_bare_hala_rules_classify_greeting() -> None:
    intent = rules.match("هلا")
    assert intent is not None
    assert intent.name == INTENT_GREETING
    assert not (intent.slots or {}).get("embedded_greeting")
    assert intent.extraction_method == "rules"


def test_production_bare_hala_decision_is_persona_llm_branch() -> None:
    ctx = _established_ctx("هلا")
    decision = DefaultDecisionEngine().decide(ctx)

    assert ctx.intent.name == INTENT_GREETING
    _assert_persona_greeting_llm(decision)


@pytest.mark.parametrize("msg", ["مرحبا", "السلام عليكم"])
def test_production_common_greetings_route_persona_llm(msg: str) -> None:
    ctx = _established_ctx(msg)
    assert ctx.intent.name == INTENT_GREETING
    assert not (ctx.intent.slots or {}).get("embedded_greeting")

    decision = DefaultDecisionEngine().decide(ctx)
    _assert_persona_greeting_llm(decision)


@pytest.mark.parametrize("msg", ["مرحبا", "السلام عليكم", "هلا"])
def test_production_common_greetings_route_template_when_avoid_enabled(
    msg: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "true")
    ctx = _established_ctx(msg)
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_GREET
    assert decision.args.get("re_greet") is True
    assert decision.action != ACTION_LLM_REPLY


def test_mixed_hala_order_request_still_commerce() -> None:
    intent = rules.match("هلا أبي أطلب")
    assert intent is not None
    assert intent.name == INTENT_START_ORDER
    assert (intent.slots or {}).get("embedded_greeting") is True
