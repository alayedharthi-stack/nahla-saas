"""Persona-safe local template engine — warm greetings and social acks."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.persona_template_engine import (  # noqa: E402
    PERSONA_ALLOWED_EMOJI,
    PERSONA_GREETING_COLD,
    PERSONA_SOCIAL_DUA_THANKS,
    PERSONA_SOCIAL_WARM_BY_CATEGORY,
    pick_persona_greeting,
    pick_persona_social_reply,
    pick_persona_variant,
    persona_reply_has_light_emoji,
    persona_reply_is_warm_greeting,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_GREET, ACTION_SOCIAL_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_GREETING,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
)


def _facts() -> CommerceFacts:
    return CommerceFacts(store_name="متجر تجريبي", has_products=True, orderable=True)


def _ctx(
    *,
    tenant_id: int = 7,
    phone: str = "+966555555555",
    history: list | None = None,
    message: str = "هلا",
    intent: Intent | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=intent
        or Intent(name=INTENT_GREETING, confidence=0.95, slots={}, raw_message=message),
        state=MerchantConversationState(greeted=False, turn=len(history or [])),
        facts=_facts(),
        history=list(history or []),
    )


class TestPersonaGreetingVariants:
    def test_pure_greeting_compose_uses_warm_local_variant(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx(message="هلا")
        decision = Decision(action=ACTION_GREET, args={}, reason="test", confidence=0.9)
        result = ActionResult(success=True, data={})

        async def _run() -> str:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value="must not call"),
            ) as mock_llm:
                reply = await composer.compose(decision, result, ctx)
            mock_llm.assert_not_called()
            return reply

        reply = asyncio.run(_run())
        assert persona_reply_is_warm_greeting(reply)
        assert persona_reply_has_light_emoji(reply) or "أبشر" in reply

    def test_greeting_variant_from_user_pool(self) -> None:
        reply = pick_persona_greeting(_ctx())
        assert reply in PERSONA_GREETING_COLD

    def test_re_greet_uses_shorter_warm_pool(self) -> None:
        reply = pick_persona_greeting(_ctx(), re_greet=True)
        assert reply
        assert persona_reply_is_warm_greeting(reply)

    def test_emoji_policy_at_most_one_allowed(self) -> None:
        for text in PERSONA_GREETING_COLD:
            assert persona_reply_has_light_emoji(text)
            emojis = [ch for ch in text if ch in PERSONA_ALLOWED_EMOJI]
            assert len(emojis) <= 1
            assert all(ch in PERSONA_ALLOWED_EMOJI for ch in emojis)


class TestPersonaSocialVariants:
    def test_thanks_reply_is_warm_with_light_emoji(self) -> None:
        ctx = _ctx(
            message="شكرا",
            intent=Intent(
                name=INTENT_SOCIAL,
                confidence=0.95,
                slots={"social_category": "thanks"},
                raw_message="شكرا",
            ),
        )
        reply = pick_persona_social_reply(ctx, "thanks")
        assert reply in PERSONA_SOCIAL_WARM_BY_CATEGORY["thanks"]
        assert persona_reply_has_light_emoji(reply)

    def test_social_compose_no_llm(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx(
            message="جزاك الله خير",
            intent=Intent(
                name=INTENT_SOCIAL,
                confidence=0.95,
                slots={"social_category": "thanks"},
                raw_message="جزاك الله خير",
            ),
        )
        decision = Decision(
            action=ACTION_SOCIAL_REPLY,
            args={"social_category": "thanks"},
            reason="test",
        )
        result = ActionResult(success=True, data={})

        async def _run() -> str:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value="must not call"),
            ) as mock_llm:
                reply = await composer.compose(decision, result, ctx)
            mock_llm.assert_not_called()
            return reply

        reply = asyncio.run(_run())
        assert reply.strip()
        assert reply in PERSONA_SOCIAL_DUA_THANKS
        assert "العفو" not in reply
        assert persona_reply_has_light_emoji(reply)

    def test_religious_thanks_uses_dua_pool_not_alafu(self) -> None:
        ctx = _ctx(message="جزاك الله خير")
        reply = pick_persona_social_reply(ctx, "thanks", inbound_text="جزاك الله خير")
        assert reply in PERSONA_SOCIAL_DUA_THANKS
        assert "العفو" not in reply

    def test_plain_thanks_stays_on_secular_pool(self) -> None:
        ctx = _ctx(message="شكرا")
        reply = pick_persona_social_reply(ctx, "thanks", inbound_text="شكرا")
        assert reply in PERSONA_SOCIAL_WARM_BY_CATEGORY["thanks"]

    def test_dua_thanks_emoji_policy(self) -> None:
        for text in PERSONA_SOCIAL_DUA_THANKS:
            assert persona_reply_has_light_emoji(text)
            emojis = [ch for ch in text if ch in PERSONA_ALLOWED_EMOJI]
            assert len(emojis) <= 1
            assert all(ch in PERSONA_ALLOWED_EMOJI for ch in emojis)

    def test_dua_thanks_avoids_immediate_repeat(self) -> None:
        first = PERSONA_SOCIAL_DUA_THANKS[0]
        ctx = _ctx(
            message="جزاك الله خير",
            history=[{"direction": "out", "body": first}],
        )
        reply = pick_persona_social_reply(ctx, "thanks", inbound_text="جزاك الله خير")
        assert reply != first

    def test_dua_thanks_no_tenant_33_special_case(self) -> None:
        t7 = pick_persona_social_reply(
            _ctx(tenant_id=7, message="جزاك الله خير"),
            "thanks",
            inbound_text="جزاك الله خير",
        )
        t33 = pick_persona_social_reply(
            _ctx(tenant_id=33, message="جزاك الله خير"),
            "thanks",
            inbound_text="جزاك الله خير",
        )
        assert t7 in PERSONA_SOCIAL_DUA_THANKS
        assert t33 in PERSONA_SOCIAL_DUA_THANKS


class TestVariationPolicy:
    def test_avoids_exact_phrase_repeat_from_history(self) -> None:
        first = PERSONA_GREETING_COLD[0]
        ctx = _ctx(
            history=[{"direction": "outbound", "body": first}],
        )
        reply = pick_persona_greeting(ctx)
        assert reply != first

    def test_avoids_same_emoji_when_alternative_exists(self) -> None:
        pool = list(PERSONA_GREETING_COLD)
        emoji_line = next(t for t in pool if "😊" in t)
        ctx = _ctx(history=[{"direction": "out", "body": emoji_line}])
        reply = pick_persona_variant(pool, ctx)
        if any("😊" not in t for t in pool):
            assert "😊" not in reply or reply == emoji_line

    def test_rotation_differs_by_tenant_not_hardcoded(self) -> None:
        a = pick_persona_greeting(_ctx(tenant_id=7, phone="+966500000001"))
        b = pick_persona_greeting(_ctx(tenant_id=99, phone="+966500000002"))
        # Different seeds may pick different variants (not guaranteed equal).
        assert a in PERSONA_GREETING_COLD
        assert b in PERSONA_GREETING_COLD

    def test_no_tenant_33_special_case_in_engine(self) -> None:
        t7 = pick_persona_greeting(_ctx(tenant_id=7))
        t33 = pick_persona_greeting(_ctx(tenant_id=33))
        assert t7 in PERSONA_GREETING_COLD
        assert t33 in PERSONA_GREETING_COLD


class TestCommerceSafety:
    def test_embedded_greeting_product_question_not_pure_greet(self) -> None:
        msg = "هلا عندكم عسل طلح؟"
        ctx = BrainContext(
            tenant_id=7,
            customer_phone="+966555555555",
            message=msg,
            intent=Intent(
                name=INTENT_GREETING,
                confidence=0.95,
                slots={"embedded_greeting": True},
                raw_message=msg,
            ),
            state=MerchantConversationState(greeted=False),
            facts=_facts(),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_GREET


class TestOrderAwareGreeting:
    def test_mid_order_greeting_is_order_aware_not_generic(self) -> None:
        from modules.ai.brain.compose.persona_template_engine import (
            PERSONA_GREETING_ORDER_AWARE,
            persona_reply_is_order_aware_greeting,
        )
        from modules.ai.brain.state.stages import STAGE_ORDERING

        state = MerchantConversationState(greeted=True, stage=STAGE_ORDERING)
        ctx = BrainContext(
            tenant_id=7,
            customer_phone="+966555555555",
            message="هلا",
            intent=Intent(name=INTENT_GREETING, confidence=0.95, slots={}, raw_message="هلا"),
            state=state,
            facts=_facts(),
        )
        reply = pick_persona_greeting(ctx, re_greet=True)
        assert reply in PERSONA_GREETING_ORDER_AWARE
        assert persona_reply_is_order_aware_greeting(reply)
        assert reply not in ("أهلًا فيك 😊", "حياك الله 🌷")

    def test_checkout_greeting_uses_checkout_pool(self) -> None:
        from modules.ai.brain.compose.persona_template_engine import (
            PERSONA_GREETING_CHECKOUT_AWARE,
        )
        from modules.ai.brain.state.stages import STAGE_CHECKOUT

        state = MerchantConversationState(greeted=True, stage=STAGE_CHECKOUT)
        ctx = BrainContext(
            tenant_id=7,
            customer_phone="+966555555555",
            message="هلا",
            intent=Intent(name=INTENT_GREETING, confidence=0.95, slots={}, raw_message="هلا"),
            state=state,
            facts=_facts(),
        )
        reply = pick_persona_greeting(ctx, re_greet=True)
        assert reply in PERSONA_GREETING_CHECKOUT_AWARE
