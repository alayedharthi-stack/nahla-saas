"""Regression: understood social/religious media must not fall to BRAIN_SILENT_ACK."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: E402
    NON_COMMERCE_IMAGE_TAG,
    inbound_has_classified_social_religious_media,
)
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_TOPIC_SOCIAL_PERSONA_ACK,
    build_social_courtesy_decision,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
)


_HADITH_IMAGE = (
    f"{NON_COMMERCE_IMAGE_TAG}\n"
    "[وصف الصورة] بطاقة دعاء تحتوي نصاً دينياً\n"
    "قال رسول الله صلى الله عليه وسلم من صلى عليّ صلاة صلى الله عليه بها عشراً"
)

_FRIDAY_DUA_IMAGE = (
    f"{NON_COMMERCE_IMAGE_TAG}\n"
    "[وصف الصورة] بطاقة جمعة مباركة\n"
    "اللهم في هذه الجمعة اغفر لنا وارحمنا"
)


def _media_ctx(
    *,
    message: str,
    social_category: str = "religious_media",
    nc_category: str = "prophet_invocation",
) -> BrainContext:
    intent = Intent(
        name=INTENT_SOCIAL,
        confidence=0.95,
        slots={"social_category": social_category},
        raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="+966500000000",
        message=message,
        intent=intent,
        state=MerchantConversationState(),
        facts=CommerceFacts(store_name="متجر"),
    )
    ctx.block_commerce_escalation = True
    ctx.non_commerce_category = nc_category
    return ctx


class TestReligiousMediaRouting:
    def test_religious_media_routes_to_template_under_cost_policy(self) -> None:
        decision = build_social_courtesy_decision(
            "religious_media",
            confidence=0.95,
            reason="test",
            block_commerce=True,
        )
        assert decision.action == ACTION_SOCIAL_REPLY
        assert decision.args.get("social_category") == "religious_media"

    def test_religious_media_routes_to_llm_when_avoid_disabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "false")
        decision = build_social_courtesy_decision(
            "religious_media",
            confidence=0.95,
            reason="test",
            block_commerce=True,
        )
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL_PERSONA_ACK

    def test_hadith_image_intent_social_routes_to_template(self) -> None:
        ctx = _media_ctx(message=_HADITH_IMAGE, social_category="religious_media")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SOCIAL_REPLY
        assert decision.args.get("social_category") == "religious_media"

    def test_prophet_invocation_routes_to_template(self) -> None:
        ctx = _media_ctx(
            message=_HADITH_IMAGE,
            social_category="prophet_invocation",
            nc_category="prophet_invocation",
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SOCIAL_REPLY
        assert decision.args.get("social_category") == "prophet_invocation"

    def test_classified_helper_matches_non_commerce_tag(self) -> None:
        assert inbound_has_classified_social_religious_media(_HADITH_IMAGE) is True

    def test_classified_helper_false_for_unknown_media(self) -> None:
        assert inbound_has_classified_social_religious_media("صورة") is False


class TestSocialReplyEmptyFallback:
    def test_empty_social_reply_retries_persona_compose(self) -> None:
        composer = DefaultComposer()
        ctx = _media_ctx(message=_HADITH_IMAGE, social_category="religious_media")
        decision = Decision(
            action=ACTION_SOCIAL_REPLY,
            args={"social_category": "religious_media"},
            reason="legacy template path",
            confidence=0.9,
        )
        result = ActionResult(success=True, data={})

        async def _run() -> str:
            with patch.object(T, "social_reply", return_value=""):
                with patch.object(
                    composer,
                    "_compose_social_persona_ack",
                    new=AsyncMock(return_value="رد طبيعي من persona"),
                ) as mock_persona:
                    reply = await composer.compose(decision, result, ctx)
                mock_persona.assert_called_once()
            return reply

        reply = asyncio.run(_run())
        assert reply == "رد طبيعي من persona"
        assert result.data.get("chosen_path") == "social_persona_compose_from_empty_template"

    def test_empty_llm_on_social_image_retries_persona(self) -> None:
        composer = DefaultComposer()
        ctx = _media_ctx(
            message=_FRIDAY_DUA_IMAGE,
            social_category="social_image",
            nc_category="social_image",
        )
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "general"},
            reason="general llm",
            confidence=0.8,
        )
        result = ActionResult(success=True, data={})

        async def _run() -> str:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value=""),
            ):
                with patch.object(
                    composer,
                    "_compose_social_persona_ack",
                    new=AsyncMock(return_value="رد جمعة"),
                ) as mock_persona:
                    reply = await composer.compose(decision, result, ctx)
                mock_persona.assert_called_once()
            return reply

        reply = asyncio.run(_run())
        assert reply == "رد جمعة"


class TestUnchangedPaths:
    def test_eid_with_occasion_still_template(self) -> None:
        decision = build_social_courtesy_decision(
            "eid_greeting",
            confidence=0.95,
            reason="test",
        )
        assert decision.action == ACTION_SOCIAL_REPLY

    def test_product_search_not_classified_as_social_religious(self) -> None:
        msg = "[وصف الصورة] عسل سدر طبيعي 500 جرام"
        assert inbound_has_classified_social_religious_media(msg) is False

    def test_pdf_receipt_not_classified(self) -> None:
        msg = "[تصنيف الملف: إيصال دفع]"
        assert inbound_has_classified_social_religious_media(msg) is False
