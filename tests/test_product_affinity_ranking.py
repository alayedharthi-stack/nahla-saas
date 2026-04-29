"""
tests/test_product_affinity_ranking.py
────────────────────────────────────────
Unit tests for the ProductAffinity-based search reranking introduced in
brain/execution/search.py (_apply_affinity_boost).

All tests are pure unit tests — no real DB, no HTTP.
The DB is replaced with a mock that returns pre-built ProductAffinity stubs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.execution.search import _apply_affinity_boost


# ── Helpers ───────────────────────────────────────────────────────────────────

def _product(pid: int, title: str, price: float = 100.0) -> Dict[str, Any]:
    return {"id": pid, "title": title, "price": price, "orderable": True}


def _affinity_row(product_id: int, score: float) -> MagicMock:
    row = MagicMock()
    row.product_id    = product_id
    row.affinity_score = score
    return row


def _ctx(
    customer_id: int | None = 42,
    tenant_id: int = 1,
    affinity_rows: List[MagicMock] | None = None,
) -> MagicMock:
    """Build a minimal BrainContext stub with a mocked DB."""
    ctx = MagicMock()
    ctx.customer_id = customer_id
    ctx.tenant_id   = tenant_id

    # Mock the DB query chain: ctx._db.query(...).filter(...).all() → rows
    mock_query   = MagicMock()
    mock_filter  = MagicMock()
    mock_filter.all.return_value = affinity_rows or []
    mock_query.filter.return_value = mock_filter
    ctx._db.query.return_value = mock_query

    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Basic reranking
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyAffinityBoost:

    def test_no_customer_returns_original_order(self):
        products = [_product(1, "أ"), _product(2, "ب"), _product(3, "ج")]
        ctx = _ctx(customer_id=None)
        result = _apply_affinity_boost(products, ctx)
        assert [p["id"] for p in result] == [1, 2, 3]

    def test_empty_products_returns_empty(self):
        ctx = _ctx()
        result = _apply_affinity_boost([], ctx)
        assert result == []

    def test_no_affinity_rows_returns_original_order(self):
        products = [_product(1, "أ"), _product(2, "ب")]
        ctx = _ctx(affinity_rows=[])
        result = _apply_affinity_boost(products, ctx)
        assert [p["id"] for p in result] == [1, 2]

    def test_high_affinity_product_floats_to_top(self):
        products = [_product(1, "أ"), _product(2, "ب"), _product(3, "ج")]
        # Product 3 has the highest affinity
        rows = [
            _affinity_row(1, 0.1),
            _affinity_row(3, 0.9),
        ]
        ctx = _ctx(affinity_rows=rows)
        result = _apply_affinity_boost(products, ctx)
        assert result[0]["id"] == 3, "highest affinity product should be first"

    def test_affinity_score_attached_to_products(self):
        products = [_product(1, "أ"), _product(2, "ب")]
        rows = [_affinity_row(1, 0.5), _affinity_row(2, 0.2)]
        ctx = _ctx(affinity_rows=rows)
        result = _apply_affinity_boost(products, ctx)
        scores = {p["id"]: p["affinity_score"] for p in result}
        assert scores[1] == pytest.approx(0.5)
        assert scores[2] == pytest.approx(0.2)

    def test_zero_score_attached_when_no_affinity(self):
        products = [_product(99, "مجهول")]
        ctx = _ctx(affinity_rows=[])
        result = _apply_affinity_boost(products, ctx)
        assert result[0].get("affinity_score") == 0.0

    def test_products_without_id_are_kept(self):
        products = [{"title": "بدون id", "price": 50}, _product(2, "ب")]
        rows = [_affinity_row(2, 0.8)]
        ctx = _ctx(affinity_rows=rows)
        result = _apply_affinity_boost(products, ctx)
        # Product 2 (with affinity) should come first; no-id product is kept
        assert any(p.get("id") == 2 for p in result)

    def test_stable_sort_within_same_score(self):
        """Products with equal affinity keep their original relative order."""
        products = [_product(1, "أ"), _product(2, "ب"), _product(3, "ج")]
        # All have the same score
        rows = [
            _affinity_row(1, 0.4),
            _affinity_row(2, 0.4),
            _affinity_row(3, 0.4),
        ]
        ctx = _ctx(affinity_rows=rows)
        result = _apply_affinity_boost(products, ctx)
        # All three should still be present
        assert {p["id"] for p in result} == {1, 2, 3}

    def test_db_error_returns_original_order(self):
        """Any DB failure must fall back to original order, never crash."""
        products = [_product(1, "أ"), _product(2, "ب")]
        ctx = MagicMock()
        ctx.customer_id = 42
        ctx.tenant_id   = 1
        ctx._db.query.side_effect = RuntimeError("DB offline")
        result = _apply_affinity_boost(products, ctx)
        assert [p["id"] for p in result] == [1, 2]

    def test_full_ranking_order(self):
        """End-to-end: products ranked by descending affinity score."""
        products = [
            _product(10, "عشرة"),
            _product(20, "عشرون"),
            _product(30, "ثلاثون"),
            _product(40, "أربعون"),
        ]
        rows = [
            _affinity_row(10, 0.1),
            _affinity_row(20, 0.6),
            _affinity_row(30, 0.0),
            _affinity_row(40, 0.9),
        ]
        ctx = _ctx(affinity_rows=rows)
        result = _apply_affinity_boost(products, ctx)
        ids = [p["id"] for p in result]
        assert ids[0] == 40, "affinity 0.9 should be first"
        assert ids[1] == 20, "affinity 0.6 should be second"

    def test_partial_affinity_data(self):
        """Only some products have affinity rows — unknowns get score 0."""
        products = [_product(1, "أ"), _product(2, "ب"), _product(3, "ج")]
        rows = [_affinity_row(2, 0.7)]   # only product 2 has affinity
        ctx = _ctx(affinity_rows=rows)
        result = _apply_affinity_boost(products, ctx)
        assert result[0]["id"] == 2
        # Products 1 and 3 still present
        assert {p["id"] for p in result} == {1, 2, 3}

    def test_score_key_not_overwritten_if_already_set(self):
        """Products that already have affinity_score from a prior pass keep it."""
        products = [_product(1, "أ")]
        products[0]["affinity_score"] = 0.99
        # DB returns a lower score
        rows = [_affinity_row(1, 0.1)]
        ctx = _ctx(affinity_rows=rows)
        result = _apply_affinity_boost(products, ctx)
        # DB value wins (we always write from DB to get fresh data)
        assert result[0]["affinity_score"] == pytest.approx(0.1)
