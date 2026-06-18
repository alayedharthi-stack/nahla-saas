"""
tests/test_social_human_context_layer.py
────────────────────────────────────────
P0 — Social & Human Context Layer regression coverage.

Cases:
  1. Long substantive messages must not trigger greeting fast-path.
  2. Social stickers must not be treated as media-only acks.
  3. Religious reminders get social routing, not CS redirect.
  4. Commerce tails stripped from social/religious replies.
  5. Job help requests route to LLM without CS boilerplate path.
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fallback_policy import contains_service_closer, strip_closer_segments  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_GREET, ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.commerce_tail_guard import (  # noqa: E402
    apply_commerce_tail_guard,
)
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut  # noqa: E402
from modules.ai.brain.social_human_context import (  # noqa: E402
    SHC_ACKNOWLEDGEMENT,
    SHC_APPRECIATION,
    SHC_DUA,
    SHC_GRATITUDE,
    SHC_JOB_HELP_REQUEST,
    SHC_RELIGIOUS_REMINDER,
    SHC_SOCIAL_STICKER,
    SocialHumanContext,
    compute_social_human_context,
    enrich_intent_with_social_human,
    try_social_human_context_decision,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_SOCIAL,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from modules.ai.media.semantic_classifier import (  # noqa: E402
    ACK_SOCIAL,
    classify_media_semantic,
)


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_GENERAL,
    intent_confidence: float = 0.75,
    slots: dict | None = None,
    metadata: dict | None = None,
    shc: SocialHumanContext | None = None,
    greeted: bool = True,
) -> BrainContext:
    intent = Intent(
        name=intent_name,
        confidence=intent_confidence,
        slots=dict(slots or {}),
        raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=message,
        intent=intent,
        state=MerchantConversationState(greeted=greeted),
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": dict(metadata or {})},
        social_human_context=shc,
    )
    return ctx


class TestGreetingFastPathAudit:
    """Case 1 — no greeting shortcut before substantive analysis."""

    _LONG_JOB_HELP = (
        "السلام عليكم، أبي مساعدة في التقديم على وظيفة محاسب "
        "عندكم، عندي خبرة ثلاث سنوات وسيرة ذاتية جاهزة"
    )

    def test_long_job_help_suppresses_greeting_fast_path(self) -> None:
        shc = compute_social_human_context(
            message=self._LONG_JOB_HELP,
            intent=Intent(
                name=INTENT_GREETING,
                confidence=0.85,
                slots={"embedded_greeting": True},
            ),
            state=MerchantConversationState(greeted=False),
        )
        assert shc.active
        assert shc.category == SHC_JOB_HELP_REQUEST
        assert shc.suppress_greeting_fast_path is True

        intent = intent_rules.match(self._LONG_JOB_HELP)
        assert intent is not None
        assert intent.name != INTENT_GREETING or intent.slots.get("embedded_greeting")

        assert should_pre_commerce_shortcut(
            intent,
            None,
            message=self._LONG_JOB_HELP,
            state=MerchantConversationState(greeted=False),
            social_human_context=shc,
        ) is False

    def test_substantive_first_turn_not_template_greet(self) -> None:
        msg = self._LONG_JOB_HELP
        shc = compute_social_human_context(
            message=msg,
            intent=Intent(name=INTENT_GENERAL, confidence=0.7, slots={}),
            state=MerchantConversationState(greeted=False),
        )
        ctx = _ctx(msg, intent_name=INTENT_GENERAL, shc=shc, greeted=False)
        decision = try_social_human_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_GREET


class TestStickerSocialUnderstanding:
    """Case 2 — stickers are social signals, not media receipts."""

    def test_expressive_sticker_classified_social(self) -> None:
        shc = compute_social_human_context(
            message=(
                "[تصنيف الستيكر: ملصق تعبيري — بدون نية شراء]\n"
                "[وصف الستيكر المرسل] ملصق تعبيري (وجه مبتسم)"
            ),
            intent=Intent(name=INTENT_GENERAL, confidence=0.6, slots={}),
            inbound_metadata={
                "normalized_type": "sticker",
                "source_type": "sticker",
                "sticker_kind": "expressive_only",
                "non_commerce_category": "social_image",
                "attachment_ack_mode": "social",
            },
            history=[{"direction": "out", "body": "تمام، طلبك جاهز"}],
        )
        assert shc.active
        assert shc.category == SHC_SOCIAL_STICKER

    def test_semantic_classifier_sticker_ack_social(self) -> None:
        sem = classify_media_semantic(
            text_blob="ملصق تعبيري (وجه مبتسم) بدون نص",
            normalized_type="sticker",
            non_commerce_category="social_image",
        )
        assert sem.ack_mode == ACK_SOCIAL
        assert sem.category != "unknown_media"

    def test_sticker_routes_to_persona_not_media_ack(self) -> None:
        shc = compute_social_human_context(
            message=(
                "[تصنيف الستيكر: ملصق تعبيري]\n"
                "[وصف الستيكر المرسل] thumbs up gesture"
            ),
            intent=Intent(name=INTENT_GENERAL, confidence=0.6, slots={}),
            inbound_metadata={
                "normalized_type": "sticker",
                "sticker_kind": "expressive_only",
                "non_commerce_category": "social_image",
            },
        )
        ctx = _ctx(
            "[تصنيف الستيكر: ملصق تعبيري]\n[وصف الستيكر المرسل] thumbs up",
            intent_name=INTENT_GENERAL,
            shc=shc,
        )
        decision = try_social_human_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY


class TestReligiousReminderHandling:
    """Case 3 — religious reminders get human replies, not CS redirect."""

    @pytest.mark.parametrize(
        "message",
        [
            "قل لا إله إلا الله",
            "صل على محمد وعلى آل محمد",
            "اللهم صل وسلم على نبينا محمد",
        ],
    )
    def test_religious_reminder_detected(self, message: str) -> None:
        shc = compute_social_human_context(
            message=message,
            intent=Intent(name=INTENT_GENERAL, confidence=0.65, slots={}),
        )
        assert shc.active
        assert shc.category in {SHC_RELIGIOUS_REMINDER, SHC_DUA}
        assert shc.block_commerce_escalation is True

    def test_religious_reminder_enriched_to_social_intent(self) -> None:
        msg = "قل لا إله إلا الله"
        intent = Intent(name=INTENT_GENERAL, confidence=0.65, slots={})
        shc = compute_social_human_context(message=msg, intent=intent)
        enriched = enrich_intent_with_social_human(intent, shc)
        assert enriched.name == INTENT_SOCIAL
        assert enriched.slots.get("social_human_intent") in {
            SHC_RELIGIOUS_REMINDER,
            SHC_DUA,
        }

    def test_religious_reminder_decision_not_general_fallback(self) -> None:
        msg = "صل على محمد"
        shc = compute_social_human_context(
            message=msg,
            intent=Intent(name=INTENT_GENERAL, confidence=0.65, slots={}),
        )
        ctx = _ctx(msg, intent_name=INTENT_GENERAL, shc=shc)
        decision = try_social_human_context_decision(ctx)
        assert decision is not None
        assert "social human" in (decision.reason or "").lower()


class TestCommerceTailGuard:
    """Case 4 — social replies must not end with CS/commerce tails."""

    _SOCIAL_WITH_TAIL = (
        "الحمد لله على السلامة يا الغالي، الله يعافيك. "
        "إذا احتجت أي حاجة من المتجر أنا هنا."
    )

    def test_commerce_tail_stripped_for_social_context(self) -> None:
        shc = SocialHumanContext(
            active=True,
            category=SHC_GRATITUDE,
            confidence=0.94,
            source="test",
            is_pure_social_turn=True,
            reply_type="social",
            block_commerce_tail=True,
        )
        ctx = _ctx("جزاك الله خير", shc=shc)
        result = apply_commerce_tail_guard(
            self._SOCIAL_WITH_TAIL,
            ctx=ctx,
            intent_name="social",
            tenant_id=7,
        )
        assert result.stripped is True
        assert "المتجر" not in result.reply
        assert "أنا هنا" not in result.reply

    def test_fallback_policy_strips_new_markers(self) -> None:
        sample = (
            "الله يسعدك. نخدمك بإذن الله. "
            "تقدر تكتب استفسارك هنا."
        )
        cleaned, stripped = strip_closer_segments(sample, non_commerce=True)
        assert stripped is True
        assert contains_service_closer(cleaned) is False


class TestJobHelpRequest:
    """Job help must not fall through to generic CS redirect."""

    def test_job_help_routes_llm_with_block_commerce(self) -> None:
        msg = "أبي مساعدة في التقديم على وظيفة عندكم"
        shc = compute_social_human_context(
            message=msg,
            intent=Intent(name=INTENT_GENERAL, confidence=0.7, slots={}),
        )
        assert shc.category == SHC_JOB_HELP_REQUEST
        ctx = _ctx(msg, intent_name=INTENT_GENERAL, shc=shc)
        decision = try_social_human_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("block_commerce_escalation") is True
        assert "job_help" in str(decision.args.get("compose_goal_override") or "")


class TestNoDuplicateSocialRoute:
    """Case 5 — existing INTENT_SOCIAL path is not duplicated."""

    def test_existing_social_intent_uses_standard_route(self) -> None:
        msg = "جزاك الله خير"
        shc = compute_social_human_context(
            message=msg,
            intent=Intent(
                name=INTENT_SOCIAL,
                confidence=0.94,
                slots={"social_category": "thanks"},
            ),
        )
        ctx = _ctx(
            msg,
            intent_name=INTENT_SOCIAL,
            slots={"social_category": "thanks"},
            shc=shc,
        )
        assert try_social_human_context_decision(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_LLM_REPLY, "social_reply"}


class TestSemanticSocialDetection:
    """Semantic-first detection — structural families, not literal whitelist."""

    @pytest.mark.parametrize(
        "message",
        [
            "الله يجعلها في ميزان حسناتك",
            "بيض الله وجهك",
            "الله يرزقنا وإياك",
            "جعلك سالم",
            "الله يبارك لك",
        ],
    )
    def test_semantic_dua_phrases_detected(self, message: str) -> None:
        shc = compute_social_human_context(
            message=message,
            intent=Intent(name=INTENT_GENERAL, confidence=0.65, slots={}),
        )
        assert shc.active
        assert shc.is_pure_social_turn
        assert shc.category in {
            SHC_DUA, SHC_GRATITUDE, SHC_APPRECIATION, SHC_ACKNOWLEDGEMENT,
        }


class TestPrimaryGoalVsSecondarySocial:
    """Mixed turns — commerce primary must not block commerce."""

    _MIXED = "الله يبارك لك، أبغى كيلو طلح"

    def test_mixed_blessing_and_order_keeps_commerce_primary(self) -> None:
        from modules.ai.brain.intent import rules as intent_rules  # noqa: PLC0415

        intent = intent_rules.match(self._MIXED)
        assert intent is not None
        shc = compute_social_human_context(
            message=self._MIXED,
            intent=intent,
        )
        assert shc.active
        assert not shc.is_pure_social_turn
        assert shc.secondary_social_signal
        assert shc.block_commerce_tail is False
        assert shc.block_commerce_escalation is False
        assert shc.reply_type == "mixed"
        assert try_social_human_context_decision(
            _ctx(self._MIXED, intent_name=intent.name, shc=shc),
        ) is None


class TestCommerceTailGuardContext:
    """Operational tails must survive; generic CS tails stripped on social."""

    def test_operational_order_tail_preserved(self) -> None:
        sample = (
            "تم تسجيل طلبك، وإذا احتجت تعديل على الطلب فأخبرني."
        )
        result = apply_commerce_tail_guard(
            sample,
            intent_name="start_order",
            conversation_objective="order",
            chosen_path="order_create",
            tenant_id=7,
        )
        assert not result.stripped
        assert "تعديل" in result.reply

    def test_social_tail_stripped_but_operational_not(self) -> None:
        shc = SocialHumanContext(
            active=True,
            category=SHC_GRATITUDE,
            confidence=0.94,
            source="test",
            is_pure_social_turn=True,
            reply_type="social",
            block_commerce_tail=True,
        )
        sample = (
            "الله يسعدك. إذا احتجت أي حاجة من المتجر أنا هنا."
        )
        result = apply_commerce_tail_guard(
            sample,
            ctx=_ctx("جزاك الله", shc=shc),
            intent_name="social",
            tenant_id=7,
        )
        assert result.stripped
        assert "المتجر" not in result.reply

