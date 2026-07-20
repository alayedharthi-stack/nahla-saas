"""Regression: normal-runtime customer prose must be LLM-owned on three audited paths."""
from __future__ import annotations

import asyncio
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
from modules.ai.brain.persona.facts_bundle import PERSONA_COMPOSER_SURFACES
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


class TestCatalogSearchMissPersonaOwnership:
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


class TestKbMissingPersonaOwnership:
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
                        },
                    ),
                ),
            ):
                return await composer.compose(decision, result, ctx)

        text = asyncio.run(_run())
        assert text != deterministic
        assert result.data.get("chosen_path") == PATH_TOP_FALLBACK
        assert result.data.get("persona_compose", {}).get("source") == "persona_llm"
