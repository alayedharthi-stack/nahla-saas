"""Routing regressions — single-product order closure, resume, and product switch."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.stages import STAGE_DISCOVERY, STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
SHIRT = {
    "title": "قميص قطني أزرق",
    "external_id": "ext-1",
    "id": 1,
    "can_checkout": True,
    "price": 120.0,
}
SHOE = {
    "title": "حذاء رياضي أبيض",
    "external_id": "ext-2",
    "id": 2,
    "can_checkout": True,
    "price": 250.0,
}
PERFUME = {
    "title": "عطر ورد 100ml",
    "external_id": "ext-3",
    "id": 3,
    "can_checkout": True,
    "price": 180.0,
}

COMPLETION_PHRASES = [
    "أبي أكمل الطلب",
    "أبي أكمل طلبي",
    "كمل الطلب",
    "تمام كمل الطلب",
    "أبغى أتم الطلب",
    "اعتمد الطلب",
    "تابع إتمام الطلب",
    "تابع الدفع",
]

COMPLETION_VARIANTS = [
    "ابي اكمل الطلب",
    "ابغى اتم الطلب",
    "كمّل الطلب",
    "تمام كمل طلبي",
    "تابع اتمام الطلب",
    "تابع الدفع لو سمحت",
]


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(
    message: str,
    *,
    stage: str = STAGE_DISCOVERY,
    with_prep: bool = False,
    candidates: list | None = None,
    focus: dict | None = None,
) -> BrainContext:
    prep = None
    if with_prep:
        prep = OrderPreparationState(
            product_id="ext-1",
            customer_first_name=GENERIC_CUSTOMER.split()[0],
            customer_phone="966500000001",
            city="الرياض",
            order_status="awaiting_address",
        )
    state = MerchantConversationState(
        stage=stage,
        greeted=True,
        order_prep=prep,
        current_product_focus=focus if focus is not None else (dict(SHIRT) if with_prep else None),
        last_search_candidates=candidates if candidates is not None else [SHIRT, SHOE, PERFUME],
    )
    intent = rules.match(message) or Intent(
        name="general", confidence=0.5, raw_message=message,
    )
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=state,
        facts=_facts(),
    )


def _decide(message: str, **kwargs):
    return DefaultDecisionEngine().decide(_ctx(message, **kwargs))


class TestExplicitProductSwitchDuringActiveOrder:
    def test_switch_does_not_silently_keep_old_product_without_candidate(self):
        msg = "أبي حذاء رياضي أبيض بدل القميص"
        d = _decide(msg, stage=STAGE_ORDERING, with_prep=True, candidates=[SHIRT])
        prod_title = (d.args.get("product") or {}).get("title", "")
        assert d.action in {ACTION_SEARCH_PRODUCTS, ACTION_PROPOSE_DRAFT_ORDER, ACTION_CLARIFY}
        assert prod_title != "قميص قطني أزرق" or d.action == ACTION_SEARCH_PRODUCTS

    def test_switch_to_verifiable_candidate_product(self):
        msg = "أبي حذاء رياضي أبيض بدل القميص"
        d = _decide(msg, stage=STAGE_ORDERING, with_prep=True, candidates=[SHIRT, SHOE])
        assert d.action == ACTION_PROPOSE_DRAFT_ORDER
        assert d.args.get("product", {}).get("title") == "حذاء رياضي أبيض"

    def test_generic_enquiry_does_not_silently_continue_old_product(self):
        msg = "عندكم حذاء رياضي؟"
        d = _decide(msg, stage=STAGE_ORDERING, with_prep=True, candidates=[SHIRT])
        assert d.action != ACTION_PROPOSE_DRAFT_ORDER or (
            d.args.get("product", {}).get("title") != "قميص قطني أزرق"
        )
        assert d.action in {ACTION_LLM_REPLY, ACTION_CLARIFY, ACTION_SEARCH_PRODUCTS}
        if d.action == ACTION_PROPOSE_DRAFT_ORDER:
            pytest.fail("generic enquiry must not silently continue locked product")

    def test_generic_enquiry_uses_clarification_not_confirmed_switch(self):
        msg = "عندكم حذاء رياضي؟"
        d = _decide(msg, stage=STAGE_ORDERING, with_prep=True, candidates=[SHIRT])
        assert d.action == ACTION_LLM_REPLY
        assert d.args.get("topic") == "active_order_product_enquiry"
        assert d.args.get("active_product") == "قميص قطني أزرق"
        assert d.args.get("suppress_checkout") is True


class TestSearchContinuationNotCheckout:
    def test_continue_search_does_not_complete_order(self):
        for msg in (
            "كمل البحث عن المنتج",
            "تابع البحث",
            "دور لي على خيار آخر",
        ):
            d = _decide(msg, stage=STAGE_ORDERING, with_prep=True, candidates=[SHIRT])
            assert d.action != ACTION_PROPOSE_DRAFT_ORDER, f"{msg!r} must not complete order"
            assert "confirmation keyword" not in (d.reason or "")


class TestCompletionPhrasesNeverSearchProducts:
    @pytest.mark.parametrize("phrase", COMPLETION_PHRASES)
    def test_discovery_without_active_order(self, phrase: str) -> None:
        d = _decide(phrase, stage=STAGE_DISCOVERY, with_prep=False)
        assert d.action != ACTION_SEARCH_PRODUCTS, phrase
        assert d.args.get("query") not in {"أكمل الطلب", "اكمل الطلب", "أتم الطلب", "اتم الطلب"}

    @pytest.mark.parametrize("phrase", COMPLETION_PHRASES)
    def test_ordering_with_active_order(self, phrase: str) -> None:
        d = _decide(phrase, stage=STAGE_ORDERING, with_prep=True)
        assert d.action == ACTION_PROPOSE_DRAFT_ORDER, phrase
        assert d.args.get("product", {}).get("title") == "قميص قطني أزرق"
        assert d.args.get("product", {}).get("price") == 120.0

    @pytest.mark.parametrize("phrase", COMPLETION_PHRASES)
    def test_discovery_clarifies_without_active_order(self, phrase: str) -> None:
        d = _decide(phrase, stage=STAGE_DISCOVERY, with_prep=False)
        assert d.action in {ACTION_CLARIFY, ACTION_PROPOSE_DRAFT_ORDER}
        assert d.action != ACTION_SEARCH_PRODUCTS

    def test_tamam_kmel_reaches_completion_with_active_order(self):
        d = _decide("تمام كمل الطلب", stage=STAGE_ORDERING, with_prep=True)
        assert d.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "order_resume" in d.reason or "checkout" in d.reason

    @pytest.mark.parametrize("variant", COMPLETION_VARIANTS)
    def test_dialect_spelling_variants(self, variant: str) -> None:
        d = _decide(variant, stage=STAGE_DISCOVERY, with_prep=False)
        assert d.action != ACTION_SEARCH_PRODUCTS
        d_active = _decide(variant, stage=STAGE_ORDERING, with_prep=True)
        assert d_active.action == ACTION_PROPOSE_DRAFT_ORDER


class TestTrackOrderUntouched:
    def test_wen_talabi_stays_track_order(self):
        d = _decide("وين طلبي؟", stage=STAGE_DISCOVERY, with_prep=False)
        assert d.action == ACTION_TRACK_ORDER

    def test_wen_talabi_with_active_order_stays_track_order(self):
        d = _decide("وين طلبي؟", stage=STAGE_ORDERING, with_prep=True)
        assert d.action == ACTION_TRACK_ORDER


class TestProductAndPriceStableAcrossTurns:
    def test_resume_preserves_product_and_price(self):
        engine = DefaultDecisionEngine()
        ctx = _ctx("أبي أكمل الطلب", stage=STAGE_ORDERING, with_prep=True)
        first = engine.decide(ctx)
        second = engine.decide(_ctx("كمل الطلب", stage=STAGE_ORDERING, with_prep=True))
        for d in (first, second):
            assert d.action == ACTION_PROPOSE_DRAFT_ORDER
            prod = d.args.get("product") or {}
            assert prod.get("title") == "قميص قطني أزرق"
            assert prod.get("price") == 120.0
            assert prod.get("external_id") == "ext-1"
