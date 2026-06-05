"""Salam etiquette on established persona_social greeting compose path."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def test_persona_greeting_compose_applies_salam_etiquette() -> None:
    composer = DefaultComposer()
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message="السلام عليكم",
        intent=Intent(name="greeting", confidence=0.95, slots={}),
        state=MerchantConversationState(turn=2),
        facts=CommerceFacts(store_name="متجر"),
    )
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": PERSONA_TOPIC_SOCIAL,
            "persona_kind": PERSONA_KIND_GREETING,
        },
        reason="test",
    )
    result = ActionResult(success=True, data={})

    async def _run() -> str:
        with patch.object(
            composer,
            "_llm_compose",
            new=AsyncMock(return_value="ياهلا 🌷"),
        ):
            return await composer.compose(decision, result, ctx)

    reply = asyncio.run(_run())
    assert reply.startswith("وعليكم السلام")
    assert "ياهلا" in reply


def test_persona_greeting_compose_does_not_call_re_greeting_template() -> None:
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    composer = DefaultComposer()
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message="هلا",
        intent=Intent(name="greeting", confidence=0.95, slots={}),
        state=MerchantConversationState(greeted=True, turn=2),
        facts=CommerceFacts(store_name="متجر"),
    )
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": PERSONA_TOPIC_SOCIAL,
            "persona_kind": PERSONA_KIND_GREETING,
        },
        reason="test",
    )
    result = ActionResult(success=True, data={})

    async def _run() -> None:
        with patch.object(T, "re_greeting") as mock_re:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value="حياك الله"),
            ):
                await composer.compose(decision, result, ctx)
            mock_re.assert_not_called()

    asyncio.run(_run())
