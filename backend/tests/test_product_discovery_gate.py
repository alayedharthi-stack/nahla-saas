"""
tests/test_product_discovery_gate.py
────────────────────────────────────
Regression: weak/ambiguous turns must not trigger top_products or
unrelated catalog recommendations.
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.order_context_gate import should_block_product_discovery
from modules.ai.brain.product_discovery_gate import (
    _extract_price_subject,
    _is_unit_only_price_message,
    _resolved_product_query,
    allows_top_products_decision,
    has_explicit_broad_browse_request,
    is_price_without_product_context,
    product_discovery_block_reason,
    should_suppress_recommendation_escalation,
    try_price_query_decision,
)
from modules.ai.brain.state.stages import STAGE_ORDERING
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_PRODUCT = {
    "title": "عسل سدر",
    "external_id": "ext-honey-1",
    "id": 101,
    "can_checkout": True,
}
_MAPS = "https://maps.app.goo.gl/abc123test"


def _active_order_ctx(message: str, *, intent_name: str = "general") -> BrainContext:
    prep = OrderPreparationState(
        product_id="ext-honey-1",
        customer_first_name="محمد",
        city="الرياض",
        order_status="awaiting_address",
    )
    state = MerchantConversationState(
        stage=STAGE_ORDERING,
        greeted=True,
        order_prep=prep,
        current_product_focus=dict(_PRODUCT),
    )
    return BrainContext(
        tenant_id=99,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.55, raw_message=message),
        state=state,
        facts=CommerceFacts(
            has_products=True,
            product_count=1,
            orderable=True,
            top_products=[dict(_PRODUCT)],
            discovery_products=[dict(_PRODUCT)],
        ),
    )


class TestProductDiscoveryGate:
    def test_active_order_location_blocks_top_products(self):
        msg = f"{_MAPS}\nأبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg)
        assert product_discovery_block_reason(ctx) == "active_fulfillment"
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_kilo_price_without_product_clarifies(self):
        msg = "كم سعر الكيلو؟"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="ask_price", confidence=0.9, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_price_without_product_context(ctx)
        decision = try_price_query_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_CLARIFY
        full = DefaultDecisionEngine().decide(ctx)
        assert full.action == ACTION_CLARIFY
        assert full.action != ACTION_SEARCH_PRODUCTS

    def test_kilo_price_with_active_product_uses_focus(self):
        msg = "كم سعر الكيلو؟"
        ctx = _active_order_ctx(msg, intent_name="ask_price")
        assert not is_price_without_product_context(ctx)
        decision = try_price_query_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "price"

    def test_unknown_message_suppresses_recommendations(self):
        assert should_suppress_recommendation_escalation(
            message="تمام",
            brain_state={"stage": "discovery"},
            intent_name="general",
        )

    def test_explicit_browse_allows_top_products(self):
        msg = "وش عندكم؟"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.6, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert has_explicit_broad_browse_request(msg)
        assert allows_top_products_decision(ctx, source="top_products", message=msg)

    def test_show_products_bare_imperative_is_explicit_browse(self):
        for msg in ("عرض المنتجات", "اعرض المنتجات"):
            assert has_explicit_broad_browse_request(msg), msg
            assert not has_explicit_broad_browse_request("تعرض المنتجات في القائمة")

    def test_show_more_requires_prior_browse_context(self):
        msg = "وريني باقي الخيارات"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.7, raw_message=msg),
            state=MerchantConversationState(
                greeted=True,
                last_search_candidates=[dict(_PRODUCT)],
            ),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert product_discovery_block_reason(ctx, source="show_more") is None

    def test_show_more_without_pool_blocked(self):
        msg = "وريني باقي الخيارات"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.7, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert product_discovery_block_reason(ctx, source="show_more") == (
            "weak_or_unknown_intent"
        )


class TestPriceAskProductExtraction:
    """P0-A: product + price-ask + unit must resolve to catalog search."""

    def _price_ctx(self, message: str) -> BrainContext:
        from modules.ai.brain.intent import rules

        intent = rules.match(message)
        assert intent is not None
        assert intent.name == "ask_price"
        return BrainContext(
            tenant_id=42,
            customer_phone="966500000099",
            message=message,
            intent=intent,
            state=MerchantConversationState(greeted=True, stage="discovery"),
            facts=CommerceFacts(has_products=True, orderable=True, product_count=5),
        )

    def test_extract_product_before_bkm_unit(self):
        assert _extract_price_subject("عسل الطلح بكم الكيلو") == "عسل الطلح"

    def test_extract_product_cross_vertical(self):
        assert _extract_price_subject("قميص قطن بكم") == "قميص قطن"
        assert _extract_price_subject("coffee beans how much per kg") == "coffee beans"

    def test_bare_kilo_price_still_unit_only(self):
        assert _extract_price_subject("كم سعر الكيلو؟") == ""
        assert _is_unit_only_price_message("كم سعر الكيلو؟")

    def test_product_bkm_unit_not_without_context(self):
        ctx = self._price_ctx("عسل الطلح بكم الكيلو")
        assert _resolved_product_query(ctx) == "عسل الطلح"
        assert not is_price_without_product_context(ctx)
        assert try_price_query_decision(ctx) is None

    def test_product_bkm_unit_routes_search_products(self):
        ctx = self._price_ctx("قميص رجالي بكم الكيلو")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("query") == "قميص رجالي"
        assert decision.action != ACTION_CLARIFY
        assert decision.action != ACTION_LLM_REPLY

    def test_confirmed_regression_case_routes_search(self):
        ctx = self._price_ctx("عسل الطلح بكم الكيلو")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("query") == "عسل الطلح"


class TestPriceAskReplayValidation:
    """P0-A replay matrix — product+price shapes vs bare unit-only asks."""

    _SEARCH_CASES = (
        ("عسل الطلح بكم", "عسل الطلح"),
        ("عسل الطلح كم سعره", "عسل الطلح"),
        ("سمر 1447 بكم", "سمر 1447"),
        ("بكم كيلو الطلح", "كيلو الطلح"),
        ("كم سعر عسل الطلح", "عسل الطلح"),
        ("عسل الطلح بكم الكيلو", "عسل الطلح"),
        ("قميص رجالي بكم الكيلو", "قميص رجالي"),
    )

    _CLARIFY_CASES = (
        "كم سعر الكيلو",
        "بكم",
        "السعر كم",
    )

    def _ctx(self, message: str) -> BrainContext:
        from modules.ai.brain.intent import rules

        intent = rules.match(message)
        if intent is None:
            intent = Intent(name="general", confidence=0.5, raw_message=message)
        return BrainContext(
            tenant_id=42,
            customer_phone="966500000099",
            message=message,
            intent=intent,
            state=MerchantConversationState(greeted=True, stage="discovery"),
            facts=CommerceFacts(has_products=True, orderable=True, product_count=5),
        )

    @pytest.mark.parametrize("message,expected_query", _SEARCH_CASES)
    def test_product_plus_price_routes_search(self, message, expected_query):
        assert _extract_price_subject(message) == expected_query
        decision = DefaultDecisionEngine().decide(self._ctx(message))
        assert decision.action == ACTION_SEARCH_PRODUCTS, (
            f"{message!r} -> {decision.action} ({decision.reason})"
        )
        assert decision.args.get("query") == expected_query

    @pytest.mark.parametrize("message", _CLARIFY_CASES)
    def test_bare_price_ask_still_clarifies(self, message):
        assert _extract_price_subject(message) == ""
        decision = DefaultDecisionEngine().decide(self._ctx(message))
        assert decision.action == ACTION_CLARIFY, (
            f"{message!r} -> {decision.action} ({decision.reason})"
        )
        assert decision.action != ACTION_SEARCH_PRODUCTS


class TestInquiryRoutingSplit:
    """Broad category inquiry must route to catalog-grounded search."""

    def _ctx(self, message: str, *, intent_name: str = "ask_product") -> BrainContext:
        return BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=message,
            intent=Intent(name=intent_name, confidence=0.82, raw_message=message),
            state=MerchantConversationState(greeted=True, stage="discovery"),
            facts=CommerceFacts(has_products=True, orderable=True),
        )

    def test_broad_inquiry_honey_routes_open_llm(self):
        msg = "أبغى الاستفسار عن العسل"
        decision = DefaultDecisionEngine().decide(self._ctx(msg))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "open_category_inquiry"
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_broad_inquiry_types_still_routes_catalog_search(self):
        msg = "أبغى أعرف أنواع العطور"
        decision = DefaultDecisionEngine().decide(self._ctx(msg))
        assert decision.action in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}
        if decision.action == ACTION_SEARCH_PRODUCTS:
            assert decision.args.get("source") == "category_browse"

    def test_broad_inquiry_cross_vertical_open_llm(self):
        for msg in (
            "أبغى الاستفسار عن الجوالات",
            "أبغى الاستفسار عن الملابس",
            "أريد معرفة التمر",
            "أبغى الاستفسار عن القهوة",
        ):
            decision = DefaultDecisionEngine().decide(self._ctx(msg))
            assert decision.action == ACTION_LLM_REPLY, msg
            assert decision.args.get("topic") == "open_category_inquiry", msg

    def test_specific_product_name_still_searches(self):
        msg = "عسل سدر طيب"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(
                name="ask_product",
                confidence=0.82,
                raw_message=msg,
                slots={"product_query": msg},
            ),
            state=MerchantConversationState(greeted=True, stage="discovery"),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_narrowing_after_candidates_still_searches(self):
        msg = "سدر"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(
                name="ask_product",
                confidence=0.82,
                raw_message=msg,
                slots={"product_query": msg},
            ),
            state=MerchantConversationState(
                greeted=True,
                stage="discovery",
                last_search_candidates=[dict(_PRODUCT)],
            ),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_classify_open_category_inquiry(self):
        from modules.ai.brain.product_discovery_gate import (
            INQUIRY_CLASS_OPEN,
            classify_product_inquiry_route,
        )

        inquiry_class, route = classify_product_inquiry_route(
            self._ctx("أبغى الاستفسار عن العسل"), query="العسل",
        )
        assert inquiry_class == INQUIRY_CLASS_OPEN
        assert route == "llm"


class TestTypesOverviewFollowUp:
    """Types/options ask must beat stale availability browse context."""

    _TYPES_SAMAR = "\u0648\u0634 \u0623\u0646\u0648\u0627\u0639 \u0627\u0644\u0633\u0645\u0631 \u0639\u0646\u062f\u0643\u0645\u061f"

    def test_extract_types_overview_query_strips_storefront_tail(self):
        from modules.ai.brain.product_discovery_gate import extract_types_overview_query

        assert extract_types_overview_query(self._TYPES_SAMAR) == "\u0633\u0645\u0631"

    def test_classify_types_ask_beats_prior_browse_context(self):
        from modules.ai.brain.product_discovery_gate import (
            INQUIRY_CLASS_BROAD,
            classify_product_inquiry_route,
        )
        from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState

        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=self._TYPES_SAMAR,
            intent=Intent(name="general", confidence=0.5, raw_message=self._TYPES_SAMAR),
            state=MerchantConversationState(
                greeted=True,
                stage="discovery",
                last_browse_query="\u0627\u0644\u0633\u0645\u0631",
                catalog_browse_pool=[{"id": 1, "title": "sample"}],
            ),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        inquiry_class, route = classify_product_inquiry_route(
            ctx, query="\u0633\u0645\u0631",
        )
        assert inquiry_class == INQUIRY_CLASS_BROAD
        assert route == "search"

    def test_types_ask_after_availability_routes_catalog_search(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState

        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=self._TYPES_SAMAR,
            intent=Intent(name="general", confidence=0.5, raw_message=self._TYPES_SAMAR),
            state=MerchantConversationState(
                greeted=True,
                stage="discovery",
                last_browse_query="\u0627\u0644\u0633\u0645\u0631",
                last_question_asked="\u062a\u0628\u064a \u0627\u0644\u0623\u0633\u0639\u0627\u0631 \u0648\u0627\u0644\u0623\u062d\u062c\u0627\u0645\u061f",
                catalog_browse_pool=[{"id": 11, "title": "sample samar"}],
            ),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}
        if decision.action == ACTION_SEARCH_PRODUCTS:
            assert decision.args.get("source") == "category_browse"
            assert decision.args.get("query") == "\u0633\u0645\u0631"
