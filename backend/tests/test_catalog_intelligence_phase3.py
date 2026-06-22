"""Catalog Intelligence Phase 3 — AI browse scope resolver."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.catalog.catalog_browse_scope_resolver import (  # noqa: E402
    BrowseScopeResolution,
    filter_products_to_merchant_group,
    match_catalog_group,
    match_group_by_collection_name,
    resolve_browse_scope,
)
from modules.ai.brain.catalog.catalog_intelligence import (  # noqa: E402
    CatalogIntelligence,
)
from modules.ai.brain.catalog.catalog_provider import CatalogProvider  # noqa: E402


_MERCHANT_GROUPS: List[Dict[str, Any]] = [
    {
        "id": 1,
        "slug": "honey",
        "label": "العسل",
        "catalog_match": "عسل,honey",
        "priority": 1,
        "is_active": True,
        "product_count": 3,
    },
    {
        "id": 2,
        "slug": "oils",
        "label": "الزيوت",
        "catalog_match": "زيت,oil",
        "priority": 2,
        "is_active": True,
        "product_count": 2,
    },
]


class _FakeProvider(CatalogProvider):
    def __init__(self, products: List[Dict[str, Any]]) -> None:
        self._products = list(products)
        self.collection_calls: List[str] = []

    def search_products(self, query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        q = str(query or "").strip().lower()
        return [
            p for p in self._products
            if q in str(p.get("title") or "").lower()
            or q in str(p.get("category") or "").lower()
        ][:limit]

    def get_top_products(self, *, limit: int = 12) -> List[Dict[str, Any]]:
        return self._products[:limit]

    def list_collections(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        return []

    def get_collection_products(self, collection_name: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        self.collection_calls.append(collection_name)
        return self.search_products(collection_name, limit=limit)


class TestMatchCatalogGroup:
    def test_matches_arabic_catalog_match(self) -> None:
        hit = match_catalog_group(_MERCHANT_GROUPS, message="وريني العسل", query="")
        assert hit is not None
        assert hit.group_slug == "honey"
        assert hit.group_label == "العسل"

    def test_matches_english_catalog_match(self) -> None:
        hit = match_catalog_group(_MERCHANT_GROUPS, message="", query="honey")
        assert hit is not None
        assert hit.group_slug == "honey"

    def test_session_locked_group_slug(self) -> None:
        hit = match_catalog_group(
            _MERCHANT_GROUPS,
            message="",
            query="",
            active_group_slug="oils",
        )
        assert hit is not None
        assert hit.group_slug == "oils"
        assert hit.match_source == "session_slug"

    def test_no_match_without_groups(self) -> None:
        assert match_catalog_group([], message="عسل") is None


class TestFilterProductsToMerchantGroup:
    def test_keeps_only_group_members(self) -> None:
        products = [
            {"id": 10, "title": "Talh"},
            {"id": 20, "title": "Sidr"},
            {"id": 30, "title": "Cream"},
        ]
        kept = filter_products_to_merchant_group(products, product_ids=[20, 10])
        assert [p["id"] for p in kept] == [10, 20]


class TestMatchGroupByCollectionName:
    def test_matches_group_label(self) -> None:
        group = match_group_by_collection_name(_MERCHANT_GROUPS, "الزيوت")
        assert group is not None
        assert group["slug"] == "oils"


class TestCatalogIntelligenceDbGroups:
    def test_list_collections_uses_db_groups(self) -> None:
        intel = CatalogIntelligence(_FakeProvider([]))
        groups = intel.list_collections(merchant_catalog_groups=_MERCHANT_GROUPS, limit=10)
        assert len(groups) == 2
        assert groups[0].group_id == "honey"
        assert groups[0].group_name == "العسل"
        assert groups[0].product_count == 3


class TestResolveBrowseScopeWithDb:
    def test_resolve_loads_product_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: _MERCHANT_GROUPS,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: (101, 102) if gid == 1 else (),
        )
        resolution = resolve_browse_scope(MagicMock(), 33, "عسل", "")
        assert resolution.matched is True
        assert resolution.group_slug == "honey"
        assert resolution.product_ids == (101, 102)


class TestDiscoveryEntryCatalogGroupFields:
    def test_decision_carries_group_metadata(self) -> None:
        from modules.ai.brain.discovery.entry import DiscoveryEntryDecision  # noqa: PLC0415

        entry = DiscoveryEntryDecision(
            matched=True,
            entry_type="category_browse",
            source="category_browse",
            query="عسل",
            category_scope="العسل",
            reason="test",
            catalog_group_slug="honey",
            catalog_group_id=1,
        )
        assert entry.catalog_group_slug == "honey"
        assert entry.catalog_group_id == 1
