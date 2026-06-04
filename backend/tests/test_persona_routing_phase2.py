"""Persona Routing Phase 2 — routing-only; no reply text assertions."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent import rules
from modules.ai.brain.intent.persona_interaction_classifier import (
    classify_persona_interaction,
)
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    INTENT_GENERAL,
    INTENT_PERSONA_INTERACTION,
    INTENT_WHO_ARE_YOU,
    Intent,
    MerchantConversationState,
)


def _ctx(msg: str, intent: Intent) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=MerchantConversationState(),
        facts=CommerceFacts(store_name="متجر الاختبار", assistant_name="نحلة"),
    )


def _decide(msg: str) -> tuple[Intent | None, object]:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(
            name=INTENT_GENERAL,
            confidence=0.5,
            slots={},
            raw_message=msg,
        )
    decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
    return intent, decision


@pytest.mark.parametrize(
    "msg,expected_kind",
    [
        ("انتي حلوة؟", "appearance"),
        ("انت حلوة؟", "appearance"),
        ("اشتقت لك", "affection"),
        ("احبك", "affection"),
        ("زعلان منك", "upset"),
        ("ليش ما تضحكين؟", "tease"),
        ("فاشلة", "tease"),
        ("انت فاشلة", "tease"),
    ],
)
def test_persona_social_routing(msg: str, expected_kind: str) -> None:
    intent, decision = _decide(msg)
    assert intent is not None
    assert intent.name == INTENT_PERSONA_INTERACTION
    assert intent.slots.get("persona_topic") == "persona_social"
    assert intent.slots.get("persona_kind") == expected_kind
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == "persona_social"
    assert decision.args.get("persona_kind") == expected_kind
    assert decision.args.get("block_commerce_escalation") is True
    assert "LLM fallback" not in (decision.reason or "")


@pytest.mark.parametrize(
    "msg",
    [
        "هل تنامين؟",
        "تنامين؟",
    ],
)
def test_persona_identity_routing(msg: str) -> None:
    intent, decision = _decide(msg)
    assert intent is not None
    assert intent.name == INTENT_WHO_ARE_YOU
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == "persona_identity"
    assert decision.args.get("block_commerce_escalation") is True
    assert "LLM fallback" not in (decision.reason or "")


def test_fashila_blocked_with_operational_context() -> None:
    assert classify_persona_interaction("خدمة فاشلة") is None
    assert classify_persona_interaction("طلبي فاشل") is None
    intent, decision = _decide("خدمة فاشلة")
    assert intent.name != INTENT_PERSONA_INTERACTION
    assert decision.args.get("topic") != "persona_social"


def test_appearance_not_persona_in_product_context() -> None:
    assert classify_persona_interaction("عسل حلوة عندكم") is None
    intent, decision = _decide("عسل حلوة عندكم")
    assert intent.name != INTENT_PERSONA_INTERACTION


def test_kafow_still_social_not_persona_interaction() -> None:
    intent, decision = _decide("كفو")
    assert intent.name != INTENT_PERSONA_INTERACTION


def test_pre_commerce_shortcut_for_persona_interaction() -> None:
    intent = rules.match("احبك")
    assert intent is not None
    assert should_pre_commerce_shortcut(intent, None) is True


def test_tnamyn_phase1_regression() -> None:
    from modules.ai.brain.intent.non_commerce_classifier import classify_non_commerce

    assert classify_non_commerce("تنامين؟") is None
    intent = rules.match("تنامين؟")
    assert intent is not None
    assert intent.name == INTENT_WHO_ARE_YOU
