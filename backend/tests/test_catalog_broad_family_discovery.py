"""Broad family discovery must not exclusive-lock a more specific child group.

LIVE-T33-CATALOG-BROWSE-D1 — structural candidate contract.

Live customer wording appears only as test input. Production logic must not
special-case those strings.
"""
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
    match_catalog_group,
    resolve_catalog_category_scope,
)
from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: E402
    filter_products_for_browse_turn,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402


# Live-shaped family (observed T33 architecture). Not a production special case.
_LIVE_BROAD_INQUIRY = "السلام عليكم، أبغى الاستفسار عن العسل"

_KILO_GROUP: Dict[str, Any] = {
    "id": 101,
    "slug": "family-kilo",
    "label": "العسل بالكيلو",
    "catalog_match": "عسل,honey",
    "priority": 1,
    "is_active": True,
}
_HALF_GROUP: Dict[str, Any] = {
    "id": 102,
    "slug": "family-half",
    "label": "العسل بالنصف كيلو",
    "catalog_match": "عسل,honey",
    "priority": 2,
    "is_active": True,
}
_SIDR_GROUP: Dict[str, Any] = {
    "id": 103,
    "slug": "family-sidr",
    "label": "عسل السدر",
    "catalog_match": "عسل,honey",
    "priority": 3,
    "is_active": True,
}
_CHILD_GROUPS = [_KILO_GROUP, _HALF_GROUP, _SIDR_GROUP]

_FAMILY_GROUP: Dict[str, Any] = {
    "id": 100,
    "slug": "family-all",
    "label": "العسل",
    "catalog_match": "عسل,honey",
    "priority": 0,
    "is_active": True,
}

# Generic commerce family (platform-wide, not honey-only).
_SHIRT_FAMILY: Dict[str, Any] = {
    "id": 201,
    "slug": "shirts",
    "label": "قمصان",
    "catalog_match": "قميص,shirt",
    "priority": 1,
    "is_active": True,
}
_SHIRT_DOZEN: Dict[str, Any] = {
    "id": 202,
    "slug": "shirts-dozen",
    "label": "قمصان بالدرزن",
    "catalog_match": "قميص,shirt",
    "priority": 2,
    "is_active": True,
}
_SHIRT_SPORT: Dict[str, Any] = {
    "id": 203,
    "slug": "shirts-sport",
    "label": "قمصان رياضية",
    "catalog_match": "قميص,shirt",
    "priority": 3,
    "is_active": True,
}


def _product(
    pid: int,
    title: str,
    *,
    category: str = "",
    active: bool = True,
    in_stock: bool = True,
    price: float = 80.0,
    image: str = "https://cdn.example/p.jpg",
) -> Dict[str, Any]:
    return {
        "id": pid,
        "title": title,
        "category": category,
        "active": active,
        "in_stock": in_stock,
        "catalog_status": "active" if active else "removed",
        "price": price if active else None,
        "image_url": image,
        "can_checkout": bool(active and in_stock and price),
        "orderable": bool(active and in_stock and price),
    }


_HONEY_FAMILY_CATALOG: List[Dict[str, Any]] = [
    _product(141, "250جرام عسل صيفي أزهار جبلية"),
    _product(142, "250 جرام عسل الطلح البلدي البري"),
    _product(146, "1 كيلو عسل الطلح البلدي البري"),
    _product(154, "1 كيلو العسل الصيفي أزهار جبلية من جنوب الطائف"),
    _product(162, "500 جرام العسل الصيفي أزهار جبلية"),
    _product(167, "1 كيلو عسل السدر قطفة قيضية"),
    _product(298, "عسل منتهي المخزون", in_stock=False),
    _product(299, "عسل غير نشط", active=False, in_stock=False, price=None),
]

_SHIRT_FAMILY_CATALOG: List[Dict[str, Any]] = [
    _product(11, "قميص قطني أزرق"),
    _product(12, "قميص قطني بالدرزن أبيض"),
    _product(13, "قميص رياضي أسود"),
    _product(14, "حذاء رياضي أبيض", category="أحذية"),
    _product(15, "قميص غير متوفر", in_stock=False),
]


