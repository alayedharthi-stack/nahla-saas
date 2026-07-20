"""
tests/test_phase16_propagation_emission.py
──────────────────────────────────────────
Phase 1.6 — Context Propagation + Anonymous Signal Emission.

These tests verify two contracts added by Phase 1.6 on top of the
existing Phase 1.5 isolation foundation:

1. ``TenantContext`` propagation:
   * ``MerchantBrain.process`` builds a single ``TenantContext`` and forwards
     it to ``SalesContextLoader.load`` and to every ``BrainContext`` consumed
     by handlers / memory updater.
   * The pipeline rejects a mismatched ``TenantContext`` passed in by the
     caller (defense in depth).

2. Anonymous signal emission:
   * ``DefaultMemoryUpdater._emit_anonymous_signal`` builds a ``TraceEvent``
     that passes ``validate_anonymized`` end-to-end.
   * The event contains zero raw identifiers / PII / prices / titles.
   * A failure in ``CrossMerchantLearningStore.record`` does NOT bubble up
     and never affects the customer reply path.
   * When the master switch is OFF the writer is never called.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _ensure_database_models_importable() -> None:
    """Some test environments shadow the ``database`` package with a stub
    that lacks ``models``.  Force-resolve the real file so ``from
    database.models import …`` succeeds inside the MemoryUpdater under test.
    Idempotent — safe to call multiple times.
    """
    import importlib.util as _ilu

    if "database.models" in sys.modules:
        return
    models_path = REPO_ROOT / "database" / "models.py"
    if not models_path.exists():
        return
    spec = _ilu.spec_from_file_location("database.models", models_path)
    if spec is None or spec.loader is None:
        return
    module = _ilu.module_from_spec(spec)
    if "database" not in sys.modules or not getattr(sys.modules["database"], "__path__", None):
        pkg = type(sys)("database")
        pkg.__path__ = [str(REPO_ROOT / "database")]
        sys.modules["database"] = pkg
    sys.modules["database.models"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # If ORM evaluation fails (e.g. missing optional driver), still
        # leave the half-built module so ``from database.models import …``
        # raises a recognizable ImportError that callers swallow.
        pass


_ensure_database_models_importable()


# ── Local helpers (copied tiny shape from test_merchant_brain) ──────────────

def _run(coro):
    return asyncio.run(coro)


def _make_facts(has_products: bool = True):
    from modules.ai.brain.types import CommerceFacts
    return CommerceFacts(
        has_products=has_products,
        product_count=5 if has_products else 0,
        in_stock_count=5 if has_products else 0,
        has_active_integration=True,
        orderable=has_products,
        has_coupons=False,
        snapshot_fresh=True,
        store_name="متجر تجريبي",
        store_url="https://store.example.com",
        store_description="متجر تجريبي",
        store_contact_phone="+966500000001",
        shipping_policy="الشحن خلال 2-4 أيام عمل",
        support_hours="9am-10pm",
        shipping_methods=["سمسا"],
        integration_platform="salla",
    )


def _make_state(**kw):
    from modules.ai.brain.types import MerchantConversationState
    return MerchantConversationState(**kw)


def _build_brain(*, classifier, state_store, facts_loader, sales_context_loader=None,
                 memory_updater=None):
    from modules.ai.brain.pipeline import MerchantBrain
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.policy import PassThroughPolicyGate
    from modules.ai.brain.execution.executor import DefaultActionExecutor
    from modules.ai.brain.compose.responder import DefaultComposer
    from modules.ai.brain.memory.updater import DefaultMemoryUpdater

    return MerchantBrain(
        classifier        = classifier,
        state_store       = state_store,
        facts_loader      = facts_loader,
        decision_engine   = DefaultDecisionEngine(),
        policy_gate       = PassThroughPolicyGate(),
        executor          = DefaultActionExecutor(),
        composer          = DefaultComposer(),
        memory_updater    = memory_updater or _NullMemoryUpdater(),
        sales_context_loader = sales_context_loader,
    )


def _make_classifier(intent):
    cls = MagicMock()
    cls.classify = AsyncMock(return_value=intent)
    return cls


def _make_state_store(state):
    store = MagicMock()
    store.load.return_value = state
    store.save.return_value = None
    store.transition.return_value = state
    return store


def _make_facts_loader(facts):
    loader = MagicMock()
    loader.load.return_value = facts
    return loader


class _NullMemoryUpdater:
    def update(self, *a, **kw):
        return None


class _RecordingSalesContextLoader:
    """Spy that captures the kwargs the pipeline passes into ``load``."""
    def __init__(self):
        self.called_with: Dict[str, Any] = {}
        from modules.ai.brain.types import SalesContextSnapshot
        self._snapshot = SalesContextSnapshot()

    def load(self, db, **kw):
        self.called_with = dict(kw)
        return self._snapshot


# ═══════════════════════════════════════════════════════════════════════════
# 1. TenantContext propagation
# ═══════════════════════════════════════════════════════════════════════════

class TestTenantContextPropagation:
    def test_pipeline_builds_context_when_caller_omits_it(self):
        from modules.ai.brain.types import (
            INTENT_GREETING,
            Intent,
        )
        intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
        spy = _RecordingSalesContextLoader()
        b = _build_brain(
            classifier=_make_classifier(intent),
            state_store=_make_state_store(_make_state(greeted=False)),
            facts_loader=_make_facts_loader(_make_facts()),
            sales_context_loader=spy,
        )

        db = MagicMock()
        _run(b.process(
            db=db,
            tenant_id=42,
            customer_phone="+966500000001",
            message="مرحبا",
            history=[],
            profile={},
        ))

        # The sales context loader must receive a fully built TenantContext.
        from modules.ai.security import TenantContext
        passed = spy.called_with.get("tenant_context")
        assert isinstance(passed, TenantContext)
        assert passed.tenant_id == 42
        assert passed.customer_phone == "+966500000001"

    def test_pipeline_reuses_caller_supplied_context(self):
        from modules.ai.brain.types import INTENT_GREETING, Intent
        from modules.ai.security import TenantIsolationLayer

        intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
        spy = _RecordingSalesContextLoader()
        b = _build_brain(
            classifier=_make_classifier(intent),
            state_store=_make_state_store(_make_state(greeted=False)),
            facts_loader=_make_facts_loader(_make_facts()),
            sales_context_loader=spy,
        )

        supplied = TenantIsolationLayer.make_context(
            42, customer_phone="+966500000001"
        )
        _run(b.process(
            db=MagicMock(),
            tenant_id=42,
            customer_phone="+966500000001",
            message="مرحبا",
            history=[],
            profile={},
            tenant_context=supplied,
        ))

        # Same identity object reused — no fresh TenantContext built.
        assert spy.called_with["tenant_context"] is supplied

    def test_pipeline_rejects_mismatched_context(self):
        from modules.ai.brain.types import INTENT_GREETING, Intent
        from modules.ai.security import TenantIsolationLayer, TenantIsolationViolation

        intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
        b = _build_brain(
            classifier=_make_classifier(intent),
            state_store=_make_state_store(_make_state(greeted=False)),
            facts_loader=_make_facts_loader(_make_facts()),
        )
        wrong = TenantIsolationLayer.make_context(99)

        with pytest.raises(TenantIsolationViolation):
            _run(b.process(
                db=MagicMock(),
                tenant_id=42,
                customer_phone="+966500000001",
                message="مرحبا",
                history=[],
                profile={},
                tenant_context=wrong,
            ))

    def test_brain_context_carries_tenant_context_into_handlers(self):
        """The MemoryUpdater receives a BrainContext with tenant_context set."""
        from modules.ai.brain.types import INTENT_GREETING, Intent
        from modules.ai.security import TenantContext

        intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
        captured = {}

        class _CapturingUpdater:
            def update(self, db, ctx, decision, result, reply, stage_before, latency_ms):
                captured["tenant_context"] = ctx.tenant_context

        b = _build_brain(
            classifier=_make_classifier(intent),
            state_store=_make_state_store(_make_state(greeted=False)),
            facts_loader=_make_facts_loader(_make_facts()),
            memory_updater=_CapturingUpdater(),
        )

        _run(b.process(
            db=MagicMock(),
            tenant_id=7,
            customer_phone="+966500000099",
            message="مرحبا",
            history=[],
            profile={},
        ))

        ctx_value = captured.get("tenant_context")
        assert isinstance(ctx_value, TenantContext)
        assert ctx_value.tenant_id == 7


# ═══════════════════════════════════════════════════════════════════════════
# 2. Anonymous signal emission
# ═══════════════════════════════════════════════════════════════════════════

def _make_brain_ctx(*, action_type="search_products", with_products=True,
                    intent_name="ask_product", confidence=0.85, total=None):
    """Build a minimal BrainContext + Decision + ActionResult shaped like a
    real Phase 2 turn would produce.  No DB / external calls involved."""
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
    facts = _make_facts(has_products=True)
    intent = Intent(name=intent_name, confidence=confidence, raw_message="msg")
    ctx = BrainContext(
        tenant_id=11,
        customer_phone="+966500000001",
        message="msg",
        intent=intent,
        state=state,
        facts=facts,
        history=[{"direction": "in", "body": "أبغى منتج"}],
        profile={"preferred_language": "ar"},
        sales_context=SalesContextSnapshot(),
        tenant_context=TenantIsolationLayer.make_context(11, customer_phone="+966500000001"),
    )

    data: Dict[str, Any] = {"chosen_path": "rule"}
    if with_products:
        data["products"] = [{"id": 1, "title": "X"}, {"id": 2, "title": "Y"}]
    if total is not None:
        data["total"] = total

    decision = Decision(action=action_type)
    result = ActionResult(success=True, data=data)
    return ctx, decision, result


class TestAnonymousSignalEmission:
    def test_emit_builds_event_passing_validate_anonymized(self, monkeypatch):
        """The constructed TraceEvent must round-trip through validate_anonymized."""
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import (
            CrossMerchantLearningStore,
            TraceEvent,
            validate_anonymized,
        )

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        captured: Dict[str, TraceEvent] = {}

        def _fake_record(self, event):
            captured["event"] = event
            # Re-validate to surface any silent leak.
            captured["validated"] = validate_anonymized(event)
            return 1

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake_record)

        ctx, decision, result = _make_brain_ctx(total=199.0)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        DefaultMemoryUpdater()._emit_anonymous_signal(
            db, ctx, decision, result, stage_before="greeting", latency_ms=42,
        )

        evt = captured["event"]
        assert isinstance(evt, TraceEvent)
        assert evt.action == "search_products"
        assert evt.outcome == "product_presented"
        assert evt.value_bucket == "100_250"
        assert evt.turn_index == 3
        assert evt.tenant_hash and len(evt.tenant_hash) == 16
        # Anti-leak: no raw identifiers / strings
        for forbidden in ("phone", "tenant_id", "customer_id", "title",
                          "message", "store_name", "price", "total"):
            assert forbidden not in evt.extra
        # Validated copy is identical-shape (sanitized extras stay valid)
        assert "stage_before" in captured["validated"].extra

    def test_emit_skipped_when_master_switch_off(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import CrossMerchantLearningStore

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: False)
        )
        called = {"n": 0}

        def _fake_record(self, event):
            called["n"] += 1
            return 1

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake_record)

        ctx, decision, result = _make_brain_ctx()
        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )
        assert called["n"] == 0

    def test_emit_failure_does_not_break_turn(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import CrossMerchantLearningStore

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )

        def _boom(self, event):
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _boom)

        ctx, decision, result = _make_brain_ctx()
        db = MagicMock()
        # Must not raise — emission is best-effort
        DefaultMemoryUpdater()._emit_anonymous_signal(
            db, ctx, decision, result, stage_before="x", latency_ms=1,
        )

    def test_full_update_with_emission_failure_still_returns(self, monkeypatch):
        """Higher-level guarantee: the public ``update`` API never raises
        even when the new emission step blows up internally."""
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import CrossMerchantLearningStore

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )

        def _boom(self, event):
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _boom)

        ctx, decision, result = _make_brain_ctx()
        db = MagicMock()
        # The other helpers in update() touch DB models defensively; we
        # supply a permissive MagicMock so they exit early.  Returning None
        # from .first() makes the affinity / sensitivity branches no-op.
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.first.return_value = None

        # No exception must escape — the public update() must absorb the
        # cross-merchant emission failure and let the turn complete.
        DefaultMemoryUpdater().update(
            db, ctx, decision, result,
            reply="ok", stage_before="x", latency_ms=1,
        )

    def test_outcome_classification_for_payment_link(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import CrossMerchantLearningStore

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        captured: Dict[str, Any] = {}

        def _fake_record(self, event):
            captured["evt"] = event
            return 1

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake_record)

        ctx, _, result = _make_brain_ctx(action_type="search_products", total=750.0)
        from modules.ai.brain.types import Decision
        decision = Decision(action="send_payment_link")

        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="checkout", latency_ms=10,
        )
        assert captured["evt"].outcome == "payment_sent"
        assert captured["evt"].value_bucket == "500_1000"

    def test_outcome_classification_for_failed_action(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.brain.types import ActionResult, Decision
        from modules.ai.security import CrossMerchantLearningStore

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        captured: Dict[str, Any] = {}

        def _fake_record(self, event):
            captured["evt"] = event
            return 1

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake_record)

        ctx, _, _ = _make_brain_ctx()
        decision = Decision(action="search_products")
        result = ActionResult(success=False, data={}, error="boom")
        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )
        assert captured["evt"].outcome == "error"
        assert captured["evt"].value_bucket == "unknown"

    def test_industry_resolution_marks_event_vertical(self, monkeypatch):
        """When TenantSettings has an industry tag, the tier becomes VERTICAL."""
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security import CrossMerchantLearningStore, LearningTier

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        captured: Dict[str, Any] = {}

        def _fake_record(self, event):
            captured["evt"] = event
            return 1

        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake_record)

        # Stub TenantSettings with industry="fashion"
        ts = MagicMock()
        ts.store_settings = {"industry": "Fashion"}
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = ts

        ctx, decision, result = _make_brain_ctx()
        DefaultMemoryUpdater()._emit_anonymous_signal(
            db, ctx, decision, result, stage_before="x", latency_ms=1,
        )
        evt = captured["evt"]
        assert evt.industry == "fashion"
        assert evt.tier == LearningTier.VERTICAL


# ═══════════════════════════════════════════════════════════════════════════
# 3. Adapter — runtime construction reuses tenant_context
# ═══════════════════════════════════════════════════════════════════════════

class TestAdapterRuntimeBinding:
    def test_build_runtime_passes_tenant_context_to_runtime(self):
        """``adapter._build_runtime`` must forward the validated context so
        ``CommerceToolRuntime`` does not silently re-derive it."""
        from modules.ai.orchestrator import adapter
        from modules.ai.security import TenantIsolationLayer

        supplied = TenantIsolationLayer.make_context(7, customer_phone="+966500000001")
        captured = {}

        class _FakeRuntime:
            def __init__(self, db, **kw):
                captured["kwargs"] = kw

        with patch("modules.ai.commerce.runtime.CommerceToolRuntime", _FakeRuntime), \
             patch("core.database.SessionLocal", MagicMock(return_value=MagicMock())):
            runtime, db = adapter._build_runtime(
                tenant_id=7,
                customer_phone="+966500000001",
                customer_id=None,
                tenant_context=supplied,
            )

        assert runtime is not None
        assert captured["kwargs"]["tenant_context"] is supplied
        assert captured["kwargs"]["tenant_id"] == 7

    def test_build_runtime_constructs_context_when_missing(self):
        from modules.ai.orchestrator import adapter
        from modules.ai.security import TenantContext

        captured = {}

        class _FakeRuntime:
            def __init__(self, db, **kw):
                captured["kwargs"] = kw

        with patch("modules.ai.commerce.runtime.CommerceToolRuntime", _FakeRuntime), \
             patch("core.database.SessionLocal", MagicMock(return_value=MagicMock())):
            runtime, _ = adapter._build_runtime(
                tenant_id=12,
                customer_phone="+966500000005",
                customer_id=None,
            )

        assert runtime is not None
        passed = captured["kwargs"]["tenant_context"]
        assert isinstance(passed, TenantContext)
        assert passed.tenant_id == 12
