"""P0 tests for catalog_product_answer FactBoundPersonaComposer."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

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

    def test_compose_failure_returns_none_for_responder_fallback(self) -> None:
        async def _run() -> None:
            with patch.object(
                FactBoundPersonaComposer,
                "compose",
                return_value=type(
                    "R",
                    (),
                    {
                        "text": "",
                        "source": "fallback_deterministic",
                        "guard_passed": False,
                        "surface": "catalog_product_answer",
                    },
                )(),
            ):
                text, result, event = await try_compose_catalog_product_answer(
                    tenant_id=33,
                    customer_phone="966542980511",
                    inbound_text="وش عندكم من عسل؟",
                    products=_HONEY_PRODUCTS,
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            assert text is None
            assert result is None
            assert event is None

        asyncio.run(_run())

    def test_blocked_when_not_allowlisted(self) -> None:
        async def _run() -> None:
            text, result, event = await try_compose_catalog_product_answer(
                tenant_id=33,
                customer_phone="966500000099",
                inbound_text="وش عندكم من عسل؟",
                products=_HONEY_PRODUCTS,
                ai_settings=_enabled_catalog_ai_settings(),
            )
            assert text is None
            assert result is None
            assert event is None

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

    def test_price_deterministic_fallback_when_llm_compose_fails(self) -> None:
        talh = {
            "id": 501,
            "title": "عسل الطلح",
            "category": "عسل",
            "price": 387,
            "can_checkout": False,
            "in_stock": False,
        }
        message = "كم سعر الطلح؟"
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
                    tenant_id=33,
                    customer_phone="966542980511",
                    inbound_text=message,
                    products=[talh],
                    catalog_search_query="طلح",
                    question_kind="price",
                    ai_settings=_enabled_catalog_ai_settings(),
                )
            assert text
            assert "387" in text
            assert "غير متاح للطلب" in text
            assert "اختر رقم" not in text
            assert compose_result is not None
            assert compose_result.source == "catalog_deterministic_fallback"
            assert event is not None
            assert event["chosen_path"] == "fact_bound_persona_compose"
            assert event["persona_compose"]["surface"] == "catalog_product_answer"
            assert event["persona_compose"]["source"] == "catalog_deterministic_fallback"
            assert event["question_kind"] == "price"
            assert event["price_source"] == "catalog"
            assert event["catalog_product_ids"] == [501]
            assert event["checkout_pressure_allowed"] is False

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

    def test_persona_compose_event_metadata_merge_includes_catalog_ids(self) -> None:
        from modules.ai.brain.persona.integration import (  # noqa: PLC0415
            merge_persona_compose_into_extra_metadata,
        )

        event = {
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "catalog_deterministic_fallback",
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
