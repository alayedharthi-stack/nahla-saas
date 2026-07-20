"""Availability-aware persona compose routing — provider boundary matrix."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

import pytest

from modules.ai.brain.persona.catalog_product_answer import (
    try_compose_catalog_product_answer,
)
from modules.ai.brain.persona.fact_bound_composer import (
    COMPOSE_ATTEMPT_PROVIDER_CALL,
    COMPOSE_ATTEMPT_SKIPPED_NO_ROUTE,
    COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED,
    FactBoundPersonaComposer,
    build_social_facts_bundle,
    resolve_persona_compose_model_route,
    resolve_persona_compose_route_resolution,
)
from modules.ai.brain.persona.integration import (
    merge_persona_compose_into_extra_metadata,
    persona_compose_metadata,
)
from modules.ai.orchestrator.llm_cost_audit import resolve_anthropic_model

_OPENAI_CONFIGURED = (
    "modules.ai.orchestrator.providers.openai_compatible_provider."
    "OpenAICompatibleProvider.is_configured"
)
_ANTHROPIC_CONFIGURED = (
    "modules.ai.orchestrator.providers.anthropic_provider."
    "AnthropicProvider.is_configured"
)

_GENERIC_SHOE = [
    {
        "id": 201,
        "title": "حذاء رياضي أبيض",
        "category": "أحذية",
        "price": 220,
        "can_checkout": True,
    },
]


@contextmanager
def _provider_availability(*, openai: bool, anthropic: bool) -> Iterator[None]:
    with patch(_OPENAI_CONFIGURED, return_value=openai):
        with patch(_ANTHROPIC_CONFIGURED, return_value=anthropic):
            yield


def _clear_persona_overrides(monkeypatch) -> None:
    monkeypatch.delenv("NAHLA_PERSONA_COMPOSE_MODEL", raising=False)
    monkeypatch.delenv("NAHLA_PERSONA_COMPOSE_PROVIDER", raising=False)


class TestPlatformDefaultRouteAvailability:
    def test_platform_default_anthropic_only_uses_canonical_model(
        self, monkeypatch
    ) -> None:
        _clear_persona_overrides(monkeypatch)
        with _provider_availability(openai=False, anthropic=True):
            bundle = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
            )
            resolution = resolve_persona_compose_route_resolution(bundle)
        assert resolution.route.source == "platform_default"
        assert resolution.route.provider == "anthropic"
        assert resolution.route.model == resolve_anthropic_model()
        assert resolution.compose_attempt == COMPOSE_ATTEMPT_PROVIDER_CALL
        assert resolution.provider_configured is True

    def test_platform_default_openai_only(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)
        with _provider_availability(openai=True, anthropic=False):
            bundle = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
            )
            resolution = resolve_persona_compose_route_resolution(bundle)
        assert resolution.route.provider == "openai_compatible"
        assert resolution.route.model == "gpt-4o-mini"
        assert resolution.compose_attempt == COMPOSE_ATTEMPT_PROVIDER_CALL

    def test_platform_default_both_prefers_openai_cost_first(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)
        with _provider_availability(openai=True, anthropic=True):
            route = resolve_persona_compose_model_route(
                build_social_facts_bundle(surface="thanks", inbound_text="شكراً")
            )
        assert route.provider == "openai_compatible"
        assert route.model == "gpt-4o-mini"

    def test_platform_default_neither_skips_no_route(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)
        with _provider_availability(openai=False, anthropic=False):
            resolution = resolve_persona_compose_route_resolution(
                build_social_facts_bundle(surface="dua", inbound_text="الله يعافيك")
            )
        assert resolution.compose_attempt == COMPOSE_ATTEMPT_SKIPPED_NO_ROUTE
        assert resolution.provider_configured is False


class TestExplicitOverrideFailClosed:
    def test_env_override_unavailable_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_PROVIDER", "openai_compatible")
        with _provider_availability(openai=False, anthropic=False):
            resolution = resolve_persona_compose_route_resolution(
                build_social_facts_bundle(surface="social_greeting", inbound_text="مرحبا")
            )
        assert resolution.route.source == "env"
        assert resolution.compose_attempt == COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED

    def test_tenant_override_unavailable_tenant_isolation(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)
        with _provider_availability(openai=False, anthropic=True):
            bundle_a = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
                tenant_id=101,
                merchant_persona={
                    "persona_composer_model": "gpt-4o-mini",
                    "persona_composer_provider": "openai_compatible",
                },
            )
            bundle_b = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
                tenant_id=202,
                merchant_persona={},
            )
            res_a = resolve_persona_compose_route_resolution(bundle_a)
            res_b = resolve_persona_compose_route_resolution(bundle_b)
        assert res_a.route.source == "tenant_override"
        assert res_a.compose_attempt == COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED
        assert res_b.route.source == "platform_default"
        assert res_b.route.provider == "anthropic"
        assert res_b.compose_attempt == COMPOSE_ATTEMPT_PROVIDER_CALL

    def test_explicit_configured_tenant_override_honored(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)
        with _provider_availability(openai=True, anthropic=True):
            bundle = build_social_facts_bundle(
                surface="thanks",
                inbound_text="شكراً",
                merchant_persona={
                    "persona_composer_model": resolve_anthropic_model(),
                    "persona_composer_provider": "anthropic",
                },
            )
            resolution = resolve_persona_compose_route_resolution(bundle)
        assert resolution.route.source == "tenant_override"
        assert resolution.route.provider == "anthropic"
        assert resolution.compose_attempt == COMPOSE_ATTEMPT_PROVIDER_CALL


class TestComposeProviderBoundary:
    def test_anthropic_only_one_provider_call(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)

        async def _run() -> None:
            bundle = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
            )
            composer = FactBoundPersonaComposer(enforce_gate=False)

            def _good_call(*_args, **_kwargs):
                return {
                    "provider": "anthropic",
                    "model": resolve_anthropic_model(),
                    "reply_text": "بخير الله يسعدك",
                    "status": "ok",
                }

            with _provider_availability(openai=False, anthropic=True):
                with patch(
                    "modules.ai.orchestrator.providers.anthropic_provider.AnthropicProvider.call",
                    side_effect=_good_call,
                ) as anthropic_call:
                    with patch(
                        "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.call",
                    ) as openai_call:
                        result = await composer.compose(bundle)

            anthropic_call.assert_called_once()
            openai_call.assert_not_called()
            assert result.source == "persona_llm"
            assert result.metadata["route_provider"] == "anthropic"
            assert result.metadata["compose_attempt"] == COMPOSE_ATTEMPT_PROVIDER_CALL
            assert result.metadata["llm_candidate_present"] is True

        asyncio.run(_run())

    def test_neither_configured_zero_calls_route_unconfigured(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)

        async def _run() -> None:
            composer = FactBoundPersonaComposer(enforce_gate=False)
            bundle = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
            )
            with _provider_availability(openai=False, anthropic=False):
                with patch(
                    "modules.ai.orchestrator.providers.anthropic_provider.AnthropicProvider.call",
                ) as anthropic_call:
                    with patch(
                        "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.call",
                    ) as openai_call:
                        result = await composer.compose(bundle)

            anthropic_call.assert_not_called()
            openai_call.assert_not_called()
            assert result.fallback_reason == "route_unconfigured"
            assert result.metadata["compose_attempt"] == COMPOSE_ATTEMPT_SKIPPED_NO_ROUTE
            assert result.metadata["llm_candidate_present"] is False

        asyncio.run(_run())

    def test_provider_empty_response_no_alternate_retry(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("NAHLA_PERSONA_COMPOSE_PROVIDER", "openai_compatible")

        async def _run() -> None:
            composer = FactBoundPersonaComposer(enforce_gate=False)
            bundle = build_social_facts_bundle(
                surface="social_checkin",
                inbound_text="كيف الحال",
            )

            def _empty_call(*_args, **_kwargs):
                return {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "reply_text": "",
                    "status": "call_error",
                }

            with _provider_availability(openai=True, anthropic=True):
                with patch(
                    "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.call",
                    side_effect=_empty_call,
                ) as openai_call:
                    with patch(
                        "modules.ai.orchestrator.providers.anthropic_provider.AnthropicProvider.call",
                    ) as anthropic_call:
                        result = await composer.compose(bundle)

            openai_call.assert_called_once()
            anthropic_call.assert_not_called()
            assert result.fallback_reason == "empty_llm"
            assert result.metadata["compose_attempt"] == COMPOSE_ATTEMPT_PROVIDER_CALL
            assert result.metadata["llm_candidate_present"] is False

        asyncio.run(_run())

    def test_provider_timeout_no_alternate_retry(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)

        async def _run() -> None:
            composer = FactBoundPersonaComposer(
                enforce_gate=False,
                timeout_seconds=0.01,
            )
            bundle = build_social_facts_bundle(
                surface="thanks",
                inbound_text="شكراً",
            )

            def _slow_call(*_args, **_kwargs):
                import time

                time.sleep(1.0)
                return {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "reply_text": "late",
                    "status": "ok",
                }

            with _provider_availability(openai=True, anthropic=False):
                with patch(
                    "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.call",
                    side_effect=_slow_call,
                ) as openai_call:
                    with patch(
                        "modules.ai.orchestrator.providers.anthropic_provider.AnthropicProvider.call",
                    ) as anthropic_call:
                        result = await composer.compose(bundle)

            openai_call.assert_called_once()
            anthropic_call.assert_not_called()
            assert result.fallback_reason == "timeout"

        asyncio.run(_run())

    def test_confined_style_anthropic_only_usage_intent(self, monkeypatch) -> None:
        """Anthropic-only confined runner: one provider call, usage intent."""
        _clear_persona_overrides(monkeypatch)

        async def _run() -> None:
            bundle = build_social_facts_bundle(
                surface="social_greeting",
                inbound_text="السلام عليكم",
            )
            composer = FactBoundPersonaComposer(enforce_gate=False)

            def _good_call(*_args, **_kwargs):
                return {
                    "provider": "anthropic",
                    "model": resolve_anthropic_model(),
                    "reply_text": "وعليكم السلام",
                    "status": "ok",
                }

            with _provider_availability(openai=False, anthropic=True):
                with patch(
                    "modules.ai.orchestrator.providers.anthropic_provider.AnthropicProvider.call",
                    side_effect=_good_call,
                ) as anthropic_call:
                    with patch(
                        "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.call",
                    ) as openai_call:
                        result = await composer.compose(bundle)

            anthropic_call.assert_called_once()
            openai_call.assert_not_called()
            assert result.metadata["route_provider"] == "anthropic"
            assert result.metadata["route_model"] == resolve_anthropic_model()
            assert result.metadata["compose_attempt"] == COMPOSE_ATTEMPT_PROVIDER_CALL

        asyncio.run(_run())


class TestPersonaRouteMetadataExport:
    def test_persona_compose_metadata_exports_route_fields(self) -> None:
        from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415

        result = PersonaComposeResult(
            text="بخير",
            source="fallback_deterministic",
            surface="social_checkin",
            facts_hash="abc",
            guard_passed=False,
            fallback_reason="route_unconfigured",
            metadata={
                "route_provider": "anthropic",
                "route_model": resolve_anthropic_model(),
                "route_tier": "tiny",
                "route_source": "platform_default",
                "route_provider_configured": True,
                "compose_attempt": COMPOSE_ATTEMPT_PROVIDER_CALL,
                "llm_candidate_present": False,
            },
        )
        meta = persona_compose_metadata(result)
        assert meta["route_provider"] == "anthropic"
        assert meta["compose_attempt"] == COMPOSE_ATTEMPT_PROVIDER_CALL
        assert meta["llm_candidate_present"] is False

    def test_catalog_path_pipeline_provenance_export(self, monkeypatch) -> None:
        _clear_persona_overrides(monkeypatch)

        async def _run() -> None:
            def _good_call(*_args, **_kwargs):
                return {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "reply_text": "حذاء رياضي أبيض سعره 220",
                    "status": "ok",
                }

            with _provider_availability(openai=True, anthropic=False):
                with patch(
                    "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider.call",
                    side_effect=_good_call,
                ):
                    text, result, event_meta = await try_compose_catalog_product_answer(
                        tenant_id=48,
                        customer_phone="966500000001",
                        inbound_text="كم سعر الحذاء الرياضي؟",
                        products=_GENERIC_SHOE,
                        catalog_search_query="حذاء",
                        search_result_count=1,
                        question_kind="price",
                        ai_settings={
                            "persona_composer_enabled": True,
                            "store_ai_mode": "test",
                            "ai_test_allowed_numbers": ["966500000001"],
                        },
                    )

            assert text.strip()
            pc = event_meta["persona_compose"]
            assert pc["route_provider"] == "openai_compatible"
            assert pc["compose_attempt"] == COMPOSE_ATTEMPT_PROVIDER_CALL
            assert pc["llm_candidate_present"] is True
            merged = merge_persona_compose_into_extra_metadata({}, event_meta)
            assert merged["persona_compose"]["route_source"] == "platform_default"

        asyncio.run(_run())
