"""Tests for FactBoundPersonaComposer (Phase 2 social test mode)."""
from __future__ import annotations

import pytest

from core.tenant import STORE_AI_MODE_TEST, merge_ai_defaults
from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards
from modules.ai.brain.persona.fact_bound_composer import (
    FactBoundPersonaComposer,
    build_social_facts_bundle,
)
from modules.ai.brain.persona.facts_bundle import PersonaComposeResult
from modules.ai.brain.persona.flags import is_persona_composer_enforce_enabled
from modules.ai.brain.persona.integration import (
    build_persona_compose_event_metadata,
    merge_persona_compose_into_extra_metadata,
    should_enforce_persona_compose,
    try_enforce_persona_compose,
)
from modules.ai.brain.persona.policy_terms import (
    find_malformed_saudi_ka_suffix_tokens,
    repair_malformed_saudi_ka_suffix,
)
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


def _compose_result(**overrides) -> PersonaComposeResult:
    base = dict(
        text="بخير الله يسعدك",
        source="persona_llm",
        surface="social_checkin",
        facts_hash="abc123",
        guard_passed=True,
        language="ar",
        dialect="saudi",
        emoji_count=1,
        latency_ms=42,
        model="test-model",
    )
    base.update(overrides)
    return PersonaComposeResult(**base)


class TestPersonaComposeEventMetadata:
    def test_build_event_metadata_includes_required_fields(self) -> None:
        result = _compose_result()
        meta = build_persona_compose_event_metadata(
            result,
            tenant_id=33,
            allowlist_result="allowed",
        )
        assert meta["chosen_path"] == "fact_bound_persona_compose"
        pc = meta["persona_compose"]
        assert pc["surface"] == "social_checkin"
        assert pc["source"] == "persona_llm"
        assert pc["guard_passed"] is True
        assert pc["tenant_id"] == 33
        assert pc["allowlist_result"] == "allowed"
        assert pc["facts_hash"] == "abc123"

    def test_merge_into_extra_metadata_for_persist(self) -> None:
        event = build_persona_compose_event_metadata(
            _compose_result(fallback_reason="timeout", guard_passed=False),
            tenant_id=33,
            allowlist_result="allowed",
        )
        merged = merge_persona_compose_into_extra_metadata(
            {"persona_ownership": {"persona_stamped": True}, "is_ai": True},
            event,
        )
        assert merged["chosen_path"] == "fact_bound_persona_compose"
        assert merged["persona_compose"]["fallback_reason"] == "timeout"
        assert merged["persona_ownership"]["persona_stamped"] is True

    def test_merge_skips_when_gate_not_used(self) -> None:
        merged = merge_persona_compose_into_extra_metadata(
            {"is_ai": True},
            None,
        )
        assert "chosen_path" not in merged
        assert "persona_compose" not in merged

    def test_brain_result_hydration_shape(self) -> None:
        brain = {
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": build_persona_compose_event_metadata(
                _compose_result(),
                tenant_id=33,
                allowlist_result="allowed",
            )["persona_compose"],
        }
        merged = merge_persona_compose_into_extra_metadata({}, brain)
        assert merged["chosen_path"] == "fact_bound_persona_compose"
        assert merged["persona_compose"]["surface"] == "social_checkin"

    def test_compose_disabled_does_not_persist_chosen_path(self) -> None:
        import asyncio  # noqa: PLC0415

        from modules.ai.brain.types import ActionResult  # noqa: PLC0415

        async def _run() -> None:
            ctx = _ctx(
                ai_settings={
                    "persona_composer_enabled": False,
                    "store_ai_mode": STORE_AI_MODE_TEST,
                    "ai_test_allowed_numbers": ["966542980511"],
                }
            )
            action = ActionResult(success=True, data={})
            out = await try_enforce_persona_compose(
                ctx,
                surface="social_checkin",
                action_result=action,
            )
            assert out is None
            assert "chosen_path" not in action.data
            assert "persona_compose" not in action.data

        asyncio.run(_run())

    def test_compose_enabled_persists_metadata_on_action_result(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from modules.ai.brain.types import ActionResult  # noqa: PLC0415

        stub_result = PersonaComposeResult(
            text="بخير الله يسعدك",
            source="persona_llm",
            surface="social_checkin",
            facts_hash="stub-hash",
            guard_passed=True,
            language="ar",
            dialect="saudi",
        )

        async def _fake_compose(_self, _bundle, ctx=None, db=None):
            return stub_result

        async def _run() -> None:
            ctx = _ctx(
                ai_settings={
                    "persona_composer_enabled": True,
                    "store_ai_mode": STORE_AI_MODE_TEST,
                    "ai_test_allowed_numbers": ["966542980511"],
                }
            )
            action = ActionResult(success=True, data={})
            with patch.object(FactBoundPersonaComposer, "compose", _fake_compose):
                out = await try_enforce_persona_compose(
                    ctx,
                    surface="social_checkin",
                    action_result=action,
                )
            assert out is not None
            assert action.data.get("chosen_path") == "fact_bound_persona_compose"
            pc = action.data.get("persona_compose") or {}
            assert pc.get("surface") == "social_checkin"
            assert pc.get("guard_passed") is True
            assert pc.get("source") == "persona_llm"
            assert pc.get("tenant_id") == 33
            assert pc.get("allowlist_result") == "allowed"

        asyncio.run(_run())


class TestMalformedSaudiKaSuffix:
    @pytest.mark.parametrize(
        "broken,fixed",
        [
            ("كيفكا وش أخباركا", "كيفك وش أخبارك"),
            ("حالكا", "حالك"),
            ("حالكـا", "حالك"),
            ("طلبكا", "طلبك"),
            ("عنوانكا", "عنوانك"),
            ("اسمكا", "اسمك"),
        ],
    )
    def test_repair_malformed_ka_suffix(self, broken: str, fixed: str) -> None:
        repaired, changed = repair_malformed_saudi_ka_suffix(broken)
        assert changed
        assert repaired == fixed
        assert not find_malformed_saudi_ka_suffix_tokens(repaired)

    @pytest.mark.parametrize(
        "valid",
        [
            "كيفك",
            "وش أخبارك",
            "حياك الله",
            "الله يعافيك",
            "أبشرك بخير",
        ],
    )
    def test_valid_saudi_wording_unchanged(self, valid: str) -> None:
        repaired, changed = repair_malformed_saudi_ka_suffix(valid)
        assert not changed
        assert repaired == valid
        assert not find_malformed_saudi_ka_suffix_tokens(valid)

    def test_guard_repairs_malformed_compose_output(self) -> None:
        bundle = build_social_facts_bundle(
            surface="social_checkin",
            inbound_text="كيف الحال",
        )
        guard = apply_persona_compose_guards(
            "حالكا وش أخباركا 🌷",
            bundle,
        )
        assert guard.passed
        assert guard.repaired
        assert "حالكا" not in guard.text
        assert "أخباركا" not in guard.text
        assert "حالك" in guard.text
        assert "أخبارك" in guard.text
