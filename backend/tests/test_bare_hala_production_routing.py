"""
Production-shaped routing audit — bare «هلا» vs persona greeting compose.

This module documents a **pre-existing rules-layer collision**, not a gap in the
established-greeting persona compose change:

  * ``INTENT_GREETING`` regex also matches «هلا» (confidence 0.95).
  * ``INTENT_START_ORDER`` has ``^\\s*(...|هلا|...)\\s*$`` (confidence 0.88).
  * Welcome-gate demotes ``INTENT_GREETING`` → strongest actionable sibling
    ``start_order`` with ``embedded_greeting=True`` when both fire.

Therefore bare inbound «هلا» often **never reaches** the decision-engine branch:

  ``INTENT_GREETING + greeted=True → persona_social + persona_kind=greeting``

``live_chat`` affects webhook identity-card gating only; the Brain path below
uses the same ``rules.match`` → ``DefaultDecisionEngine`` chain.

**Do not fix here** (constitutional): no keyword patches, no greeting-regex edits.
Track separately: bare-hala / start_order collision audit.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
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

# Documented collision — see module docstring.
KNOWN_BARE_HALA_COLLISION = True


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


@pytest.mark.xfail(
    KNOWN_BARE_HALA_COLLISION,
    reason=(
        "Known rules collision: bare «هلا» → start_order+embedded_greeting; "
        "persona greeting branch requires separate audit (no regex patch here)"
    ),
    strict=True,
)
def test_production_bare_hala_reaches_persona_social_greeting() -> None:
    """Desired end state after a future bare-hala / start_order audit.

    Today this xfail documents that production-shaped «هلا» does NOT reliably
    hit persona_social+greeting until the classifier collision is resolved.
    """
    assert is_established_greet_persona_compose_enabled()
    ctx = _established_ctx("هلا")
    decision = DefaultDecisionEngine().decide(ctx)

    assert ctx.intent.name == INTENT_GREETING
    assert not (ctx.intent.slots or {}).get("embedded_greeting")
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING


def test_production_bare_hala_rules_classify_start_order_embedded() -> None:
    """Current production classifier output for bare «هلا»."""
    intent = rules.match("هلا")
    assert intent is not None
    assert intent.name == INTENT_START_ORDER
    assert intent.slots.get("embedded_greeting") is True
    assert intent.extraction_method == "rules+welcome_gate"


def test_production_bare_hala_decision_not_persona_greeting_branch() -> None:
    """Established + greeted + inbound «هلا» — actual decision today."""
    ctx = _established_ctx("هلا")
    decision = DefaultDecisionEngine().decide(ctx)

    assert ctx.intent.name != INTENT_GREETING or bool(
        (ctx.intent.slots or {}).get("embedded_greeting")
    )
    assert decision.args.get("topic") != PERSONA_TOPIC_SOCIAL or (
        decision.args.get("persona_kind") != PERSONA_KIND_GREETING
    )


@pytest.mark.parametrize("msg", ["مرحبا", "السلام عليكم"])
def test_production_common_greetings_reach_persona_social(msg: str) -> None:
    """Positive control — these do not collide with start_order bare pattern."""
    ctx = _established_ctx(msg)
    assert ctx.intent.name == INTENT_GREETING
    assert not (ctx.intent.slots or {}).get("embedded_greeting")

    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
