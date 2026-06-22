"""Catalog Intelligence Phase 5 — best sellers and alternatives runtime."""
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

from modules.ai.brain.catalog.catalog_ranking_runtime import (  # noqa: E402
    load_best_seller_catalog_products,
    resolve_orderable_alternatives,
)


class TestResolveOrderableAlternatives:
    def test_prefers_merchant_relations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        merchant = [
            {"id": 20, "title": "Alt A", "external_id": "ext-20", "orderable": True},
        ]
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime.load_merchant_alternative_products",
            lambda *_a, **_k: merchant,
        )
        fallback = [
            {"id": 10, "title": "Rejected", "external_id": "ext-10", "orderable": True},
            {"id": 30, "title": "Fallback", "external_id": "ext-30", "orderable": True},
        ]
        alts = resolve_orderable_alternatives(
            MagicMock(),
            1,
            source_product_id=10,
            fallback_candidates=fallback,
            limit=1,
        )
        assert [a["id"] for a in alts] == [20]

    def test_supplements_from_fallback_when_relations_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime.load_merchant_alternative_products",
            lambda *_a, **_k: [],
        )
        fallback = [
            {"id": 10, "title": "Rejected", "external_id": "ext-10", "orderable": True},
            {"id": 30, "title": "Fallback", "external_id": "ext-30", "orderable": True},
        ]
        alts = resolve_orderable_alternatives(
            MagicMock(),
            1,
            source_product_id=10,
            fallback_candidates=fallback,
            limit=2,
        )
        assert [a["id"] for a in alts] == [30]


class TestLoadBestSellerCatalogProducts:
    def test_returns_hydrated_products(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "services.catalog_intelligence_service.get_catalog_settings",
            lambda _db, _tid: {},
        )
        monkeypatch.setattr(
            "services.catalog_intelligence_service.read_best_sellers",
            lambda *_a, **_k: [{"product_id": 5}, {"product_id": 6}],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime._resolve_group_id_for_browse",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime.hydrate_catalog_products_by_ids",
            lambda *_a, **_k: [
                {"id": 5, "title": "Best 1", "external_id": "a"},
                {"id": 6, "title": "Best 2", "external_id": "b"},
            ],
        )

        products = load_best_seller_catalog_products(MagicMock(), 7, limit=5)
        assert len(products) == 2
        assert products[0]["id"] == 5

    def test_auto_mode_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "services.catalog_intelligence_service.get_catalog_settings",
            lambda _db, _tid: {"best_seller_mode": "auto"},
        )
        products = load_best_seller_catalog_products(MagicMock(), 7)
        assert products == []
