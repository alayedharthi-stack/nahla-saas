"""CatalogNavigator ownership — groups navigation, owner lock, exit boundaries."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.navigation import (  # noqa: E402
    PATH_GROUPS,
    PATH_GROUP_PRODUCTS,
    PATH_TOP_FALLBACK,
    try_catalog_navigation_decision,
)
from modules.ai.brain.catalog.navigation_signals import (  # noqa: E402
    evaluate_catalog_navigation_signals,
    is_phantom_category_scope,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.catalog_navigate import CatalogNavigateHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

MSG_START = "\u0627\u0628\u064a \u0627\u0637\u0644\u0628"
MSG_BROWSE = "\u0648\u0634 \u0639\u0646\u062f\u0643\u0645"
MSG_TYPES = "\u0648\u0634 \u0627\u0644\u0627\u0646\u0648\u0627\u0639 \u0627\u0644\u064a \u0639\u0646\u062f\u0643\u0645"
MSG_DETAILS = "\u0627\u0628\u064a \u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u0645\u062a\u0648\u0641\u0631 \u0639\u0646\u062f\u0643\u0645"
MSG_BACK = "\u0631\u062c\u0639\u0646\u064a \u0644\u0644\u0623\u0642\u0633\u0627\u0645"
MSG_SWITCH = "\u0627\u0628\u064a \u0645\u062c\u0645\u0648\u0639\u0629 \u062b\u0627\u0646\u064a\u0629"
MSG_COMPARE = "\u0648\u0634 \u0627\u0644\u0641\u0631\u0642 \u0628\u064a\u0646 \u0627\u0644\u0642\u0633\u0645 \u0627\u0644\u0627\u0648\u0644 \u0648\u0627\u0644\u062b\u0627\u0646\u064a"
MSG_ADVICE = "\u0648\u0634 \u062a\u0646\u0635\u062d\u0646\u064a"
MSG_SUITABLE = "\u0623\u064a\u0647\u0645 \u0623\u0646\u0633\u0628"
MSG_INFO = "\u0647\u0644 \u0647\u0648 \u062e\u0627\u0645"
MSG_SHIPPING = "\u0648\u064a\u0646 \u0637\u0644\u0628\u064a"
MSG_SUPPORT = "\u0628\u063a\u064a \u0627\u0643\u0644\u0645 \u0645\u0648\u0638\u0641"
MSG_PRODUCT = "\u0627\u0628\u064a \u0639\u0633\u0644 \u0633\u062f\u0631 1 \u0643\u064a\u0644\u0648"

COLLECTIONS = [
    {"group_id": "honey", "group_name": "\u0627\u0644\u0639\u0633\u0644", "browse_rank": 1},
    {"group_id": "oils", "group_name": "\u0627\u0644\u0632\u064a\u0648\u062a", "browse_rank": 2},
    {"group_id": "gifts", "group_name": "\u0627\u0644\u0647\u062f\u0627\u064a\u0627", "browse_rank": 3},
]

HONEY_PRODUCTS = [
    {"id": "1", "external_id": "1", "title": "Talh 1kg", "category": "Honey", "price": 220},
    {"id": "2", "external_id": "2", "title": "Sidr 1kg", "category": "Honey", "price": 250},
]

GROUPS_REPLY = "\u0627\u0644\u0623\u0642\u0633\u0627\u0645 \u0627\u0644\u0645\u062a\u0648\u0641\u0631\u0629:\n\n1. \u0627\u0644\u0639\u0633\u0644\n2. \u0627\u0644\u0632\u064a\u0648\u062a"


def _facts(*, product_count: int = 30, store_url: str = "", maps_url: str = "") -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=product_count,
        in_stock_count=product_count,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
        store_url=store_url,
        maps_url=maps_url,
        top_products=HONEY_PRODUCTS,
    )


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
    db: Any = None,
    intent_name: str | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(
            name=intent_name or "general",
            confidence=0.5,
            raw_message=msg,
        )
    ctx = BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(),
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _collections_state(*, selected: str = "") -> MerchantConversationState:
    state = MerchantConversationState(
        greeted=True,
        stage="discovery",
        turn=5,
        last_presented_collections=list(COLLECTIONS),
        selection_context_turn=4,
        catalog_navigation_source="groups",
    )
    state.selected_collection = selected
    return state


def _mock_plan_collections():
    from modules.ai.brain.catalog.catalog_intelligence import CatalogGroup, DiscoveryPlan  # noqa: PLC0415
    from modules.ai.brain.commerce.discovery_strategy import DiscoveryMode, DiscoveryStrategyResult  # noqa: E402

    groups = [
        CatalogGroup(group_id="honey", group_name="\u0627\u0644\u0639\u0633\u0644", browse_rank=1),
        CatalogGroup(group_id="oils", group_name="\u0627\u0644\u0632\u064a\u0648\u062a", browse_rank=2),
    ]
    plan = DiscoveryPlan(output_kind="collections", collections=groups)
    strategy = DiscoveryStrategyResult(mode=DiscoveryMode.COLLECTIONS_FIRST, initial_count=3)
    return plan, strategy, MagicMock()


class TestCatalogNavigatorSignals:
    def test_phantom_scope_stopword(self):
        assert is_phantom_category_scope("\u0627\u0644\u064a") is True
        assert is_phantom_category_scope("\u0639\u0633\u0644 \u0633\u062f\u0631") is False

    def test_advisory_exits_navigator(self):
        ctx = _ctx(MSG_ADVICE, state=_collections_state())
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.advisory_or_comparison is True

    def test_comparison_exits_navigator(self):
        ctx = _ctx(MSG_COMPARE, state=_collections_state())
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.advisory_or_comparison is True


class TestCatalogNavigatorDecisions:
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_general_browse_returns_owned_groups(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_BROWSE))
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args["chosen_path"] == PATH_GROUPS
        assert decision.args["owner_locked"] is True

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_start_order_bare_does_not_claim_groups(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_START))
        assert decision is None

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_types_overview_browse_returns_groups(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_TYPES))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUPS

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_availability_details_browse_returns_groups(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_DETAILS))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUPS

    def test_group_pick_by_number(self):
        decision = try_catalog_navigation_decision(_ctx("1", state=_collections_state()))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUP_PRODUCTS
        assert decision.args["catalog_group_id"] == "honey"

    def test_group_pick_by_name(self):
        decision = try_catalog_navigation_decision(
            _ctx("\u0627\u0644\u0639\u0633\u0644", state=_collections_state()),
        )
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUP_PRODUCTS

    def test_back_to_groups(self):
        state = _collections_state(selected="honey")
        state.last_presented_group_products = HONEY_PRODUCTS
        decision = try_catalog_navigation_decision(_ctx(MSG_BACK, state=state))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUPS

    def test_switch_group(self):
        decision = try_catalog_navigation_decision(
            _ctx(MSG_SWITCH, state=_collections_state(selected="honey")),
        )
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_GROUPS

    def test_product_information_blocked(self):
        decision = try_catalog_navigation_decision(_ctx(MSG_INFO))
        assert decision is None

    def test_shipping_blocked(self):
        ctx = _ctx(MSG_SHIPPING, intent_name="track_order")
        decision = try_catalog_navigation_decision(ctx)
        assert decision is None

    def test_support_blocked(self):
        ctx = _ctx(MSG_SUPPORT, intent_name="talk_human")
        decision = try_catalog_navigation_decision(ctx)
        assert decision is None

    def test_specific_product_not_auto_groups(self):
        decision = try_catalog_navigation_decision(_ctx(MSG_PRODUCT))
        assert decision is None

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=[])
    def test_no_groups_top_fallback(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_BROWSE))
        assert decision is not None
        assert decision.args["chosen_path"] == PATH_TOP_FALLBACK
        assert decision.args.get("navigator_no_groups_fallback") is True

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_advice_after_groups_not_navigator(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_ADVICE, state=_collections_state()))
        assert decision is None

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_comparison_after_groups_not_navigator(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_COMPARE, state=_collections_state()))
        assert decision is None

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_suitability_after_groups_not_navigator(self, _mock_groups):
        decision = try_catalog_navigation_decision(_ctx(MSG_SUITABLE, state=_collections_state()))
        assert decision is None


class TestCatalogNavigatorGuards:
    def test_catalog_grounding_guard_allows_groups_path(self):
        result = apply_catalog_product_grounding_guard(
            reply=GROUPS_REPLY,
            inbound_text=MSG_BROWSE,
            chosen_path=PATH_GROUPS,
        )
        assert result.replaced is False
        assert result.action == "allowed"

    def test_catalog_grounding_guard_blocks_rewrite_on_groups(self):
        polluted = GROUPS_REPLY + "\n\u0639\u0633\u0644 \u0645\u062e\u062a\u0644\u0637 \u063a\u064a\u0631 \u0645\u0648\u062c\u0648\u062f"
        result = apply_catalog_product_grounding_guard(
            reply=polluted,
            inbound_text=MSG_BROWSE,
            chosen_path=PATH_GROUPS,
        )
        assert result.replaced is False

    def test_product_claim_guard_allows_navigator_path(self):
        result = apply_product_claim_grounding_guard(
            reply=GROUPS_REPLY,
            chosen_path=PATH_GROUPS,
        )
        assert result.replaced is False


class TestCatalogNavigatorEngine:
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_engine_routes_browse_before_llm(self, _mock_groups):
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx(MSG_BROWSE))
        assert decision.action == ACTION_CATALOG_NAVIGATE

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_engine_routes_bare_start_to_channel_selection(self, _mock_groups):
        engine = DefaultDecisionEngine()
        ctx = _ctx(
            MSG_START,
            db=MagicMock(),
            intent_name="start_order",
        )
        ctx.facts = _facts(
            store_url="https://shop.example",
            maps_url="https://maps.example.com/showroom",
        )
        decision = engine.decide(ctx)
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "purchase_channel_selection"

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_start_order_with_product_focus_not_navigator(self, _mock_groups):
        state = MerchantConversationState(
            greeted=True,
            stage="deciding",
            turn=3,
            current_product_focus={"id": 1, "external_id": "ext-1", "title": "Sample"},
        )
        ctx = _ctx(MSG_START, state=state)
        ctx.intent = Intent(name="start_order", confidence=0.9, raw_message=MSG_START)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_CATALOG_NAVIGATE


class TestCatalogNavigateHandler:
    def test_handler_sets_owner_metadata(self):
        handler = CatalogNavigateHandler()
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "show_groups",
                "chosen_path": PATH_GROUPS,
                "owner_step": "browse_groups",
            },
            reason="test",
            confidence=0.9,
        )
        ctx = _ctx(MSG_BROWSE, db=MagicMock())

        with patch.object(handler, "_render_groups", new=AsyncMock(return_value={
            "discovery_presentation_text": GROUPS_REPLY,
            "product_lines": GROUPS_REPLY,
            "discovery_output_kind": "collections",
            "collections": COLLECTIONS,
            "chosen_path": PATH_GROUPS,
            "navigation_state_patch": {
                "last_presented_collections": COLLECTIONS,
                "catalog_navigation_source": "groups",
            },
        })):
            result = asyncio.run(handler.handle(decision, ctx))

        assert result.success is True
        assert result.data["owner_locked"] is True
        assert result.data["navigator_owner"] is True
        assert result.data["chosen_path"] == PATH_GROUPS
        assert result.data["owner_replaced"] is False
        assert result.data["owner_reply_hash"]
