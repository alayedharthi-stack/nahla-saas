"""Phase 4B — selection context follow-ups after discovery presentation."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_objective import COMMERCE_OBJECTIVE_DISCOVERY
from modules.ai.brain.commerce.conversational_priority import (
    positive_commerce_signal,
    try_absence_non_sales_decision,
)
from modules.ai.brain.commerce.selection_context import (
    find_larger_sizes,
    find_matching_variants,
    resolve_selection_context,
    stamp_selection_context_from_products,
    try_selection_context_decision,
)
from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent import rules
from modules.ai.brain.product_discovery_gate import product_discovery_block_reason
from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState

PRESENTED = [
    {
        "id": "101",
        "external_id": "101",
        "title": "Alpha Blend 500g",
        "display_label": "\u0645\u0646\u062a\u062c \u0623\u0644\u0641\u0627 500 \u062c\u0645",
        "price": 200,
        "category": "category_a",
    },
    {
        "id": "102",
        "external_id": "102",
        "title": "Alpha Blend 1kg",
        "display_label": "\u0645\u0646\u062a\u062c \u0623\u0644\u0641\u0627 1 \u0643\u064a\u0644\u0648",
        "price": 387,
        "category": "category_a",
    },
    {
        "id": "103",
        "external_id": "103",
        "title": "Beta Reserve 500g",
        "display_label": "\u0645\u0646\u062a\u062c \u0628\u064a\u062a\u0627 500 \u062c\u0645",
        "price": 210,
        "category": "category_a",
    },
    {
        "id": "104",
        "external_id": "104",
        "title": "Beta Reserve 1kg",
        "display_label": "\u0645\u0646\u062a\u062c \u0628\u064a\u062a\u0627 1 \u0643\u064a\u0644\u0648",
        "price": 425,
        "category": "category_a",
    },
]

BROWSE_POOL = PRESENTED + [
    {
        "id": "105",
        "external_id": "105",
        "title": "Alpha Blend 5kg",
        "price": 1800,
        "category": "category_a",
    },
]

MSG_SIZE = "\u0641\u064a\u0647 \u0628\u0627\u0644\u0643\u064a\u0644\u0648\u061f"
MSG_FIRST = "\u0623\u0628\u063a\u0649 \u0627\u0644\u0623\u0648\u0644"
MSG_SECOND = "\u0623\u0628\u063a\u0649 \u0627\u0644\u062b\u0627\u0646\u064a"
MSG_PRICE_FIRST = "\u0643\u0645 \u0633\u0639\u0631 \u0627\u0644\u0623\u0648\u0644\u061f"
MSG_NAME = "\u0623\u0628\u063a\u0649 \u0623\u0644\u0641\u0627"
MSG_SAME_KG = "\u0623\u0628\u063a\u0649 \u0646\u0641\u0633\u0647 \u0644\u0643\u0646 \u0643\u064a\u0644\u0648"
MSG_BIGGER = "\u0641\u064a\u0647 \u0623\u0643\u0628\u0631\u061f"
MSG_ALT_SIZE = "\u0641\u064a\u0647 \u062d\u062c\u0645 \u062b\u0627\u0646\u064a\u061f"
MSG_PACK = "\u0641\u064a\u0647 \u0639\u0628\u0648\u0629 \u0623\u0643\u0628\u0631\u061f"
MSG_IDENTITY = "\u0645\u0646 \u0627\u0646\u062a"
MSG_PAYMENT = "\u0643\u064a\u0641 \u0627\u062f\u0641\u0639"
MSG_TRACKING = "\u0648\u064a\u0646 \u0637\u0644\u0628\u064a\u0629"


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=30,
        in_stock_count=30,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
        top_products=PRESENTED[:2],
    )


def _browse_state(*, selected_id: str = "") -> MerchantConversationState:
    state = MerchantConversationState(
        greeted=True,
        stage="discovery",
        turn=4,
        commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        last_browse_query="category_a",
        catalog_browse_pool=list(BROWSE_POOL),
    )
    stamp_selection_context_from_products(
        state,
        products=PRESENTED,
        selected_collection="primary_category",
        discovery_mode="collections_first",
    )
    state.last_search_candidates = list(PRESENTED)
    if selected_id:
        state.selected_product_id = selected_id
    return state


def _ctx(msg: str, *, state: MerchantConversationState | None = None) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        intent=intent,
        state=state or _browse_state(),
        facts=_facts(),
    )


class TestSelectionContextResolver:
    def test_size_filter_from_presented_pool(self) -> None:
        ctx = _ctx(MSG_SIZE)
        resolution = resolve_selection_context(ctx)
        assert resolution is not None
        assert resolution.kind == "size_availability"
        titles = [p["title"] for p in resolution.products or []]
        assert "Alpha Blend 1kg" in titles
        assert "Beta Reserve 1kg" in titles
        assert all("500g" not in t for t in titles)

    def test_ordinal_first_selects_list_head(self) -> None:
        ctx = _ctx(MSG_FIRST)
        decision = try_selection_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args["product"]["id"] == "101"

    def test_ordinal_second_selects_second_product(self) -> None:
        ctx = _ctx(MSG_SECOND)
        decision = try_selection_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args["product"]["id"] == "102"

    def test_price_ordinal_uses_presented_list(self) -> None:
        ctx = _ctx(MSG_PRICE_FIRST)
        decision = try_selection_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "selection_context_price"
        assert decision.args["product"]["id"] == "101"

    def test_name_pick_resolves_against_presented_products(self) -> None:
        ctx = _ctx(MSG_NAME)
        decision = try_selection_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert len(decision.args.get("products") or []) >= 2

    def test_same_product_kg_after_selection(self) -> None:
        ctx = _ctx(MSG_SAME_KG, state=_browse_state(selected_id="101"))
        decision = try_selection_context_decision(ctx)
        assert decision is not None
        assert decision.action in {ACTION_PROPOSE_DRAFT_ORDER, ACTION_SEARCH_PRODUCTS}
        if decision.action == ACTION_PROPOSE_DRAFT_ORDER:
            assert decision.args["product"]["id"] == "102"

    def test_larger_size_from_family(self) -> None:
        ctx = _ctx(MSG_BIGGER, state=_browse_state(selected_id="101"))
        decision = try_selection_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        titles = [p["title"] for p in decision.args.get("products") or []]
        assert any("1kg" in t or "5kg" in t for t in titles)

    def test_catalog_helpers_are_evidence_based(self) -> None:
        reference = PRESENTED[0]
        larger = find_larger_sizes(reference, BROWSE_POOL)
        assert any(p["id"] == "102" for p in larger)
        kg_matches = find_matching_variants(BROWSE_POOL, message="1 kg")
        assert len(kg_matches) >= 2


class TestSelectionContextEngineIntegration:
    def test_size_question_not_blocked_as_weak_intent(self) -> None:
        ctx = _ctx(MSG_SIZE)
        assert product_discovery_block_reason(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert str(decision.args.get("source") or "").startswith("selection_context")

    def test_size_question_not_social_fallback(self) -> None:
        ctx = _ctx(MSG_SIZE)
        assert positive_commerce_signal(ctx.message, state=ctx.state) is True
        assert try_absence_non_sales_decision(ctx) is None

    def test_alt_size_follow_up(self) -> None:
        ctx = _ctx(MSG_ALT_SIZE, state=_browse_state(selected_id="101"))
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert str(decision.args.get("source") or "").startswith("selection_context")

    def test_packaging_follow_up(self) -> None:
        ctx = _ctx(MSG_PACK, state=_browse_state(selected_id="101"))
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_SEARCH_PRODUCTS, ACTION_CLARIFY}


class TestSelectionContextIsolation:
    def test_identity_flow_without_selection_context(self) -> None:
        state = MerchantConversationState(greeted=True, stage="discovery")
        ctx = _ctx(MSG_IDENTITY, state=state)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert try_selection_context_decision(ctx) is None

    def test_payment_flow_without_selection_context(self) -> None:
        state = MerchantConversationState(greeted=True, stage="discovery")
        ctx = _ctx(MSG_PAYMENT, state=state)
        assert try_selection_context_decision(ctx) is None

    def test_tracking_flow_without_selection_context(self) -> None:
        state = MerchantConversationState(greeted=True, stage="checkout")
        ctx = _ctx(MSG_TRACKING, state=state)
        assert try_selection_context_decision(ctx) is None
