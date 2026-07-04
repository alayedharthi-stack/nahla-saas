"""Tests for FactBoundPersonaComposer (Phase 2 social test mode)."""
from __future__ import annotations

import pytest

from core.tenant import STORE_AI_MODE_TEST, merge_ai_defaults
from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards
from modules.ai.brain.persona.fact_bound_composer import (
    FactBoundPersonaComposer,
    build_social_facts_bundle,
)
from modules.ai.brain.persona.flags import is_persona_composer_enforce_enabled
from modules.ai.brain.persona.integration import should_enforce_persona_compose
from modules.ai.brain.persona.surface_resolver import (
    resolve_greet_surface,
    resolve_social_surface,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)
from tests.constitution_helpers import (
    assert_no_non_saudi_arabic,
    rejects_checkout_pressure_after_social,
    rejects_social_support_bot_phrase,
    social_replies_are_non_deterministic,
    try_compose_persona_samples,
)


def _ctx(
    *,
    tenant_id: int = 33,
    phone: str = "966542980511",
    message: str = "كيف الحال",
    ai_settings: dict | None = None,
) -> BrainContext:
    mc: dict = {}
    if ai_settings is not None:
        mc["ai_settings"] = ai_settings
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.9, raw_message=message),
        state=MerchantConversationState(),
        facts=CommerceFacts(),
        merchant_context=mc,
    )


class TestPersonaComposeGate:
    def test_gate_requires_test_mode_and_allowlist(self) -> None:
        ai = merge_ai_defaults(
            {
                "persona_composer_enabled": True,
                "store_ai_mode": STORE_AI_MODE_TEST,
                "ai_test_allowed_numbers": ["966542980511"],
            }
        )
        assert is_persona_composer_enforce_enabled(
            tenant_id=33,
            customer_phone="966542980511",
            ai_settings=ai,
        )

    def test_gate_blocks_non_allowlisted_tenant(self) -> None:
        ai = merge_ai_defaults(
            {
                "persona_composer_enabled": True,
                "store_ai_mode": STORE_AI_MODE_TEST,
                "ai_test_allowed_numbers": ["966542980511"],
            }
        )
        assert not is_persona_composer_enforce_enabled(
            tenant_id=99,
            customer_phone="966542980511",
            ai_settings=ai,
        )

    def test_gate_blocks_when_disabled(self) -> None:
        ai = merge_ai_defaults(
            {
                "persona_composer_enabled": False,
                "store_ai_mode": STORE_AI_MODE_TEST,
                "ai_test_allowed_numbers": ["966542980511"],
            }
        )
        assert not is_persona_composer_enforce_enabled(
            tenant_id=33,
            customer_phone="966542980511",
            ai_settings=ai,
        )

    def test_should_enforce_on_ctx_when_configured(self) -> None:
        ctx = _ctx(
            ai_settings={
                "persona_composer_enabled": True,
                "store_ai_mode": STORE_AI_MODE_TEST,
                "ai_test_allowed_numbers": ["966542980511"],
            }
        )
        assert should_enforce_persona_compose(ctx, surface="social_checkin")

    def test_should_not_enforce_outside_test_mode(self) -> None:
        ctx = _ctx(
            ai_settings={
                "persona_composer_enabled": True,
                "store_ai_mode": "on",
                "ai_test_allowed_numbers": ["966542980511"],
            }
        )
        assert not should_enforce_persona_compose(ctx, surface="social_checkin")


class TestSurfaceResolver:
    def test_checkin_maps_to_social_checkin(self) -> None:
        ctx = _ctx(message="كيف الحال")
        assert resolve_greet_surface(ctx) == "social_checkin"

    def test_thanks_maps_to_thanks_surface(self) -> None:
        assert resolve_social_surface("thanks", inbound_text="شكراً") == "thanks"

    def test_commerce_checkout_skips_greet_compose(self) -> None:
        from modules.ai.brain.state.stages import STAGE_CHECKOUT  # noqa: PLC0415

        ctx = _ctx(message="السلام عليكم")
        ctx.state.stage = STAGE_CHECKOUT
        assert resolve_greet_surface(ctx) is None


class TestPersonaComposeGuards:
    def test_rejects_checkout_pressure_on_social(self) -> None:
        bundle = build_social_facts_bundle(
            surface="social_checkin",
            inbound_text="كيف الحال",
        )
        guard = apply_persona_compose_guards(
            "بخير، وش طريقة الدفع المناسبة لك؟",
            bundle,
        )
        assert not guard.passed or not rejects_checkout_pressure_after_social(
            guard.text,
            "كيف الحال",
        )

    def test_rejects_banned_support_bot_opener(self) -> None:
        bundle = build_social_facts_bundle(
            surface="social_greeting",
            inbound_text="كيف الحال",
        )
        guard = apply_persona_compose_guards(
            "كيف أقدر أساعدك اليوم؟",
            bundle,
        )
        assert not guard.passed
        assert guard.failed_reason == "banned_support_bot_opener"

    def test_rejects_payment_credential(self) -> None:
        bundle = build_social_facts_bundle(
            surface="social_greeting",
            inbound_text="السلام عليكم",
        )
        guard = apply_persona_compose_guards(
            "تفضل IBAN: SA0380000000608010167519",
            bundle,
        )
        assert not guard.passed
        assert guard.failed_reason == "payment_credential"


class TestFactBoundPersonaComposer:
    def test_compose_returns_guarded_text(self) -> None:
        import asyncio  # noqa: PLC0415

        async def _run() -> None:
            composer = FactBoundPersonaComposer(enforce_gate=False)
            bundle = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
            )

            async def _good_llm(_bundle):
                return "بخير الله يسعدك، وش تحتاج؟"

            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.text.strip()
            assert result.guard_passed
            assert result.source == "persona_llm"

        asyncio.run(_run())

    def test_compose_falls_back_on_timeout(self) -> None:
        import asyncio  # noqa: PLC0415

        async def _run() -> None:
            composer = FactBoundPersonaComposer(enforce_gate=False, timeout_seconds=0.01)

            async def _slow(_bundle):
                await asyncio.sleep(1.0)
                return "late"

            composer._llm_callable = _slow  # noqa: SLF001
            bundle = build_social_facts_bundle(
                surface="thanks",
                inbound_text="شكراً",
            )
            result = await composer.compose(bundle)
            assert result.text.strip()
            assert result.source == "fallback_deterministic"
            assert result.fallback_reason == "timeout"

        asyncio.run(_run())


class TestConstitutionProbeSamples:
    def test_samples_vary_and_pass_saudi_policy(self) -> None:
        replies = try_compose_persona_samples("social_checkin", "كيف الحال")
        assert social_replies_are_non_deterministic(replies)
        for text in replies:
            assert_no_non_saudi_arabic(text)
            assert not rejects_social_support_bot_phrase(text)
            assert not rejects_checkout_pressure_after_social(text, "كيف الحال")

    def test_thanks_and_dua_samples_vary(self) -> None:
        thanks = try_compose_persona_samples("thanks", "شكراً")
        dua = try_compose_persona_samples("dua", "الله يعطيك العافية")
        assert social_replies_are_non_deterministic(thanks)
        assert social_replies_are_non_deterministic(dua)
