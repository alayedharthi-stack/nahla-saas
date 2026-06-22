"""P0 — enforce catalog groups navigation (collections_first presenter path)."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.catalog_intelligence import (  # noqa: E402
    CatalogGroup,
    DiscoveryPlan,
)
from modules.ai.brain.commerce.catalog_search_evidence import (  # noqa: E402
    apply_catalog_search_evidence_gate,
)
from modules.ai.brain.commerce.collection_navigation import (  # noqa: E402
    resolve_collection_pick,
    try_collection_navigation_decision,
)
from modules.ai.brain.commerce.discovery_strategy import DiscoveryMode  # noqa: E402
from modules.ai.brain.commerce.product_media import detect_product_media_turn  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import resolve_discovery_entry  # noqa: E402
from modules.ai.brain.execution.search import ProductSearchHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.product_discovery_gate import extract_types_overview_query  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
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

COLLECTIONS = [
    {"group_id": "honey", "group_name": "\u0627\u0644\u0639\u0633\u0644", "browse_rank": 1},
    {"group_id": "oils", "group_name": "\u0627\u0644\u0632\u064a\u0648\u062a", "browse_rank": 2},
    {"group_id": "gifts", "group_name": "\u0627\u0644\u0647\u062f\u0627\u064a\u0627", "browse_rank": 3},
]

HONEY_PRODUCTS = [
    {"id": "1", "external_id": "1", "title": "Talh 1kg", "category": "Honey", "price": 220},
    {"id": "2", "external_id": "2", "title": "Sidr 1kg", "category": "Honey", "price": 250},
]

OIL_PRODUCTS = [
    {"id": "9", "external_id": "9", "title": "Olive Oil 500ml", "category": "Oils", "price": 45},
]


def _facts(*, product_count: int = 30) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=product_count,
        in_stock_count=product_count,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
        top_products=HONEY_PRODUCTS,
    )


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
    db: Any = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
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
        last_discovery_mode=DiscoveryMode.COLLECTIONS_FIRST.value,
        last_presented_collections=list(COLLECTIONS),
        selection_context_turn=4,
    )
    state.selected_collection = selected
    return state


def _mock_db_groups() -> MagicMock:
    db = MagicMock()
    return db


class TestCatalogSearchGateCollectionsFirst:
    def test_start_order_collections_first_passes_gate(self) -> None:
        ctx = _ctx(MSG_START)
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": "",
                "source": "top_products_start_order",
                "discovery_mode": DiscoveryMode.COLLECTIONS_FIRST.value,
                "discovery_entry_type": "start_order_bare",
            },
            reason="start order bare",
            confidence=0.85,
        )
        out = apply_catalog_search_evidence_gate(ctx, decision)
        assert out.action == ACTION_SEARCH_PRODUCTS
        assert out.action != ACTION_LLM_REPLY

    def test_wesh_aindakom_passes_gate_with_collections_first(self) -> None:
        ctx = _ctx(MSG_BROWSE)
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": "",
                "source": "top_products",
                "discovery_mode": DiscoveryMode.COLLECTIONS_FIRST.value,
                "discovery_entry_type": "global_browse",
            },
            reason="global browse",
            confidence=0.92,
        )
        out = apply_catalog_search_evidence_gate(ctx, decision)
        assert out.action == ACTION_SEARCH_PRODUCTS


class TestDiscoveryDecisionCollectionsFirst:
    def test_start_order_decision_is_search_not_llm(self) -> None:
        db = _mock_db_groups()
        ctx = _ctx(MSG_START, db=db)
        with patch(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            return_value=[
                {"slug": "honey", "label": "Honey", "priority": 1, "is_active": True, "product_count": 4},
                {"slug": "oils", "label": "Oils", "priority": 2, "is_active": True, "product_count": 2},
            ],
        ):
            decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != ACTION_LLM_REPLY
        assert decision.args.get("discovery_mode") == DiscoveryMode.COLLECTIONS_FIRST.value

    def test_wesh_aindakom_decision_is_search_not_llm(self) -> None:
        ctx = _ctx(MSG_BROWSE, db=_mock_db_groups())
        with patch(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            return_value=[
                {"slug": "a", "label": "A", "priority": 1, "is_active": True, "product_count": 3},
                {"slug": "b", "label": "B", "priority": 2, "is_active": True, "product_count": 2},
            ],
        ):
            decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != ACTION_LLM_REPLY


class TestTypesOverviewQueryExtraction:
    def test_global_types_ask_does_not_extract_yali(self) -> None:
        assert extract_types_overview_query(MSG_TYPES) == ""


class TestCollectionNavigationResolver:
    def test_ordinal_pick_resolves_group(self) -> None:
        picked = resolve_collection_pick("2", COLLECTIONS)
        assert picked is not None
        assert picked.group_id == "oils"
        assert picked.group_name == "\u0627\u0644\u0632\u064a\u0648\u062a"

    def test_name_pick_resolves_group(self) -> None:
        picked = resolve_collection_pick("\u0627\u0644\u0639\u0633\u0644", COLLECTIONS)
        assert picked is not None
        assert picked.group_id == "honey"

    def test_back_to_collections_returns_browse_groups(self) -> None:
        ctx = _ctx(MSG_BACK, state=_collections_state(selected="honey"))
        decision = try_collection_navigation_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == "browse_catalog_groups"
        assert decision.args.get("query") == ""

    def test_switch_group_returns_browse_groups(self) -> None:
        ctx = _ctx(MSG_SWITCH, state=_collections_state(selected="honey"))
        decision = try_collection_navigation_decision(ctx)
        assert decision is not None
        assert decision.args.get("source") == "browse_catalog_groups"


class TestSearchHandlerCollectionsPresentation:
    def test_start_order_renders_collections_not_products(self) -> None:
        ctx = _ctx(MSG_START, db=_mock_db_groups())
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": "",
                "source": "top_products_start_order",
                "discovery_mode": DiscoveryMode.COLLECTIONS_FIRST.value,
                "discovery_entry_type": "start_order_bare",
            },
            reason="start order",
            confidence=0.85,
        )
        plan = DiscoveryPlan(
            output_kind="collections",
            collections=[
                CatalogGroup(group_id="honey", group_name="Honey", browse_rank=1),
                CatalogGroup(group_id="oils", group_name="Oils", browse_rank=2),
            ],
        )
        payload = ActionResult(
            success=True,
            data={
                "products": [],
                "collections": [g.to_dict() for g in plan.collections],
                "product_lines": "1. Honey\n2. Oils",
                "discovery_output_kind": "collections",
                "discovery_presentation_text": "1. Honey\n2. Oils",
                "count": 0,
                "query": "",
            },
        )

        async def _run() -> ActionResult:
            with patch(
                "modules.ai.brain.execution.search._apply_discovery_strategy",
                return_value=payload,
            ):
                return await ProductSearchHandler().handle(decision, ctx)

        result = asyncio.run(_run())
        assert result.success is True
        assert result.data.get("discovery_output_kind") == "collections"
        assert len(result.data.get("collections") or []) >= 2
        assert len(result.data.get("products") or []) == 0

    def test_group_pick_renders_group_products_only(self) -> None:
        ctx = _ctx("1", state=_collections_state())
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": "\u0627\u0644\u0639\u0633\u0644",
                "source": "collections_first",
                "discovery_mode": DiscoveryMode.COLLECTIONS_FIRST.value,
                "catalog_group_id": "honey",
            },
            reason="collection pick",
            confidence=0.92,
        )
        payload = ActionResult(
            success=True,
            data={
                "products": HONEY_PRODUCTS,
                "product_lines": "honey list",
                "discovery_output_kind": "products",
                "discovery_presentation_text": "honey list",
                "count": len(HONEY_PRODUCTS),
                "query": "\u0627\u0644\u0639\u0633\u0644",
            },
        )

        async def _run() -> ActionResult:
            with patch(
                "modules.ai.brain.execution.search._apply_discovery_strategy",
                return_value=payload,
            ):
                return await ProductSearchHandler().handle(decision, ctx)

        result = asyncio.run(_run())
        titles = [p["title"] for p in result.data.get("products") or []]
        assert all("Olive" not in t for t in titles)
        assert len(titles) == 2


class TestProductMediaGlobalBrowse:
    def test_details_availability_not_product_media(self) -> None:
        verdict = detect_product_media_turn(MSG_DETAILS, intent_name="general")
        assert verdict.matched is False
        assert verdict.reason == "global_availability_browse"


class TestProductSelectionAfterGroup:
    def test_bare_number_after_group_products_does_not_propose_order(self) -> None:
        from modules.ai.brain.commerce.selection_context import (  # noqa: PLC0415
            stamp_selection_context_from_products,
            try_selection_context_decision,
        )

        state = _collections_state(selected="honey")
        stamp_selection_context_from_products(
            state,
            products=HONEY_PRODUCTS,
            collections=COLLECTIONS,
            discovery_mode=DiscoveryMode.COLLECTIONS_FIRST.value,
            selected_collection="honey",
        )
        ctx = _ctx("1", state=state)
        decision = try_selection_context_decision(ctx)
        assert decision is None

    def test_explicit_buy_after_group_can_propose_order(self) -> None:
        from modules.ai.brain.commerce.selection_context import (  # noqa: PLC0415
            stamp_selection_context_from_products,
            try_selection_context_decision,
        )

        state = _collections_state(selected="honey")
        stamp_selection_context_from_products(
            state,
            products=HONEY_PRODUCTS,
            discovery_mode=DiscoveryMode.COLLECTIONS_FIRST.value,
            selected_collection="honey",
        )
        ctx = _ctx("\u0623\u0628\u063a\u0649 \u0627\u0644\u0623\u0648\u0644", state=state)
        decision = try_selection_context_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


class TestDiscoveryEntryGlobalBrowse:
    def test_details_availability_is_global_browse_entry(self) -> None:
        entry = resolve_discovery_entry(_ctx(MSG_DETAILS))
        assert entry.matched is True
        assert entry.entry_type == "global_browse"
