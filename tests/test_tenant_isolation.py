"""
tests/test_tenant_isolation.py
──────────────────────────────
Unit tests for the foundational TenantIsolationLayer.

These tests intentionally exercise the *contract* — not the integration —
because the layer must hold even when no DB or LLM is reachable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _import_layer():
    from modules.ai.security import (
        TenantContext,
        TenantIsolationLayer,
        TenantIsolationViolation,
    )
    return TenantContext, TenantIsolationLayer, TenantIsolationViolation


# ── make_context ─────────────────────────────────────────────────────────────

class TestMakeContext:
    def test_rejects_none_tenant_id(self):
        TenantContext, Layer, Violation = _import_layer()
        with pytest.raises(Violation):
            Layer.make_context(None)

    def test_rejects_zero_tenant_id(self):
        TenantContext, Layer, Violation = _import_layer()
        with pytest.raises(Violation):
            Layer.make_context(0)

    def test_rejects_negative_tenant_id(self):
        TenantContext, Layer, Violation = _import_layer()
        with pytest.raises(Violation):
            Layer.make_context(-5)

    def test_rejects_non_numeric_tenant_id(self):
        TenantContext, Layer, Violation = _import_layer()
        with pytest.raises(Violation):
            Layer.make_context("abc")

    def test_accepts_string_int(self):
        TenantContext, Layer, _ = _import_layer()
        ctx = Layer.make_context("42")
        assert ctx.tenant_id == 42

    def test_rejects_negative_customer_id(self):
        _, Layer, Violation = _import_layer()
        with pytest.raises(Violation):
            Layer.make_context(1, customer_id=-1)

    def test_normalises_industry_to_lower(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(1, industry="Fashion")
        assert ctx.industry == "fashion"


# ── assert_active ────────────────────────────────────────────────────────────

class TestAssertActive:
    def test_rejects_none(self):
        _, Layer, Violation = _import_layer()
        with pytest.raises(Violation):
            Layer.assert_active(None)

    def test_rejects_wrong_type(self):
        _, Layer, Violation = _import_layer()
        with pytest.raises(Violation):
            Layer.assert_active("ctx")  # type: ignore[arg-type]


# ── scope_query ──────────────────────────────────────────────────────────────

class TestScopeQuery:
    def _model_with_tenant(self):
        m = MagicMock()
        m.__name__ = "Order"
        m.tenant_id = MagicMock()
        return m

    def test_appends_tenant_filter(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(7)
        model = self._model_with_tenant()
        query = MagicMock()
        Layer.scope_query(query, model, ctx)
        query.filter.assert_called_once()

    def test_skips_for_non_tenant_models(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(7)
        model = MagicMock()
        model.__name__ = "Tenant"
        query = MagicMock()
        result = Layer.scope_query(query, model, ctx)
        query.filter.assert_not_called()
        assert result is query

    def test_raises_when_model_lacks_tenant_id(self):
        _, Layer, Violation = _import_layer()
        ctx = Layer.make_context(7)
        model = MagicMock(spec=["__name__"])  # no tenant_id attr
        model.__name__ = "WeirdTable"
        query = MagicMock()
        with pytest.raises(Violation):
            Layer.scope_query(query, model, ctx)


# ── assert_belongs / filter_records ──────────────────────────────────────────

class TestAssertBelongs:
    def test_passes_when_match(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(3)
        rec = MagicMock(tenant_id=3)
        Layer.assert_belongs(rec, ctx)

    def test_rejects_when_mismatch(self):
        _, Layer, Violation = _import_layer()
        ctx = Layer.make_context(3)
        rec = MagicMock(tenant_id=99)
        with pytest.raises(Violation):
            Layer.assert_belongs(rec, ctx)

    def test_passes_when_no_tenant_attr(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(3)

        class Plain:
            pass

        Layer.assert_belongs(Plain(), ctx)

    def test_filter_drops_cross_tenant_records(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(3)
        records = [
            MagicMock(tenant_id=3),
            MagicMock(tenant_id=5),
            MagicMock(tenant_id=3),
        ]
        kept = Layer.filter_records(records, ctx)
        assert len(kept) == 2


# ── verify_payload ───────────────────────────────────────────────────────────

class TestVerifyPayload:
    def test_overrides_missing_tenant_id(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(11)
        clean = Layer.verify_payload({"query": "abc"}, ctx)
        assert clean["tenant_id"] == 11
        assert clean["query"] == "abc"

    def test_rejects_mismatched_tenant_id(self):
        _, Layer, Violation = _import_layer()
        ctx = Layer.make_context(11)
        with pytest.raises(Violation):
            Layer.verify_payload({"tenant_id": 99, "query": "x"}, ctx)

    def test_accepts_matching_tenant_id(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(11)
        clean = Layer.verify_payload({"tenant_id": 11, "query": "x"}, ctx)
        assert clean["tenant_id"] == 11

    def test_rejects_non_int_tenant_id(self):
        _, Layer, Violation = _import_layer()
        ctx = Layer.make_context(11)
        with pytest.raises(Violation):
            Layer.verify_payload({"tenant_id": "abc"}, ctx)

    def test_non_dict_payload_returns_safe_dict(self):
        _, Layer, _ = _import_layer()
        ctx = Layer.make_context(11)
        clean = Layer.verify_payload("not-a-dict", ctx)  # type: ignore[arg-type]
        assert clean == {"tenant_id": 11}


# ── CommerceToolRuntime hard-fails on bad tenant ────────────────────────────

class TestRuntimeIsolation:
    def test_runtime_rejects_zero_tenant(self):
        from modules.ai.commerce.runtime import CommerceToolRuntime
        from modules.ai.security import TenantIsolationViolation
        with pytest.raises(TenantIsolationViolation):
            CommerceToolRuntime(MagicMock(), tenant_id=0)

    def test_runtime_rejects_negative_tenant(self):
        from modules.ai.commerce.runtime import CommerceToolRuntime
        from modules.ai.security import TenantIsolationViolation
        with pytest.raises(TenantIsolationViolation):
            CommerceToolRuntime(MagicMock(), tenant_id=-2)

    def test_runtime_rejects_payload_with_wrong_tenant(self):
        import asyncio
        from modules.ai.commerce.runtime import CommerceToolRuntime

        runtime = CommerceToolRuntime(MagicMock(), tenant_id=1)
        result = asyncio.run(
            runtime.execute("search_products", {"tenant_id": 2, "query": "x"})
        )
        assert result.ok is False
        assert result.error == "tenant_isolation_violation"
