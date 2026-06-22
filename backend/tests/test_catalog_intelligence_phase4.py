"""Catalog Intelligence Phase 4 — product card scope filtering."""
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

from modules.ai.brain.catalog.catalog_product_card_filter import (  # noqa: E402
    filter_product_card_attachments,
)


def _attachments(*ids: int) -> List[Dict[str, Any]]:
    return [{"kind": "product_card", "id": pid, "title": f"Product {pid}"} for pid in ids]


class TestProductCardGroupFilter:
    def test_drops_cards_outside_merchant_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: [{"id": 1, "slug": "honey", "label": "Honey", "is_active": True}],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.resolve_browse_scope",
            lambda *_a, **_k: MagicMock(
                matched=True,
                group_slug="honey",
                product_ids=(10, 11),
                evidence={},
            ),
        )
        monkeypatch.setattr(
            "modules.ai.brain.commerce.commerce_browse_category_guard.resolve_browse_category_scope",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_product_card_filter._load_products_for_attachments",
            lambda *_a, **_k: {},
        )

        result = filter_product_card_attachments(
            _attachments(10, 99),
            db=MagicMock(),
            tenant_id=1,
            message="وريني العسل",
        )
        assert [a["id"] for a in result.attachments] == [10]
        assert result.dropped == 1

    def test_keeps_all_when_no_merchant_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: [],
        )
        result = filter_product_card_attachments(
            _attachments(1, 2),
            db=MagicMock(),
            tenant_id=1,
        )
        assert len(result.attachments) == 2
        assert result.dropped == 0

    def test_cross_category_guard_drops_attachment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: [{"id": 1, "slug": "honey", "label": "Honey", "is_active": True}],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.resolve_browse_scope",
            lambda *_a, **_k: MagicMock(matched=False, group_slug="", product_ids=(), evidence={}),
        )
        monkeypatch.setattr(
            "modules.ai.brain.commerce.commerce_browse_category_guard.resolve_browse_category_scope",
            lambda *_a, **_k: "عسل",
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_product_card_filter._load_products_for_attachments",
            lambda *_a, **_k: {
                5: {"id": 5, "title": "Bee Venom Cream", "category": "cream", "tags": []},
            },
        )
        monkeypatch.setattr(
            "modules.ai.brain.commerce.commerce_browse_category_guard.should_exclude_cross_category_product",
            lambda *_a, **_k: True,
        )

        result = filter_product_card_attachments(
            _attachments(5),
            db=MagicMock(),
            tenant_id=1,
            message="ابي عسل",
        )
        assert result.attachments == []
        assert result.dropped == 1

    def test_explicit_product_request_bypasses_group_drop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: [{"id": 1, "slug": "honey", "label": "Honey", "is_active": True}],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.resolve_browse_scope",
            lambda *_a, **_k: MagicMock(
                matched=True,
                group_slug="honey",
                product_ids=(10,),
                evidence={},
            ),
        )
        monkeypatch.setattr(
            "modules.ai.brain.commerce.commerce_browse_category_guard.resolve_browse_category_scope",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_product_card_filter._load_products_for_attachments",
            lambda *_a, **_k: {},
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_product_card_filter._attachment_explicitly_requested",
            lambda _msg, title: title == "Special Gift Box",
        )

        result = filter_product_card_attachments(
            [{"kind": "product_card", "id": 99, "title": "Special Gift Box"}],
            db=MagicMock(),
            tenant_id=1,
            message="وريني Special Gift Box",
        )
        assert len(result.attachments) == 1
        assert result.dropped == 0
