"""
Regression: stated-price candidate selection, price-negation guard, and
Arabic definite-article catalog search normalization.

BEFORE (proven defect):
  - «أريد الفستان سعره 114 ريال» with candidates {289, 114} picked 289 and
    LLM could assert 114 was wrong because only invented_price blocked absent amounts.
  - «الفستان» search returned 0 hits while «فستان» returned results.

Generic merchant data per AGENTS.md — not tied to tenant 1.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.store_knowledge import (  # noqa: E402
    _catalog_search_arabic_feminine_plural_variants,
    _catalog_search_query_variants,
)
from modules.ai.brain.commerce.candidate_price_selection import (  # noqa: E402
    extract_stated_price_constraint,
    resolve_candidates_by_stated_price,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent import rules
from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    _resolve_catalog_compose_rows,
    build_catalog_product_answer_facts_bundle,
)
from modules.ai.brain.persona.compose_guards import apply_persona_compose_guards
from modules.ai.brain.state.stages import STAGE_DISCOVERY
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
DRESS_A = {
    "id": 23,
    "title": "فستان سهرة",
    "price": 289.0,
    "external_id": "ext-dress-289",
    "can_checkout": True,
    "orderable": True,
}
DRESS_B = {
    "id": 37,
    "title": "فستان سهرة",
    "price": 114.0,
    "external_id": "ext-dress-114",
    "can_checkout": True,
    "orderable": True,
}
DRESS_C = {
    "id": 38,
    "title": "فستان سهرة",
    "price": 114.0,
    "external_id": "ext-dress-114b",
    "can_checkout": True,
    "orderable": True,
}
SHOE = {
    "id": 51,
    "title": "حذاء رياضي أبيض",
    "price": 199.0,
    "external_id": "ext-shoe",
    "can_checkout": True,
    "orderable": True,
}


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(
    message: str,
    *,
    candidates: List[Dict[str, Any]],
    intent_name: str = "start_order",
) -> BrainContext:
    intent = rules.match(message) or Intent(
        name=intent_name, confidence=0.9, raw_message=message,
    )
    if intent_name != "general":
        intent = Intent(name=intent_name, confidence=0.9, raw_message=message)
    return BrainContext(
        tenant_id=31,
        customer_phone="966500000099",
        message=message,
        intent=intent,
        state=MerchantConversationState(
            stage=STAGE_DISCOVERY,
            greeted=True,
            last_search_candidates=list(candidates),
        ),
        facts=_facts(),
    )


class TestStatedPriceExtraction:
    def test_extracts_single_price_from_inbound(self) -> None:
        msg = "أريد الفستان سعره 114 ريال"
        assert extract_stated_price_constraint(msg) == 114

    def test_reuses_extract_reply_prices_not_parallel_parser(self) -> None:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            extract_reply_prices,
        )

        msg = "أريد الفستان سعره 114 ريال"
        assert extract_stated_price_constraint(msg) == next(iter(extract_reply_prices(msg)))


class TestCandidatePriceResolution:
    def test_selects_matching_price_among_same_name_candidates(self) -> None:
        msg = "أريد الفستان سعره 114 ريال"
        resolution = resolve_candidates_by_stated_price(msg, [DRESS_A, DRESS_B])
        assert resolution.kind == "selected"
        assert resolution.selected is not None
        assert resolution.selected["id"] == 37
        assert resolution.selected["price"] == 114.0

    def test_no_match_presents_pool_without_wrong_price_claim(self) -> None:
        msg = "أريد الفستان سعره 150 ريال"
        resolution = resolve_candidates_by_stated_price(msg, [DRESS_A, DRESS_B])
        assert resolution.kind == "no_match"
        assert resolution.stated_price == 150
        assert len(resolution.candidates) >= 1

    def test_duplicate_name_and_price_requires_clarify(self) -> None:
        msg = "أريد الفستان سعره 114 ريال"
        resolution = resolve_candidates_by_stated_price(msg, [DRESS_A, DRESS_B, DRESS_C])
        assert resolution.kind == "clarify"
        assert len(resolution.candidates) == 2
        assert all(c["price"] == 114.0 for c in resolution.candidates)


class TestDecisionEnginePriceConstraint:
    def test_stated_price_selects_114_not_289(self) -> None:
        msg = "أريد الفستان سعره 114 ريال"
        decision = DefaultDecisionEngine().decide(
            _ctx(msg, candidates=[DRESS_A, DRESS_B], intent_name="ask_price"),
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        product = decision.args.get("product") or {}
        assert product.get("id") in {37, "37"}
        assert product.get("price") == 114.0
        assert product.get("id") not in {23, "23"}

    def test_unmatched_stated_price_narrows_options(self) -> None:
        msg = "أريد الفستان سعره 150 ريال"
        decision = DefaultDecisionEngine().decide(
            _ctx(msg, candidates=[DRESS_A, DRESS_B], intent_name="ask_price"),
        )
        assert decision.action == ACTION_NARROW
        products = decision.args.get("products") or []
        assert len(products) >= 1
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_duplicate_name_price_clarifies(self) -> None:
        msg = "أريد الفستان سعره 114 ريال"
        decision = DefaultDecisionEngine().decide(
            _ctx(msg, candidates=[DRESS_A, DRESS_B, DRESS_C], intent_name="ask_price"),
        )
        assert decision.action == ACTION_CLARIFY
        assert decision.args.get("topic") == "product_price_ambiguity"


class TestCatalogComposePriceConstraint:
    def test_compose_rows_select_single_price_match(self) -> None:
        rows = [
            {"id": 23, "title": "فستان سهرة", "price": 289.0, "can_checkout": True},
            {"id": 37, "title": "فستان سهرة", "price": 114.0, "can_checkout": True},
        ]
        resolved, meta = _resolve_catalog_compose_rows(
            rows,
            catalog_search_query="فستان سهرة",
            inbound_text="أريد الفستان سعره 114 ريال",
        )
        assert len(resolved) == 1
        assert resolved[0]["id"] == 37
        assert not meta.get("require_clarification")

    def test_compose_facts_bundle_does_not_keep_ambiguity_on_price_pick(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="أريد الفستان سعره 114 ريال",
            tenant_id=31,
            products=[DRESS_A, DRESS_B],
            catalog_search_query="فستان سهرة",
        )
        products = bundle.verified_facts.get("catalog_products") or []
        assert len(products) == 1
        assert products[0].get("id") == 37


class TestNegatedValidCandidatePriceGuard:
    _BAD_REPLY = (
        "حاضر، يبدو أن السعر غير صحيح، الفستان الموجود لدينا سعره 289 ريال."
    )

    def _facts_with_candidates(self) -> dict[str, Any]:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="أريد الفستان سعره 114 ريال",
            tenant_id=31,
            products=[DRESS_A, DRESS_B],
            catalog_search_query="فستان سهرة",
        )
        return dict(bundle.verified_facts)

    def test_rejects_negating_price_present_in_candidate_facts(self) -> None:
        from modules.ai.brain.persona.facts_bundle import (  # noqa: PLC0415
            PersonaConstraints,
            PersonaFactsBundle,
        )

        facts = self._facts_with_candidates()
        facts["allow_price_mention"] = True
        facts["ambiguous_catalog_candidates"] = [
            {"id": 23, "title": "فستان سهرة", "price": 289},
            {"id": 37, "title": "فستان سهرة", "price": 114},
        ]
        bundle = PersonaFactsBundle(
            surface="catalog_product_answer",
            inbound_text=facts["inbound_text"],
            language="ar",
            dialect="saudi_arabic",
            verified_facts=facts,
            customer_context={},
            merchant_persona={},
            constraints=PersonaConstraints(max_chars=420, max_emojis=2),
            tenant_id=31,
            customer_phone="",
        )
        guard = apply_persona_compose_guards(self._BAD_REPLY, bundle)
        assert guard.passed is False
        assert guard.failed_reason == "negated_valid_candidate_price"

    def test_absent_price_negation_keeps_invented_price_amount_behavior(self) -> None:
        from modules.ai.brain.persona.facts_bundle import (  # noqa: PLC0415
            PersonaConstraints,
            PersonaFactsBundle,
        )

        facts = self._facts_with_candidates()
        facts["inbound_text"] = "أريد الفستان سعره 999 ريال"
        facts["allow_price_mention"] = True
        bundle = PersonaFactsBundle(
            surface="catalog_product_answer",
            inbound_text=facts["inbound_text"],
            language="ar",
            dialect="saudi_arabic",
            verified_facts=facts,
            customer_context={},
            merchant_persona={},
            constraints=PersonaConstraints(max_chars=420, max_emojis=2),
            tenant_id=31,
            customer_phone="",
        )
        reply = "يبدو أن السعر غير صحيح، أقرب سعر عندنا 289 ريال."
        guard = apply_persona_compose_guards(reply, bundle)
        assert guard.passed is False
        assert guard.failed_reason == "invented_price_amount"


class TestCatalogSearchFemininePluralVariants:
    def test_saaat_yields_saaah_variants(self) -> None:
        variants = _catalog_search_arabic_feminine_plural_variants("ساعات")
        assert variants == ["ساعة", "ساعه"]

    def test_trailing_question_mark_stripped(self) -> None:
        variants = _catalog_search_arabic_feminine_plural_variants("ساعات؟")
        assert variants == ["ساعة", "ساعه"]

    def test_multi_token_skipped(self) -> None:
        assert _catalog_search_arabic_feminine_plural_variants("ساعات يد") == []

    def test_short_token_skipped(self) -> None:
        assert _catalog_search_arabic_feminine_plural_variants("ات") == []

    def test_non_plural_unchanged(self) -> None:
        assert _catalog_search_arabic_feminine_plural_variants("ساعة") == []


class TestCatalogSearchDefiniteArticleNormalization:
    def test_query_variants_strip_leading_al(self) -> None:
        variants = _catalog_search_query_variants("الفستان")
        assert "الفستان" in variants
        assert "فستان" in variants

    def test_english_query_unchanged(self) -> None:
        variants = _catalog_search_query_variants("Classic Rose Perfume")
        assert variants == ["Classic Rose Perfume"]

    def test_multi_token_arabic_query_variants(self) -> None:
        variants = _catalog_search_query_variants("الفستان الأحمر")
        assert "فستان أحمر" in variants or "الفستان الأحمر" in variants

    def _make_builder_with_products(
        self, products: List[SimpleNamespace],
    ) -> CatalogContextBuilder:
        db = MagicMock()
        builder = CatalogContextBuilder(db, tenant_id=31)

        def _query_chain(rows: List[SimpleNamespace]):
            chain = MagicMock()
            chain.filter.return_value = chain
            chain.order_by.return_value = chain
            chain.limit.return_value = chain
            chain.all.return_value = rows
            return chain

        def _execute_side_effect(sql, params):
            q = str(params.get("q") or "")
            hits = [
                p.id for p in products
                if q.lower() in str(p.title or "").lower()
            ]
            result = MagicMock()
            result.__iter__ = lambda self: iter((row,) for row in hits)
            return result

        db.execute.side_effect = _execute_side_effect
        db.query.side_effect = lambda model: _query_chain([
            p for p in products
            if str(getattr(p, "title", "")).lower().find(
                str(db.query.call_args or "") and ""
            ) is not None
        ])
        return builder, db, products

    def test_definite_and_bare_queries_share_normalized_variant(self) -> None:
        bare_variants = _catalog_search_query_variants("فستان")
        definite_variants = _catalog_search_query_variants("الفستان")
        assert bare_variants[0] == "فستان"
        assert definite_variants[0] == "الفستان"
        assert "فستان" in definite_variants

    def test_title_starting_with_al_still_found_by_exact_query(self) -> None:
        variants = _catalog_search_query_variants("الأسود كلاسيك")
        assert "الأسود كلاسيك" in variants
