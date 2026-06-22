"""Catalog Intelligence Phase 6 — telemetry and setup validation."""
from __future__ import annotations

import logging
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

from modules.ai.brain.catalog.catalog_intelligence_telemetry import (  # noqa: E402
    emit_catalog_intelligence_event,
)
from routers.catalog_intelligence import router  # noqa: E402
from services.catalog_intelligence_service import (  # noqa: E402
    validate_catalog_intelligence_setup,
)


class TestCatalogIntelligenceTelemetry:
    def test_emits_grep_friendly_line(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="nahla.catalog_intelligence.telemetry")
        emit_catalog_intelligence_event(
            "browse_scope",
            tenant_id=33,
            slug="honey-core",
            products=4,
        )
        assert any(
            "[CATALOG_INTELLIGENCE]" in rec.message and "event=browse_scope" in rec.message
            for rec in caplog.records
        )

    def test_emit_never_raises(self) -> None:
        emit_catalog_intelligence_event("broken", tenant_id=1, bad=object())


class _FakeGroup:
    def __init__(
        self,
        *,
        gid: int,
        slug: str,
        label: str,
        is_active: bool = True,
        catalog_match: str = "",
        items: List[Any] | None = None,
    ) -> None:
        self.id = gid
        self.slug = slug
        self.label = label
        self.is_active = is_active
        self.catalog_match = catalog_match
        self.items = items or []
        self.priority = 100


class _FakeItem:
    def __init__(self, product_id: int) -> None:
        self.product_id = product_id


class TestValidateCatalogIntelligenceSetup:
    def test_flags_empty_groups_and_uncategorized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        groups = [
            _FakeGroup(gid=1, slug="honey", label="Honey", items=[]),
            _FakeGroup(
                gid=2,
                slug="dates",
                label="Dates",
                catalog_match="تمر, dates",
                items=[_FakeItem(10)],
            ),
        ]

        class _GroupQuery:
            def filter(self, *_a, **_k):
                return self

            def order_by(self, *_a, **_k):
                return self

            def all(self):
                return groups

        ranking_query = MagicMock()
        ranking_query.filter.return_value = ranking_query
        ranking_query.count.return_value = 0

        def _query_model(model: Any) -> Any:
            name = getattr(model, "__name__", None)
            if name is None and hasattr(model, "class_"):
                name = getattr(model.class_, "__name__", "")
            if name == "ProductGroup":
                return _GroupQuery()
            return ranking_query

        db = MagicMock()
        db.query.side_effect = _query_model

        monkeypatch.setattr(
            "services.catalog_intelligence_service.get_catalog_settings",
            lambda _db, _tid: {"best_seller_mode": "manual", "small_catalog_threshold": 2},
        )
        monkeypatch.setattr(
            "services.catalog_intelligence_service._active_product_ids",
            lambda _db, _tid: {10, 11, 12},
        )

        report = validate_catalog_intelligence_setup(db, 1)
        codes = {issue["code"] for issue in report["issues"]}
        assert "empty_group" in codes
        assert "uncategorized_products" in codes
        assert "no_best_sellers" in codes
        assert report["summary"]["uncategorized_products"] == 2

    def test_duplicate_catalog_match_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        groups = [
            _FakeGroup(gid=1, slug="a", label="A", catalog_match="عسل, honey", items=[_FakeItem(1)]),
            _FakeGroup(gid=2, slug="b", label="B", catalog_match="honey", items=[_FakeItem(2)]),
        ]

        class _GroupQuery:
            def filter(self, *_a, **_k):
                return self

            def order_by(self, *_a, **_k):
                return self

            def all(self):
                return groups

        ranking_query = MagicMock()
        ranking_query.filter.return_value = ranking_query
        ranking_query.count.return_value = 1

        def _query_model(model: Any) -> Any:
            name = getattr(model, "__name__", None)
            if name is None and hasattr(model, "class_"):
                name = getattr(model.class_, "__name__", "")
            if name == "ProductGroup":
                return _GroupQuery()
            return ranking_query

        db = MagicMock()
        db.query.side_effect = _query_model

        monkeypatch.setattr(
            "services.catalog_intelligence_service.get_catalog_settings",
            lambda _db, _tid: {},
        )
        monkeypatch.setattr(
            "services.catalog_intelligence_service._active_product_ids",
            lambda _db, _tid: {1, 2},
        )

        report = validate_catalog_intelligence_setup(db, 1)
        assert any(i["code"] == "duplicate_catalog_match" for i in report["issues"])


class TestValidationRouteRegistered:
    def test_validation_endpoint_exists(self) -> None:
        paths = {route.path for route in router.routes}
        assert "/catalog-intelligence/validation" in paths
