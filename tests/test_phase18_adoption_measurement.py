"""
tests/test_phase18_adoption_measurement.py
──────────────────────────────────────────
Phase 1.8 — Policy Observation & Adoption Measurement.

Coverage
────────
1. Trace metadata
   * ``_emit_anonymous_signal`` records ``hint_*`` fields when a
     ``policy_hint`` is attached to the decision.
   * The recorded event still passes ``validate_anonymized``
     (no raw tenant / store data leaked through hint metadata).
   * Absence of ``policy_hint`` keeps existing behavior unchanged
     (only ``hint_present=False`` is added — every other field is
     identical to a Phase 1.6/1.7 trace).
   * Failures inside ``_collect_hint_metadata`` (malformed hint dict)
     never break the turn — emission still proceeds with
     ``hint_present=False``.

2. Adoption aggregator (pure functions)
   * Deterministic counts for alignment / conversion buckets.
   * Per-intent and per-(industry, intent) breakdowns are exact.
   * Empty input → zero-valued, NaN-free report.
   * Signals without ``hint_present`` only land in ``hint_absent_count``.

3. Readiness gate
   * Below-sample buckets are blocked with an ``insufficient_sample``
     reason.
   * Below-uplift buckets are blocked with an ``insufficient_uplift``
     reason even when alignment is high.
   * Sensitive intents block on ANY negative uplift, regardless of
     other gates.
   * A clean bucket (large sample, high uplift, high alignment) is
     reported as ready.

4. No-break guarantee
   * A failing ``CrossMerchantLearningStore.record`` does not raise out
     of ``DefaultMemoryUpdater.update`` (regression coverage for
     Phase 1.6 promise, re-asserted with the new hint metadata path
     in place).
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _ensure_database_models_importable() -> None:
    """Force-resolve ``database.models`` even when another test left a
    partial ``database`` namespace package on ``sys.modules``."""
    if "database.models" in sys.modules:
        return
    models_path = REPO_ROOT / "database" / "models.py"
    if not models_path.exists():
        return
    spec = importlib.util.spec_from_file_location("database.models", models_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    if "database" not in sys.modules or not getattr(sys.modules["database"], "__path__", None):
        pkg = type(sys)("database")
        pkg.__path__ = [str(REPO_ROOT / "database")]
        sys.modules["database"] = pkg
    sys.modules["database.models"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        pass


_ensure_database_models_importable()


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures — mirror the helpers used in test_phase16_propagation_emission.py
# so a hint-aware variant of the same trace pipeline can be exercised in isolation.
# ─────────────────────────────────────────────────────────────────────────────

def _make_facts(**kw):
    from modules.ai.brain.types import CommerceFacts
    base = dict(
        has_products=True, product_count=2, in_stock_count=2,
        has_active_integration=True, orderable=True,
        snapshot_fresh=True, store_name="x",
    )
    base.update(kw)
    return CommerceFacts(**base)


def _make_brain_ctx(*, action_type="search_products", intent_name="ask_product",
                    confidence=0.85, total=None, with_products=True):
    """Build a minimal BrainContext / Decision / ActionResult triple."""
    from modules.ai.brain.types import (
        ActionResult,
        BrainContext,
        Decision,
        Intent,
        MerchantConversationState,
        SalesContextSnapshot,
    )
    from modules.ai.security import TenantIsolationLayer

    state = MerchantConversationState(greeted=True, stage="exploring", turn=3)
    intent = Intent(name=intent_name, confidence=confidence, raw_message="msg")
    ctx = BrainContext(
        tenant_id=11,
        customer_phone="+966500000001",
        message="msg",
        intent=intent,
        state=state,
        facts=_make_facts(),
        history=[{"direction": "in", "body": "أبغى منتج"}],
        profile={"preferred_language": "ar"},
        sales_context=SalesContextSnapshot(),
        tenant_context=TenantIsolationLayer.make_context(11, customer_phone="+966500000001"),
    )

    data: Dict[str, Any] = {"chosen_path": "rule"}
    if with_products:
        data["products"] = [{"id": 1}, {"id": 2}]
    if total is not None:
        data["total"] = total

    decision = Decision(action=action_type)
    result   = ActionResult(success=True, data=data)
    return ctx, decision, result


# ═══════════════════════════════════════════════════════════════════════════
# 1. Trace metadata
# ═══════════════════════════════════════════════════════════════════════════

class TestTraceHintMetadata:
    def _capture_event(self, monkeypatch):
        """Helper — patch CrossMerchantLearningStore.record to capture event."""
        from modules.ai.security import CrossMerchantLearningStore, validate_anonymized

        captured: Dict[str, Any] = {}

        def _fake_record(self, event):
            captured["event"]     = event
            captured["validated"] = validate_anonymized(event)
            return 1

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake_record)
        return captured

    def test_records_hint_metadata_when_present_and_aligned(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater

        captured = self._capture_event(monkeypatch)
        ctx, decision, result = _make_brain_ctx(action_type="search_products")
        decision.args["policy_hint"] = {
            "scope":              "global",
            "industry":           "*",
            "intent":             "ask_product",
            "recommended_action": "search_products",
            "recommended_ui":     "list",
            "confidence":         0.82,
            "sample_size":        140,
            "matches_inner":      True,
        }

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        DefaultMemoryUpdater()._emit_anonymous_signal(
            db, ctx, decision, result, stage_before="exploring", latency_ms=50,
        )

        evt = captured["event"]
        # Hint metadata is present and aligned.
        assert evt.extra["hint_present"] is True
        assert evt.extra["hint_aligned"] is True
        assert evt.extra["hint_used"] is False  # Phase 1.8 is observation only
        assert evt.extra["hint_action"] == "search_products"
        assert evt.extra["hint_ui"] == "list"
        assert evt.extra["hint_scope"] == "global"
        assert evt.extra["hint_confidence_bucket"] == "high"
        assert evt.extra["hint_sample_bucket"] == "100_500"

        # Validated round-trip preserves hint keys.
        assert captured["validated"].extra["hint_present"] is True
        assert captured["validated"].extra["hint_action"] == "search_products"

    def test_records_hint_misaligned_when_action_differs(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater

        captured = self._capture_event(monkeypatch)
        ctx, decision, result = _make_brain_ctx(action_type="recommend_addon")
        # Hint says "search_products" but inner picked "recommend_addon".
        decision.args["policy_hint"] = {
            "scope":              "vertical",
            "industry":           "fashion",
            "intent":             "ask_product",
            "recommended_action": "search_products",
            "recommended_ui":     "product_cards",
            "confidence":         0.55,
            "sample_size":        45,
            "matches_inner":      False,
        }

        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )

        evt = captured["event"]
        assert evt.extra["hint_present"] is True
        assert evt.extra["hint_aligned"] is False
        assert evt.extra["hint_scope"] == "vertical"
        assert evt.extra["hint_confidence_bucket"] == "medium"
        assert evt.extra["hint_sample_bucket"] == "30_100"

    def test_records_hint_absent_when_no_policy_hint(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater

        captured = self._capture_event(monkeypatch)
        ctx, decision, result = _make_brain_ctx()
        # No policy_hint key on decision.args.

        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )

        evt = captured["event"]
        assert evt.extra["hint_present"] is False
        # Absence sentinel must be the ONLY hint_* key — no leakage.
        for k in ("hint_action", "hint_aligned", "hint_ui", "hint_scope",
                  "hint_confidence_bucket", "hint_sample_bucket", "hint_used"):
            assert k not in evt.extra

    def test_malformed_hint_does_not_break_emission(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater

        captured = self._capture_event(monkeypatch)
        ctx, decision, result = _make_brain_ctx()
        # Garbage type — must downgrade to hint_present=False without raising.
        decision.args["policy_hint"] = "not-a-dict"

        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )

        evt = captured["event"]
        assert evt.extra["hint_present"] is False

    def test_event_with_hint_passes_validate_anonymized_no_raw_data(self, monkeypatch):
        """End-to-end anti-leak guard: hint metadata must survive
        ``validate_anonymized`` and contain no forbidden keys."""
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security.trace_schema import FORBIDDEN_TRACE_KEYS

        captured = self._capture_event(monkeypatch)
        ctx, decision, result = _make_brain_ctx()
        decision.args["policy_hint"] = {
            "scope":              "global",
            "recommended_action": "search_products",
            "recommended_ui":     "list",
            "confidence":         0.91,
            "sample_size":        2500,
            "matches_inner":      True,
            # Even if a future caller accidentally smuggled raw fields,
            # the validator + whitelist must drop them.
            "phone":              "+966500000099",
            "tenant_id":          42,
        }

        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )

        validated = captured["validated"]
        for forbidden in FORBIDDEN_TRACE_KEYS:
            assert forbidden not in validated.extra, (
                f"forbidden key {forbidden!r} leaked into validated trace"
            )
        # Confidence bucket retained; raw confidence value never present.
        assert validated.extra["hint_confidence_bucket"] == "very_high"
        assert "confidence" not in validated.extra
        assert validated.extra["hint_sample_bucket"] == "2k_plus"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Adoption aggregator
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _Sig:
    intent: str
    industry: str = "*"
    outcome: str = "browse"
    extra: Dict[str, Any] = field(default_factory=dict)


def _hint_signal(*, intent, industry, aligned, converted, action="search_products"):
    return _Sig(
        intent=intent, industry=industry,
        outcome=("conversion" if converted else "browse"),
        extra={
            "hint_present": True,
            "hint_aligned": aligned,
            "hint_action":  action,
        },
    )


class TestAdoptionAggregator:
    def test_empty_signals_returns_zero_report(self):
        from modules.ai.learning import compute_adoption_metrics

        report = compute_adoption_metrics([])
        assert report.overall.sample_size == 0
        assert report.overall.hint_alignment_rate == 0.0
        assert report.overall.conversion_when_aligned == 0.0
        assert report.overall.observed_uplift == 0.0
        assert report.by_intent == {}
        assert report.by_industry_intent == {}

    def test_alignment_and_conversion_rates_are_exact(self):
        from modules.ai.learning import compute_adoption_metrics

        signals = []
        # ask_product / fashion: 60 hinted turns
        #   45 aligned, of which 27 converted → 0.6 conv when aligned
        #   15 not aligned, of which  3 converted → 0.2 conv when not aligned
        for _ in range(27):
            signals.append(_hint_signal(intent="ask_product", industry="fashion",
                                        aligned=True, converted=True))
        for _ in range(18):
            signals.append(_hint_signal(intent="ask_product", industry="fashion",
                                        aligned=True, converted=False))
        for _ in range(3):
            signals.append(_hint_signal(intent="ask_product", industry="fashion",
                                        aligned=False, converted=True))
        for _ in range(12):
            signals.append(_hint_signal(intent="ask_product", industry="fashion",
                                        aligned=False, converted=False))

        report = compute_adoption_metrics(signals)
        stats = report.by_intent["ask_product"]

        assert stats.sample_size == 60
        assert stats.hint_present_count == 60
        assert stats.aligned_count == 45
        assert stats.not_aligned_count == 15
        assert stats.aligned_conversion_count == 27
        assert stats.not_aligned_conversion_count == 3

        assert stats.hint_alignment_rate == pytest.approx(45 / 60)
        assert stats.conversion_when_aligned == pytest.approx(27 / 45)
        assert stats.conversion_when_not_aligned == pytest.approx(3 / 15)
        assert stats.observed_uplift == pytest.approx(0.6 - 0.2)

        # Vertical breakdown carries the same bucket because all signals
        # were industry='fashion'.
        ii = report.by_industry_intent[("fashion", "ask_product")]
        assert ii.aligned_count == 45

    def test_signals_without_hint_only_count_in_absent_bucket(self):
        from modules.ai.learning import compute_adoption_metrics

        signals = [
            _Sig(intent="greeting", outcome="conversion",
                 extra={"hint_present": False}),
            _Sig(intent="greeting", outcome="browse",
                 extra={}),  # missing extra entirely
            _Sig(intent="greeting", outcome="conversion",
                 extra={"hint_present": True, "hint_aligned": True}),
        ]
        report = compute_adoption_metrics(signals)
        stats = report.by_intent["greeting"]
        assert stats.sample_size == 3
        assert stats.hint_present_count == 1
        assert stats.hint_absent_count == 2
        assert stats.aligned_count == 1
        assert stats.aligned_conversion_count == 1
        # Not-aligned bucket is empty; rate is exactly 0 (no NaN).
        assert stats.conversion_when_not_aligned == 0.0

    def test_unknown_industry_excluded_from_industry_breakdown(self):
        from modules.ai.learning import compute_adoption_metrics

        signals = [
            _hint_signal(intent="ask_product", industry="unknown",
                         aligned=True, converted=True),
            _hint_signal(intent="ask_product", industry="*",
                         aligned=True, converted=True),
            _hint_signal(intent="ask_product", industry="fashion",
                         aligned=True, converted=True),
        ]
        report = compute_adoption_metrics(signals)
        # Only the fashion bucket lands in the per-industry rollup.
        assert list(report.by_industry_intent.keys()) == [("fashion", "ask_product")]
        # All three still hit the per-intent bucket.
        assert report.by_intent["ask_product"].sample_size == 3


# ═══════════════════════════════════════════════════════════════════════════
# 3. Readiness gate
# ═══════════════════════════════════════════════════════════════════════════

class TestReadinessGate:
    def test_below_sample_size_blocks_with_explicit_reason(self):
        from modules.ai.learning import (
            AdoptionStats,
            ReadinessGate,
        )
        stats = AdoptionStats(intent="ask_product", industry="fashion",
                              hint_present_count=20, aligned_count=15,
                              not_aligned_count=5,
                              aligned_conversion_count=10,
                              not_aligned_conversion_count=1)
        verdict = ReadinessGate(min_sample_size=100,
                                min_observed_uplift=0.05,
                                min_alignment_rate=0.3).evaluate(stats)
        assert verdict.ready is False
        assert any(r.startswith("insufficient_sample:") for r in verdict.reasons)

    def test_below_uplift_blocks_even_with_high_alignment(self):
        from modules.ai.learning import AdoptionStats, ReadinessGate
        # 100 hinted, 90 aligned, conversions equal in both buckets
        stats = AdoptionStats(intent="ask_product",
                              hint_present_count=100,
                              aligned_count=90,
                              not_aligned_count=10,
                              aligned_conversion_count=45,  # 0.5
                              not_aligned_conversion_count=5)  # 0.5
        verdict = ReadinessGate(min_sample_size=100,
                                min_observed_uplift=0.05,
                                min_alignment_rate=0.3).evaluate(stats)
        assert verdict.ready is False
        assert any(r.startswith("insufficient_uplift:") for r in verdict.reasons)

    def test_sensitive_intent_blocks_on_negative_uplift(self):
        from modules.ai.learning import AdoptionStats, ReadinessGate
        # large sample, high alignment, NEGATIVE uplift — sensitive intent
        # must reject regardless of other gates.
        stats = AdoptionStats(intent="checkout",
                              hint_present_count=500,
                              aligned_count=400,
                              not_aligned_count=100,
                              aligned_conversion_count=200,   # 0.50
                              not_aligned_conversion_count=70)  # 0.70
        verdict = ReadinessGate(min_sample_size=100,
                                min_observed_uplift=-1.0,  # disable uplift gate
                                min_alignment_rate=0.0).evaluate(stats)
        assert verdict.ready is False
        assert "sensitive_regression" in verdict.reasons

    def test_clean_bucket_is_ready(self):
        from modules.ai.learning import AdoptionStats, ReadinessGate
        stats = AdoptionStats(intent="ask_product", industry="fashion",
                              hint_present_count=200,
                              aligned_count=160,                 # 0.80 alignment
                              not_aligned_count=40,
                              aligned_conversion_count=120,      # 0.75
                              not_aligned_conversion_count=12)   # 0.30
        verdict = ReadinessGate(min_sample_size=100,
                                min_observed_uplift=0.05,
                                min_alignment_rate=0.3).evaluate(stats)
        assert verdict.ready is True
        assert verdict.reasons == ()

    def test_evaluate_report_partitions_intents(self):
        from modules.ai.learning import (
            AdoptionStats,
            AdoptionReport,
            ReadinessGate,
        )
        ready = AdoptionStats(intent="ask_product",
                              hint_present_count=200,
                              aligned_count=140,
                              not_aligned_count=60,
                              aligned_conversion_count=110,
                              not_aligned_conversion_count=15)
        blocked = AdoptionStats(intent="checkout",
                                hint_present_count=10,
                                aligned_count=5, not_aligned_count=5,
                                aligned_conversion_count=2,
                                not_aligned_conversion_count=1)
        report = AdoptionReport(
            overall=AdoptionStats(intent="*"),
            by_intent={"ask_product": ready, "checkout": blocked},
        )
        summary = ReadinessGate().evaluate_report(report)
        assert summary.ready_intents == ["ask_product"]
        assert summary.blocked_intents == ["checkout"]


# ═══════════════════════════════════════════════════════════════════════════
# 4. No-break guarantee
# ═══════════════════════════════════════════════════════════════════════════

class TestNoBreakGuarantee:
    def test_emission_failure_does_not_break_update(self, monkeypatch):
        """Even with the new hint metadata path, ``update`` is silent on
        an underlying ``record`` outage."""
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import CrossMerchantLearningStore

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )

        def _boom(self, event):
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _boom)

        ctx, decision, result = _make_brain_ctx()
        decision.args["policy_hint"] = {
            "recommended_action": "search_products",
            "recommended_ui":     "list",
            "scope":              "global",
            "confidence":         0.7,
            "sample_size":        120,
            "matches_inner":      True,
        }

        # No exception must propagate.
        DefaultMemoryUpdater().update(
            db=MagicMock(),
            ctx=ctx,
            decision=decision,
            result=result,
            reply="hello",
            stage_before="exploring",
            latency_ms=10,
        )

    def test_absence_of_hint_keeps_extra_keys_stable(self, monkeypatch):
        """Regression guard: the only NEW key when no hint is present is
        ``hint_present=False``. All previously-existing keys are unchanged."""
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import CrossMerchantLearningStore, validate_anonymized

        captured: Dict[str, Any] = {}

        def _fake_record(self, event):
            captured["event"] = validate_anonymized(event)
            return 1

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake_record)

        ctx, decision, result = _make_brain_ctx()
        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )

        evt = captured["event"]
        # Phase 1.6 keys still present
        assert "stage_before" in evt.extra
        assert "decision_path" in evt.extra
        assert "intent_confidence_bucket" in evt.extra
        assert "rule_version" in evt.extra
        # Phase 1.8 sentinel present
        assert evt.extra["hint_present"] is False
        # No accidental hint_* leakage
        leak_keys = {"hint_action", "hint_ui", "hint_scope",
                     "hint_aligned", "hint_used"}
        assert leak_keys.isdisjoint(evt.extra.keys())