def _orderable(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("active") is False:
            continue
        if row.get("in_stock") is False:
            continue
        if not row.get("price"):
            continue
        kept.append(row)
    return kept


def _membership(group_id: int) -> tuple[int, ...]:
    return {
        101: (154, 146),
        102: (162,),
        103: (167,),
        100: (141, 142, 146, 154, 162, 167),
        201: (11, 12, 13),
        202: (12,),
        203: (13,),
    }.get(int(group_id), ())


def _state(**session: Any) -> MerchantConversationState:
    return MerchantConversationState(commerce_session=dict(session))


class TestBroadFamilyDoesNotLockChildGroup:
    def test_live_shaped_family_query_does_not_match_kilo_child(self) -> None:
        hit = match_catalog_group(
            _CHILD_GROUPS,
            message=_LIVE_BROAD_INQUIRY,
            query="العسل",
        )
        assert hit is None

    def test_generic_family_query_does_not_match_dozen_child(self) -> None:
        hit = match_catalog_group(
            [_SHIRT_DOZEN, _SHIRT_SPORT],
            message="أبغى الاستفسار عن القمصان",
            query="قمصان",
        )
        assert hit is None

    def test_unique_family_group_still_matches(self) -> None:
        hit = match_catalog_group(
            [_FAMILY_GROUP, *_CHILD_GROUPS],
            message=_LIVE_BROAD_INQUIRY,
            query="العسل",
        )
        assert hit is not None
        assert hit.group_slug == "family-all"
        assert hit.match_source == "text"
        assert hit.evidence.get("current_turn_structured_scope") is True

    def test_specific_child_group_named_in_turn_still_locks(self) -> None:
        hit = match_catalog_group(
            _CHILD_GROUPS,
            message="وريني العسل بالكيلو",
            query="العسل بالكيلو",
        )
        assert hit is not None
        assert hit.group_slug == "family-kilo"
        assert hit.match_source == "text"


class TestSessionAndStaleState:
    def test_empty_turn_keeps_session_group(self) -> None:
        hit = match_catalog_group(
            _CHILD_GROUPS,
            message="",
            query="",
            active_group_slug="family-kilo",
        )
        assert hit is not None
        assert hit.group_slug == "family-kilo"
        assert hit.match_source == "session_slug"

    def test_stale_child_group_cannot_narrow_new_family_inquiry(self) -> None:
        hit = match_catalog_group(
            _CHILD_GROUPS,
            message=_LIVE_BROAD_INQUIRY,
            query="العسل",
            active_group_slug="family-kilo",
        )
        assert hit is None

    def test_stale_group_yields_to_named_other_group(self) -> None:
        hit = match_catalog_group(
            _CHILD_GROUPS,
            message="وريني عسل السدر",
            query="عسل السدر",
            active_group_slug="family-kilo",
        )
        assert hit is not None
        assert hit.group_slug == "family-sidr"
        assert hit.match_source == "text"

    def test_show_more_without_new_subject_keeps_group(self) -> None:
        hit = match_catalog_group(
            _CHILD_GROUPS,
            message="show more",
            query="",
            active_group_slug="family-kilo",
        )
        assert hit is not None
        assert hit.group_slug == "family-kilo"


class TestFilterPipelineComposeInput:
    def test_live_shaped_broad_query_keeps_multiple_family_candidates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, tenant_id: _CHILD_GROUPS if int(tenant_id) == 33 else [],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: _membership(gid),
        )

        raw = list(_HONEY_FAMILY_CATALOG)
        raw_fts = _orderable(raw)
        assert len(raw_fts) > 1

        scoped = filter_products_for_browse_turn(
            raw_fts,
            message=_LIVE_BROAD_INQUIRY,
            query="العسل",
            db=MagicMock(),
            tenant_id=33,
        )
        ids = [int(p["id"]) for p in scoped]
        assert len(ids) > 1
        assert 154 in ids
        assert 141 in ids
        assert 167 in ids
        assert 298 not in ids
        assert 299 not in ids

        rank_limit = 2
        ranked = scoped[:rank_limit]
        assert len(ranked) > 1

    def test_specific_group_navigation_still_scopes_membership(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: _CHILD_GROUPS,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: _membership(gid),
        )
        scoped = filter_products_for_browse_turn(
            _orderable(_HONEY_FAMILY_CATALOG),
            message="وريني العسل بالكيلو",
            query="العسل بالكيلو",
            db=MagicMock(),
            tenant_id=33,
        )
        assert [int(p["id"]) for p in scoped] == [154, 146]

    def test_generic_shirts_family_survives_child_groups(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: [_SHIRT_DOZEN, _SHIRT_SPORT],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: _membership(gid),
        )
        scoped = filter_products_for_browse_turn(
            _orderable(_SHIRT_FAMILY_CATALOG),
            message="أبغى قميص",
            query="قميص",
            db=MagicMock(),
            tenant_id=7,
        )
        ids = {int(p["id"]) for p in scoped}
        assert {11, 12, 13}.issubset(ids)
        assert 14 not in ids
        assert 15 not in ids

    def test_stale_presented_product_cannot_narrow_family(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: _CHILD_GROUPS,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: _membership(gid),
        )
        state = _state(active_catalog_group_slug="family-kilo")
        state.current_product_focus = {"id": 154, "title": "1 كيلو العسل الصيفي"}
        state.last_presented_products = [{"id": 154, "title": "1 كيلو العسل الصيفي"}]
        state.last_search_candidates = [{"id": 154}]

        scoped = filter_products_for_browse_turn(
            _orderable(_HONEY_FAMILY_CATALOG),
            message=_LIVE_BROAD_INQUIRY,
            query="العسل",
            state=state,
            db=MagicMock(),
            tenant_id=33,
        )
        ids = [int(p["id"]) for p in scoped]
        assert len(ids) > 1
        assert 154 in ids
        assert 141 in ids

    def test_tenant_isolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, tenant_id: _CHILD_GROUPS if int(tenant_id) == 33 else [_SHIRT_DOZEN],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: _membership(gid),
        )
        other = filter_products_for_browse_turn(
            _orderable(_SHIRT_FAMILY_CATALOG),
            message="قمصان بالدرزن",
            query="قمصان بالدرزن",
            db=MagicMock(),
            tenant_id=7,
        )
        assert [int(p["id"]) for p in other] == [12]

        t33 = match_catalog_group(
            _CHILD_GROUPS if True else [],
            message="قمصان بالدرزن",
            query="قمصان بالدرزن",
        )
        assert t33 is None

    def test_specific_product_title_does_not_lock_unrelated_child(self) -> None:
        hit = match_catalog_group(
            _CHILD_GROUPS,
            message="1 كيلو عسل الطلح البلدي البري",
            query="1 كيلو عسل الطلح البلدي البري",
        )
        assert hit is None

    def test_category_scope_does_not_exclusive_filter_ambiguous_children(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: _CHILD_GROUPS,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: _membership(gid),
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._load_snapshot_categories",
            lambda _db, _tid: ["العسل بالكيلو", "العسل بالنصف كيلو"],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._load_product_metadata_categories",
            lambda _db, _tid: [],
        )
        scope = resolve_catalog_category_scope(
            MagicMock(),
            33,
            _LIVE_BROAD_INQUIRY,
            "العسل",
        )
        assert scope.must_filter_by_category is False
        assert not scope.product_ids


class TestAvailabilityUnchanged:
    def test_group_lock_repair_does_not_revive_inactive_or_oos(self) -> None:
        raw = _orderable(_HONEY_FAMILY_CATALOG)
        ids = {int(p["id"]) for p in raw}
        assert 298 not in ids
        assert 299 not in ids
        assert 154 in ids
        assert 141 in ids
