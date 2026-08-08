"""Scoped availability truth: matching_set vs product vs variant.

Platform-wide regressions for category-existence vs product/variant authority.
Does not assert exact customer-facing Arabic phrases except emergency false-negative ban.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in (_BACKEND, os.path.join(_BACKEND, ".."), os.path.join(_BACKEND, "..", "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    SUBJECT_SCOPE_MATCHING_SET,
    SUBJECT_SCOPE_PRODUCT,
    SUBJECT_SCOPE_VARIANT,
    _catalog_product_answer_emergency_fallback,
    build_catalog_product_answer_facts_bundle,
    classify_catalog_availability_subject_scope,
)
from modules.ai.brain.persona.compose_guards import (  # noqa: E402
    apply_persona_compose_guards,
)
from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: E402


def _dress(product_id: int, *, price: int, can_checkout: bool = True) -> dict:
    return {
        "id": product_id,
        "external_id": f"ext-dress-{product_id}",
        "title": "فستان",
        "category": "فساتين",
        "price": price,
        "can_checkout": can_checkout,
        "orderable": can_checkout,
    }


def _shoe(product_id: int, *, can_checkout: bool = True) -> dict:
    return {
        "id": product_id,
        "title": "حذاء رياضي أبيض",
        "category": "أحذية",
        "price": 220,
        "can_checkout": can_checkout,
        "orderable": can_checkout,
    }


class TestSubjectScopeClassification:
    def test_matching_set_existence_ask(self) -> None:
        assert (
            classify_catalog_availability_subject_scope(
                "عندكم فستان؟",
                query="فستان",
                question_kind="availability",
                requested_facets=["availability"],
            )
            == SUBJECT_SCOPE_MATCHING_SET
        )

    def test_product_deixis(self) -> None:
        assert (
            classify_catalog_availability_subject_scope(
                "هل هذا الفستان متوفر؟",
                query="فستان",
                question_kind="availability",
                requested_facets=["availability"],
            )
            == SUBJECT_SCOPE_PRODUCT
        )

    def test_variant_size_ask(self) -> None:
        assert (
            classify_catalog_availability_subject_scope(
                "هل مقاس XL متوفر؟",
                query="جاكيت",
                question_kind="availability",
                requested_facets=["availability"],
            )
            == SUBJECT_SCOPE_VARIANT
        )

    def test_compound_is_product_scope(self) -> None:
        assert (
            classify_catalog_availability_subject_scope(
                "كم سعر عطر ورد 100ml وهل هو متوفر؟",
                query="عطر ورد 100ml",
                question_kind="compound",
                requested_facets=["price", "availability"],
            )
            == SUBJECT_SCOPE_PRODUCT
        )


class TestMatchingSetExistence:
    def test_multiple_same_title_dresses_allow_aggregate_existence(self) -> None:
        products = [
            _dress(21, price=77),
            _dress(22, price=114),
            _dress(23, price=289),
            _dress(24, price=199),
        ]
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم فستان؟",
            tenant_id=1,
            products=products,
            catalog_search_query="فستان",
            search_result_count=6,
        )
        facts = bundle.verified_facts
        assert facts["subject_scope"] == SUBJECT_SCOPE_MATCHING_SET
        assert facts["question_kind"] == "availability"
        assert facts["eligible_product_count"] == 4
        assert facts["category_existence"] is True
        assert facts["require_clarification"] is True
        assert facts["catalog_ambiguity"] is True
        assert facts["allow_matching_set_existence_mention"] is True
        assert facts["allow_availability_mention"] is True
        assert facts["availability_evidence_kind"] == "matching_set_orderability"

        guard = apply_persona_compose_guards(
            "نعم عندنا أكثر من فستان متوفر، أي موديل يناسبك؟",
            bundle,
        )
        assert guard.passed is True
        assert guard.failed_reason != "invented_availability"

        prompt = build_user_prompt(bundle)
        assert "subject_scope: matching_set" in prompt
        assert "category_existence: True" in prompt
        assert "matching-set existence is confirmed" in prompt

    def test_matching_set_zero_results_no_false_positive(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم فستان؟",
            tenant_id=1,
            products=[],
            catalog_search_query="فستان",
            search_result_count=0,
        )
        facts = bundle.verified_facts
        assert facts["subject_scope"] == SUBJECT_SCOPE_MATCHING_SET
        assert facts["category_existence"] is False
        assert facts["allow_matching_set_existence_mention"] is False
        assert facts["has_positive_availability"] is False

        guard = apply_persona_compose_guards(
            "نعم الفستان متوفر حالياً",
            bundle,
        )
        assert guard.passed is False
        assert guard.failed_reason in {
            "invented_availability",
            "unsupported_available_claim",
        }

    def test_tenant_isolation_on_facts(self) -> None:
        products = [_dress(31, price=120), _dress(32, price=150)]
        bundle_a = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم فستان؟",
            tenant_id=11,
            customer_phone="966500000011",
            products=products,
            catalog_search_query="فستان",
        )
        bundle_b = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم فستان؟",
            tenant_id=12,
            customer_phone="966500000012",
            products=products,
            catalog_search_query="فستان",
        )
        assert bundle_a.tenant_id == 11
        assert bundle_b.tenant_id == 12
        assert bundle_a.verified_facts["category_existence"] is True
        assert bundle_b.verified_facts["category_existence"] is True


class TestProductAndVariantScopes:
    def test_resolved_product_availability_contract(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="هل هذا الفستان متوفر؟",
            tenant_id=2,
            products=[_dress(41, price=180)],
            catalog_search_query="فستان",
        )
        facts = bundle.verified_facts
        assert facts["subject_scope"] == SUBJECT_SCOPE_PRODUCT
        assert facts.get("require_clarification") is not True
        assert facts["allow_product_availability_mention"] is True
        assert facts["allow_availability_mention"] is True

        guard = apply_persona_compose_guards(
            "نعم هذا الفستان متوفر للطلب حالياً.",
            bundle,
        )
        assert guard.passed is True

    def test_ambiguous_product_compound_still_requires_clarification(self) -> None:
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
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="كم سعر عطر ورد 100ml وهل هو متوفر؟",
            tenant_id=21,
            products=products,
            catalog_search_query="عطر ورد 100ml",
        )
        facts = bundle.verified_facts
        assert facts["subject_scope"] == SUBJECT_SCOPE_PRODUCT
        assert facts["require_clarification"] is True
        assert facts["allow_availability_mention"] is False
        assert facts["category_existence"] is False

        guard = apply_persona_compose_guards(
            "عطر ورد 100ml سعره 185 ريال وهو متوفر",
            bundle,
        )
        assert guard.passed is False
        assert guard.failed_reason in {
            "invented_availability",
            "ambiguous_premature_price_selection",
        }

    def test_variant_scope_without_variant_evidence_blocks_claim(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="هل مقاس XL متوفر؟",
            tenant_id=3,
            products=[_shoe(71, can_checkout=True)],
            catalog_search_query="حذاء رياضي أبيض",
        )
        facts = bundle.verified_facts
        assert facts["subject_scope"] == SUBJECT_SCOPE_VARIANT
        assert facts["allow_variant_availability_mention"] is False
        assert facts["allow_availability_mention"] is False

        guard = apply_persona_compose_guards(
            "مقاس XL متوفر حالياً",
            bundle,
        )
        assert guard.passed is False
        assert guard.failed_reason == "invented_availability"

    def test_variant_scope_with_variant_row_allows_claim(self) -> None:
        product = {
            "id": 81,
            "title": "جاكيت شتوي",
            "category": "ملابس",
            "price": 320,
            "variant_id": "81-XL",
            "can_checkout": True,
            "orderable": True,
            "in_stock": True,
        }
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="هل مقاس XL متوفر؟",
            tenant_id=3,
            products=[product],
            catalog_search_query="جاكيت شتوي",
            decision_args={"product": product},
        )
        facts = bundle.verified_facts
        assert facts["subject_scope"] == SUBJECT_SCOPE_VARIANT
        assert facts["allow_variant_availability_mention"] is True
        assert facts["allow_availability_mention"] is True

        guard = apply_persona_compose_guards(
            "مقاس XL متوفر للطلب حالياً.",
            bundle,
        )
        assert guard.passed is True


class TestOrderabilityNotInventory:
    def test_can_checkout_does_not_project_inventory_available_without_stock_field(
        self,
    ) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم حذاء رياضي أبيض؟",
            tenant_id=4,
            products=[_shoe(91)],
            catalog_search_query="حذاء رياضي أبيض",
        )
        rows = bundle.verified_facts.get("catalog_products") or []
        assert rows
        assert rows[0].get("orderable") is True
        assert "available" not in rows[0]
        assert bundle.verified_facts["availability_evidence_kind"] == (
            "matching_set_orderability"
        )


class TestEmergencyFallbackScopedTruth:
    def test_matching_set_eligible_emergency_not_false_negative(self) -> None:
        products = [_dress(21, price=77), _dress(22, price=114)]
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم فستان؟",
            tenant_id=1,
            products=products,
            catalog_search_query="فستان",
        )
        assert bundle.verified_facts["category_existence"] is True
        result = _catalog_product_answer_emergency_fallback(
            bundle,
            reason="invented_availability",
        )
        assert "لا تتوفر حالة توفر مؤكدة" not in result.text
        assert result.text.strip()
        # Deterministic path may mention orderability; must not deny existence.
        assert "متوفر" in result.text or "كتالوج" in result.text

    def test_matching_set_zero_still_may_use_unavailable_emergency(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم فستان؟",
            tenant_id=1,
            products=[],
            catalog_search_query="فستان",
        )
        result = _catalog_product_answer_emergency_fallback(
            bundle,
            reason="compose_unavailable",
        )
        assert "لا تتوفر حالة توفر مؤكدة في الكتالوج حالياً." == result.text


class TestPersonaExpressionFree:
    def test_no_exact_reply_required_for_matching_set(self) -> None:
        products = [_dress(21, price=77), _dress(22, price=114)]
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="عندكم فستان؟",
            tenant_id=1,
            products=products,
            catalog_search_query="فستان",
        )
        candidates = (
            "نعم عندنا فساتين، تفضلي أي موديل؟",
            "موجود أكثر من فستان متوفر، أي واحد تبيه؟",
            "أيوه عندنا فستان، تبيني أفرق لك بين الخيارات؟",
        )
        for text in candidates:
            guard = apply_persona_compose_guards(text, bundle)
            assert guard.passed is True, text
