"""AI-D06 — established structured referent is commerce evidence.

Semantic contract: quantity/size follow-ups keep a current canonical product
referent. Do not assert exact customer-facing Arabic wording.
"""
from __future__ import annotations

import inspect
import os
import sys
from typing import Any, Dict, List, Set

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.catalog_reasoning_evidence import (  # noqa: E402
    collect_catalog_reasoning_candidates,
)
from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    canonical_product_referent,
    has_structured_catalog_identity,
    set_product_focus,
)
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
    parse_price_amount,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.state.stages import STAGE_DISCOVERY  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_GENERAL,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

# Observed-case catalog rows (incident-shaped). Runtime must not special-case them.
_TALH_1KG = {
    "id": 146,
    "external_id": "sku-talh-1kg",
    "title": "عسل الطلح 1 كيلو",
    "price": 387,
    "can_checkout": True,
    "in_stock": True,
}
_TALH_500G = {
    "id": 156,
    "external_id": "sku-talh-500g",
    "title": "عسل الطلح 500 جرام",
    "price": 193,
    "can_checkout": True,
    "in_stock": True,
}
_TALH_250G = {
    "id": 142,
    "external_id": "sku-talh-250g",
    "title": "عسل الطلح 250 جرام",
    "price": 126,
    "can_checkout": True,
    "in_stock": True,
}

# Generic non-honey commerce — proves the contract is not a Talh repair.
_SHOE = {
    "id": 501,
    "external_id": "sku-white-shoe",
    "title": "حذاء رياضي أبيض",
    "price": 249,
    "can_checkout": True,
    "in_stock": True,
}
_COFFEE_1KG = {
    "id": 701,
    "external_id": "sku-coffee-1kg",
    "title": "بن محمص فاخر 1 كيلو",
    "price": 95,
    "can_checkout": True,
    "in_stock": True,
}
_COFFEE_500G = {
    "id": 702,
    "external_id": "sku-coffee-500g",
    "title": "بن محمص فاخر 500 جرام",
    "price": 55,
    "can_checkout": True,
    "in_stock": True,
}
_COFFEE_250G = {
    "id": 703,
    "external_id": "sku-coffee-250g",
    "title": "بن محمص فاخر 250 جرام",
    "price": 32,
    "can_checkout": True,
    "in_stock": True,
}
_FOREIGN_PRODUCT = {
    "id": 8801,
    "external_id": "sku-other-tenant",
    "title": "عطر ورد 100ml",
    "price": 180,
    "can_checkout": True,
    "in_stock": True,
}

_QUANTITY_FOLLOWUP = "ابي اقل من كيلو"
_SHOE_QTY_FOLLOWUP = "ابي حبة"
_THANKS = "شكرا"
_SOCIAL_LAUGH = "هههه 😄"


def _intent(message: str) -> Intent:
    return Intent(name=INTENT_GENERAL, confidence=0.55, raw_message=message)


def _state(product: Dict[str, Any] | None = None) -> MerchantConversationState:
    state = MerchantConversationState(greeted=True, stage=STAGE_DISCOVERY)
    if product is not None:
        set_product_focus(state, dict(product), reason="ai_d06_test_focus", turn=1)
    return state


def _facts(*products: Dict[str, Any]) -> CommerceFacts:
    rows = [dict(p) for p in products]
    return CommerceFacts(
        has_products=bool(rows),
        product_count=len(rows),
        in_stock_count=len(rows),
        orderable=True,
        store_name="متجر تجريبي عام",
        assistant_name="نحلة",
        top_products=list(rows),
        discovery_products=list(rows),
    )


def _ctx(
    message: str,
    *,
    state: MerchantConversationState | None = None,
    facts: CommerceFacts | None = None,
    tenant_id: int = 77,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=message,
        intent=_intent(message),
        state=state if state is not None else _state(),
        facts=facts if facts is not None else _facts(),
        profile={"inbound_metadata": {}},
    )


def _prices_from_rows(rows: List[Dict[str, Any]]) -> Set[int]:
    prices: Set[int] = set()
    for row in rows:
        amount = parse_price_amount(row.get("price"))
        if amount is not None:
            prices.add(amount)
    return prices


def _evidence(*, grounded_prices: Set[int]) -> ProductClaimGroundingEvidence:
    return ProductClaimGroundingEvidence(
        grounded_prices=frozenset(grounded_prices),
        grounded_text_corpus="",
        available_products=({"id": 1, "title": "منتج تجريبي", "can_checkout": True},),
        unavailable_products=(),
        catalog_products_this_turn=True,
        catalog_miss_this_turn=False,
        recent_catalog_miss=False,
        recent_no_synced=False,
        has_checkout_catalog=True,
        executor_product_ids=frozenset({1}),
        kb_section_ids=frozenset(),
    )


