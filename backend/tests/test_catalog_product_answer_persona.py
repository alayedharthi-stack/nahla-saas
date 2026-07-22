"""P0 tests for catalog_product_answer FactBoundPersonaComposer."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from modules.ai.brain.persona.catalog_product_answer import (
    build_catalog_product_answer_facts_bundle,
    build_catalog_product_answer_event_metadata,
    catalog_product_answer_deterministic_fallback,
    classify_catalog_question_kind,
    try_compose_catalog_product_answer,
)
from modules.ai.brain.persona.fact_bound_composer import FactBoundPersonaComposer
from modules.ai.brain.persona.facts_bundle import PERSONA_COMPOSER_SURFACES, PersonaComposeResult


def _enabled_catalog_ai_settings() -> dict:
    return {
        "persona_composer_enabled": True,
        "store_ai_mode": "test",
        "ai_test_allowed_numbers": ["966542980511"],
        "persona_composer_surfaces": list(PERSONA_COMPOSER_SURFACES),
    }


_HONEY_PRODUCTS = [
    {
        "id": 101,
        "title": "عسل السدر القيضي",
        "category": "عسل",
        "price": 400,
        "can_checkout": True,
    },
    {
        "id": 102,
        "title": "عسل الطلح",
        "category": "عسل",
        "price": 350,
        "variant_id": 9001,
        "can_checkout": True,
    },
]

_GENERIC_SHOE = [
    {
        "id": 201,
        "title": "حذاء رياضي أبيض",
        "category": "أحذية",
        "price": 220,
        "can_checkout": True,
    },
]


class TestCatalogQuestionKind:
    def test_soft_browse_kind(self) -> None:
        assert classify_catalog_question_kind("وش عندكم من عسل؟") == "browse"

    def test_availability_kind(self) -> None:
        assert classify_catalog_question_kind("عندكم سدر؟") == "availability"

    def test_price_kind(self) -> None:
        assert classify_catalog_question_kind("كم سعر الطلح؟") == "price"


class TestCatalogProductAnswerFactsBundle:
    def test_honey_browse_bundle_metadata(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="وش عندكم من عسل؟",
            tenant_id=33,
            customer_phone="966542980511",
            products=_HONEY_PRODUCTS,
            catalog_search_query="عسل",
            search_result_count=2,
            category_scope="عسل",
            allowed_category="عسل",
            question_kind="browse",
        )
        facts = bundle.verified_facts
        assert facts["question_kind"] == "browse"
        assert facts["category_scope"] == "عسل"
        assert facts["catalog_product_ids"] == [101, 102]
        assert facts["variant_ids"] == [9001]
        assert facts["allow_checkout_pressure"] is False
        assert facts["has_positive_availability"] is True
        assert "price_source" not in facts

    def test_category_boundary_excludes_non_honey_products(self) -> None:
        from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            filter_products_to_browse_category,
        )

        mixed = list(_HONEY_PRODUCTS) + [
            {
                "id": 999,
                "title": "كريم سم النحل",
                "category": "عناية",
                "price": 120,
                "can_checkout": True,
            },
            {
                "id": 998,
                "title": "زيت الأرغان",
                "category": "زيوت",
                "price": 90,
                "can_checkout": True,
            },
        ]
        filtered = filter_products_to_browse_category(
            mixed,
            message="وش عندكم من عسل؟",
            query="عسل",
            source="category_browse",
        )
        ids = [p["id"] for p in filtered]
        assert 101 in ids and 102 in ids
        assert 999 not in ids
        assert 998 not in ids


class TestCatalogProductAnswerPersonaCompose:
    def test_soft_browse_compose_success_metadata(self) -> None:
        message = "وش عندكم من عسل؟"

        async def _run() -> None:
            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text=message,
                tenant_id=33,
                customer_phone="966542980511",
                products=_HONEY_PRODUCTS,
                catalog_search_query="عسل",
                category_scope="عسل",
                question_kind="browse",
            )

            async def _good_llm(_bundle):
                return "عندنا عسل السدر القيضي وعسل الطلح من خياراتنا 🍯"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True
            assert result.surface == "catalog_product_answer"

            with patch.object(FactBoundPersonaComposer, "compose", return_value=result):
                text, compose_result, event = await try_compose_catalog_product_answer(
                    tenant_id=33,
                    customer_phone="966542980511",
                    inbound_text=message,
                    products=_HONEY_PRODUCTS,
                    catalog_search_query="عسل",
                    category_scope="عسل",
                    question_kind="browse",
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            assert text
            assert compose_result is not None
            assert event is not None
            assert event["chosen_path"] == "fact_bound_persona_compose"
            assert event["persona_compose"]["surface"] == "catalog_product_answer"
            assert event["persona_compose"]["source"] == "persona_llm"
            assert event["persona_compose"]["guard_passed"] is True
            assert event["question_kind"] == "browse"
            assert event["category_scope"] == "عسل"
            assert event["catalog_product_ids"] == [101, 102]
            assert event["checkout_pressure_allowed"] is False
            assert "اسمك" not in text
            assert "عنوانك" not in text

        asyncio.run(_run())

    def test_availability_no_invented_mتوفر_without_facts(self) -> None:
        products = [
            {
                "id": 301,
                "title": "عسل سدر صيفي",
                "category": "عسل",
                "price": 380,
                "can_checkout": False,
            }
        ]

        async def _run() -> None:
            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text="عندكم سدر؟",
                products=products,
                catalog_search_query="سدر",
                category_scope="عسل",
                question_kind="availability",
            )

            async def _bad_llm(_bundle):
                return "نعم السدر متوفر عندنا الآن"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _bad_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "fallback_deterministic"
            assert "متوفر" not in result.text or result.guard_passed is False

        asyncio.run(_run())

    def test_price_matches_catalog_row(self) -> None:
        products = [_HONEY_PRODUCTS[1]]

        async def _run() -> None:
            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text="كم سعر الطلح؟",
                products=products,
                catalog_search_query="طلح",
                question_kind="price",
            )

            async def _good_llm(_bundle):
                return "عسل الطلح سعره 350 ريال 🍯"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.guard_passed is True
            assert "350" in result.text

            async def _bad_llm(_bundle):
                return "عسل الطلح سعره 999 ريال"

            composer._llm_callable = _bad_llm  # noqa: SLF001
            bad = await composer.compose(bundle)
            assert bad.source == "fallback_deterministic"

        asyncio.run(_run())

    def test_compose_failure_returns_audited_emergency_fallback(self) -> None:
        async def _run() -> None:
            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                return_value=PersonaComposeResult(
                    text="",
                    source="fallback_deterministic",
                    surface="catalog_product_answer",
                    facts_hash="abc",
                    guard_passed=False,
                    guard_failed_reason="guard_failed",
                    fallback_reason="guard_failed",
                ),
            ):
                text, result, event = await try_compose_catalog_product_answer(
                    tenant_id=33,
                    customer_phone="966542980511",
                    inbound_text="وش عندكم من عسل؟",
                    products=_HONEY_PRODUCTS,
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            assert text
            assert result is not None
            assert result.source == "fallback_deterministic"
            assert event is not None
            assert event["compose_source"] == "fallback_deterministic"

        asyncio.run(_run())

    def test_rollout_disabled_still_compose_once(self) -> None:
        async def _run() -> None:
            compose = AsyncMock(
                return_value=PersonaComposeResult(
                    text="عندنا خيارات عسل في الكتالوج.",
                    source="persona_llm",
                    surface="catalog_product_answer",
                    facts_hash="facts",
                    guard_passed=True,
                )
            )
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_catalog_product_answer(
                    tenant_id=33,
                    customer_phone="966500000099",
                    inbound_text="وش عندكم من عسل؟",
                    products=_HONEY_PRODUCTS,
                    ai_settings={
                        "persona_composer_enabled": False,
                        "store_ai_mode": "live",
                    },
                )
            compose.assert_awaited_once()
            assert text
            assert result is not None
            assert event is not None

        asyncio.run(_run())

    def test_generic_merchant_browse(self) -> None:
        async def _run() -> None:
            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text="وش عندكم من أحذية؟",
                tenant_id=1,
                products=_GENERIC_SHOE,
                catalog_search_query="أحذية",
                category_scope="أحذية",
                question_kind="browse",
            )

            async def _good_llm(_bundle):
                return "عندنا حذاء رياضي أبيض ضمن تشكيلتنا 👟"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.guard_passed is True
            assert "حذاء" in result.text

        asyncio.run(_run())

    def test_event_metadata_merge_fields(self) -> None:
        from modules.ai.brain.persona.integration import (  # noqa: PLC0415
            merge_persona_compose_into_extra_metadata,
        )

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر الطلح؟",
            products=_HONEY_PRODUCTS,
            catalog_search_query="طلح",
            category_scope="عسل",
            question_kind="price",
        )
        result = FactBoundPersonaComposer(enforce_gate=False)
        compose_result = type(
            "CR",
            (),
            {
                "text": "عندنا عسل السدر وعسل الطلح",
                "source": "persona_llm",
                "surface": "catalog_product_answer",
                "facts_hash": "abc123",
                "guard_passed": True,
                "guard_failed_reason": "",
                "fallback_reason": "",
                "language": "ar",
                "dialect": "saudi_arabic",
                "emoji_count": 1,
                "latency_ms": 10,
                "model": "gpt-4o-mini",
                "metadata": {},
            },
        )()
        event = build_catalog_product_answer_event_metadata(
            compose_result,  # type: ignore[arg-type]
            tenant_id=33,
            allowlist_result="allowed",
            catalog_facts=bundle.verified_facts,
            catalog_fact_products=[
                {
                    "id": 101,
                    "title": "عسل سدر",
                    "price": "ر.س. ٣٨٧٫٠٠",
                    "can_checkout": False,
                },
            ],
        )
        merged = merge_persona_compose_into_extra_metadata({}, event)
        assert merged["catalog_product_ids"] == [101, 102]
        assert merged["category_scope"] == "عسل"
        assert merged["price_source"] == "catalog"
        assert merged["catalog_fact_products"][0]["id"] == 101
        assert merged["catalog_fact_products"][0]["price"] == "ر.س. ٣٨٧٫٠٠"


class TestCatalogSurfaceRegistration:
    def test_surface_in_persona_composer_surfaces(self) -> None:
        assert "catalog_product_answer" in PERSONA_COMPOSER_SURFACES


class TestCatalogComposeProductLists:
    def test_price_uses_category_filtered_facts_not_display_candidates(self) -> None:
        from modules.ai.brain.compose.responder import (  # noqa: PLC0415
            catalog_compose_products_for_search_turn,
        )

        non_orderable = {
            "id": 501,
            "title": "عسل الطلح",
            "price": 387,
            "can_checkout": False,
            "in_stock": False,
        }
        orderable_other = {
            "id": 502,
            "title": "عسل سدر",
            "price": 400,
            "can_checkout": True,
        }
        facts = [non_orderable]
        candidates = [orderable_other]
        selected = catalog_compose_products_for_search_turn(
            question_kind="price",
            category_filtered_facts=facts,
            display_candidates=candidates,
        )
        assert selected == facts
        assert selected[0]["id"] == 501

    def test_browse_uses_display_candidates_only(self) -> None:
        from modules.ai.brain.compose.responder import (  # noqa: PLC0415
            catalog_compose_products_for_search_turn,
        )

        candidates = list(_HONEY_PRODUCTS)
        selected = catalog_compose_products_for_search_turn(
            question_kind="browse",
            category_filtered_facts=candidates,
            display_candidates=candidates,
        )
        assert selected == candidates


class TestCatalogPriceNonOrderableFacts:
    def test_price_non_orderable_facts_bundle_and_compose(self) -> None:
        talh = {
            "id": 501,
            "title": "عسل الطلح",
            "category": "عسل",
            "price": 387,
            "can_checkout": False,
            "in_stock": False,
        }
        message = "كم سعر الطلح؟"

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=message,
            tenant_id=33,
            customer_phone="966542980511",
            products=[talh],
            catalog_search_query="طلح",
            question_kind="price",
        )
        facts = bundle.verified_facts
        assert facts["question_kind"] == "price"
        assert facts["catalog_product_ids"] == [501]
        assert facts["price_source"] == "catalog"
        assert facts["allow_checkout_pressure"] is False
        assert facts["has_positive_availability"] is False

        async def _run() -> None:
            async def _good_llm(_bundle):
                return "عسل الطلح سعره 387 ريال 🍯"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.guard_passed is True
            assert "387" in result.text
            assert "اسمك" not in result.text
            assert "عنوانك" not in result.text

            with patch.object(FactBoundPersonaComposer, "compose", return_value=result):
                text, compose_result, event = await try_compose_catalog_product_answer(
                    tenant_id=33,
                    customer_phone="966542980511",
                    inbound_text=message,
                    products=[talh],
                    catalog_search_query="طلح",
                    question_kind="price",
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            assert text
            assert compose_result is not None
            assert event is not None
            assert event["persona_compose"]["surface"] == "catalog_product_answer"
            assert event["persona_compose"]["source"] == "persona_llm"
            assert event["price_source"] == "catalog"
            assert event["catalog_product_ids"] == [501]
            assert event["checkout_pressure_allowed"] is False

        asyncio.run(_run())

    def test_price_emergency_fallback_when_llm_compose_fails(self) -> None:
        shoe = {
            "id": 301,
            "title": "حذاء رياضي أبيض",
            "category": "أحذية",
            "price": 220,
            "can_checkout": False,
            "in_stock": False,
        }
        message = "كم سعر حذاء رياضي أبيض؟"
        failed = PersonaComposeResult(
            text="ياهلا ومرحبا",
            source="fallback_deterministic",
            surface="catalog_product_answer",
            facts_hash="abc123",
            guard_passed=False,
            guard_failed_reason="guard_failed",
        )

        async def _run() -> None:
            with patch.object(FactBoundPersonaComposer, "compose", return_value=failed):
                text, compose_result, event = await try_compose_catalog_product_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text=message,
                    products=[shoe],
                    catalog_search_query="حذاء رياضي أبيض",
                    question_kind="price",
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            assert text
            assert compose_result is not None
            assert compose_result.source == "fallback_deterministic"
            assert event is not None
            assert event["chosen_path"] == "fact_bound_persona_compose"
            assert event["compose_source"] == "fallback_deterministic"
            assert event["fallback_reason"]
            assert event["eligible_product_count"] == 0
            assert "catalog_deterministic_fallback" not in str(
                event.get("persona_compose", {}).get("source")
            )

        asyncio.run(_run())

    def test_catalog_product_answer_deterministic_fallback_price_only(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر الطلح؟",
            products=[
                {
                    "id": 501,
                    "title": "عسل الطلح",
                    "price": 387,
                    "can_checkout": False,
                }
            ],
            catalog_search_query="طلح",
            question_kind="price",
        )
        text = catalog_product_answer_deterministic_fallback(bundle)
        assert "387" in text
        assert "غير متاح للطلب" in text
        assert "متوفر" not in text

    def test_compose_row_resolves_sale_price_when_price_null(self) -> None:
        product = {
            "id": 109,
            "title": "عسل طلح نجد البري",
            "category": "عسل",
            "price": None,
            "sale_price": 387,
            "can_checkout": False,
        }
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر الطلح؟",
            products=[product],
            catalog_search_query="طلح",
            question_kind="price",
        )
        facts = bundle.verified_facts
        rows = facts["catalog_products"]
        assert len(rows) == 1
        assert rows[0]["price"] == 387
        assert facts["price_source"] == "catalog"
        assert facts["allow_price_mention"] is True

        from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: PLC0415

        prompt = build_user_prompt(bundle)
        assert "387" in prompt
        assert "price=387" in prompt

    def test_compose_row_resolves_regular_price_when_sale_null(self) -> None:
        product = {
            "id": 121,
            "title": "عسل طلح جبلي",
            "category": "عسل",
            "price": None,
            "sale_price": None,
            "regular_price": 1475,
            "can_checkout": False,
        }
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر الطلح؟",
            products=[product],
            catalog_search_query="طلح",
            question_kind="price",
        )
        facts = bundle.verified_facts
        rows = facts["catalog_products"]
        assert len(rows) == 1
        assert rows[0]["price"] == 1475
        assert facts["price_source"] == "catalog"
        assert facts["allow_price_mention"] is True

        from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: PLC0415

        prompt = build_user_prompt(bundle)
        assert "1475" in prompt
        assert "price=1475" in prompt

    def test_compose_no_price_when_unparseable(self) -> None:
        product = {
            "id": 109,
            "title": "عسل طلح نجد البري",
            "category": "عسل",
            "price": None,
            "sale_price": "اتصل للسعر",
            "regular_price": "",
            "can_checkout": False,
        }
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر الطلح؟",
            products=[product],
            catalog_search_query="طلح",
            question_kind="price",
        )
        facts = bundle.verified_facts
        rows = facts["catalog_products"]
        assert len(rows) == 1
        assert "price" not in rows[0]
        assert "price_source" not in facts
        assert facts["allow_price_mention"] is False

        from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: PLC0415

        prompt = build_user_prompt(bundle)
        assert "price=" not in prompt

    def test_sale_price_only_does_not_stamp_catalog_from_unrelated_price_column(self) -> None:
        talh = {
            "id": 109,
            "title": "عسل طلح نجد البري",
            "price": None,
            "sale_price": 387,
            "can_checkout": False,
        }
        other = {
            "id": 200,
            "title": "شمع عسل",
            "price": 50,
            "can_checkout": True,
        }
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر الطلح؟",
            products=[talh, other],
            catalog_search_query="طلح",
            question_kind="price",
        )
        talh_row = next(
            row for row in bundle.verified_facts["catalog_products"]
            if row.get("id") == 109
        )
        assert talh_row["price"] == 387
        assert bundle.verified_facts["price_source"] == "catalog"
        assert bundle.verified_facts["allow_checkout_pressure"] is False

    def test_persona_compose_event_metadata_merge_includes_catalog_ids(self) -> None:
        from modules.ai.brain.persona.integration import (  # noqa: PLC0415
            merge_persona_compose_into_extra_metadata,
        )

        event = {
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "fallback_deterministic",
                "guard_passed": False,
            },
            "question_kind": "price",
            "catalog_product_ids": [109, 121],
            "price_source": "catalog",
            "checkout_pressure_allowed": False,
        }
        merged = merge_persona_compose_into_extra_metadata({}, event)
        assert merged["catalog_product_ids"] == [109, 121]
        assert merged["price_source"] == "catalog"
        assert merged["checkout_pressure_allowed"] is False
        assert merged["persona_compose"]["surface"] == "catalog_product_answer"

    def test_compose_guard_accepts_ascii_price_for_arabic_formatted_catalog_fact(self) -> None:
        from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر الطلح؟",
            products=[{
                "id": 109,
                "title": "عسل طلح نجد البري",
                "price": "ر.س. ٣٨٧٫٠٠",
                "can_checkout": False,
            }],
            catalog_search_query="طلح",
            question_kind="price",
        )
        guard = apply_persona_compose_guards(
            "عسل طلح نجد البري سعره 387 ريال، والمنتج غير متاح للطلب حالياً",
            bundle,
        )
        assert guard.passed is True

    def test_availability_non_orderable_no_mتوفر_claim(self) -> None:
        products = [
            {
                "id": 601,
                "title": "عسل سدر صيفي",
                "category": "عسل",
                "price": 380,
                "can_checkout": False,
                "in_stock": False,
            }
        ]

        async def _run() -> None:
            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text="عندكم سدر؟",
                products=products,
                catalog_search_query="سدر",
                category_scope="عسل",
                question_kind="availability",
            )
            assert bundle.verified_facts["has_positive_availability"] is False

            async def _safe_llm(_bundle):
                return "عسل السدر الصيفي موجود ضمن تشكيلتنا لكن التوفر حالياً غير مؤكد"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _safe_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.guard_passed is True
            assert "متوفر" not in result.text

        asyncio.run(_run())


class TestResponderCatalogFactProductsSideChannel:
    def test_facts_products_merge_catalog_fact_products_for_price(self) -> None:
        from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            filter_products_for_browse_turn,
        )

        talh = {
            "id": 501,
            "title": "عسل الطلح",
            "category": "عسل",
            "price": 387,
            "can_checkout": False,
            "in_stock": False,
        }
        orderable = {
            "id": 502,
            "title": "عسل سدر",
            "category": "عسل",
            "price": 400,
            "can_checkout": True,
        }
        raw_products = [orderable]
        catalog_fact_products = [talh]
        merged = list(raw_products) + list(catalog_fact_products)
        facts = filter_products_for_browse_turn(
            merged,
            message="كم سعر الطلح؟",
            query="طلح",
            source="search",
        )
        ids = {p["id"] for p in facts}
        assert 501 in ids
        assert all(p.get("can_checkout") for p in raw_products)
        assert not talh.get("can_checkout")


class TestCatalogFactProductRows:
    def test_catalog_fact_product_rows_coerces_namespace(self) -> None:
        from types import SimpleNamespace

        from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
            catalog_fact_product_rows,
        )

        row = SimpleNamespace(
            id=109,
            title="عسل طلح",
            price="ر.س. ٣٨٧٫٠٠",
            can_checkout=False,
        )
        rows = catalog_fact_product_rows([row])
        assert len(rows) == 1
        assert rows[0]["id"] == 109
        assert rows[0]["price"] == "ر.س. ٣٨٧٫٠٠"
        assert rows[0]["can_checkout"] is False


_JACKET_PRODUCT = {
    "id": 28,
    "title": "جاكيت",
    "category": "ملابس",
    "price": 169,
    "can_checkout": True,
    "in_stock": True,
}

_TALH_PRICE_PRODUCTS = [
    {
        "id": 109,
        "title": "عسل طلح نجد البري إنتاج منحلنا  1 كيلو",
        "category": "عسل",
        "price": 387,
        "can_checkout": False,
        "in_stock": False,
    },
    {
        "id": 121,
        "title": "عسل طلح نجد البري إنتاج منحلنا  5 كيلو",
        "category": "عسل",
        "price": 1475,
        "can_checkout": False,
        "in_stock": False,
    },
]


class TestCatalogQaPersonaCompose:
    def test_non_checkout_shoe_price_uses_one_compose(self) -> None:
        shoe = {
            "id": 301,
            "title": "حذاء رياضي أبيض",
            "category": "أحذية",
            "price": 220,
            "can_checkout": False,
        }

        async def _run() -> None:
            compose = AsyncMock(
                return_value=PersonaComposeResult(
                    text="حذاء رياضي أبيض سعره 220 ريال.",
                    source="persona_llm",
                    surface="catalog_product_answer",
                    facts_hash="facts",
                    guard_passed=True,
                )
            )
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                text, result, event = await try_compose_catalog_product_answer(
                    tenant_id=11,
                    customer_phone="966500000001",
                    inbound_text="كم سعر حذاء رياضي أبيض؟",
                    products=[shoe],
                    catalog_search_query="حذاء رياضي أبيض",
                    question_kind="price",
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            compose.assert_awaited_once()
            assert text
            assert result.source == "persona_llm"
            assert event["eligible_product_count"] == 0
            assert event["compose_source"] == "persona_llm"

        asyncio.run(_run())


class TestCatalogAvailabilityFactsSemantics:
    """Platform-wide facts/prompt semantics for price vs availability turns."""

    _GENERIC_PERFUME = {
        "id": 401,
        "title": "عطر ورد",
        "category": "عطور",
        "price": 185,
        "can_checkout": False,
    }

    _GENERIC_CLOTHING = {
        "id": 402,
        "title": "قميص قطني أزرق",
        "category": "ملابس",
        "price": 129,
        "can_checkout": True,
    }

    def test_price_non_orderable_disallows_availability_prose_invitation(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر حذاء رياضي أبيض؟",
            tenant_id=12,
            products=[self._GENERIC_PERFUME],
            catalog_search_query="حذاء",
            question_kind="price",
        )
        facts = bundle.verified_facts
        assert facts["allow_price_mention"] is True
        assert facts["allow_availability_mention"] is False
        assert facts["has_positive_availability"] is False
        assert facts["price_source"] == "catalog"
        assert "availability_source" not in facts

        from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: PLC0415

        prompt = build_user_prompt(bundle)
        assert "has_positive_availability: False" in prompt
        assert "allow_availability_mention: False" in prompt
        assert "do not add availability or stock-status claims" in prompt
        assert "available=" not in prompt

    def test_price_compliant_candidate_passes_without_availability_claim(self) -> None:
        product = dict(self._GENERIC_PERFUME)

        async def _run() -> None:
            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text="كم سعر عطر ورد؟",
                tenant_id=12,
                products=[product],
                question_kind="price",
            )

            async def _good_llm(_bundle):
                return "عطر ورد سعره 185 ريال."

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True
            assert "185" in result.text

        asyncio.run(_run())

    def test_price_candidate_with_unsupported_mتوفر_still_fails_guard(self) -> None:
        from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر عطر ورد؟",
            products=[self._GENERIC_PERFUME],
            question_kind="price",
        )
        guard = apply_persona_compose_guards(
            "عطر ورد سعره 185 ريال وهو متوفر الآن",
            bundle,
        )
        assert guard.passed is False
        assert guard.failed_reason in {
            "invented_availability",
            "unsupported_available_claim",
        }

    def test_navigation_availability_inbound_classifies_availability(self) -> None:
        from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
            _build_catalog_navigation_bundle,
        )

        shoe = dict(self._GENERIC_CLOTHING)
        shoe["title"] = "حذاء رياضي أبيض"
        shoe["category"] = "أحذية"
        bundle, _rows = _build_catalog_navigation_bundle(
            tenant_id=14,
            customer_phone="966500000002",
            inbound_text="عندكم حذاء رياضي أبيض؟",
            products=[shoe],
            navigator_no_groups_fallback=True,
            decision_args={},
            settings={},
        )
        facts = bundle.verified_facts
        assert facts["question_kind"] == "availability"
        assert facts["navigation_browse"] is False
        assert facts["allow_availability_mention"] is True
        assert facts["has_positive_availability"] is True

    def test_navigation_availability_uncertainty_compose_passes(self) -> None:
        from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
            _build_catalog_navigation_bundle,
        )

        perfume = dict(self._GENERIC_PERFUME)

        async def _run() -> None:
            bundle, _rows = _build_catalog_navigation_bundle(
                tenant_id=15,
                customer_phone="966500000003",
                inbound_text="عندكم عطر ورد؟",
                products=[perfume],
                navigator_no_groups_fallback=True,
                decision_args={},
                settings={},
            )
            assert bundle.verified_facts["question_kind"] == "availability"
            assert bundle.verified_facts["has_positive_availability"] is False

            async def _safe_llm(_bundle):
                return "عطر ورد موجود ضمن تشكيلتنا لكن التوفر حالياً غير مؤكد"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _safe_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True
            assert "متوفر" not in result.text

        asyncio.run(_run())

    def test_navigation_true_browse_forbids_availability_claims(self) -> None:
        from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
            _build_catalog_navigation_bundle,
        )

        bundle, _rows = _build_catalog_navigation_bundle(
            tenant_id=16,
            customer_phone="966500000004",
            inbound_text="وش عندكم من أحذية؟",
            products=[self._GENERIC_CLOTHING],
            navigator_no_groups_fallback=False,
            decision_args={},
            settings={},
        )
        facts = bundle.verified_facts
        assert facts["question_kind"] == "browse"
        assert facts["navigation_browse"] is True
        assert facts["allow_availability_mention"] is False

        from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: PLC0415

        prompt = build_user_prompt(bundle)
        assert "do not mention availability or stock status" in prompt

    def test_positive_availability_evidence_allows_grounded_mتوفر(self) -> None:
        async def _run() -> None:
            bundle = build_catalog_product_answer_facts_bundle(
                inbound_text="عندكم قميص قطني؟",
                tenant_id=17,
                products=[self._GENERIC_CLOTHING],
                question_kind="availability",
            )
            assert bundle.verified_facts["has_positive_availability"] is True

            async def _good_llm(_bundle):
                return "نعم القميص القطني الأزرق متوفر للطلب حالياً"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _good_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True

        asyncio.run(_run())

    def test_tenant_isolation_no_fixed_tenant_in_bundle(self) -> None:
        bundle_a = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر عطر ورد؟",
            tenant_id=901,
            products=[self._GENERIC_PERFUME],
            question_kind="price",
        )
        bundle_b = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر عطر ورد؟",
            tenant_id=902,
            products=[self._GENERIC_PERFUME],
            question_kind="price",
        )
        assert bundle_a.tenant_id == 901
        assert bundle_b.tenant_id == 902
        assert bundle_a.verified_facts["allow_availability_mention"] is False
        assert bundle_b.verified_facts["allow_availability_mention"] is False

    def test_availability_without_orderable_fields_omits_false_evidence(self) -> None:
        product = {
            "id": 501,
            "title": "قميص قطني أزرق",
            "category": "ملابس",
            "price": 129,
        }
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم قميص قطني؟",
            tenant_id=18,
            products=[product],
            question_kind="availability",
        )
        facts = bundle.verified_facts
        assert facts["allow_availability_mention"] is True
        assert facts["has_positive_availability"] is False
        assert "availability_source" not in facts
        assert "availability_evidence" not in facts
        row = facts["catalog_products"][0]
        assert "orderable" not in row
        assert "available" not in row

        async def _run() -> None:
            async def _safe_llm(_bundle):
                return "القميص ضمن تشكيلتنا لكن التوفر حالياً غير مؤكد"

            composer = FactBoundPersonaComposer(enforce_gate=False)
            composer._llm_callable = _safe_llm  # noqa: SLF001
            result = await composer.compose(bundle)
            assert result.source == "persona_llm"
            assert result.guard_passed is True
            assert "متوفر" not in result.text

        asyncio.run(_run())


class TestCompoundCatalogFacetsSemantics:
    """Platform-wide compound price+availability facet and ambiguity semantics."""

    _WHITE_SHOE = {
        "id": 501,
        "title": "حذاء رياضي أبيض",
        "category": "أحذية",
        "price": 220,
        "can_checkout": True,
    }

    def test_unique_generic_product_compound_enables_both_facets(self) -> None:
        from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
            classify_catalog_requested_facets,
        )
        from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: PLC0415

        message = "كم سعر حذاء رياضي أبيض وهل هو متوفر؟"
        facets = classify_catalog_requested_facets(message, query="حذاء رياضي أبيض")
        assert facets == ["price", "availability"]

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=message,
            tenant_id=20,
            products=[self._WHITE_SHOE],
            catalog_search_query="حذاء رياضي أبيض",
            question_kind="price",
        )
        facts = bundle.verified_facts
        assert facts["question_kind"] == "compound"
        assert facts["requested_facets"] == ["price", "availability"]
        assert facts["allow_price_mention"] is True
        assert facts["allow_availability_mention"] is True
        assert facts["has_positive_availability"] is True
        assert facts.get("catalog_ambiguity") is not True

        prompt = build_user_prompt(bundle)
        assert "requested_facets: price, availability" in prompt
        assert "answer both verified price and per-product availability" in prompt
        assert "available=True" in prompt

    def test_duplicate_exact_names_with_conflicting_prices_require_clarification(
        self,
    ) -> None:
        from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards

        products = [
            {
                "id": 601,
                "title": "عطر ورد 100ml",
                "category": "عطور",
                "price": 185,
                "can_checkout": True,
            },
            {
                "id": 602,
                "title": "عطر ورد 100ml",
                "category": "عطور",
                "price": 210,
                "can_checkout": True,
            },
        ]
        message = "كم سعر عطر ورد 100ml وهل هو متوفر؟"
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=message,
            tenant_id=21,
            products=products,
            catalog_search_query="عطر ورد 100ml",
            question_kind="price",
        )
        facts = bundle.verified_facts
        assert facts["catalog_ambiguity"] is True
        assert facts["require_clarification"] is True
        assert facts["allow_price_mention"] is False
        assert facts["allow_availability_mention"] is False
        assert len(facts.get("ambiguous_catalog_candidates") or []) == 2

        from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: PLC0415

        prompt = build_user_prompt(bundle)
        assert "catalog_ambiguity: true" in prompt
        assert "ambiguous_candidate:" in prompt
        assert "do not pick one price" in prompt

        guard = apply_persona_compose_guards(
            "عطر ورد 100ml سعره 185 ريال وهو متوفر",
            bundle,
        )
        assert guard.passed is False
        assert guard.failed_reason == "invented_price"

    def test_identical_exact_duplicates_still_require_clarification(self) -> None:
        products = [
            {
                "id": 611,
                "title": "عطر ورد 100ml",
                "category": "عطور",
                "price": 185,
                "can_checkout": True,
            },
            {
                "id": 612,
                "title": "عطر ورد 100ml",
                "category": "عطور",
                "price": 185,
                "can_checkout": True,
            },
        ]
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر عطر ورد 100ml وهل هو متوفر؟",
            tenant_id=21,
            products=products,
            catalog_search_query="عطر ورد 100ml",
        )
        facts = bundle.verified_facts
        assert facts["catalog_ambiguity"] is True
        assert facts["require_clarification"] is True
        assert facts["catalog_ambiguity_reason"] == "multiple_exact_title_candidates"
        assert len(facts["ambiguous_catalog_candidates"]) == 2
        assert facts["allow_price_mention"] is False
        assert facts["allow_availability_mention"] is False
        assert facts["allow_checkout_pressure"] is False
        assert facts["allow_slot_prompts"] is False
        assert catalog_product_answer_deterministic_fallback(bundle) == ""

    def test_mixed_availability_duplicates_do_not_generalize_stock(self) -> None:
        products = [
            {
                "id": 701,
                "title": "قميص قطني أزرق",
                "category": "ملابس",
                "price": 129,
                "can_checkout": True,
            },
            {
                "id": 702,
                "title": "قميص قطني أزرق",
                "category": "ملابس",
                "price": 129,
                "can_checkout": False,
            },
        ]
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر قميص قطني أزرق وهل هو متوفر؟",
            tenant_id=22,
            products=products,
            catalog_search_query="قميص قطني أزرق",
        )
        facts = bundle.verified_facts
        assert facts["catalog_ambiguity"] is True
        candidates = facts.get("ambiguous_catalog_candidates") or []
        availability_values = {
            bool(row.get("available"))
            for row in candidates
            if isinstance(row, dict) and "available" in row
        }
        assert availability_values == {True, False}
        assert facts["allow_availability_mention"] is False

    def test_unique_compound_fallback_is_one_line_for_one_product(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر حذاء رياضي أبيض وهل هو متوفر؟",
            tenant_id=22,
            products=[self._WHITE_SHOE],
            catalog_search_query="حذاء رياضي أبيض",
        )
        text = catalog_product_answer_deterministic_fallback(bundle)
        assert text
        assert "\n" not in text
        assert "220" in text
        assert "متوفر" in text
        assert text.count("حذاء رياضي أبيض") == 1
        assert "اسمك" not in text
        assert "عنوانك" not in text
        assert "طريقة الدفع" not in text

    def test_ambiguous_compound_has_no_deterministic_clarification(self) -> None:
        products = [
            {**self._WHITE_SHOE, "id": 721},
            {**self._WHITE_SHOE, "id": 722},
        ]
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر حذاء رياضي أبيض وهل هو متوفر؟",
            tenant_id=22,
            products=products,
            catalog_search_query="حذاء رياضي أبيض",
        )
        assert bundle.verified_facts["require_clarification"] is True
        assert catalog_product_answer_deterministic_fallback(bundle) == ""

    def test_compound_compose_metadata_stays_llm_owned(self) -> None:
        async def _run() -> None:
            shoe = dict(self._WHITE_SHOE)
            compose = AsyncMock(
                return_value=PersonaComposeResult(
                    text="حذاء رياضي أبيض سعره 220 ريال وهو متوفر للطلب.",
                    source="persona_llm",
                    surface="catalog_product_answer",
                    facts_hash="shoe-compound",
                    guard_passed=True,
                )
            )
            with patch.object(FactBoundPersonaComposer, "compose", new=compose):
                _text, result, event = await try_compose_catalog_product_answer(
                    tenant_id=23,
                    customer_phone="966500000010",
                    inbound_text="كم سعر حذاء رياضي أبيض وهل هو متوفر؟",
                    products=[shoe],
                    catalog_search_query="حذاء رياضي أبيض",
                    question_kind="price",
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            assert result.source == "persona_llm"
            assert event["compose_source"] == "persona_llm"
            assert event["llm_candidate_present"] is True
            assert event["final_text_transformed"] is False
            assert event["final_customer_text_source"] == "persona_llm"
            assert event["question_kind"] == "compound"
            assert event["requested_facets"] == ["price", "availability"]
            assert event["checkout_pressure_allowed"] is False

        asyncio.run(_run())
