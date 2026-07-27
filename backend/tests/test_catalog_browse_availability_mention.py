"""Regression tests for evidence-backed availability wording on catalog browse turns."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in (_BACKEND, os.path.join(_BACKEND, ".."), os.path.join(_BACKEND, "..", "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    _build_catalog_navigation_bundle,
    build_catalog_product_answer_facts_bundle,
    classify_catalog_question_kind,
)
from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards  # noqa: E402
from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: E402


def _orderable_product(
    *,
    product_id: int,
    title: str,
    price: int,
) -> dict:
    return {
        "id": product_id,
        "external_id": f"ext-{product_id}",
        "title": title,
        "category": "ملابس",
        "price": price,
        "can_checkout": True,
        "orderable": True,
        "in_stock": True,
    }


def _twelve_orderable_products() -> list[dict]:
    titles = [
        "حذاء رياضي أبيض",
        "قميص قطني أزرق",
        "بنطلون جينز رمادي",
        "جاكيت شتوي أسود",
        "تيشيرت بولو أخضر",
        "شورت رياضي رمادي",
        "حذاء جري أسود",
        "قميص كاجوال بيج",
        "بنطلون كاجوال كحلي",
        "جاكيت خفيف أزرق",
        "تيشيرت قطني أبيض",
        "شورت سباحة أزرق",
    ]
    return [
        _orderable_product(product_id=1000 + idx, title=title, price=120 + idx * 5)
        for idx, title in enumerate(titles)
    ]


def _non_orderable_product(
    *,
    product_id: int,
    title: str,
    price: int,
) -> dict:
    return {
        "id": product_id,
        "external_id": None,
        "title": title,
        "category": "ملابس",
        "price": price,
        "can_checkout": False,
        "orderable": False,
        "in_stock": False,
    }


class TestCatalogBrowseAvailabilityMention:
    """Browse turns with orderable evidence may use availability wording; guards stay closed otherwise."""

    def test_evidence_backed_browse_permits_availability_wording(self) -> None:
        message = "وش المنتجات المتوفرة طيب؟"
        products = _twelve_orderable_products()

        assert classify_catalog_question_kind(message) == "browse"

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=message,
            tenant_id=21,
            products=products,
            search_result_count=len(products),
        )
        facts = bundle.verified_facts
        assert facts["question_kind"] == "browse"
        assert facts["has_positive_availability"] is True
        assert facts["allow_availability_mention"] is True

        grounded_reply = (
            "من المنتجات المتوفرة عندنا: حذاء رياضي أبيض، قميص قطني أزرق، وبنطلون جينز رمادي"
        )
        guard = apply_persona_compose_guards(grounded_reply, bundle)
        assert guard.passed is True
        assert not guard.failed_reason

        invented_reply = "ساعة ذكية فاخرة متوفرة الآن وسعرها 999 ريال"
        invented_guard = apply_persona_compose_guards(invented_reply, bundle)
        assert invented_guard.passed is False
        assert invented_guard.failed_reason == "invented_price"

    def test_zero_orderable_products_keeps_guard_closed(self) -> None:
        message = "وش المنتجات المتوفرة طيب؟"
        products = [
            _non_orderable_product(product_id=2001, title="حذاء رياضي أبيض", price=150),
            _non_orderable_product(product_id=2002, title="قميص قطني أزرق", price=120),
        ]

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=message,
            tenant_id=22,
            products=products,
        )
        facts = bundle.verified_facts
        assert facts["allow_availability_mention"] is False
        assert facts["has_positive_availability"] is False

        availability_reply = "حذاء رياضي أبيض متوفر الآن"
        guard = apply_persona_compose_guards(availability_reply, bundle)
        assert guard.passed is False
        assert guard.failed_reason == "invented_availability"

        fabricated_reply = "ساعة ذكية فاخرة متوفرة الآن"
        fabricated_guard = apply_persona_compose_guards(fabricated_reply, bundle)
        assert fabricated_guard.passed is False
        assert fabricated_guard.failed_reason == "invented_availability"

        empty_bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=message,
            tenant_id=22,
            products=[],
        )
        empty_facts = empty_bundle.verified_facts
        assert empty_facts["allow_availability_mention"] is False
        assert empty_facts["has_positive_availability"] is False
        empty_guard = apply_persona_compose_guards(availability_reply, empty_bundle)
        assert empty_guard.passed is False
        assert empty_guard.failed_reason == "invented_availability"

    def test_browse_without_availability_token_is_unchanged(self) -> None:
        message = "وش عندكم منتجات؟"
        products = _twelve_orderable_products()

        assert classify_catalog_question_kind(message) == "browse"

        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=message,
            tenant_id=23,
            products=products,
            search_result_count=len(products),
        )
        facts = bundle.verified_facts
        assert facts["question_kind"] == "browse"
        assert facts["allow_price_mention"] is False
        assert facts["allow_checkout_pressure"] is False
        assert facts["allow_slot_prompts"] is False
        assert facts["allow_superiority_claims"] is False

        neutral_reply = "عندنا تشكيلة من حذاء رياضي أبيض وقميص قطني أزرق وبنطلون جينز رمادي"
        guard = apply_persona_compose_guards(neutral_reply, bundle)
        assert guard.passed is True
        assert not guard.failed_reason

    def test_production_browse_message_prompt_coherence_with_navigation_bundle(self) -> None:
        message = "وش المنتجات المتوفرة طيب؟"
        orderable = _orderable_product(product_id=3001, title="حذاء رياضي أبيض", price=150)
        non_orderable = _non_orderable_product(
            product_id=3002,
            title="عطر ورد",
            price=185,
        )

        orderable_bundle, _rows = _build_catalog_navigation_bundle(
            tenant_id=24,
            customer_phone="966500000010",
            inbound_text=message,
            products=[orderable],
            navigator_no_groups_fallback=False,
            decision_args={},
            settings={},
        )
        orderable_facts = orderable_bundle.verified_facts
        assert orderable_facts["question_kind"] == "browse"
        assert orderable_facts["navigation_browse"] is True
        assert orderable_facts["allow_availability_mention"] is True
        assert orderable_facts["has_positive_availability"] is True

        orderable_prompt = build_user_prompt(orderable_bundle)
        assert (
            "mention positive availability only for products with available=true in facts"
            in orderable_prompt
        )
        assert "do not mention availability or stock status" not in orderable_prompt
        assert "available=True" in orderable_prompt

        non_orderable_bundle, _rows = _build_catalog_navigation_bundle(
            tenant_id=24,
            customer_phone="966500000010",
            inbound_text=message,
            products=[non_orderable],
            navigator_no_groups_fallback=False,
            decision_args={},
            settings={},
        )
        non_orderable_facts = non_orderable_bundle.verified_facts
        assert non_orderable_facts["allow_availability_mention"] is False
        assert non_orderable_facts["has_positive_availability"] is False

        non_orderable_prompt = build_user_prompt(non_orderable_bundle)
        assert (
            "mention positive availability only for products with available=true in facts"
            not in non_orderable_prompt
        )
        assert "available=" not in non_orderable_prompt
        assert "do not invent products, prices, availability" in non_orderable_prompt
