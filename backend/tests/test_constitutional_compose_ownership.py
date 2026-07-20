"""Regression: normal-runtime customer prose must be LLM-owned on three audited paths."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from modules.ai.brain.catalog.navigation import PATH_TOP_FALLBACK
from modules.ai.brain.compose.responder import DefaultComposer
from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE, ACTION_SEARCH_PRODUCTS
from modules.ai.brain.persona.catalog_product_answer import (
    build_catalog_search_miss_facts_bundle,
    try_compose_catalog_navigation_browse_answer,
    try_compose_catalog_search_miss_answer,
)
from modules.ai.brain.persona.fact_bound_composer import FactBoundPersonaComposer
from modules.ai.brain.persona.facts_bundle import (
    PERSONA_COMPOSER_SURFACES,
    PersonaComposeResult,
)
from modules.ai.brain.persona.kb_product_answer import (
    build_kb_product_answer_facts_bundle,
    try_compose_kb_product_answer,
)
from modules.ai.brain.types import (
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _enabled_ai_settings() -> dict:
    return {
        "persona_composer_enabled": True,
        "store_ai_mode": "test",
        "ai_test_allowed_numbers": ["966500000001"],
        "persona_composer_allowlist_tenants": [11],
        "persona_composer_surfaces": list(PERSONA_COMPOSER_SURFACES),
    }


def _ctx(message: str, *, intent_name: str = "ask_product") -> BrainContext:
    return BrainContext(
        tenant_id=11,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.9,
            raw_message=message,
            extraction_method="rules",
        ),
        state=MerchantConversationState(greeted=True, stage="exploring"),
        history=[],
        facts=CommerceFacts(has_products=True, orderable=True, product_count=5),
        merchant_context={"ai_settings": _enabled_ai_settings()},
    )


_GENERIC_SHOES = [
    {
        "id": 301,
        "title": "حذاء رياضي أبيض",
        "category": "أحذية",
        "price": 220,
        "can_checkout": True,
    },
]


def _disabled_rollout_settings() -> dict:
    return {
        "persona_composer_enabled": False,
        "store_ai_mode": "live",
        "ai_test_allowed_numbers": [],
        "persona_composer_allowlist_tenants": [],
        "persona_composer_surfaces": [],
    }


def _persona_success(*, surface: str, text: str) -> PersonaComposeResult:
    return PersonaComposeResult(
        text=text,
        source="persona_llm",
        surface=surface,
        facts_hash="facts-hash",
        guard_passed=True,
        fallback_reason="",
        language="ar",
        dialect="saudi_arabic",
        emoji_count=0,
        latency_ms=1,
        model="test-model",
    )


def _assert_complete_provenance(
    event: dict,
    *,
    source: str,
    chosen_path: str,
    fallback_action_type: str = "",
) -> None:
    assert event["compose_source"] == source
    assert event["response_mode"] == "grounded_persona_compose"
    assert event["chosen_path"] == chosen_path
    assert event["llm_candidate_present"] is (source == "persona_llm")
    assert event["final_text_transformed"] is False
    assert event["final_transform_reasons"] == []
    assert event["final_customer_text_source"] == source
    if source == "fallback_deterministic":
        assert event["fallback_reason"]
        assert event["fallback_action_type"] == fallback_action_type


class TestCatalogSearchMissPersonaOwnership:
    def test_rollout_disabled_still_calls_one_grounded_compose(self) -> None:
        async def _run() -> None:
            compose = AsyncMock(return_value=_persona_success(
                surface="catalog_product_answer",
                text="لم يظهر تطابق مؤكد للمنتج المطلوب.",
            ))
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_catalog_search_miss_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="هل يوجد قميص قطني أزرق؟",
                    resolved_subject="قميص قطني أزرق",
                    catalog_search_query="قميص قطني أزرق",
                    ai_settings=_disabled_rollout_settings(),
                )
            compose.assert_awaited_once()
            assert text
            assert result.source == "persona_llm"
            assert event["persona_compose"]["allowlist_result"] != "allowed"
            _assert_complete_provenance(
                event,
                source="persona_llm",
                chosen_path="catalog_miss_resolved_subject",
            )

        asyncio.run(_run())

    def test_provider_failure_is_audited_fallback(self) -> None:
        async def _run() -> None:
            compose = AsyncMock(side_effect=RuntimeError("provider unavailable"))
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_catalog_search_miss_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="هل يوجد قميص قطني أزرق؟",
                    resolved_subject="قميص قطني أزرق",
                    catalog_search_query="قميص قطني أزرق",
                    ai_settings=_disabled_rollout_settings(),
                )
            compose.assert_awaited_once()
            assert text
            assert result.source == "fallback_deterministic"
            _assert_complete_provenance(
                event,
                source="fallback_deterministic",
                chosen_path="catalog_miss_resolved_subject",
                fallback_action_type="catalog_search_miss",
            )

        asyncio.run(_run())

    def test_resolved_subject_miss_uses_one_persona_compose_call(self) -> None:
        bundle = build_catalog_search_miss_facts_bundle(
            inbound_text="كم سعر حذاء رياضي أبيض؟",
            resolved_subject="حذاء رياضي أبيض",
            catalog_search_query="حذاء رياضي أبيض",
            tenant_id=11,
            customer_phone="966500000001",
        )
        assert bundle.verified_facts["question_kind"] == "search_miss"
        assert bundle.verified_facts["has_catalog_products"] is False
        assert bundle.verified_facts["allow_price_mention"] is False

        async def _run() -> None:
            async def _stub_llm(_bundle):
                return "ما لقيت تطابقاً واضحاً لحذاء رياضي أبيض في الكتالوج حالياً."

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _stub_llm  # noqa: SLF001
            composed = await composer.compose(bundle)
            assert composed.source == "persona_llm"

            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                new=AsyncMock(return_value=composed),
            ):
                text, result, event = await try_compose_catalog_search_miss_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="كم سعر حذاء رياضي أبيض؟",
                    resolved_subject="حذاء رياضي أبيض",
                    catalog_search_query="حذاء رياضي أبيض",
                    ai_settings=_enabled_ai_settings(),
                )
            assert text
            assert result is not None
            assert result.source == "persona_llm"
            assert event is not None
            assert event["chosen_path"] == "catalog_miss_resolved_subject"
            assert event["persona_compose"]["source"] == "persona_llm"
            assert "ريال" not in text

        asyncio.run(_run())

    def test_responder_search_miss_routes_persona_not_template(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("كم سعر قميص قطني أزرق؟", intent_name="ask_price")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "قميص قطني أزرق"},
            reason="test",
        )
        result = ActionResult(
            success=False,
            error="no_search_hits",
            data={"message": "no_search_hits_no_top_fallback"},
        )

        async def _run() -> tuple[str, dict]:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_search_miss_answer",
                new=AsyncMock(
                    return_value=(
                        "ما ظهر عندي تطابق واضح لقميص قطني أزرق في الكتالوج.",
                        FactBoundPersonaComposer(enforce_gate=False),
                        {
                            "chosen_path": "catalog_miss_resolved_subject",
                            "persona_compose": {"source": "persona_llm"},
                            "compose_source": "persona_llm",
                            "response_mode": "grounded_persona_compose",
                            "llm_candidate_present": True,
                            "final_text_transformed": False,
                            "final_transform_reasons": [],
                            "final_customer_text_source": "persona_llm",
                        },
                    ),
                ),
            ):
                text = await composer.compose(decision, result, ctx)
                return text, dict(result.data or {})

        text, data = asyncio.run(_run())
        assert data.get("chosen_path") == "catalog_miss_resolved_subject"
        assert data.get("persona_compose", {}).get("source") == "persona_llm"
        assert "ريال" not in text

    def test_responder_rejects_legacy_search_miss_without_metadata(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("هل يوجد قميص قطني أزرق؟", intent_name="ask_product")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "قميص قطني أزرق"},
            reason="test",
        )
        result = ActionResult(
            success=False,
            error="no_search_hits",
            data={"message": "no_search_hits_no_top_fallback"},
        )
        legacy = "ما لقيت قميص قطني أزرق في الكتالوج حالياً."

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer."
                "try_compose_catalog_search_miss_answer",
                new=AsyncMock(return_value=(legacy, None, None)),
            ):
                return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert text != legacy
        _assert_complete_provenance(
            result.data,
            source="fallback_deterministic",
            chosen_path="catalog_miss_resolved_subject",
            fallback_action_type="catalog_search_miss",
        )


class TestKbMissingPersonaOwnership:
    def test_rollout_disabled_missing_kb_still_calls_compose_once(self) -> None:
        async def _run() -> None:
            compose = AsyncMock(return_value=_persona_success(
                surface="kb_product_answer",
                text="لا تتوفر تفاصيل موثقة لهذا العطر في قاعدة المعرفة.",
            ))
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_kb_product_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="ما مميزات عطر ورد 100ml؟",
                    decision_args={
                        "topic": "product_knowledge_facts",
                        "question_kind": "features",
                        "allowed_facts": {
                            "product_title": "عطر ورد 100ml",
                            "kb_sections": [],
                        },
                        "missing_facts": ["kb_product_facts"],
                    },
                    ai_settings=_disabled_rollout_settings(),
                )
            compose.assert_awaited_once()
            assert text
            assert result is not None and result.source == "persona_llm"
            assert event is not None
            assert event["knowledge_source"] == "missing_kb"
            _assert_complete_provenance(
                event,
                source="persona_llm",
                chosen_path="fact_bound_persona_compose",
            )

        asyncio.run(_run())

    def test_missing_kb_provider_failure_has_full_fallback_provenance(self) -> None:
        async def _run() -> None:
            compose = AsyncMock(side_effect=RuntimeError("provider unavailable"))
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_kb_product_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="ما مميزات عطر ورد 100ml؟",
                    decision_args={
                        "topic": "product_knowledge_facts",
                        "question_kind": "features",
                        "allowed_facts": {
                            "product_title": "عطر ورد 100ml",
                            "kb_sections": [],
                        },
                        "missing_facts": ["kb_product_facts"],
                    },
                    ai_settings=_disabled_rollout_settings(),
                )
            compose.assert_awaited_once()
            assert text
            assert result is not None and result.source == "fallback_deterministic"
            assert event is not None
            _assert_complete_provenance(
                event,
                source="fallback_deterministic",
                chosen_path="fact_bound_persona_compose",
                fallback_action_type="kb_product_answer",
            )

        asyncio.run(_run())

    def test_missing_kb_attempts_compose_before_fallback(self) -> None:
        async def _run() -> None:
            bundle = build_kb_product_answer_facts_bundle(
                inbound_text="ما مميزات عطر ورد 100ml؟",
                question_kind="features",
                allowed_facts={
                    "product_title": "عطر ورد 100ml",
                    "kb_sections": [],
                },
                missing_facts=["kb_product_facts"],
            )

            async def _stub_llm(_bundle):
                return "ما عندي تفاصيل مؤكدة عن عطر ورد 100ml في قاعدة المعرفة حالياً."

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _stub_llm  # noqa: SLF001
            composed = await composer.compose(bundle)
            assert composed.source == "persona_llm"

            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                new=AsyncMock(return_value=composed),
            ):
                text, result, event = await try_compose_kb_product_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="ما مميزات عطر ورد 100ml؟",
                    decision_args={
                        "topic": "product_knowledge_facts",
                        "question_kind": "features",
                        "allowed_facts": {
                            "product_title": "عطر ورد 100ml",
                            "kb_sections": [],
                        },
                        "missing_facts": ["kb_product_facts"],
                    },
                    ai_settings=_enabled_ai_settings(),
                )
            assert text
            assert result is not None
            assert result.source == "persona_llm"
            assert event is not None
            assert event["knowledge_source"] == "missing_kb"
            assert event["persona_compose"]["source"] == "persona_llm"
            assert "ريال" not in text

        asyncio.run(_run())


class TestCatalogNavigationTopFallbackOwnership:
    def test_rollout_disabled_still_calls_one_grounded_compose(self) -> None:
        async def _run() -> None:
            compose = AsyncMock(return_value=_persona_success(
                surface="catalog_product_answer",
                text="هذه أبرز خيارات الأحذية في الكتالوج.",
            ))
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_catalog_navigation_browse_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="وش عندكم؟",
                    products=_GENERIC_SHOES,
                    navigator_no_groups_fallback=True,
                    ai_settings=_disabled_rollout_settings(),
                )
            compose.assert_awaited_once()
            assert text
            assert result.source == "persona_llm"
            assert event["catalog_product_ids"] == [301]
            _assert_complete_provenance(
                event,
                source="persona_llm",
                chosen_path=PATH_TOP_FALLBACK,
            )

        asyncio.run(_run())

    def test_provider_and_fact_projection_failures_are_audited(self) -> None:
        async def _run() -> None:
            compose = AsyncMock(side_effect=RuntimeError("provider unavailable"))
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_catalog_navigation_browse_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="وش عندكم؟",
                    products=_GENERIC_SHOES,
                    navigator_no_groups_fallback=True,
                    ai_settings=_disabled_rollout_settings(),
                )
            compose.assert_awaited_once()
            assert text
            assert result.source == "fallback_deterministic"
            _assert_complete_provenance(
                event,
                source="fallback_deterministic",
                chosen_path=PATH_TOP_FALLBACK,
                fallback_action_type="catalog_navigation_browse",
            )

            no_rows_compose = AsyncMock()
            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                new=no_rows_compose,
            ):
                _, no_rows_result, no_rows_event = (
                    await try_compose_catalog_navigation_browse_answer(
                        tenant_id=11,
                        customer_phone="966500000001",
                        inbound_text="وش عندكم؟",
                        products=[],
                        navigator_no_groups_fallback=True,
                        ai_settings=_disabled_rollout_settings(),
                    )
                )
            no_rows_compose.assert_not_awaited()
            assert no_rows_result.source == "fallback_deterministic"
            assert no_rows_event["fallback_reason"] == "missing_catalog_fact_rows"
            _assert_complete_provenance(
                no_rows_event,
                source="fallback_deterministic",
                chosen_path=PATH_TOP_FALLBACK,
                fallback_action_type="catalog_navigation_browse",
            )

        asyncio.run(_run())

    def test_top_fallback_browse_uses_persona_compose(self) -> None:
        async def _run() -> None:
            from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
                build_catalog_product_answer_facts_bundle,
            )

            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text="وش عندكم؟",
                tenant_id=11,
                customer_phone="966500000001",
                products=_GENERIC_SHOES,
                question_kind="browse",
            )
            verified = dict(bundle.verified_facts)
            verified["navigation_browse"] = True
            verified["navigator_no_groups_fallback"] = True
            bundle = bundle.__class__(
                surface=bundle.surface,
                inbound_text=bundle.inbound_text,
                language=bundle.language,
                dialect=bundle.dialect,
                verified_facts=verified,
                customer_context=bundle.customer_context,
                merchant_persona=bundle.merchant_persona,
                constraints=bundle.constraints,
                tenant_id=bundle.tenant_id,
                customer_phone=bundle.customer_phone,
            )

            async def _stub_llm(_bundle):
                return "هذي أبرز المنتجات عندنا حالياً."

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _stub_llm  # noqa: SLF001
            composed = await composer.compose(bundle)
            assert composed.source == "persona_llm"

            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                new=AsyncMock(return_value=composed),
            ):
                text, result, event = await try_compose_catalog_navigation_browse_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="وش عندكم؟",
                    products=_GENERIC_SHOES,
                    navigator_no_groups_fallback=True,
                    ai_settings=_enabled_ai_settings(),
                )
            assert text
            assert result is not None
            assert result.source == "persona_llm"
            assert event is not None
            assert event["chosen_path"] == PATH_TOP_FALLBACK
            assert event["persona_compose"]["source"] == "persona_llm"

        asyncio.run(_run())

    def test_responder_top_fallback_prefers_persona_over_presenter(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("وش المتوفر؟")
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={"chosen_path": PATH_TOP_FALLBACK, "navigator_step": "top_products_fallback"},
            reason="navigation",
        )
        deterministic = "1. حذاء رياضي أبيض — 220 ريال"
        result = ActionResult(
            success=True,
            data={
                "discovery_presentation_text": deterministic,
                "product_lines": deterministic,
                "discovery_output_kind": "products",
                "chosen_path": PATH_TOP_FALLBACK,
                "products": _GENERIC_SHOES,
                "navigator_no_groups_fallback": True,
                "turn_owner": "catalog_navigation",
                "owner_locked": True,
            },
        )

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_navigation_browse_answer",
                new=AsyncMock(
                    return_value=(
                        "عندنا حذاء رياضي أبيض ضمن أبرز المنتجات.",
                        None,
                        {
                            "chosen_path": PATH_TOP_FALLBACK,
                            "persona_compose": {"source": "persona_llm"},
                            "compose_source": "persona_llm",
                            "response_mode": "grounded_persona_compose",
                            "llm_candidate_present": True,
                            "final_text_transformed": False,
                            "final_transform_reasons": [],
                            "final_customer_text_source": "persona_llm",
                        },
                    ),
                ),
            ):
                return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert text != deterministic
        assert result.data.get("chosen_path") == PATH_TOP_FALLBACK
        assert result.data.get("persona_compose", {}).get("source") == "persona_llm"

    def test_responder_rejects_legacy_navigation_text_without_metadata(self) -> None:
        composer = DefaultComposer()
        ctx = _ctx("وش المتوفر؟")
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={"chosen_path": PATH_TOP_FALLBACK},
            reason="navigation",
        )
        legacy = "1. حذاء رياضي أبيض — 220 ريال"
        result = ActionResult(
            success=True,
            data={
                "discovery_presentation_text": legacy,
                "discovery_output_kind": "products",
                "chosen_path": PATH_TOP_FALLBACK,
                "products": _GENERIC_SHOES,
                "turn_owner": "catalog_navigation",
                "owner_locked": True,
            },
        )

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer."
                "try_compose_catalog_navigation_browse_answer",
                new=AsyncMock(return_value=(None, None, None)),
            ):
                return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert text != legacy
        _assert_complete_provenance(
            result.data,
            source="fallback_deterministic",
            chosen_path=PATH_TOP_FALLBACK,
            fallback_action_type="catalog_navigation_browse",
        )
        assert result.data["pending_candidates"] == _GENERIC_SHOES


def test_strict_tenant_48_llm_call_caps_remain_present() -> None:
    manifest = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "engineering"
        / "real-channel-acceptance-scenario-manifest.json"
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    tenant_48 = [
        scenario
        for scenario in payload.get("scenarios", [])
        if scenario.get("tenant_id") == 48
    ]
    assert len(tenant_48) >= 4
    assert all(scenario.get("max_llm_calls") == 4 for scenario in tenant_48[:4])
