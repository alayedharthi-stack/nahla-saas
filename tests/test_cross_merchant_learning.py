"""
tests/test_cross_merchant_learning.py
─────────────────────────────────────
Unit tests for the anonymized cross-merchant learning store + schema.

These tests confirm three properties:

1. The trace schema strictly rejects events that still carry raw fields.
2. The tenant hash is deterministic for the same salt and never the raw id.
3. ``CrossMerchantLearningStore`` only writes anonymized rows and skips
   silently when the master switch is off or the model is not available.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _import_schema():
    from modules.ai.security import (
        FORBIDDEN_TRACE_KEYS,
        LearningTier,
        OutcomeKind,
        TraceEvent,
        UIMode,
        anonymize_tenant,
        sanitize_extra,
        validate_anonymized,
        value_bucket,
    )
    return {
        "FORBIDDEN_TRACE_KEYS": FORBIDDEN_TRACE_KEYS,
        "LearningTier": LearningTier,
        "OutcomeKind": OutcomeKind,
        "TraceEvent": TraceEvent,
        "UIMode": UIMode,
        "anonymize_tenant": anonymize_tenant,
        "sanitize_extra": sanitize_extra,
        "validate_anonymized": validate_anonymized,
        "value_bucket": value_bucket,
    }


# ── anonymize_tenant ─────────────────────────────────────────────────────────

class TestAnonymizeTenant:
    def test_deterministic_for_same_salt(self):
        s = _import_schema()
        a = s["anonymize_tenant"](42, salt="fixed")
        b = s["anonymize_tenant"](42, salt="fixed")
        assert a == b

    def test_different_for_different_tenants(self):
        s = _import_schema()
        a = s["anonymize_tenant"](1, salt="fixed")
        b = s["anonymize_tenant"](2, salt="fixed")
        assert a != b

    def test_changes_with_salt(self):
        s = _import_schema()
        a = s["anonymize_tenant"](42, salt="alpha")
        b = s["anonymize_tenant"](42, salt="beta")
        assert a != b

    def test_truncated_length(self):
        s = _import_schema()
        h = s["anonymize_tenant"](42, salt="x")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


# ── value_bucket ────────────────────────────────────────────────────────────

class TestValueBucket:
    def test_zero_is_zero_bucket(self):
        s = _import_schema()
        assert s["value_bucket"](0) == "zero"

    def test_small_value(self):
        s = _import_schema()
        assert s["value_bucket"](20) == "0_50"

    def test_medium_value(self):
        s = _import_schema()
        assert s["value_bucket"](220) == "100_250"

    def test_high_value(self):
        s = _import_schema()
        assert s["value_bucket"](6000) == "5000_plus"

    def test_unknown_for_garbage(self):
        s = _import_schema()
        assert s["value_bucket"]("not-a-number") == "unknown"


# ── sanitize_extra ──────────────────────────────────────────────────────────

class TestSanitizeExtra:
    def test_drops_unknown_keys(self):
        s = _import_schema()
        clean = s["sanitize_extra"]({
            "fact_guard_modified": True,
            "phone": "+966500000000",
            "message": "hello",
        })
        assert clean == {"fact_guard_modified": True}

    def test_returns_empty_for_non_dict(self):
        s = _import_schema()
        assert s["sanitize_extra"](None) == {}
        assert s["sanitize_extra"]("garbage") == {}


# ── validate_anonymized ─────────────────────────────────────────────────────

class TestValidateAnonymized:
    def _good_event(self):
        s = _import_schema()
        return s["TraceEvent"](
            tenant_hash=s["anonymize_tenant"](7, salt="fixed"),
            industry="fashion",
            intent="ask_product",
            action="search_products",
            ui_mode=s["UIMode"].LIST,
            outcome=s["OutcomeKind"].PRODUCT_PRESENTED,
            value_bucket="100_250",
            turn_index=2,
            model_path="rule",
            latency_ms=120,
            tier=s["LearningTier"].GLOBAL,
            extra={"fact_guard_modified": False},
        )

    def test_passes_for_clean_event(self):
        s = _import_schema()
        clean = s["validate_anonymized"](self._good_event())
        assert clean.industry == "fashion"
        assert clean.outcome == "product_presented"

    def test_rejects_merchant_tier(self):
        s = _import_schema()
        evt = self._good_event()
        evt.tier = s["LearningTier"].MERCHANT
        with pytest.raises(ValueError):
            s["validate_anonymized"](evt)

    def test_rejects_unknown_tier(self):
        s = _import_schema()
        evt = self._good_event()
        evt.tier = "weird"
        with pytest.raises(ValueError):
            s["validate_anonymized"](evt)

    def test_rejects_raw_tenant_hash(self):
        s = _import_schema()
        evt = self._good_event()
        evt.tenant_hash = "7"  # raw id pretending to be hash
        with pytest.raises(ValueError):
            s["validate_anonymized"](evt)

    def test_rejects_forbidden_extra_key(self):
        s = _import_schema()
        evt = self._good_event()
        # Bypass sanitize_extra by injecting after construction.
        evt.extra = {"phone": "+966500000000"}
        with pytest.raises(ValueError):
            s["validate_anonymized"](evt)

    def test_rejects_non_event_input(self):
        s = _import_schema()
        with pytest.raises(ValueError):
            s["validate_anonymized"]({"not": "an event"})  # type: ignore[arg-type]


# ── CrossMerchantLearningStore ──────────────────────────────────────────────

class TestCrossMerchantStore:
    def _good_event(self):
        s = _import_schema()
        return s["TraceEvent"](
            tenant_hash=s["anonymize_tenant"](7, salt="fixed"),
            industry="fashion",
            intent="ask_product",
            action="search_products",
            ui_mode=s["UIMode"].LIST,
            outcome=s["OutcomeKind"].PRODUCT_PRESENTED,
            value_bucket="100_250",
            tier=s["LearningTier"].GLOBAL,
        )

    def test_record_skips_when_disabled(self, monkeypatch):
        from modules.ai.security import CrossMerchantLearningStore
        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: False)
        )
        db = MagicMock()
        result = CrossMerchantLearningStore(db).record(self._good_event())
        assert result is None
        db.add.assert_not_called()

    def test_record_writes_only_anonymized_columns(self, monkeypatch):
        # Ensure the canonical models module is the one importable as
        # ``database.models`` (some other test setups add a stub module
        # called ``database`` that shadows it).
        import importlib
        import sys as _sys
        models_path = REPO_ROOT / "database" / "models.py"
        spec = importlib.util.spec_from_file_location("database.models", models_path)
        db_models = importlib.util.module_from_spec(spec)
        # Register a parent package shim so ``database.models`` resolves cleanly.
        if "database" not in _sys.modules or not getattr(_sys.modules["database"], "__path__", None):
            pkg = type(_sys)("database")
            pkg.__path__ = [str(REPO_ROOT / "database")]
            _sys.modules["database"] = pkg
        _sys.modules["database.models"] = db_models
        spec.loader.exec_module(db_models)  # type: ignore[union-attr]

        from modules.ai.security import CrossMerchantLearningStore
        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )

        captured = {}

        class CrossMerchantSignal:  # name matters: isolation layer checks __name__
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.id = 123

        monkeypatch.setattr(db_models, "CrossMerchantSignal", CrossMerchantSignal)

        telemetry_db = MagicMock()
        operational_db = MagicMock()
        store = CrossMerchantLearningStore(
            operational_db,
            session_factory=lambda: telemetry_db,
        )
        new_id = store.record(self._good_event())

        assert new_id == 123
        operational_db.add.assert_not_called()
        telemetry_db.add.assert_called_once()
        # Only safe categorical columns must appear
        assert set(captured.keys()) == {
            "tenant_hash", "industry", "intent", "action", "ui_mode",
            "outcome", "value_bucket", "turn_index", "model_path",
            "latency_ms", "tier", "extra",
        }
        # Not a raw integer tenant id leaking in
        assert isinstance(captured["tenant_hash"], str)
        assert captured["tenant_hash"] != "7"

    def test_record_rejects_event_with_raw_phone(self, monkeypatch):
        from modules.ai.security import CrossMerchantLearningStore
        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        evt = self._good_event()
        evt.extra = {"phone": "+966500000000"}
        db = MagicMock()
        with pytest.raises(ValueError):
            CrossMerchantLearningStore(db).record(evt)
        db.add.assert_not_called()
