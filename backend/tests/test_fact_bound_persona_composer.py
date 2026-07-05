"""Tests for FactBoundPersonaComposer (Phase 2 social test mode)."""
from __future__ import annotations

import pytest

from core.tenant import STORE_AI_MODE_TEST, merge_ai_defaults
from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards
from modules.ai.brain.persona.fact_bound_composer import (
    FactBoundPersonaComposer,
    build_social_facts_bundle,
    resolve_persona_compose_model_route,
    resolve_persona_compose_timeout_seconds,
)
from modules.ai.brain.persona.facts_bundle import PersonaComposeResult
from modules.ai.brain.persona.flags import is_persona_composer_enforce_enabled
from modules.ai.brain.persona.integration import (
    build_persona_compose_event_metadata,
    merge_persona_compose_into_extra_metadata,
    should_enforce_persona_compose,
    try_enforce_persona_compose,
    try_enforce_phatic_llm_persona_compose,
)
from modules.ai.brain.persona.policy_terms import (
    find_malformed_saudi_ka_suffix_tokens,
    repair_malformed_saudi_ka_suffix,
)
from modules.ai.brain.persona.surface_resolver import (
    resolve_greet_surface,
    resolve_phatic_llm_surface,
    resolve_social_surface,
)
from modules.ai.brain.decision.actions import ACTION_GREET, ACTION_LLM_REPLY, ACTION_SOCIAL_REPLY
from modules.ai.brain.types import (
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    INTENT_GENERAL,
    ActionResult,
    Decision,
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


class TestPersonaComposeTimeoutConfig:
    def test_default_timeout_is_three_seconds(self, monkeypatch) -> None:
        monkeypatch.delenv("NAHLA_PERSONA_COMPOSE_TIMEOUT_SECONDS", raising=False)
        assert resolve_persona_compose_timeout_seconds() == 3.0
        composer = FactBoundPersonaComposer(enforce_gate=False)
        assert composer._timeout_seconds == 3.0  # noqa: SLF001

    def test_env_override_respected(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_TIMEOUT_SECONDS", "5")
        assert resolve_persona_compose_timeout_seconds() == 5.0
        composer = FactBoundPersonaComposer(enforce_gate=False)
        assert composer._timeout_seconds == 5.0  # noqa: SLF001

    def test_invalid_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_TIMEOUT_SECONDS", "not-a-number")
        assert resolve_persona_compose_timeout_seconds() == 3.0

    def test_timeout_clamped_to_bounds(self, monkeypatch) -> None:
        monkeypatch.delenv("NAHLA_PERSONA_COMPOSE_TIMEOUT_SECONDS", raising=False)
        assert resolve_persona_compose_timeout_seconds(override=0.1) == 0.5
        assert resolve_persona_compose_timeout_seconds(override=99.0) == 10.0

    def test_explicit_constructor_override_wins_over_env(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_TIMEOUT_SECONDS", "8")
        composer = FactBoundPersonaComposer(enforce_gate=False, timeout_seconds=2.0)
        assert composer._timeout_seconds == 2.0  # noqa: SLF001


class TestPersonaComposeModelRouting:
    def test_platform_default_uses_tiny_tier_openai_model(self, monkeypatch) -> None:
        monkeypatch.delenv("NAHLA_PERSONA_COMPOSE_MODEL", raising=False)
        monkeypatch.delenv("NAHLA_PERSONA_COMPOSE_PROVIDER", raising=False)
        bundle = build_social_facts_bundle(
            surface="social_checkin",
            inbound_text="كيف الحال",
        )
        route = resolve_persona_compose_model_route(bundle)
        assert route.source == "platform_default"
        assert route.provider == "openai_compatible"
        assert route.model == "gpt-4o-mini"

    def test_env_override_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_PROVIDER", "openai_compatible")
        bundle = build_social_facts_bundle(
            surface="thanks",
            inbound_text="شكراً",
        )
        route = resolve_persona_compose_model_route(bundle)
        assert route.source == "env"
        assert route.model == "gpt-4o-mini"
        assert route.provider == "openai_compatible"

    def test_tenant_override_respected(self) -> None:
        bundle = build_social_facts_bundle(
            surface="dua",
            inbound_text="الله يعطيك العافية",
            merchant_persona={"persona_composer_model": "gpt-4o-mini"},
        )
        route = resolve_persona_compose_model_route(bundle)
        assert route.source == "tenant_override"
        assert route.model == "gpt-4o-mini"
        assert route.provider == "openai_compatible"

    def test_stale_model_provider_failure_falls_back_with_reason(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        async def _run() -> None:
            bundle = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
                merchant_persona={"persona_composer_model": "claude-3-5-haiku-20241022"},
            )

            def _empty_provider_call(*_args, **_kwargs):
                return {
                    "provider": "anthropic",
                    "model": "claude-3-5-haiku-20241022",
                    "reply_text": "",
                    "status": "call_error",
                }

            composer = FactBoundPersonaComposer(enforce_gate=False)
            with patch(
                "modules.ai.orchestrator.providers.anthropic_provider.AnthropicProvider.is_configured",
                return_value=True,
            ):
                with patch(
                    "modules.ai.orchestrator.providers.anthropic_provider.AnthropicProvider.call",
                    side_effect=_empty_provider_call,
                ):
                    result = await composer.compose(bundle)
            assert result.source == "fallback_deterministic"
            assert result.guard_passed is False
            assert result.fallback_reason == "empty_llm"
            assert result.facts_hash
            assert result.surface == "social_checkin"

        asyncio.run(_run())

    def test_configured_model_path_sets_persona_llm_metadata(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        async def _run() -> None:
            bundle = build_social_facts_bundle(
                surface="social_greeting",
                inbound_text="السلام عليكم",
            )

            def _good_provider_call(*_args, **_kwargs):
                return {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "reply_text": "وعليكم السلام ورحمة الله 😊",
                    "status": "ok",
                }

            composer = FactBoundPersonaComposer(enforce_gate=False)
            with patch(
                "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.is_configured",
                return_value=True,
            ):
                with patch(
                    "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.call",
                    side_effect=_good_provider_call,
                ):
                    result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True
            assert result.model == "gpt-4o-mini"
            assert result.facts_hash

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


def _llm_social_decision(
    *,
    message: str,
    social_category: str = "wellbeing_check",
) -> Decision:
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "persona_social",
            "social_category": social_category,
            "block_commerce_escalation": True,
        },
        reason="test phatic llm",
        confidence=0.9,
    )


def _enabled_ai_settings() -> dict:
    return {
        "persona_composer_enabled": True,
        "store_ai_mode": STORE_AI_MODE_TEST,
        "ai_test_allowed_numbers": ["966542980511"],
    }


class TestPhaticLlmSurfaceResolver:
    @pytest.mark.parametrize(
        "message,category,expected",
        [
            ("السلام عليكم", "greeting", "social_greeting"),
            ("كيف الحال", "wellbeing_check", "social_checkin"),
            ("شكراً", "thanks", "thanks"),
            ("الله يعطيك العافية", "blessing", "thanks"),
            ("انت وش أخبارك؟", "wellbeing_check", "social_checkin"),
        ],
    )
    def test_resolves_phase2_surfaces_for_phatic_llm(
        self,
        message: str,
        category: str,
        expected: str,
    ) -> None:
        ctx = _ctx(message=message, ai_settings=_enabled_ai_settings())
        surface = resolve_phatic_llm_surface(
            ctx,
            decision=_llm_social_decision(message=message, social_category=category),
        )
        assert surface == expected

    @pytest.mark.parametrize(
        "message",
        ["نعم", "اعتمد", "تحويل بنكي", "اسمي هشام"],
    )
    def test_checkout_continuation_skips_phatic_surface(self, message: str) -> None:
        ctx = _ctx(message=message, ai_settings=_enabled_ai_settings())
        surface = resolve_phatic_llm_surface(ctx, decision=_llm_social_decision(message=message))
        assert surface is None

    def test_non_test_mode_gate_blocks_enforce(self) -> None:
        ctx = _ctx(
            message="كيف الحال",
            ai_settings={
                "persona_composer_enabled": True,
                "store_ai_mode": "on",
                "ai_test_allowed_numbers": ["966542980511"],
            },
        )
        assert resolve_phatic_llm_surface(ctx, decision=_llm_social_decision(message="كيف الحال"))
        assert not should_enforce_persona_compose(ctx, surface="social_checkin")

    def test_composer_disabled_skips_enforce(self) -> None:
        ctx = _ctx(
            message="كيف الحال",
            ai_settings={
                "persona_composer_enabled": False,
                "store_ai_mode": STORE_AI_MODE_TEST,
                "ai_test_allowed_numbers": ["966542980511"],
            },
        )
        assert resolve_phatic_llm_surface(ctx, decision=_llm_social_decision(message="كيف الحال"))
        assert not should_enforce_persona_compose(ctx, surface="social_checkin")


class TestPhaticLlmPersonaComposeRouting:
    def test_phatic_llm_path_persists_metadata(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

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
            ctx = _ctx(message="كيف الحال", ai_settings=_enabled_ai_settings())
            action = ActionResult(success=True, data={})
            decision = _llm_social_decision(message="كيف الحال")
            with patch.object(FactBoundPersonaComposer, "compose", _fake_compose):
                out = await try_enforce_phatic_llm_persona_compose(
                    ctx,
                    decision=decision,
                    action_result=action,
                )
            assert out is not None
            assert action.data.get("chosen_path") == "fact_bound_persona_compose"
            pc = action.data.get("persona_compose") or {}
            assert pc.get("surface") == "social_checkin"
            assert pc.get("source") == "persona_llm"
            assert pc.get("guard_passed") is True
            assert pc.get("tenant_id") == 33
            assert pc.get("allowlist_result") == "allowed"

        asyncio.run(_run())

    def test_non_allowlisted_phone_skips_enforce(self) -> None:
        import asyncio  # noqa: PLC0415

        async def _run() -> None:
            ctx = _ctx(
                phone="966500000099",
                message="كيف الحال",
                ai_settings=_enabled_ai_settings(),
            )
            action = ActionResult(success=True, data={})
            out = await try_enforce_phatic_llm_persona_compose(
                ctx,
                decision=_llm_social_decision(message="كيف الحال"),
                action_result=action,
            )
            assert out is None
            assert "chosen_path" not in action.data

        asyncio.run(_run())

    def test_responder_llm_reply_uses_fact_bound_before_llm(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415

        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        stub_result = PersonaComposeResult(
            text="بخير الله يسعدك",
            source="persona_llm",
            surface="social_checkin",
            facts_hash="stub-hash",
            guard_passed=True,
        )

        async def _run() -> None:
            ctx = _ctx(message="كيف الحال", ai_settings=_enabled_ai_settings())
            result = ActionResult(success=True, data={})
            decision = _llm_social_decision(message="كيف الحال")
            composer = DefaultComposer()
            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                AsyncMock(return_value=stub_result),
            ):
                with patch.object(
                    DefaultComposer,
                    "_llm_compose",
                    AsyncMock(return_value="should-not-run"),
                ) as llm_mock:
                    text = await composer.compose(decision, result, ctx)
            assert text == "بخير الله يسعدك"
            assert result.data.get("chosen_path") == "fact_bound_persona_compose"
            llm_mock.assert_not_called()

        asyncio.run(_run())

    def test_responder_llm_reply_falls_back_to_llm_when_gate_off(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415

        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        async def _run() -> None:
            ctx = _ctx(
                message="كيف الحال",
                ai_settings={
                    "persona_composer_enabled": False,
                    "store_ai_mode": STORE_AI_MODE_TEST,
                    "ai_test_allowed_numbers": ["966542980511"],
                },
            )
            result = ActionResult(success=True, data={})
            decision = _llm_social_decision(message="كيف الحال")
            composer = DefaultComposer()
            with patch.object(
                DefaultComposer,
                "_llm_compose",
                AsyncMock(return_value="legacy llm reply"),
            ) as llm_mock:
                text = await composer.compose(decision, result, ctx)
            assert text == "legacy llm reply"
            assert "fact_bound_persona_compose" not in str(result.data.get("chosen_path") or "")
            llm_mock.assert_called_once()

        asyncio.run(_run())


def _ctx_reply_state_only(
    *,
    tenant_id: int = 33,
    phone: str = "966542980511",
    message: str = "كيف الحال",
    ai_settings: dict | None = None,
) -> BrainContext:
    """Production-shaped ctx: ai_settings only on reply_state.merchant_context."""
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.9, raw_message=message),
        state=MerchantConversationState(),
        facts=CommerceFacts(),
        merchant_context={},
    )
    ctx.reply_state = BrainReplyState(
        merchant_context={
            "ai_settings": merge_ai_defaults(ai_settings or _enabled_ai_settings()),
        },
    )
    return ctx


def _assert_fact_bound_metadata(action: ActionResult) -> None:
    assert action.data.get("chosen_path") == "fact_bound_persona_compose"
    pc = action.data.get("persona_compose") or {}
    assert pc
    assert pc.get("surface")
    assert pc.get("source")
    assert pc.get("guard_passed") is True
    assert pc.get("facts_hash")


class TestProdShapedAiSettingsLookup:
    def test_gate_opens_from_reply_state_merchant_context(self) -> None:
        ctx = _ctx_reply_state_only(message="كيف الحال")
        assert should_enforce_persona_compose(ctx, surface="social_checkin")

    def test_gate_closed_when_reply_state_missing_settings(self) -> None:
        ctx = _ctx_reply_state_only(
            ai_settings={
                "persona_composer_enabled": False,
                "store_ai_mode": STORE_AI_MODE_TEST,
                "ai_test_allowed_numbers": ["966542980511"],
            },
        )
        assert not should_enforce_persona_compose(ctx, surface="social_checkin")

    def test_phatic_llm_persists_metadata_reply_state_only(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

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
            ctx = _ctx_reply_state_only(message="كيف الحال")
            action = ActionResult(success=True, data={})
            out = await try_enforce_phatic_llm_persona_compose(
                ctx,
                decision=_llm_social_decision(message="كيف الحال"),
                action_result=action,
            )
            assert out is not None
            _assert_fact_bound_metadata(action)

        with patch.object(FactBoundPersonaComposer, "compose", _fake_compose):
            asyncio.run(_run())

    @pytest.mark.parametrize(
        "message,category,surface",
        [
            ("كيف الحال", "wellbeing_check", "social_checkin"),
            ("شكراً", "thanks", "thanks"),
            ("انت وش أخبارك؟", "wellbeing_check", "social_checkin"),
        ],
    )
    def test_phatic_llm_turns_persist_metadata_prod_shaped(
        self, message: str, category: str, surface: str,
    ) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        stub_result = PersonaComposeResult(
            text="رد تجريبي",
            source="persona_llm",
            surface=surface,
            facts_hash="hash",
            guard_passed=True,
        )

        async def _fake_compose(_self, _bundle, ctx=None, db=None):
            return stub_result

        async def _run() -> None:
            ctx = _ctx_reply_state_only(message=message)
            action = ActionResult(success=True, data={})
            out = await try_enforce_phatic_llm_persona_compose(
                ctx,
                decision=_llm_social_decision(message=message, social_category=category),
                action_result=action,
            )
            assert out is not None
            _assert_fact_bound_metadata(action)
            assert (action.data.get("persona_compose") or {}).get("surface") == surface

        with patch.object(FactBoundPersonaComposer, "compose", _fake_compose):
            asyncio.run(_run())

    def test_greet_path_persists_metadata_prod_shaped(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415

        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        stub_result = PersonaComposeResult(
            text="وعليكم السلام",
            source="persona_llm",
            surface="social_greeting",
            facts_hash="greet-hash",
            guard_passed=True,
        )

        async def _run() -> None:
            ctx = _ctx_reply_state_only(message="السلام عليكم")
            result = ActionResult(success=True, data={})
            decision = Decision(
                action=ACTION_GREET,
                args={},
                reason="test greet",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                AsyncMock(return_value=stub_result),
            ):
                with patch.object(
                    DefaultComposer,
                    "_llm_compose",
                    AsyncMock(return_value="should-not-run"),
                ) as llm_mock:
                    text = await composer.compose(decision, result, ctx)
            assert text == "وعليكم السلام"
            _assert_fact_bound_metadata(result)
            llm_mock.assert_not_called()

        asyncio.run(_run())

    def test_dua_social_reply_persists_metadata_prod_shaped(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415

        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

        stub_result = PersonaComposeResult(
            text="الله يعافيك يا الغالي",
            source="persona_llm",
            surface="dua",
            facts_hash="dua-hash",
            guard_passed=True,
        )

        async def _run() -> None:
            ctx = _ctx_reply_state_only(message="الله يعطيك العافية")
            result = ActionResult(success=True, data={})
            decision = Decision(
                action=ACTION_SOCIAL_REPLY,
                args={"social_category": "dua"},
                reason="test dua",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                AsyncMock(return_value=stub_result),
            ):
                with patch.object(
                    DefaultComposer,
                    "_compose_social_persona_ack",
                    AsyncMock(return_value="should-not-run"),
                ) as ack_mock:
                    text = await composer.compose(decision, result, ctx)
            assert text == "الله يعافيك يا الغالي"
            _assert_fact_bound_metadata(result)
            ack_mock.assert_not_called()

        asyncio.run(_run())

    @pytest.mark.parametrize("message", ["نعم", "اعتمد", "تحويل بنكي"])
    def test_checkout_continuation_not_forced_into_composer(self, message: str) -> None:
        ctx = _ctx_reply_state_only(message=message)
        ctx.state.stage = "checkout"
        assert resolve_phatic_llm_surface(
            ctx,
            decision=_llm_social_decision(message=message),
        ) is None

    def test_blocked_phone_skips_enforce_reply_state_only(self) -> None:
        import asyncio  # noqa: PLC0415

        async def _run() -> None:
            ctx = _ctx_reply_state_only(
                phone="966500000099",
                message="كيف الحال",
            )
            action = ActionResult(success=True, data={})
            out = await try_enforce_phatic_llm_persona_compose(
                ctx,
                decision=_llm_social_decision(message="كيف الحال"),
                action_result=action,
            )
            assert out is None
            assert "chosen_path" not in action.data

        asyncio.run(_run())


def _enabled_payment_ai_settings() -> dict:
    return {
        "persona_composer_enabled": True,
        "store_ai_mode": STORE_AI_MODE_TEST,
        "ai_test_allowed_numbers": ["966542980511"],
        "persona_composer_surfaces": [
            "social_greeting",
            "social_checkin",
            "thanks",
            "dua",
            "payment_media_intro",
        ],
    }


class TestPaymentMediaIntroPersonaCompose:
    def test_compose_success_metadata(self) -> None:
        import asyncio  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from modules.ai.brain.persona.payment_media_intro import (
            build_payment_media_intro_facts_bundle,
            try_compose_payment_media_intro,
        )

        async def _run() -> None:
            bundle = build_payment_media_intro_facts_bundle(
                inbound_text="أرسل باركود الراجحي",
                tenant_id=33,
                customer_phone="966542980511",
                media_key="payment_rajhi_barcode",
                media_url_present=True,
            )

            async def _good_llm(_bundle):
                return "تفضل باركود التحويل، وبعد التحويل أرسل الإيصال 🧾"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True
            assert result.surface == "payment_media_intro"
            assert result.facts_hash

            with patch.object(FactBoundPersonaComposer, "compose", return_value=result):
                text, compose_result, event = await try_compose_payment_media_intro(
                    tenant_id=33,
                    customer_phone="966542980511",
                    inbound_text="أرسل باركود الراجحي",
                    media_key="payment_rajhi_barcode",
                    media_url_present=True,
                    ai_settings=_enabled_payment_ai_settings(),
                )
            assert text
            assert compose_result is not None
            assert event is not None
            assert event["chosen_path"] == "fact_bound_persona_compose"
            assert event["persona_compose"]["source"] == "persona_llm"
            assert event["persona_compose"]["surface"] == "payment_media_intro"

        asyncio.run(_run())

    def test_timeout_fallback_stays_safe(self) -> None:
        import asyncio  # noqa: PLC0415

        from modules.ai.brain.persona.payment_media_intro import (
            build_payment_media_intro_facts_bundle,
        )

        async def _run() -> None:
            composer = FactBoundPersonaComposer(enforce_gate=False, timeout_seconds=0.01)

            async def _slow(_bundle):
                await asyncio.sleep(1.0)
                return "late"

            composer._llm_callable = _slow  # noqa: SLF001
            bundle = build_payment_media_intro_facts_bundle(
                inbound_text="أرسل باركود الراجحي",
                media_key="payment_rajhi_barcode",
                media_url_present=True,
            )
            result = await composer.compose(bundle)
            assert result.source == "fallback_deterministic"
            assert result.fallback_reason == "timeout"
            assert result.guard_passed is False
            assert "باركود" in result.text or "الإيصال" in result.text

        asyncio.run(_run())

    def test_missing_media_url_fallback_does_not_claim_sent(self) -> None:
        import asyncio  # noqa: PLC0415

        from modules.ai.brain.persona.payment_media_intro import (
            build_payment_media_intro_facts_bundle,
        )

        async def _run() -> None:
            bundle = build_payment_media_intro_facts_bundle(
                inbound_text="أرسل باركود الراجحي",
                media_key="payment_rajhi_barcode",
                media_url_present=False,
            )

            async def _bad_llm(_bundle):
                return "تفضل الباركود هذا للتحويل"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _bad_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "fallback_deterministic"
            assert "غير متوفرة" in result.text or result.guard_passed is False

        asyncio.run(_run())

    def test_pending_transfer_allows_receipt_language_in_fallback(self) -> None:
        from modules.ai.brain.persona.fallback_catalog import deterministic_fallback
        from modules.ai.brain.persona.payment_media_intro import (
            build_payment_media_intro_facts_bundle,
        )

        bundle = build_payment_media_intro_facts_bundle(
            inbound_text="أرسل باركود الراجحي",
            media_key="payment_rajhi_barcode",
            media_url_present=True,
            payment_status="pending",
        )
        text = deterministic_fallback(bundle, reason="test")
        assert "تم الدفع" not in text
        assert "الإيصال" in text

    def test_confirmed_payment_fallback_skips_receipt_ask(self) -> None:
        from modules.ai.brain.persona.fallback_catalog import deterministic_fallback
        from modules.ai.brain.persona.payment_media_intro import (
            build_payment_media_intro_facts_bundle,
        )

        bundle = build_payment_media_intro_facts_bundle(
            inbound_text="أرسل باركود الراجحي",
            media_key="payment_rajhi_barcode",
            media_url_present=True,
            payment_status="confirmed",
        )
        text = deterministic_fallback(bundle, reason="test")
        assert "أرسل الإيصال" not in text
        assert "تم الدفع" not in text

    def test_non_allowlisted_phone_uses_legacy_intro(self) -> None:
        import asyncio  # noqa: PLC0415

        from modules.ai.brain.persona.payment_media_intro import (
            try_compose_payment_media_intro,
        )

        async def _run() -> None:
            text, result, event = await try_compose_payment_media_intro(
                tenant_id=33,
                customer_phone="966500000099",
                inbound_text="أرسل باركود الراجحي",
                media_key="payment_rajhi_barcode",
                media_url_present=True,
                ai_settings=_enabled_payment_ai_settings(),
            )
            assert text
            assert result is None
            assert event is None

        asyncio.run(_run())
