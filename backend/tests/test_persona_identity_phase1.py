"""Phase 1 — identity probes route to thin persona compose (persona_identity).

Operational flows unchanged. No template pool expansion — identity turns
must reach ACTION_LLM_REPLY with strict response_goal guards.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _build_who_are_you_ctx(*, message: str):
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_WHO_ARE_YOU,
    )

    state = MerchantConversationState()
    facts = CommerceFacts(
        has_products=True,
        product_count=3,
        orderable=True,
        store_name="متجر الاختبار",
        assistant_name="نحلة",
    )
    intent = Intent(
        name=INTENT_WHO_ARE_YOU,
        confidence=0.98,
        slots={},
        raw_message=message,
    )
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=message,
        intent=intent,
        state=state,
        facts=facts,
    )


def test_tnamyn_not_non_commerce_dua() -> None:
    from modules.ai.brain.intent.non_commerce_classifier import classify_non_commerce

    assert classify_non_commerce("تنامين؟") is None


def test_tnamyn_intent_is_who_are_you() -> None:
    from modules.ai.brain.intent import rules
    from modules.ai.brain.types import INTENT_WHO_ARE_YOU

    intent = rules.match("تنامين؟")
    assert intent is not None
    assert intent.name == INTENT_WHO_ARE_YOU


def test_who_are_you_routes_to_persona_llm() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_FAQ_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    for msg in (
        "هل أنت نحلة؟",
        "من أنت؟",
        "تنامين؟",
        "هل أنت ذكاء اصطناعي؟",
    ):
        ctx = _build_who_are_you_ctx(message=msg)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY, msg
        assert decision.args.get("topic") == "persona_identity", msg
        assert decision.args.get("block_commerce_escalation") is True, msg
        assert decision.action != ACTION_FAQ_REPLY, msg


def test_tnamyn_not_social_reply() -> None:
    from modules.ai.brain.decision.actions import ACTION_SOCIAL_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    ctx = _build_who_are_you_ctx(message="تنامين؟")
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action != ACTION_SOCIAL_REPLY


def test_persona_identity_response_goal() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.pipeline import _compose_base_response_goal
    from modules.ai.brain.types import Decision, SuggestionSnapshot

    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={"topic": "persona_identity", "block_commerce_escalation": True},
        reason="identity probe — thin persona compose",
    )
    goal = _compose_base_response_goal(decision, SuggestionSnapshot())
    assert "persona_identity" in goal
    assert "Do NOT pitch products" in goal
    assert "Do NOT use onboarding bullet" in goal or "onboarding bullet" in goal


def test_pre_commerce_shortcut_for_who_are_you() -> None:
    from modules.ai.brain.intent import rules
    from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut

    intent = rules.match("هل أنت نحلة؟")
    assert intent is not None
    assert should_pre_commerce_shortcut(intent, None) is True


def test_detect_identity_topic_unchanged_for_man_ant() -> None:
    from modules.ai.routing.conversation_mode import detect_identity_topic

    assert detect_identity_topic("من أنت؟") == "identity"


def test_pure_greeting_still_detects_greeting() -> None:
    from modules.ai.routing.conversation_mode import detect_identity_topic

    assert detect_identity_topic("السلام عليكم") == "greeting"


def _webhook_identity_early_return_allowed(*, mode: str, identity_topic: str) -> bool:
    """Mirror ``whatsapp_webhook.py`` Phase-1 identity/greeting fast-path gate.

    Only ``identity_topic=greeting`` may call ``render_identity_reply`` and
    return early. ``identity_topic=identity`` must fall through to Brain.
    """
    from modules.ai.routing.conversation_mode import MODE_IDENTITY_REPLY

    return (
        mode == MODE_IDENTITY_REPLY
        and identity_topic == "greeting"
    )


def test_webhook_identity_topic_falls_through_to_brain_persona() -> None:
    """Regression: ``من أنت؟`` must not hit ``render_identity_reply`` at webhook."""
    from pathlib import Path

    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.intent import rules
    from modules.ai.routing.conversation_mode import (
        MODE_IDENTITY_REPLY,
        detect_identity_topic,
    )

    text = "من أنت؟"

    assert detect_identity_topic(text) == "identity"
    assert _webhook_identity_early_return_allowed(
        mode=MODE_IDENTITY_REPLY,
        identity_topic="identity",
    ) is False

    intent = rules.match(text)
    assert intent is not None
    ctx = _build_who_are_you_ctx(message=text)
    ctx.intent = intent
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == "persona_identity"

    webhook_src = (
        Path(_BACKEND) / "routers" / "whatsapp_webhook.py"
    ).read_text(encoding="utf-8")
    assert 'identity_topic == "greeting"' in webhook_src
    assert "render_identity_reply" in webhook_src
    assert "[PERSONA_IDENTITY]" in webhook_src
    assert 'identity_topic == "identity"' in webhook_src

    greeting_block_start = webhook_src.index('identity_topic == "greeting"')
    identity_block_start = webhook_src.index('identity_topic == "identity"')
    greeting_slice = webhook_src[greeting_block_start:greeting_block_start + 800]
    identity_slice = webhook_src[identity_block_start:identity_block_start + 400]
    assert "render_identity_reply" in greeting_slice
    assert "return" in greeting_slice
    assert "render_identity_reply" not in identity_slice


def test_webhook_greeting_topic_still_early_returns_render_identity() -> None:
    """Pure greeting keeps deterministic ``render_identity_reply`` at webhook."""
    from modules.ai.routing.conversation_mode import (
        MODE_IDENTITY_REPLY,
        detect_identity_topic,
    )

    text = "السلام عليكم"
    assert detect_identity_topic(text) == "greeting"
    assert _webhook_identity_early_return_allowed(
        mode=MODE_IDENTITY_REPLY,
        identity_topic="greeting",
    ) is True