class TestActiveReferentQuantityFollowup:
    def test_structured_referent_keeps_quantity_followup_in_commerce(self) -> None:
        state = _state(_TALH_1KG)
        assert has_structured_catalog_identity(canonical_product_referent(state))
        verdict = resolve_current_turn_social_non_commerce(
            _QUANTITY_FOLLOWUP,
            intent=_intent(_QUANTITY_FOLLOWUP),
            state=state,
        )
        assert verdict.matched is False
        assert verdict.reason != "quantity_without_product_evidence"
        assert verdict.category != "quantity_without_product"

    def test_decision_does_not_block_commerce_for_quantity_followup(self) -> None:
        ctx = _ctx(
            _QUANTITY_FOLLOWUP,
            state=_state(_TALH_1KG),
            facts=_facts(_TALH_1KG, _TALH_500G, _TALH_250G),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        social_meta = (decision.args or {}).get("current_turn_social_non_commerce") or {}
        assert social_meta.get("reason") != "quantity_without_product_evidence"
        assert (decision.args or {}).get("topic") != "non_sales_ambiguous"


class TestNoReferentQuantity:
    def test_quantity_like_without_structured_referent_stays_productless(self) -> None:
        verdict = resolve_current_turn_social_non_commerce(
            _QUANTITY_FOLLOWUP,
            intent=_intent(_QUANTITY_FOLLOWUP),
            state=_state(),
        )
        assert verdict.matched is True
        assert verdict.reason == "quantity_without_product_evidence"
        assert verdict.category == "quantity_without_product"


class TestStaleHistoryOnly:
    def test_raw_history_text_does_not_revive_commerce(self) -> None:
        state = _state()
        state.last_recommended_products = []
        state.last_presented_products = [
            dict(_TALH_1KG),
            dict(_TALH_500G),
        ]
        verdict = resolve_current_turn_social_non_commerce(
            _QUANTITY_FOLLOWUP,
            intent=_intent(_QUANTITY_FOLLOWUP),
            state=state,
        )
        assert canonical_product_referent(state) is None or not has_structured_catalog_identity(
            canonical_product_referent(state)
        )
        assert verdict.reason == "quantity_without_product_evidence"

    def test_title_only_focus_is_not_a_structured_referent(self) -> None:
        state = MerchantConversationState(greeted=True, stage=STAGE_DISCOVERY)
        state.current_product_focus = {"title": "عسل الطلح 1 كيلو"}
        assert has_structured_catalog_identity(state.current_product_focus) is False
        verdict = resolve_current_turn_social_non_commerce(
            _QUANTITY_FOLLOWUP,
            intent=_intent(_QUANTITY_FOLLOWUP),
            state=state,
        )
        assert verdict.reason == "quantity_without_product_evidence"


class TestStructuredReferentOnly:
    def test_classifier_uses_canonical_focus_not_inbound_product_words(self) -> None:
        state = _state(_SHOE)
        referent = canonical_product_referent(state)
        assert referent is not None
        assert referent["id"] == _SHOE["id"]
        verdict = resolve_current_turn_social_non_commerce(
            _SHOE_QTY_FOLLOWUP,
            intent=_intent(_SHOE_QTY_FOLLOWUP),
            state=state,
        )
        assert "حذاء" not in _SHOE_QTY_FOLLOWUP
        assert verdict.matched is False
        assert verdict.reason != "quantity_without_product_evidence"


class TestAuthoritativeCatalogFacts:
    def test_incident_shaped_catalog_facts_reach_compose_evidence(self) -> None:
        state = _state(_TALH_1KG)
        rows = collect_catalog_reasoning_candidates(
            facts=_facts(_TALH_1KG, _TALH_500G, _TALH_250G),
            state=state,
        )
        prices = _prices_from_rows(rows)
        ids = {row.get("id") for row in rows}
        assert 387 in prices
        assert 193 in prices
        assert 126 in prices
        assert 146 in ids
        assert 156 in ids
        assert 142 in ids
        # Platform must not select a smaller size on behalf of Brain.
        assert canonical_product_referent(state)["id"] == 146

    def test_generic_coffee_weights_reach_compose_evidence(self) -> None:
        state = _state(_COFFEE_1KG)
        rows = collect_catalog_reasoning_candidates(
            facts=_facts(_COFFEE_1KG, _COFFEE_500G, _COFFEE_250G),
            state=state,
        )
        prices = _prices_from_rows(rows)
        assert {95, 55, 32}.issubset(prices)
        assert canonical_product_referent(state)["id"] == 701


class TestNoInventedPriceFact:
    def test_no_platform_fact_marks_200_authoritative(self) -> None:
        rows = collect_catalog_reasoning_candidates(
            facts=_facts(_TALH_1KG, _TALH_500G, _TALH_250G),
            state=_state(_TALH_1KG),
        )
        prices = _prices_from_rows(rows)
        assert 200 not in prices
        assert 387 in prices
        assert 193 in prices


class TestGroundingDefense:
    def test_ungrounded_price_not_exempted_by_social_classification(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
            return _evidence(grounded_prices={387, 193, 126})

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
            _fake_build,
        )
        result = apply_product_claim_grounding_guard(
            reply="الحجم الأصغر بسعر 200 ريال",
            tenant_id=77,
            inbound_metadata={"inbound_text": _QUANTITY_FOLLOWUP},
            order_state=_state(),
        )
        assert result.action != "allowed_social_noncommerce"
        assert result.stripped is True or result.replaced is True
        assert "200" not in result.reply

    def test_grounded_price_remains_allowed_on_social_classified_inbound(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
            return _evidence(grounded_prices={193})

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
            _fake_build,
        )
        result = apply_product_claim_grounding_guard(
            reply="المتوفر حالياً 193 ريال",
            tenant_id=77,
            inbound_metadata={"inbound_text": _QUANTITY_FOLLOWUP},
            order_state=_state(),
        )
        assert result.action in {"allowed", "allowed_catalog_product_price_fact"}
        assert "193" in result.reply


class TestSocialRegression:
    def test_thanks_without_referent_does_not_replay_catalog(self) -> None:
        ctx = _ctx(
            _THANKS,
            state=_state(),
            facts=_facts(_SHOE),
        )
        ctx.state.last_recommended_products = [dict(_SHOE), dict(_FOREIGN_PRODUCT)]
        decision = DefaultDecisionEngine().decide(ctx)
        verdict = resolve_current_turn_social_non_commerce(
            _THANKS,
            intent=ctx.intent,
            state=ctx.state,
        )
        assert verdict.matched is True
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action not in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}
        assert (decision.args or {}).get("block_commerce_escalation") is True


