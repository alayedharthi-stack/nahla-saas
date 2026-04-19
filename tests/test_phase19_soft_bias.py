"""
tests/test_phase19_soft_bias.py
───────────────────────────────
Phase 1.9 — Soft Policy Bias (guarded, narrow, reversible).

Coverage map
────────────
1. Master switch
   * Bias is a strict no-op when ``LEARNED_POLICY_BIAS_ENABLED`` is False.
   * Toggling the switch back off mid-process restores the inner
     decision exactly (reversibility guarantee).

2. Hard guards (cannot be unlocked by config)
   * ``PROTECTED_ACTIONS`` — every payment / order / coupon / handoff
     action is left untouched even when readiness, allowlist and hint
     would otherwise pass.
   * ``PROTECTED_INTENTS`` — sensitive intents (checkout / payment /
     objection / handoff / abandon / complaint / refund / cancel)
     never receive a bias.

3. Operator gates
   * Per-intent allowlist filters out non-listed intents.
   * Per-industry allowlist excludes industries that aren't in the
     rollout, while ``"*"`` opens everything.

4. Readiness gate
   * No registry → no bias.
   * Verdict ``ready=False`` → no bias.
   * Verdict ``ready=True`` + non-protected action + matching intent
     → bias is applied; ``decision.action`` is unchanged; only
     whitelisted ``args`` keys are mutated.

5. Anti-leak / no-break
   * Malformed hint dict ⇒ no exception, no bias.
   * Registry that raises ⇒ no exception, no bias.
   * Inner engine that returns a frozen-style decision ⇒ no exception
     (the layer simply skips when ``args`` isn't a dict).

6. Trace integration
   * When bias is applied, ``MemoryUpdater._emit_anonymous_signal``
     records ``hint_used=True``, ``bias_applied=True``, ``bias_type``,
     ``bias_reason``, ``final_ui_mode``, ``final_recommendation_shape``,
     ``final_choice_count_bucket``.  All fields survive
     ``validate_anonymized``.
   * When bias is NOT applied, only ``hint_present`` /
     ``hint_used=False`` appear — no Phase 1.9 keys leak.

7. Default brain wiring
   * ``build_default_brain()`` returns a brain whose decision engine is
     wrapped by both the override and the bias layers, but the bias
     layer is inert because the master switch defaults to off.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _ensure_database_models_importable() -> None:
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
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Intent:
    name: str
    confidence: float = 0.85


@dataclass
class _Facts:
    industry: str = ""


@dataclass
class _BrainCtx:
    intent: _Intent
    tenant_context: Any = None
    facts: Any = None
    _db: Any = None


class _StubInner:
    """Minimal ``DecisionMaker`` returning a Decision we control."""
    def __init__(self, *, action: str, args: Optional[dict] = None):
        self._action = action
        self._args = dict(args or {})

    def decide(self, ctx):
        from modules.ai.brain.types import Decision
        return Decision(action=self._action, args=dict(self._args))


def _make_hint(*, action="search_products", ui="product_cards", scope="global",
               industry="*", confidence=0.82, sample_size=300, matches_inner=True):
    return {
        "scope":              scope,
        "industry":           industry,
        "intent":             "ask_product",
        "recommended_action": action,
        "recommended_ui":     ui,
        "confidence":         confidence,
        "sample_size":        sample_size,
        "matches_inner":      matches_inner,
    }


def _ready_verdict(*, intent="ask_product", industry="*", uplift=0.18,
                   alignment=0.7, sample=300):
    from modules.ai.learning import ReadinessVerdict
    return ReadinessVerdict(
        intent=intent, industry=industry, ready=True, reasons=(),
        sample_size=sample, hint_alignment_rate=alignment, observed_uplift=uplift,
    )


def _unready_verdict(intent="ask_product", industry="*"):
    from modules.ai.learning import ReadinessVerdict
    return ReadinessVerdict(
        intent=intent, industry=industry, ready=False,
        reasons=("insufficient_uplift:0.01<0.05",),
        sample_size=10, hint_alignment_rate=0.0, observed_uplift=0.0,
    )


class _StubRegistry:
    """Hand-rolled registry stub — accepts ``verdicts`` and supports raise mode."""
    def __init__(self, verdicts: Optional[Dict[tuple, Any]] = None,
                 *, raises: bool = False):
        self._verdicts = verdicts or {}
        self._raises = raises
        self.calls = []

    def get(self, intent, industry=""):
        self.calls.append((intent, industry))
        if self._raises:
            raise RuntimeError("simulated outage")
        intent_n = (intent or "").lower()
        industry_n = (industry or "").lower()
        if industry_n and industry_n not in {"*", "unknown"}:
            v = self._verdicts.get((industry_n, intent_n))
            if v is not None:
                return v
        return self._verdicts.get(intent_n)


def _enable_bias(monkeypatch, *, enabled=True,
                 intents=None, industries="*"):
    """Patch ``LEARNED_POLICY_BIAS_*`` config flags.

    Note: the environment gate (added in the staging-rollout milestone)
    is bypassed here with ``LEARNED_POLICY_BIAS_ENVIRONMENTS="*"`` so
    these protection-contract tests don't depend on ``ENVIRONMENT``.
    """
    import core.config as cfg
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ENABLED", enabled, raising=False)
    monkeypatch.setattr(
        cfg, "LEARNED_POLICY_BIAS_INTENTS",
        intents if intents is not None
        else "ask_product,greeting,faq,browse,product_inquiry,recommendation",
        raising=False,
    )
    monkeypatch.setattr(
        cfg, "LEARNED_POLICY_BIAS_INDUSTRIES", industries, raising=False
    )
    # Disable the environment gate for the protection-contract tests.
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ENVIRONMENTS", "*", raising=False)
    # Per-component flags — leave full surface enabled for these tests.
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_UI_MODE", True, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_CHOICE_COUNT", True, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_RECOMMENDATION_STYLE", True, raising=False)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Master switch & reversibility
# ═══════════════════════════════════════════════════════════════════════════

class TestMasterSwitch:
    def test_disabled_bypasses_layer_completely(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=False)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry({"ask_product": _ready_verdict()}),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))

        # No bias-related keys leak into args.
        for k in ("bias_applied", "bias_type", "preferred_ui_mode",
                  "recommendation_style", "choice_count"):
            assert k not in decision.args
        assert decision.action == "search_products"

    def test_reversibility_via_config(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        registry = _StubRegistry({"ask_product": _ready_verdict()})

        # First call — bias enabled → applied.
        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=registry,
        )
        d1 = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert d1.args.get("bias_applied") is True

        # Second call — bias disabled → no application.
        _enable_bias(monkeypatch, enabled=False)
        d2 = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "bias_applied" not in d2.args


# ═══════════════════════════════════════════════════════════════════════════
# 2. Hard guards — cannot be unlocked by config
# ═══════════════════════════════════════════════════════════════════════════

class TestProtectedActions:
    @pytest.mark.parametrize("action", [
        "create_order", "propose_draft_order", "send_payment_link",
        "apply_coupon", "suggest_coupon", "checkout", "complete_order",
        "cancel_order", "refund", "track_order", "handoff",
        "handoff_to_human",
    ])
    def test_protected_action_never_biased(self, monkeypatch, action):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action=action, args={"policy_hint": _make_hint()}),
            registry=_StubRegistry({"ask_product": _ready_verdict()}),
            allowed_intents=["ask_product"],
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))

        assert decision.action == action  # unchanged
        assert "bias_applied" not in decision.args
        assert "preferred_ui_mode" not in decision.args


class TestProtectedIntents:
    @pytest.mark.parametrize("intent", [
        "checkout", "payment", "complete_order", "objection", "complaint",
        "handoff", "support_handoff", "abandon", "abandoned", "refund",
        "cancel", "cancellation",
    ])
    def test_protected_intent_never_biased(self, monkeypatch, intent):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True, intents="*")
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry({intent: _ready_verdict(intent=intent)}),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent(intent)))
        assert "bias_applied" not in decision.args


class TestProtectionConstants:
    def test_protected_sets_are_immutable_lowercase(self):
        from modules.ai.learning import PROTECTED_ACTIONS, PROTECTED_INTENTS
        assert all(s == s.lower() for s in PROTECTED_ACTIONS)
        assert all(s == s.lower() for s in PROTECTED_INTENTS)
        # Sanity — money / handoff actions are present
        assert "send_payment_link" in PROTECTED_ACTIONS
        assert "apply_coupon" in PROTECTED_ACTIONS
        assert "handoff_to_human" in PROTECTED_ACTIONS
        assert "checkout" in PROTECTED_INTENTS
        assert "objection" in PROTECTED_INTENTS

    def test_helpers_normalise_input(self):
        from modules.ai.learning import is_action_protected, is_intent_protected
        assert is_action_protected("CREATE_ORDER") is True
        assert is_action_protected(" send_payment_link ") is True
        assert is_action_protected("search_products") is False
        assert is_intent_protected("Checkout") is True
        assert is_intent_protected("ask_product") is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. Operator gates
# ═══════════════════════════════════════════════════════════════════════════

class TestAllowlists:
    def test_intent_not_in_allowlist_skips_bias(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True,
                     intents="ask_product,greeting")
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry({"recommendation": _ready_verdict(intent="recommendation")}),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("recommendation")))
        assert "bias_applied" not in decision.args

    def test_industry_filter_excludes_unlisted(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer
        from modules.ai.security import TenantIsolationLayer

        _enable_bias(monkeypatch, enabled=True, industries="electronics")
        tc = TenantIsolationLayer.make_context(
            42, customer_phone="+966500000001", industry="fashion",
        )
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint(industry="fashion")}),
            registry=_StubRegistry({
                ("fashion", "ask_product"): _ready_verdict(industry="fashion"),
            }),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product"), tenant_context=tc))
        assert "bias_applied" not in decision.args

    def test_industry_wildcard_allows_everything(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer
        from modules.ai.security import TenantIsolationLayer

        _enable_bias(monkeypatch, enabled=True, industries="*")
        tc = TenantIsolationLayer.make_context(
            42, customer_phone="+966500000001", industry="fashion",
        )
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint(industry="fashion")}),
            registry=_StubRegistry({
                ("fashion", "ask_product"): _ready_verdict(industry="fashion"),
            }),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product"), tenant_context=tc))
        assert decision.args["bias_applied"] is True
        assert decision.args["bias_industry"] == "fashion"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Readiness gate
# ═══════════════════════════════════════════════════════════════════════════

class TestReadinessGate:
    def test_no_bias_when_no_registry_and_no_db(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            # registry omitted; ctx has no _db → cannot lazy-build
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "bias_applied" not in decision.args

    def test_no_bias_when_verdict_unready(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry({"ask_product": _unready_verdict()}),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "bias_applied" not in decision.args

    def test_no_bias_when_verdict_missing(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry({}),  # nothing in registry
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "bias_applied" not in decision.args

    def test_bias_applied_with_full_metadata(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint(ui="product_cards")}),
            registry=_StubRegistry({"ask_product": _ready_verdict()}),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))

        # decision.action is preserved.
        assert decision.action == "search_products"
        # Whitelisted args were set.
        assert decision.args["preferred_ui_mode"] == "product_cards"
        assert decision.args["recommendation_style"] == "cards"
        assert decision.args["choice_count"] == 4
        # Bias annotations present.
        assert decision.args["bias_applied"] is True
        assert "ui" in decision.args["bias_type"]
        assert decision.args["bias_reason"].startswith("ready:uplift_")
        assert decision.args["bias_intent"] == "ask_product"
        assert decision.args["bias_industry"] == "*"

    def test_bias_does_not_overwrite_existing_choice_count(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint(ui="list"),
                             "choice_count": 7}),
            registry=_StubRegistry({"ask_product": _ready_verdict()}),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        # User-set choice count is preserved.
        assert decision.args["choice_count"] == 7
        # bias_type does not include choice_count.
        assert "choice_count" not in decision.args["bias_type"]


# ═══════════════════════════════════════════════════════════════════════════
# 5. Anti-leak / no-break
# ═══════════════════════════════════════════════════════════════════════════

class TestNoBreakGuarantees:
    def test_malformed_hint_does_not_break_turn(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": "not-a-dict"}),
            registry=_StubRegistry({"ask_product": _ready_verdict()}),
        )
        # Must not raise; falls through with no bias.
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "bias_applied" not in decision.args

    def test_registry_failure_does_not_break_turn(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(raises=True),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "bias_applied" not in decision.args
        assert decision.action == "search_products"

    def test_inner_with_non_dict_args_returns_unchanged(self, monkeypatch):
        from modules.ai.brain.types import Decision
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, enabled=True)

        class _Frozen:
            def decide(self, ctx):
                d = Decision(action="search_products")
                d.args = None  # type: ignore[assignment]
                return d

        layer = PolicyBiasLayer(
            _Frozen(),
            registry=_StubRegistry({"ask_product": _ready_verdict()}),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert decision.action == "search_products"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Trace integration via _emit_anonymous_signal
# ═══════════════════════════════════════════════════════════════════════════

def _make_facts_full(**kw):
    from modules.ai.brain.types import CommerceFacts
    base = dict(
        has_products=True, product_count=2, in_stock_count=2,
        has_active_integration=True, orderable=True,
        snapshot_fresh=True, store_name="x",
    )
    base.update(kw)
    return CommerceFacts(**base)


def _build_brain_ctx_for_trace(*, action="search_products"):
    from modules.ai.brain.types import (
        ActionResult, BrainContext, Decision, Intent,
        MerchantConversationState, SalesContextSnapshot,
    )
    from modules.ai.security import TenantIsolationLayer

    state  = MerchantConversationState(greeted=True, stage="exploring", turn=4)
    intent = Intent(name="ask_product", confidence=0.85, raw_message="msg")
    ctx = BrainContext(
        tenant_id=11,
        customer_phone="+966500000001",
        message="msg",
        intent=intent,
        state=state,
        facts=_make_facts_full(),
        history=[{"direction": "in", "body": "x"}],
        profile={"preferred_language": "ar"},
        sales_context=SalesContextSnapshot(),
        tenant_context=TenantIsolationLayer.make_context(11, customer_phone="+966500000001"),
    )
    decision = Decision(
        action=action,
        args={
            "policy_hint":          _make_hint(),
            "preferred_ui_mode":    "product_cards",
            "recommendation_style": "cards",
            "choice_count":         4,
            "bias_applied":         True,
            "bias_type":            "ui+choice_count+recommendation_style",
            "bias_reason":          "ready:uplift_0.18:n=100_500",
            "bias_intent":          "ask_product",
            "bias_industry":        "*",
        },
    )
    result = ActionResult(success=True, data={"products": [{"id": 1}, {"id": 2}],
                                              "chosen_path": "rule"})
    return ctx, decision, result


class TestTraceIntegration:
    def _capture(self, monkeypatch):
        from modules.ai.security import CrossMerchantLearningStore, validate_anonymized
        captured: Dict[str, Any] = {}

        def _fake(self, event, *, commit=True):
            captured["event"]     = event
            captured["validated"] = validate_anonymized(event)
            return 1

        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        monkeypatch.setattr(CrossMerchantLearningStore, "record", _fake)
        return captured

    def test_records_full_bias_trace_when_applied(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater

        captured = self._capture(monkeypatch)
        ctx, decision, result = _build_brain_ctx_for_trace()

        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="exploring", latency_ms=12,
        )

        evt = captured["event"]
        assert evt.extra["hint_present"] is True
        assert evt.extra["hint_used"] is True
        assert evt.extra["bias_applied"] is True
        assert evt.extra["bias_type"] == "ui+choice_count+recommendation_style"
        assert evt.extra["bias_reason"] == "ready:uplift_0.18:n=100_500"
        assert evt.extra["bias_intent"] == "ask_product"
        assert evt.extra["bias_industry"] == "*"
        assert evt.extra["final_ui_mode"] in {"product_cards", "list", "buttons", "text"}
        assert evt.extra["final_recommendation_shape"] == "cards"
        assert evt.extra["final_choice_count_bucket"] == "3_4"

        # Validated copy preserves all whitelisted bias keys.
        validated = captured["validated"]
        for key in ("bias_applied", "bias_type", "bias_reason",
                    "final_ui_mode", "final_recommendation_shape",
                    "final_choice_count_bucket"):
            assert key in validated.extra

    def test_no_bias_keys_when_layer_inactive(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.brain.types import (
            ActionResult, BrainContext, Decision, Intent,
            MerchantConversationState, SalesContextSnapshot,
        )
        from modules.ai.security import TenantIsolationLayer

        captured = self._capture(monkeypatch)

        # Decision with hint but NO bias_* markers (bias was never applied).
        ctx = BrainContext(
            tenant_id=11,
            customer_phone="+966500000001",
            message="msg",
            intent=Intent(name="ask_product", confidence=0.9, raw_message="m"),
            state=MerchantConversationState(turn=2),
            facts=_make_facts_full(),
            history=[],
            profile={},
            sales_context=SalesContextSnapshot(),
            tenant_context=TenantIsolationLayer.make_context(11, customer_phone="+966500000001"),
        )
        decision = Decision(action="search_products",
                            args={"policy_hint": _make_hint()})
        result = ActionResult(success=True, data={"products": [{"id": 1}]})

        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="exploring", latency_ms=8,
        )

        evt = captured["event"]
        assert evt.extra["hint_present"] is True
        assert evt.extra["hint_used"] is False
        # No phase-1.9 keys present
        for key in ("bias_applied", "bias_type", "bias_reason",
                    "final_ui_mode", "final_recommendation_shape",
                    "final_choice_count_bucket"):
            assert key not in evt.extra

    def test_bias_trace_has_no_forbidden_keys(self, monkeypatch):
        from modules.ai.brain.memory.updater import DefaultMemoryUpdater
        from modules.ai.security.trace_schema import FORBIDDEN_TRACE_KEYS

        captured = self._capture(monkeypatch)
        ctx, decision, result = _build_brain_ctx_for_trace()
        DefaultMemoryUpdater()._emit_anonymous_signal(
            MagicMock(), ctx, decision, result, stage_before="x", latency_ms=1,
        )
        validated = captured["validated"]
        for forbidden in FORBIDDEN_TRACE_KEYS:
            assert forbidden not in validated.extra


# ═══════════════════════════════════════════════════════════════════════════
# 7. Default brain wiring
# ═══════════════════════════════════════════════════════════════════════════

class TestBrainWiring:
    def test_build_default_brain_wraps_with_layers_inert_by_default(self, monkeypatch):
        # Ensure master switch is off (default).
        import core.config as cfg
        monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ENABLED", False, raising=False)

        from modules.ai.brain.pipeline import build_default_brain
        from modules.ai.learning import PolicyBiasLayer, PolicyOverrideLayer

        brain = build_default_brain()
        engine = brain._decision_engine
        # Outermost layer is the bias layer
        assert isinstance(engine, PolicyBiasLayer)
        # Inner layer is the override layer
        inner = engine._inner
        assert isinstance(inner, PolicyOverrideLayer)
        # And the inner of the override is the rule engine (not another wrapper)
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        assert isinstance(inner._inner, DefaultDecisionEngine)

        # With master switch off, the bias layer is a strict no-op.
        assert PolicyBiasLayer.is_enabled() is False
