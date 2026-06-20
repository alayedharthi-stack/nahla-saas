"""P0 - discovery browse sequence: start order -> global browse -> category."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_objective import (
    COMMERCE_OBJECTIVE_DISCOVERY,
    get_commerce_objective,
    update_commerce_objective,
)
from modules.ai.brain.commerce.conversational_priority import (
    positive_commerce_signal,
    try_absence_non_sales_decision,
)
from modules.ai.brain.commerce.discovery_strategy import DiscoveryMode
from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.discovery.entry import (
    CATEGORY_BROWSE,
    GLOBAL_BROWSE,
    START_ORDER_BARE,
    resolve_discovery_entry,
)
from modules.ai.brain.intent import rules
from modules.ai.brain.product_discovery_gate import product_discovery_block_reason
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

MSG_START = "\u0627\u0628\u064a \u0627\u0637\u0644\u0628"
MSG_BROWSE = "\u0648\u0634 \u0639\u0646\u062f\u0643\u0645"
MSG_HONEY = "\u0639\u0633\u0644"
MSG_IDENTITY = "\u0645\u0646 \u0627\u0646\u062a"
MSG_PRICE = "\u0643\u0645 \u0633\u0639\u0631 \u0627\u0644\u0643\u064a\u0644\u0648\u061f"
GUIDED_Q = "\u0648\u0634 \u0646\u0648\u0639 \u0627\u0644\u0645\u0646\u062a\u062c \u0627\u0644\u0644\u064a \u062a\u062f\u0648\u0631 \u0639\u0644\u064a\u0647"


def _facts(*, has_products: bool = True) -> CommerceFacts:
    return CommerceFacts(
        has_products=has_products,
        product_count=30 if has_products else 0,
        in_stock_count=30 if has_products else 0,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
        top_products=[
            {"title": "honey1", "external_id": "1", "price": 120, "category": "honey"},
            {"title": "honey2", "external_id": "2", "price": 130, "category": "honey"},
        ],
    )


def _ctx(msg: str, *, state: MerchantConversationState | None = None) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=_facts(),
    )


class TestDiscoveryBrowseSequenceRegression:
    def test_start_order_sets_discovery_objective(self) -> None:
        ctx = _ctx(MSG_START)
        entry = resolve_discovery_entry(ctx)
        assert entry.entry_type == START_ORDER_BARE
        update_commerce_objective(ctx, entry)
        assert get_commerce_objective(ctx.state) == COMMERCE_OBJECTIVE_DISCOVERY

    def test_wesh_aindakom_routes_global_browse_not_absence_gate(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        )
        ctx = _ctx(MSG_BROWSE, state=state)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        assert entry.entry_type == GLOBAL_BROWSE
        assert positive_commerce_signal(ctx.message, intent_name=ctx.intent.name, state=state)
        assert try_absence_non_sales_decision(ctx) is None

    def test_wesh_aindakom_decision_is_catalog_search_not_guided_clarify(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        )
        ctx = _ctx(MSG_BROWSE, state=state)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != ACTION_CLARIFY
        assert decision.args.get("discovery_mode") == DiscoveryMode.DIRECT_CATALOG.value
        question = str(decision.args.get("question") or "")
        assert GUIDED_Q not in question

    def test_asal_after_discovery_is_category_browse(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            last_discovery_mode=DiscoveryMode.DIRECT_CATALOG.value,
        )
        ctx = _ctx(MSG_HONEY, state=state)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        assert entry.entry_type == CATEGORY_BROWSE
        assert entry.query == MSG_HONEY
        assert product_discovery_block_reason(ctx) is None
        assert try_absence_non_sales_decision(ctx) is None

    def test_asal_decision_is_catalog_search_not_social_fallback(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            last_discovery_mode=DiscoveryMode.DIRECT_CATALOG.value,
        )
        ctx = _ctx(MSG_HONEY, state=state)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != ACTION_LLM_REPLY
        assert decision.args.get("topic") != "non_sales_ambiguous"
        assert decision.args.get("query") == MSG_HONEY

    def test_identity_still_bypasses_discovery(self) -> None:
        decision = DefaultDecisionEngine().decide(_ctx(MSG_IDENTITY))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "persona_identity"

    def test_price_still_bypasses_discovery(self) -> None:
        ctx = _ctx(MSG_PRICE)
        ctx.intent = Intent(name="ask_price", confidence=0.9, raw_message=ctx.message)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_CLARIFY
        assert decision.action != ACTION_SEARCH_PRODUCTS