class TestAiD11Regression:
    def test_social_turn_without_valid_referent_stays_non_commerce(self) -> None:
        state = _state()
        state.last_recommended_products = [dict(_SHOE), dict(_FOREIGN_PRODUCT)]
        ctx = _ctx(_SOCIAL_LAUGH, state=state, facts=_facts(_SHOE, _FOREIGN_PRODUCT))
        decision = DefaultDecisionEngine().decide(ctx)
        verdict = resolve_current_turn_social_non_commerce(
            _SOCIAL_LAUGH,
            intent=ctx.intent,
            state=state,
        )
        assert verdict.matched is True
        assert verdict.reason != "quantity_without_product_evidence"
        assert decision.action not in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}
        assert (decision.args or {}).get("block_commerce_escalation") is True


class TestTenantIsolation:
    def test_catalog_facts_stay_on_the_active_tenant_snapshot(self) -> None:
        state = _state(_SHOE)
        rows = collect_catalog_reasoning_candidates(
            facts=_facts(_SHOE),
            state=state,
        )
        ids = {row.get("id") for row in rows}
        prices = _prices_from_rows(rows)
        assert 501 in ids
        assert 8801 not in ids
        assert 180 not in prices
        assert canonical_product_referent(state)["id"] == 501


class TestGenericNonHoney:
    def test_quantity_followup_on_shoe_referent_is_not_productless(self) -> None:
        state = _state(_SHOE)
        verdict = resolve_current_turn_social_non_commerce(
            _SHOE_QTY_FOLLOWUP,
            intent=_intent(_SHOE_QTY_FOLLOWUP),
            state=state,
        )
        assert verdict.matched is False
        assert canonical_product_referent(state)["title"] == "حذاء رياضي أبيض"

    def test_coffee_quantity_followup_without_repeating_product_name(self) -> None:
        state = _state(_COFFEE_1KG)
        verdict = resolve_current_turn_social_non_commerce(
            _QUANTITY_FOLLOWUP,
            intent=_intent(_QUANTITY_FOLLOWUP),
            state=state,
        )
        assert "بن" not in _QUANTITY_FOLLOWUP
        assert verdict.matched is False
        assert verdict.reason != "quantity_without_product_evidence"


class TestNoScriptedIntelligence:
    def test_runtime_owners_have_no_size_price_or_tenant_special_case(self) -> None:
        from modules.ai.brain import current_turn_social_non_commerce as social_mod
        from modules.ai.brain.commerce import catalog_reasoning_evidence
        from modules.ai.brain.postprocess import product_claim_grounding_guard as pcgg

        helper_src = inspect.getsource(social_mod._has_canonical_structured_product_referent)
        collect_src = inspect.getsource(catalog_reasoning_evidence.collect_catalog_reasoning_candidates)
        guard_src = inspect.getsource(pcgg.apply_product_claim_grounding_guard)
        for src in (helper_src, collect_src, guard_src):
            lowered = src.lower()
            assert "500g" not in lowered
            assert "193" not in src
            assert "اقل من كيلو" not in src
            assert "نصف كيلو" not in src
            assert "tenant_id == 33" not in src
            assert "tenant_id=33" not in src
            assert "talh" not in lowered
            assert "طلح" not in src
            assert "33" not in helper_src
