"""
tests/test_social_human_context_e2e.py
──────────────────────────────────────
E2E integration: classify → social_human_context → decide → compose → postprocess.

Production regressions came from layer interaction, not a single classifier.
These five cases mirror production scenarios A–E.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fallback_policy import contains_service_closer  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_GREET,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: E402
    resolve_commerce_block,
)
from modules.ai.brain.intent_priority import (  # noqa: E402
    compute_customer_intent_priority,
    enrich_intent_with_priority,
)
from modules.ai.brain.postprocess.commerce_tail_guard import (  # noqa: E402
    apply_commerce_tail_guard,
)
from modules.ai.brain.postprocess.service_closer_guard import (  # noqa: E402
    apply_service_closer_guard,
)
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut  # noqa: E402
from modules.ai.brain.social_human_context import (  # noqa: E402
    SocialHumanContext,
    compute_social_human_context,
    enrich_intent_with_social_human,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

_CS_TAIL_MARKERS = (
    "إذا احتجت أي حاجة من المتجر",
    "نخدمك بإذن الله",
    "اكتب استفسارك هنا",
    "كيف أقدر أخدمك",
)
_MEDIA_ACK_MARKERS = (
    "وصلت رسالتك",
    "وصلني الملصق",
    "وصلتني الصورة",
)


@dataclass
class SocialTurnResult:
    intent: Intent
    shc: SocialHumanContext
    decision: Any
    reply: str
    pre_commerce_shortcut: bool
    chosen_path: str


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        orderable=True,
        product_count=10,
        in_stock_count=10,
        store_name="متجر تجريبي",
    )


def _prepare_turn(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    greeted: bool = True,
) -> tuple[BrainContext, Intent, SocialHumanContext]:
    intent = intent_rules.match(message)
    if intent is None:
        intent = Intent(
            name="general",
            confidence=0.65,
            slots={},
            raw_message=message,
        )

    state = MerchantConversationState(greeted=greeted)
    profile: Dict[str, Any] = {"inbound_metadata": dict(metadata or {})}

    ip = compute_customer_intent_priority(
        message=message,
        intent=intent,
        state=state,
        profile=profile,
    )
    intent = enrich_intent_with_priority(intent, ip)

    nc_match = None
    try:
        nc_match = resolve_commerce_block(
            message,
            inbound_metadata=metadata,
            intent_name=intent.name,
            intent_confidence=intent.confidence,
        )
    except Exception:
        nc_match = None

    shc = compute_social_human_context(
        message=message,
        intent=intent,
        state=state,
        history=list(history or []),
        inbound_metadata=metadata,
        nc_match=nc_match,
        intent_priority=ip,
    )
    intent = enrich_intent_with_social_human(intent, shc)

    ctx = BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=message,
        intent=intent,
        state=state,
        facts=_facts(),
        history=list(history or []),
        profile=profile,
        intent_priority=ip,
        social_human_context=shc,
    )
    if shc.block_commerce_escalation and shc.is_pure_social_turn:
        ctx.block_commerce_escalation = True
        if nc_match is not None:
            ctx.non_commerce_category = str(getattr(nc_match, "category", "") or "")

    return ctx, intent, shc


def _postprocess_reply(
    reply: str,
    *,
    ctx: BrainContext,
    chosen_path: str = "",
) -> str:
    out = reply or ""
    ctg = apply_commerce_tail_guard(
        out,
        ctx=ctx,
        intent_name=str(getattr(ctx.intent, "name", "") or ""),
        inbound_text=ctx.message or "",
        conversation_objective=str(
            getattr(ctx.state, "active_conversation_objective", "") or ""
        ),
        chosen_path=chosen_path,
        tenant_id=ctx.tenant_id,
    )
    out = ctg.reply
    scg = apply_service_closer_guard(
        out,
        inbound_text=ctx.message or "",
        non_commerce_block_mode=bool(ctx.block_commerce_escalation),
        block_commerce_escalation=bool(ctx.block_commerce_escalation),
        tenant_id=ctx.tenant_id,
    )
    return scg.reply


async def run_full_social_turn(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    greeted: bool = True,
    mock_llm_reply: str = "",
    executor_data: Optional[Dict[str, Any]] = None,
) -> SocialTurnResult:
    ctx, intent, shc = _prepare_turn(
        message,
        history=history,
        metadata=metadata,
        greeted=greeted,
    )
    pre = should_pre_commerce_shortcut(
        intent,
        None,
        message=message,
        state=ctx.state,
        social_human_context=shc,
    )
    decision = DefaultDecisionEngine().decide(ctx)
    result = ActionResult(success=True, data=dict(executor_data or {}))
    composer = DefaultComposer()
    action = str(getattr(decision, "action", "") or "")

    async def _compose() -> str:
        if mock_llm_reply:
            with patch(
                "modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
                new=AsyncMock(return_value=mock_llm_reply),
            ), patch(
                "modules.ai.brain.compose.responder.DefaultComposer._compose_social_persona_ack",
                new=AsyncMock(return_value=mock_llm_reply),
            ):
                return await composer.compose(decision, result, ctx)
        return await composer.compose(decision, result, ctx)

    if (
        mock_llm_reply
        and action in {ACTION_LLM_REPLY, ACTION_SOCIAL_REPLY}
        and not executor_data
    ):
        # Personality compose is mocked — E2E here validates decision + postprocess.
        raw_reply = mock_llm_reply
    else:
        raw_reply = await _compose()
    chosen_path = str(result.data.get("chosen_path") or "")
    final_reply = _postprocess_reply(
        raw_reply,
        ctx=ctx,
        chosen_path=chosen_path or str(getattr(decision, "action", "") or ""),
    )
    return SocialTurnResult(
        intent=intent,
        shc=shc,
        decision=decision,
        reply=final_reply,
        pre_commerce_shortcut=pre,
        chosen_path=chosen_path,
    )


def _run(coro):
    return asyncio.run(coro)


class TestCaseAMixedBlessingAndOrder:
    """الله يبارك لك + أبغى كيلو طلح — commerce must stay primary."""

    _MSG = "الله يبارك لك\nأبغى كيلو طلح"

    def test_commerce_primary_not_blocked(self) -> None:
        ctx, intent, shc = _prepare_turn(self._MSG)
        decision = DefaultDecisionEngine().decide(ctx)

        assert not shc.is_pure_social_turn
        assert shc.secondary_social_signal
        assert shc.block_commerce_escalation is False
        assert shc.block_commerce_tail is False
        assert shc.reply_type == "mixed"
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action not in {ACTION_SOCIAL_REPLY, ACTION_GREET}

    def test_compose_keeps_commerce_and_postprocess_preserves_order(self) -> None:
        commerce_reply = (
            "الله يبارك فيك 🌹\n\n"
            "عسل طلح — 120 ر.س للكilo\n"
            "تبغى أكمل الطلب؟"
        )
        with patch(
            "modules.ai.brain.compose.templates.product_results",
            return_value=commerce_reply,
        ):
            turn = _run(
                run_full_social_turn(
                    self._MSG,
                    executor_data={
                        "products": [
                            {
                                "title": "كيلو طلح",
                                "price": 120,
                                "id": 1,
                                "can_checkout": True,
                                "orderable": True,
                            },
                        ],
                        "query": "كيلو طلح",
                    },
                )
            )
        assert turn.decision.action == ACTION_SEARCH_PRODUCTS
        assert turn.shc.reply_type == "mixed"
        assert not turn.shc.block_commerce_escalation
        assert "طلح" in turn.reply
        assert not any(m in turn.reply for m in _CS_TAIL_MARKERS)


class TestCaseBReligiousReminder:
    """قل لا إله إلا الله — human reply, no commerce escalation."""

    _MSG = "قل لا إله إلا الله\nوصل على النبي"

    def test_routes_social_not_commerce(self) -> None:
        ctx, intent, shc = _prepare_turn(self._MSG)
        decision = DefaultDecisionEngine().decide(ctx)

        assert shc.is_pure_social_turn
        assert shc.block_commerce_escalation is True
        assert decision.action in {ACTION_LLM_REPLY, ACTION_SOCIAL_REPLY}
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_compose_postprocess_no_cs_tail(self) -> None:
        bad_tail = (
            "وعليكم السلام ورحمة الله. "
            "إذا احتجت أي حاجة من المتجر أنا هنا. "
            "نخدمك بإذن الله."
        )
        turn = _run(
            run_full_social_turn(self._MSG, mock_llm_reply=bad_tail)
        )
        assert turn.decision.action in {ACTION_LLM_REPLY, ACTION_SOCIAL_REPLY}
        assert "المتجر" not in turn.reply
        assert "بإذن الله" not in turn.reply
        assert not contains_service_closer(turn.reply)


class TestCaseCStickerAfterLocation:
    """Respect/thanks sticker after location share — not media ack."""

    _MSG = (
        "[تصنيف الستيكر: ملصق تعبيري — بدون نية شراء]\n"
        "[وصف الستيكر المرسل] thumbs up gesture of gratitude"
    )
    _META = {
        "normalized_type": "sticker",
        "source_type": "sticker",
        "sticker_kind": "expressive_only",
        "non_commerce_category": "social_image",
        "attachment_ack_mode": "social",
    }
    _HISTORY = [
        {
            "direction": "out",
            "body": "تفضل موقعنا: https://maps.google.com/example",
        },
        {
            "direction": "in",
            "body": "تمام",
        },
    ]

    def test_sticker_social_not_media_ack(self) -> None:
        ctx, _, shc = _prepare_turn(
            self._MSG,
            history=self._HISTORY,
            metadata=self._META,
        )
        decision = DefaultDecisionEngine().decide(ctx)

        assert shc.category == "social_sticker"
        assert shc.is_pure_social_turn
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_GREET
        assert decision.args.get("block_commerce_escalation") is True

    def test_reply_is_warm_not_receipt(self) -> None:
        warm = "الله يبارك فيك، تسلم 🤝"
        turn = _run(
            run_full_social_turn(
                self._MSG,
                history=self._HISTORY,
                metadata=self._META,
                mock_llm_reply=warm,
            )
        )
        assert not any(m in turn.reply for m in _MEDIA_ACK_MARKERS)
        assert turn.reply.strip()


class TestCaseDJobHelpNoGreetingFastPath:
    """تعرف أحد يوظف؟ — single path, no separate greeting."""

    _MSG = "تعرف أحد يوظف؟"

    def test_no_greeting_shortcut_single_decision(self) -> None:
        ctx, intent, shc = _prepare_turn(self._MSG, greeted=False)
        pre = should_pre_commerce_shortcut(
            intent,
            None,
            message=self._MSG,
            state=ctx.state,
            social_human_context=shc,
        )
        decision = DefaultDecisionEngine().decide(ctx)

        assert shc.category == "job_help_request"
        assert shc.suppress_greeting_fast_path is True
        assert pre is False
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_GREET
        assert decision.args.get("block_commerce_escalation") is True

    def test_compose_single_reply_no_greeting_card(self) -> None:
        body = (
            "والله ما عندنا توظيف مباشر حالياً، "
            "لكن لو حاب ترسل سيرتك نحولها للإدارة."
        )
        turn = _run(
            run_full_social_turn(
                self._MSG,
                greeted=False,
                mock_llm_reply=body,
            )
        )
        assert turn.decision.action == ACTION_LLM_REPLY
        assert turn.decision.action != ACTION_GREET
        assert "أهلاً" not in turn.reply[:20]
        assert not any(m in turn.reply for m in _CS_TAIL_MARKERS)


class TestCaseEPureSocialNoCommerceTail:
    """الحمد لله على السلامة — natural ending, tails stripped."""

    _MSG = "الحمد لله على السلامة"

    def test_pure_social_blocks_commerce(self) -> None:
        ctx, _, shc = _prepare_turn(self._MSG)
        decision = DefaultDecisionEngine().decide(ctx)

        assert shc.is_pure_social_turn
        assert shc.block_commerce_tail is True
        assert decision.action in {ACTION_LLM_REPLY, ACTION_SOCIAL_REPLY}

    def test_postprocess_strips_forced_cs_tails(self) -> None:
        llm_with_tail = (
            "الحمد لله على سلامتك يا الغالي، الله يطمن قلبك. "
            "إذا احتجت أي حاجة من المتجر أنا هنا. "
            "نخدمك بإذن الله. اكتب استفسارك هنا."
        )
        turn = _run(
            run_full_social_turn(
                self._MSG,
                mock_llm_reply=llm_with_tail,
            )
        )
        assert turn.decision.action == ACTION_LLM_REPLY
        assert turn.shc.block_commerce_tail is True
        assert "السلام" in turn.reply or "الغالي" in turn.reply or "الحمد" in turn.reply
        for marker in _CS_TAIL_MARKERS:
            assert marker not in turn.reply
        assert not contains_service_closer(turn.reply)
