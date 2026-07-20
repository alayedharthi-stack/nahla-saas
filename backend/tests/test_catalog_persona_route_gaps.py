"""Regression: catalog search-miss prompt contract and grounded persona route provenance."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.ai.brain.catalog.navigation import PATH_TOP_FALLBACK
from modules.ai.brain.persona.catalog_product_answer import (
    build_catalog_navigation_emergency_outcome,
    build_catalog_search_miss_facts_bundle,
    build_catalog_search_miss_emergency_outcome,
    try_compose_catalog_navigation_browse_answer,
    try_compose_catalog_search_miss_answer,
)
from modules.ai.brain.persona.fact_bound_composer import (
    COMPOSE_ATTEMPT_PROVIDER_CALL,
    FactBoundPersonaComposer,
)
from modules.ai.brain.persona.facts_bundle import PERSONA_COMPOSER_SURFACES, PersonaComposeResult
from modules.ai.brain.persona.prompts import build_user_prompt
from modules.ai.compose.reply_metadata_export import extract_persona_route_provenance
from services.merchant_brain_turn import (
    PersonaRouteProvenance,
    _build_persona_compose_event,
    _build_provenance,
)


def _enabled_ai_settings(*, tenant_id: int = 77) -> dict:
    return {
        "persona_composer_enabled": True,
        "store_ai_mode": "test",
        "ai_test_allowed_numbers": ["966500000001"],
        "persona_composer_allowlist_tenants": [tenant_id],
        "persona_composer_surfaces": list(PERSONA_COMPOSER_SURFACES),
    }


class TestSearchMissPromptContract:
    def test_search_miss_facts_project_zero_results_and_no_availability(self) -> None:
        bundle = build_catalog_search_miss_facts_bundle(
            inbound_text="كم سعر قميص قطني أزرق؟",
            resolved_subject="قميص قطني أزرق",
            catalog_search_query="قميص قطني أزرق",
            tenant_id=77,
            customer_phone="966500000001",
        )
        facts = bundle.verified_facts
        assert facts["search_result_count"] == 0
        assert facts["allow_availability_mention"] is False
        assert facts["has_positive_availability"] is False
        assert facts["has_catalog_products"] is False

    def test_search_miss_prompt_includes_availability_guard_instruction(self) -> None:
        bundle = build_catalog_search_miss_facts_bundle(
            inbound_text="كم سعر قميص قطني أزرق؟",
            resolved_subject="قميص قطني أزرق",
            catalog_search_query="قميص قطني أزرق",
        )
        prompt = build_user_prompt(bundle)
        assert "search_result_count: 0" in prompt
        assert "allow_availability_mention: false" in prompt
        assert "zero search matches is not proof of stock status" in prompt

    def test_compliant_search_miss_candidate_passes_persona_llm(self) -> None:
        async def _run() -> None:
            bundle = build_catalog_search_miss_facts_bundle(
                inbound_text="كم سعر حذاء رياضي أبيض؟",
                resolved_subject="حذاء رياضي أبيض",
                catalog_search_query="حذاء رياضي أبيض",
            )

            async def _compliant_llm(_bundle):
                return "ما لقيت تطابقاً واضحاً لحذاء رياضي أبيض في الكتالوج حالياً."

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _compliant_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True

        asyncio.run(_run())

    def test_availability_candidate_still_rejected(self) -> None:
        async def _run() -> None:
            bundle = build_catalog_search_miss_facts_bundle(
                inbound_text="هل يوجد قميص قطني أزرق؟",
                resolved_subject="قميص قطني أزرق",
                catalog_search_query="قميص قطني أزرق",
            )

            async def _availability_llm(_bundle):
                return "المنتج غير متوفر حالياً في الكتالوج"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _availability_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "fallback_deterministic"
            assert result.guard_failed_reason == "invented_availability"

        asyncio.run(_run())


class TestSearchMissFallbackRouteRetention:
    def test_guard_rejected_search_miss_fallback_retains_route_metadata(self) -> None:
        async def _run() -> None:
            attempted = PersonaComposeResult(
                text="غير متوفر",
                source="fallback_deterministic",
                surface="catalog_product_answer",
                facts_hash="abc",
                guard_passed=False,
                guard_failed_reason="invented_availability",
                fallback_reason="invented_availability",
                metadata={
                    "route_provider": "anthropic",
                    "route_model": "claude-test",
                    "route_tier": "tiny",
                    "route_source": "platform_default",
                    "route_provider_configured": True,
                    "compose_attempt": COMPOSE_ATTEMPT_PROVIDER_CALL,
                    "llm_candidate_present": True,
                },
            )

            async def _bad_llm(_bundle):
                return "غير متوفر حالياً"

            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                new=AsyncMock(return_value=attempted),
            ) as compose_mock:
                text, result, event = await try_compose_catalog_search_miss_answer(
                    tenant_id=77,
                    customer_phone="966500000001",
                    inbound_text="هل يوجد قميص قطني أزرق؟",
                    resolved_subject="قميص قطني أزرق",
                    catalog_search_query="قميص قطني أزرق",
                    ai_settings=_enabled_ai_settings(),
                )
            compose_mock.assert_awaited_once()
            assert result.source == "fallback_deterministic"
            assert result.fallback_reason == "invented_availability"
            assert result.metadata.get("route_provider") == "anthropic"
            assert result.metadata.get("compose_attempt") == COMPOSE_ATTEMPT_PROVIDER_CALL
            assert result.metadata.get("llm_candidate_present") is False
            assert event["compose_source"] == "fallback_deterministic"
            assert event["llm_candidate_present"] is False
            assert event["fallback_reason"] == "invented_availability"
            pc = event["persona_compose"]
            assert pc["route_provider"] == "anthropic"
            assert pc["compose_attempt"] == COMPOSE_ATTEMPT_PROVIDER_CALL
            assert pc["llm_candidate_present"] is False
            assert text

        asyncio.run(_run())

    def test_no_route_fallback_stays_null(self) -> None:
        text, result, event = build_catalog_search_miss_emergency_outcome(
            tenant_id=77,
            customer_phone="966500000001",
            inbound_text="كم سعر قميص؟",
            resolved_subject="قميص",
            reason="compose_unavailable",
        )
        assert text
        assert result.source == "fallback_deterministic"
        assert not result.metadata.get("route_provider")
        route = extract_persona_route_provenance(event)
        assert route is None


class TestNavigationPersonaRouteProvenance:
    def test_build_persona_compose_event_exports_catalog_navigation_route(self) -> None:
        brain_result = {
            "chosen_path": PATH_TOP_FALLBACK,
            "compose_source": "persona_llm",
            "response_mode": "grounded_persona_compose",
            "llm_candidate_present": True,
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
                "route_provider": "openai_compatible",
                "route_model": "gpt-4o-mini",
                "route_tier": "tiny",
                "route_source": "platform_default",
                "route_provider_configured": True,
                "compose_attempt": COMPOSE_ATTEMPT_PROVIDER_CALL,
                "llm_candidate_present": True,
            },
        }
        event = _build_persona_compose_event(brain_result)
        assert event is not None
        assert event["chosen_path"] == PATH_TOP_FALLBACK
        route = extract_persona_route_provenance(event)
        assert route is not None
        assert route["route_provider"] == "openai_compatible"

    def test_navigation_fallback_retains_attempted_route(self) -> None:
        async def _run() -> None:
            attempted = PersonaComposeResult(
                text="",
                source="fallback_deterministic",
                surface="catalog_product_answer",
                facts_hash="abc",
                guard_passed=False,
                fallback_reason="timeout",
                metadata={
                    "route_provider": "openai_compatible",
                    "route_model": "gpt-4o-mini",
                    "route_tier": "tiny",
                    "route_source": "platform_default",
                    "route_provider_configured": True,
                    "compose_attempt": COMPOSE_ATTEMPT_PROVIDER_CALL,
                    "llm_candidate_present": False,
                },
            )

            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                new=AsyncMock(return_value=attempted),
            ):
                text, result, event = await try_compose_catalog_navigation_browse_answer(
                    tenant_id=77,
                    customer_phone="966500000001",
                    inbound_text="وش عندكم؟",
                    products=[],
                    chosen_path=PATH_TOP_FALLBACK,
                    navigator_no_groups_fallback=True,
                    ai_settings=_enabled_ai_settings(),
                )
            assert result.source == "fallback_deterministic"
            assert result.fallback_reason == "timeout"
            assert result.metadata.get("route_provider") == "openai_compatible"
            assert event["compose_source"] == "fallback_deterministic"
            assert event["llm_candidate_present"] is False
            route = extract_persona_route_provenance(event)
            assert route is not None
            assert route["route_provider"] == "openai_compatible"
            assert text

        asyncio.run(_run())

    def test_navigation_no_route_fallback_stays_null(self) -> None:
        text, result, event = build_catalog_navigation_emergency_outcome(
            tenant_id=77,
            customer_phone="966500000001",
            inbound_text="وش عندكم؟",
            products=[],
            reason="compose_unavailable",
        )
        assert text
        assert not result.metadata.get("route_provider")
        assert extract_persona_route_provenance(event) is None

    def test_search_miss_build_provenance_exports_route_on_success(self) -> None:
        event = {
            "chosen_path": "catalog_miss_resolved_subject",
            "compose_source": "persona_llm",
            "llm_candidate_present": True,
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
                "route_provider": "anthropic",
                "route_model": "claude-test",
                "route_tier": "tiny",
                "route_source": "platform_default",
                "route_provider_configured": True,
                "compose_attempt": COMPOSE_ATTEMPT_PROVIDER_CALL,
                "llm_candidate_present": True,
            },
        }
        provenance = _build_provenance(
            reply_text="reply",
            brain_reply_candidate="reply",
            brain_result={
                "chosen_path": "catalog_miss_resolved_subject",
                "compose_source": "persona_llm",
            },
            brain_persona_compose_event=_build_persona_compose_event(
                {
                    "chosen_path": "catalog_miss_resolved_subject",
                    "compose_source": "persona_llm",
                    "llm_candidate_present": True,
                    "persona_compose": event["persona_compose"],
                }
            ),
            trace=SimpleNamespace(
                response_mode="",
                reply_source="",
                fallback_source="",
                chosen_path="",
            ),
        )
        assert provenance.persona_route == PersonaRouteProvenance(
            route_provider="anthropic",
            route_model="claude-test",
            route_tier="tiny",
            route_source="platform_default",
            route_provider_configured=True,
            compose_attempt=COMPOSE_ATTEMPT_PROVIDER_CALL,
        )
