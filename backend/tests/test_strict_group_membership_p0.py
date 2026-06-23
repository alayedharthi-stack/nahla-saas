"""P0 — strict ProductGroupItem membership for CatalogNavigator group products."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.catalog_provider import (  # noqa: E402
    GroupProductsFetchResult,
    LocalCatalogProvider,
)
from modules.ai.brain.catalog.collections_pagination import normalize_collections_page  # noqa: E402
from modules.ai.brain.catalog.navigation import (  # noqa: E402
    PATH_GROUP_PRODUCTS,
    try_catalog_navigation_decision,
)
from modules.ai.brain.commerce.collection_navigation import resolve_collection_pick  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE  # noqa: E402
from modules.ai.brain.execution.catalog_navigate import CatalogNavigateHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

GROUP_A = {
    "group_db_id": 10,
    "group_id": "group-a",
    "group_slug": "group-a",
    "group_name": "منتجات عامة A",
    "browse_rank": 1,
}
GROUP_B = {
    "group_db_id": 20,
    "group_id": "group-b",
    "group_slug": "group-b",
    "group_name": "منتجات عامة B",
    "browse_rank": 2,
}
COLLECTIONS = [GROUP_A, GROUP_B]

DB_GROUPS = [
    {"id": 10, "slug": "group-a", "label": "منتجات عامة A", "priority": 1, "is_active": True, "product_count": 4},
    {"id": 20, "slug": "group-b", "label": "منتجات عامة B", "priority": 2, "is_active": True, "product_count": 2},
]

PRODUCTS_A = [
    {"id": 101, "external_id": "101", "title": "A1", "price": 10, "in_stock": True},
    {"id": 102, "external_id": "102", "title": "A2", "price": 11, "in_stock": True},
    {"id": 103, "external_id": "103", "title": "A3", "price": 12, "in_stock": True},
    {"id": 104, "external_id": "104", "title": "A4", "price": 13, "in_stock": True},
]
PRODUCTS_B = [
    {"id": 201, "external_id": "201", "title": "B1", "price": 20, "in_stock": True},
    {"id": 202, "external_id": "202", "title": "B2", "price": 21, "in_stock": True},
]
GLOBAL_SEARCH_HITS = [
    {"id": 999, "external_id": "999", "title": "Generic match", "price": 99, "in_stock": True},
]


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=20,
        in_stock_count=20,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="store",
    )


def _ctx(msg: str, *, state: MerchantConversationState | None = None, db: Any = None) -> BrainContext:
    intent = rules.match(msg) or Intent(name="general", confidence=0.5, raw_message=msg)
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000033",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(),
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _collections_state(*, page: List[Dict[str, Any]] | None = None) -> MerchantConversationState:
    shown = page or normalize_collections_page(COLLECTIONS[:2], offset=0)
    state = MerchantConversationState(
        greeted=True,
        stage="discovery",
        turn=5,
        last_presented_collections=shown,
        selection_context_turn=4,
        catalog_navigation_source="groups",
    )
    state.collections_pool = list(COLLECTIONS)
    state.collections_offset = 0
    return state


class _TrackingProvider:
    def __init__(self, *, fetch_result: GroupProductsFetchResult | None = None) -> None:
        self.search_calls: List[str] = []
        self.collection_name_calls: List[str] = []
        self.by_id_calls: List[int] = []
        self._fetch_result = fetch_result or GroupProductsFetchResult(
            products=[],
            product_source="scoped_empty",
            group_db_id=10,
            membership_count=4,
            orderable_count=0,
            empty_reason="no_orderable_members",
        )

    def search_products(self, query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        self.search_calls.append(str(query))
        return list(GLOBAL_SEARCH_HITS)

    def get_top_products(self, *, limit: int = 12) -> List[Dict[str, Any]]:
        return []

    def list_collections(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        return []

    def get_collection_products(self, collection_name: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        self.collection_name_calls.append(str(collection_name))
        return self.search_products(collection_name, limit=limit)

    def get_collection_products_by_id(
        self,
        group_id: int,
        *,
        limit: int = 12,
        allow_search_fallback: bool = False,
        group_slug: str = "",
        group_name: str = "",
    ) -> GroupProductsFetchResult:
        self.by_id_calls.append(int(group_id))
        return self._fetch_result


class TestCatalogGroupIdentity:
    def test_list_collections_preserves_group_db_id(self) -> None:
        from modules.ai.brain.catalog.catalog_intelligence import CatalogIntelligence  # noqa: PLC0415

        provider = MagicMock()
        intel = CatalogIntelligence(provider)
        groups = intel.list_collections(limit=10, merchant_catalog_groups=DB_GROUPS)
        assert len(groups) == 2
        assert groups[0].group_db_id == 10
        assert groups[0].group_slug == "group-a"
        assert groups[0].group_id == "group-a"
        payload = groups[0].to_dict()
        assert payload["group_db_id"] == 10
        assert payload["group_slug"] == "group-a"
        assert payload["group_name"] == "منتجات عامة A"


class TestStrictGroupFetch:
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.read_group_membership_ids", return_value=(101, 102, 103, 104))
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.hydrate_group_products", return_value=PRODUCTS_A)
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups", return_value=DB_GROUPS)
    def test_group_with_four_orderable_items_returns_same_ids(
        self,
        _mock_groups: MagicMock,
        _mock_hydrate: MagicMock,
        _mock_membership: MagicMock,
    ) -> None:
        provider = LocalCatalogProvider(db=MagicMock(), tenant_id=33)
        result = provider.get_collection_products_by_id(
            10,
            allow_search_fallback=False,
            group_slug="group-a",
            group_name="منتجات عامة A",
        )
        assert result.product_source == "group_items"
        assert result.membership_count == 4
        assert result.orderable_count == 4
        assert result.products_returned == 4
        assert [p["id"] for p in result.products] == [101, 102, 103, 104]

    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.read_group_membership_ids", return_value=(101, 102))
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.hydrate_group_products", return_value=[])
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups", return_value=DB_GROUPS)
    def test_non_orderable_membership_returns_scoped_empty_not_global(
        self,
        _mock_groups: MagicMock,
        _mock_hydrate: MagicMock,
        _mock_membership: MagicMock,
    ) -> None:
        provider = LocalCatalogProvider(db=MagicMock(), tenant_id=33)
        with patch.object(provider, "search_products") as mock_search:
            result = provider.get_collection_products_by_id(
                10,
                allow_search_fallback=False,
                group_slug="group-a",
                group_name="منتجات عامة A",
            )
            mock_search.assert_not_called()
        assert result.product_source == "scoped_empty"
        assert result.products == []
        assert result.membership_count == 2
        assert result.orderable_count == 0
        assert result.empty_reason == "no_orderable_members"

    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.read_group_membership_ids")
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.hydrate_group_products")
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups", return_value=DB_GROUPS)
    def test_fetch_by_id_does_not_use_fuzzy_name_match(
        self,
        _mock_groups: MagicMock,
        mock_hydrate: MagicMock,
        mock_membership: MagicMock,
    ) -> None:
        mock_membership.return_value = (201, 202)
        mock_hydrate.return_value = PRODUCTS_B
        provider = LocalCatalogProvider(db=MagicMock(), tenant_id=33)
        with patch(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.match_group_by_collection_name",
        ) as mock_fuzzy:
            result = provider.get_collection_products_by_id(
                20,
                allow_search_fallback=False,
                group_slug="group-b",
                group_name="منتجات عامة B",
            )
            mock_fuzzy.assert_not_called()
        assert result.product_source == "group_items"
        assert [p["id"] for p in result.products] == [201, 202]
        mock_membership.assert_called_once()


class TestNavigatorGroupPick:
    def test_shared_name_word_picking_b_does_not_use_group_a(self) -> None:
        resolution = resolve_collection_pick("2", normalize_collections_page(COLLECTIONS[:2], offset=0))
        assert resolution is not None
        assert resolution.group_db_id == 20
        assert resolution.group_slug == "group-b"
        assert resolution.group_id == "group-b"

    def test_coll_buttons_pass_group_db_id(self) -> None:
        page = normalize_collections_page(COLLECTIONS[:2], offset=0)
        pick_one = resolve_collection_pick("1", page)
        pick_two = resolve_collection_pick("2", page)
        assert pick_one is not None and pick_one.group_db_id == 10
        assert pick_two is not None and pick_two.group_db_id == 20

    def test_navigation_decision_includes_group_db_id(self) -> None:
        decision = try_catalog_navigation_decision(_ctx("2", state=_collections_state()))
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args["chosen_path"] == PATH_GROUP_PRODUCTS
        assert decision.args["catalog_group_db_id"] == 20
        current = decision.args["navigation_state_patch"]["current_catalog_group"]
        assert current["group_db_id"] == 20
        assert current["group_slug"] == "group-b"

    def test_navigator_group_pick_does_not_call_search_products(self) -> None:
        provider = _TrackingProvider(
            fetch_result=GroupProductsFetchResult(
                products=list(PRODUCTS_B),
                product_source="group_items",
                group_db_id=20,
                group_slug="group-b",
                group_name="منتجات عامة B",
                membership_count=2,
                orderable_count=2,
                products_returned=2,
            ),
        )
        ctx = _ctx("2", state=_collections_state())
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "show_group_products",
                "chosen_path": PATH_GROUP_PRODUCTS,
                "catalog_group_id": "group-b",
                "catalog_group_slug": "group-b",
                "catalog_group_db_id": 20,
                "query": "منتجات عامة B",
                "source": "collections_first",
                "discovery_mode": "collections_first",
                "initial_count": 3,
            },
            reason="test",
            confidence=0.9,
        )
        handler = CatalogNavigateHandler()
        with patch(
            "modules.ai.brain.catalog.catalog_provider.get_catalog_provider",
            return_value=provider,
        ), patch(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            return_value=DB_GROUPS,
        ), patch(
            "modules.ai.brain.catalog.catalog_intelligence.attach_discovery_signals_from_db",
            side_effect=lambda products, **_: products,
        ), patch(
            "modules.ai.brain.catalog.presentation_contract.validate_discovery_products",
            side_effect=lambda products: products,
        ):
            result = asyncio.run(handler.handle(decision, ctx))

        assert result.success is True
        assert provider.by_id_calls == [20]
        assert provider.search_calls == []
        assert provider.collection_name_calls == []
        assert result.data["products"]
        assert all(p["id"] in {201, 202} for p in result.data["products"])

    def test_empty_scoped_response_has_no_global_products(self) -> None:
        provider = _TrackingProvider()
        ctx = _ctx("1", state=_collections_state())
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "show_group_products",
                "chosen_path": PATH_GROUP_PRODUCTS,
                "catalog_group_id": "group-a",
                "catalog_group_slug": "group-a",
                "catalog_group_db_id": 10,
                "query": "منتجات عامة A",
                "source": "collections_first",
                "discovery_mode": "collections_first",
                "initial_count": 3,
            },
            reason="test",
            confidence=0.9,
        )
        handler = CatalogNavigateHandler()
        with patch(
            "modules.ai.brain.catalog.catalog_provider.get_catalog_provider",
            return_value=provider,
        ), patch(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            return_value=DB_GROUPS,
        ), patch(
            "modules.ai.brain.catalog.catalog_intelligence.attach_discovery_signals_from_db",
            side_effect=lambda products, **_: products,
        ), patch(
            "modules.ai.brain.catalog.presentation_contract.validate_discovery_products",
            side_effect=lambda products: products,
        ):
            result = asyncio.run(handler.handle(decision, ctx))

        assert result.success is True
        assert result.data["products"] == []
        assert provider.search_calls == []
        evidence = result.data.get("discovery_plan") or {}
        assert evidence.get("product_source") == "scoped_empty"
        assert evidence.get("membership_count") == 4
        assert evidence.get("orderable_count") == 0

    def test_telemetry_fields_present_in_plan_evidence(self) -> None:
        provider = _TrackingProvider(
            fetch_result=GroupProductsFetchResult(
                products=list(PRODUCTS_A),
                product_source="group_items",
                group_db_id=10,
                group_slug="group-a",
                group_name="منتجات عامة A",
                membership_count=4,
                orderable_count=4,
                products_returned=4,
            ),
        )
        ctx = _ctx("1", state=_collections_state())
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "navigator_step": "show_group_products",
                "chosen_path": PATH_GROUP_PRODUCTS,
                "catalog_group_id": "group-a",
                "catalog_group_slug": "group-a",
                "catalog_group_db_id": 10,
                "query": "منتجات عامة A",
                "source": "collections_first",
                "discovery_mode": "collections_first",
                "initial_count": 3,
            },
            reason="test",
            confidence=0.9,
        )
        handler = CatalogNavigateHandler()
        with patch(
            "modules.ai.brain.catalog.catalog_provider.get_catalog_provider",
            return_value=provider,
        ), patch(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            return_value=DB_GROUPS,
        ), patch(
            "modules.ai.brain.catalog.catalog_intelligence.attach_discovery_signals_from_db",
            side_effect=lambda products, **_: products,
        ), patch(
            "modules.ai.brain.catalog.presentation_contract.validate_discovery_products",
            side_effect=lambda products: products,
        ):
            result = asyncio.run(handler.handle(decision, ctx))

        evidence = result.data.get("discovery_plan") or {}
        assert evidence.get("product_source") == "group_items"
        assert evidence.get("membership_count") == 4
        assert evidence.get("orderable_count") == 4
        assert evidence.get("products_returned") == 4


class TestSearchHandlerUnaffected:
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.hydrate_group_products", return_value=[])
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.resolve_browse_scope")
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.match_group_by_collection_name")
    @patch("modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups", return_value=DB_GROUPS)
    def test_name_based_collection_fetch_still_available_outside_navigator(
        self,
        _mock_groups: MagicMock,
        mock_match: MagicMock,
        mock_scope: MagicMock,
        _mock_hydrate: MagicMock,
    ) -> None:
        mock_match.return_value = DB_GROUPS[0]
        mock_scope.return_value = MagicMock(matched=True, product_ids=(999,))
        provider = LocalCatalogProvider(db=MagicMock(), tenant_id=33)
        with patch.object(provider, "search_products", return_value=GLOBAL_SEARCH_HITS) as mock_search:
            products = provider.get_collection_products("منتجات عامة A", limit=5)
        assert products == GLOBAL_SEARCH_HITS
        mock_search.assert_called_once()
