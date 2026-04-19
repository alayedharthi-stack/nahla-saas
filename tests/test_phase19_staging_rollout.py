"""
tests/test_phase19_staging_rollout.py
─────────────────────────────────────
Phase 1.9 → narrow staging trial.

Coverage map
────────────
1. Environment guard
   * Master switch ON + env != staging  ⇒ bias is inert.
   * Master switch ON + env  = staging  ⇒ bias fires.
   * ``LEARNED_POLICY_BIAS_ENVIRONMENTS="*"`` opts out of the gate.

2. Per-component flags
   * Default: UI + choice_count enabled, recommendation_style disabled.
   * ``LEARNED_POLICY_BIAS_ALLOW_UI_MODE=False`` removes UI from the
     applied set even when readiness/intent/industry all pass.
   * Disabling all components ⇒ ``bias_applied=False``.

3. Bias-comparison aggregator (pure)
   * Counts only signals with ``hint_present=True``.
   * Splits correctly into ``bias_on`` / ``bias_off`` groups.
   * Computes deterministic rates for the six metrics
     (selection / progression / checkout / payment / conversion /
     fallback).
   * Per-intent and per-(industry, intent) rollups isolate buckets.
   * Empty / malformed signals never raise.
   * Output is anonymized — only floats, ints, categorical labels.

4. End-to-end OFF↔ON parity
   * The exact same inner decision returned by the rule engine is
     present in both modes.  Only ``decision.args`` differs by the
     three whitelisted bias keys when ON.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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


# ── Shared stubs (mirror test_phase19_soft_bias) ────────────────────────────

@dataclass
class _Intent:
    name: str
    confidence: float = 0.85


@dataclass
class _BrainCtx:
    intent: _Intent
    tenant_context: Any = None
    facts: Any = None
    _db: Any = None


class _StubInner:
    def __init__(self, *, action: str, args: Optional[dict] = None):
        self._action = action
        self._args = dict(args or {})

    def decide(self, ctx):
        from modules.ai.brain.types import Decision
        return Decision(action=self._action, args=dict(self._args))


def _make_hint(*, ui="product_cards"):
    return {
        "scope":              "global",
        "industry":           "*",
        "intent":             "ask_product",
        "recommended_action": "search_products",
        "recommended_ui":     ui,
        "confidence":         0.82,
        "sample_size":        300,
        "matches_inner":      True,
    }


def _ready_verdict():
    from modules.ai.learning import ReadinessVerdict
    return ReadinessVerdict(
        intent="ask_product", industry="*", ready=True, reasons=(),
        sample_size=300, hint_alignment_rate=0.7, observed_uplift=0.18,
    )


class _StubRegistry:
    def __init__(self, verdict):
        self._v = verdict

    def get(self, intent, industry=""):
        return self._v


def _enable_bias(monkeypatch, *,
                 master=True,
                 environment="staging",
                 environments="staging",
                 intents="ask_product",
                 industries="*",
                 ui_mode=True,
                 choice_count=True,
                 recommendation_style=False):
    import core.config as cfg
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ENABLED", master, raising=False)
    monkeypatch.setattr(cfg, "ENVIRONMENT", environment, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ENVIRONMENTS", environments, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_INTENTS", intents, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_INDUSTRIES", industries, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_UI_MODE", ui_mode, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_CHOICE_COUNT", choice_count, raising=False)
    monkeypatch.setattr(cfg, "LEARNED_POLICY_BIAS_ALLOW_RECOMMENDATION_STYLE",
                        recommendation_style, raising=False)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Environment guard
# ═══════════════════════════════════════════════════════════════════════════

class TestEnvironmentGate:
    def test_production_blocks_bias_even_when_master_on(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, environment="production",
                     environments="staging")
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(_ready_verdict()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "bias_applied" not in decision.args
        assert PolicyBiasLayer.is_enabled() is False

    def test_staging_env_allows_bias(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, environment="staging",
                     environments="staging")
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(_ready_verdict()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert decision.args["bias_applied"] is True
        assert PolicyBiasLayer.is_enabled() is True

    def test_wildcard_environment_disables_gate(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, environment="production",
                     environments="*")
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(_ready_verdict()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert decision.args["bias_applied"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 2. Per-component flags
# ═══════════════════════════════════════════════════════════════════════════

class TestComponentFlags:
    def test_default_disables_recommendation_style(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch)  # default: ui+choice on, style off
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint(ui="product_cards")}),
            registry=_StubRegistry(_ready_verdict()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))

        assert decision.args["bias_applied"] is True
        assert "preferred_ui_mode" in decision.args
        assert "choice_count" in decision.args
        # recommendation_style is off in this rollout phase
        assert "recommendation_style" not in decision.args
        assert "recommendation_style" not in decision.args["bias_type"]

    def test_disabling_ui_removes_ui_component(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, ui_mode=False)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint(ui="product_cards")}),
            registry=_StubRegistry(_ready_verdict()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))

        assert "preferred_ui_mode" not in decision.args
        # choice_count is still derived from the recommended UI string,
        # which is still read from the hint even though we don't apply
        # it back as preferred_ui_mode.
        assert decision.args.get("choice_count") in {3, 4, 5}
        assert "ui" not in decision.args["bias_type"]
        assert "choice_count" in decision.args["bias_type"]

    def test_all_components_disabled_marks_bias_inert(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        _enable_bias(monkeypatch, ui_mode=False, choice_count=False,
                     recommendation_style=False)
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(_ready_verdict()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))

        # bias_* annotations are still recorded so dashboards can see
        # "considered but inert" — but bias_applied must be False because
        # no concrete arg was actually mutated.
        assert decision.args["bias_applied"] is False
        assert decision.args["bias_type"] == "noop"
        assert "preferred_ui_mode"    not in decision.args
        assert "choice_count"         not in decision.args
        assert "recommendation_style" not in decision.args


# ═══════════════════════════════════════════════════════════════════════════
# 3. Bias-comparison aggregator
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _Sig:
    """Lightweight stand-in for ``CrossMerchantSignal`` used in pure tests."""
    intent:   str
    industry: str
    action:   str
    outcome:  str
    extra:    Dict[str, Any] = field(default_factory=dict)


def _hint(extra: Dict[str, Any], *, applied: bool) -> Dict[str, Any]:
    out = dict(extra)
    out.setdefault("hint_present", True)
    out.setdefault("bias_applied", applied)
    return out


class TestBiasComparisonAggregator:
    def test_only_hint_present_signals_count(self):
        from modules.ai.learning import compute_bias_comparison

        signals = [
            _Sig("ask_product", "fashion", "search_products", "conversion",
                 extra={"hint_present": False, "bias_applied": True}),
            _Sig("ask_product", "fashion", "search_products", "conversion",
                 extra=_hint({"had_buttons": True, "stage_before": "browse",
                              "stage_after": "checkout"}, applied=True)),
        ]
        report = compute_bias_comparison(signals)
        assert report.overall.bias_on.n == 1
        assert report.overall.bias_off.n == 0

    def test_split_by_bias_applied(self):
        from modules.ai.learning import compute_bias_comparison

        signals = [
            # bias ON, conversion
            _Sig("ask_product", "fashion", "search_products", "conversion",
                 extra=_hint({"had_buttons": True}, applied=True)),
            _Sig("ask_product", "fashion", "search_products", "conversion",
                 extra=_hint({"had_buttons": True}, applied=True)),
            # bias OFF, browse only
            _Sig("ask_product", "fashion", "search_products", "browse",
                 extra=_hint({"had_buttons": True}, applied=False)),
            _Sig("ask_product", "fashion", "search_products", "abandoned",
                 extra=_hint({"had_buttons": True}, applied=False)),
        ]
        report = compute_bias_comparison(signals)

        assert report.overall.bias_on.n == 2
        assert report.overall.bias_off.n == 2
        assert report.overall.bias_on.conversion_rate == 1.0
        assert report.overall.bias_off.conversion_rate == 0.0
        # Positive uplift on conversion, negative on fallback (better).
        assert report.overall.deltas.conversion_rate > 0
        assert report.overall.deltas.fallback_rate < 0

    def test_per_intent_isolation(self):
        from modules.ai.learning import compute_bias_comparison

        signals = [
            _Sig("ask_product", "*", "search_products", "conversion",
                 extra=_hint({}, applied=True)),
            _Sig("greeting",    "*", "greet",          "browse",
                 extra=_hint({}, applied=True)),
        ]
        report = compute_bias_comparison(signals)
        assert "ask_product" in report.by_intent
        assert "greeting"    in report.by_intent
        assert report.by_intent["ask_product"].bias_on.conversion_rate == 1.0
        assert report.by_intent["greeting"].bias_on.conversion_rate    == 0.0

    def test_per_industry_intent_isolation(self):
        from modules.ai.learning import compute_bias_comparison

        signals = [
            _Sig("ask_product", "fashion",     "search_products", "conversion",
                 extra=_hint({}, applied=True)),
            _Sig("ask_product", "electronics", "search_products", "browse",
                 extra=_hint({}, applied=True)),
        ]
        report = compute_bias_comparison(signals)
        assert ("fashion", "ask_product") in report.by_industry_intent
        assert ("electronics", "ask_product") in report.by_industry_intent
        # global-tier rows ("*") must not be folded into any vertical.
        assert all(industry != "*"
                   for industry, _ in report.by_industry_intent.keys())

    def test_payment_and_checkout_metrics(self):
        from modules.ai.learning import compute_bias_comparison

        signals = [
            # checkout initiated, payment requested
            _Sig("ask_product", "fashion", "send_payment_link", "checkout_started",
                 extra=_hint({}, applied=True)),
            # checkout reached final conversion (counts as both)
            _Sig("ask_product", "fashion", "search_products", "payment_sent",
                 extra=_hint({}, applied=True)),
            # control: neither
            _Sig("ask_product", "fashion", "search_products", "browse",
                 extra=_hint({}, applied=False)),
        ]
        report = compute_bias_comparison(signals)
        on  = report.overall.bias_on
        off = report.overall.bias_off
        assert on.checkout_initiation_rate == 1.0  # 2/2
        assert on.payment_link_rate        == 1.0  # 2/2 (action OR outcome)
        assert off.checkout_initiation_rate == 0.0
        assert off.payment_link_rate        == 0.0

    def test_fallback_rate_includes_clarify_and_abandoned(self):
        from modules.ai.learning import compute_bias_comparison

        signals = [
            _Sig("ask_product", "*", "clarify",  "browse",
                 extra=_hint({}, applied=False)),
            _Sig("ask_product", "*", "search_products", "abandoned",
                 extra=_hint({}, applied=False)),
            _Sig("ask_product", "*", "handoff_to_human", "handoff",
                 extra=_hint({}, applied=False)),
            _Sig("ask_product", "*", "search_products", "conversion",
                 extra=_hint({}, applied=True)),
        ]
        report = compute_bias_comparison(signals)
        assert report.overall.bias_off.fallback_rate == 1.0
        assert report.overall.bias_on.fallback_rate  == 0.0

    def test_progression_uses_stage_change_or_outcome(self):
        from modules.ai.learning import compute_bias_comparison

        signals = [
            # progression via stage change
            _Sig("ask_product", "*", "search_products", "browse",
                 extra=_hint({"stage_before": "exploring",
                              "stage_after":  "decided"}, applied=True)),
            # progression via outcome
            _Sig("ask_product", "*", "search_products", "added_to_cart",
                 extra=_hint({}, applied=True)),
            # no progression
            _Sig("ask_product", "*", "search_products", "browse",
                 extra=_hint({"stage_before": "exploring",
                              "stage_after":  "exploring"}, applied=True)),
        ]
        report = compute_bias_comparison(signals)
        # 2 of 3 turns counted as progression
        assert abs(report.overall.bias_on.progression_rate - (2 / 3)) < 1e-9

    def test_empty_signals_returns_zero_report(self):
        from modules.ai.learning import compute_bias_comparison
        report = compute_bias_comparison([])
        assert report.overall.bias_on.n == 0
        assert report.overall.bias_off.n == 0
        assert report.by_intent == {}
        assert report.by_industry_intent == {}

    def test_malformed_signal_does_not_raise(self):
        from modules.ai.learning import compute_bias_comparison

        bad = object()  # has no .extra/.intent attributes
        good = _Sig("ask_product", "*", "search_products", "conversion",
                    extra=_hint({}, applied=True))
        report = compute_bias_comparison([bad, good])
        assert report.overall.bias_on.n == 1

    def test_to_dict_anonymized_serialisation(self):
        from modules.ai.learning import compute_bias_comparison
        from modules.ai.security.trace_schema import FORBIDDEN_TRACE_KEYS

        signals = [
            _Sig("ask_product", "fashion", "search_products", "conversion",
                 extra=_hint({}, applied=True)),
        ]
        payload = compute_bias_comparison(signals).to_dict()

        import json
        json.dumps(payload)  # JSON-serialisable

        # Walk every dict key in the output and ensure no exact match
        # against ``FORBIDDEN_TRACE_KEYS``.  Substring matches are
        # acceptable (e.g. metric ``payment_link_rate`` contains
        # ``payment_link`` — that is a metric name, not a raw URL field).
        def _walk_keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield k
                    yield from _walk_keys(v)
            elif isinstance(obj, list):
                for item in obj:
                    yield from _walk_keys(item)

        keys = set(_walk_keys(payload))
        leaks = keys & FORBIDDEN_TRACE_KEYS
        assert not leaks, f"forbidden keys leaked: {leaks}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. End-to-end OFF↔ON parity
# ═══════════════════════════════════════════════════════════════════════════

class TestOffOnParity:
    def test_action_unchanged_only_args_extended(self, monkeypatch):
        from modules.ai.learning import PolicyBiasLayer

        # OFF baseline
        monkeypatch.setattr("core.config.LEARNED_POLICY_BIAS_ENABLED",
                            False, raising=False)
        layer_off = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(_ready_verdict()),
        )
        d_off = layer_off.decide(_BrainCtx(intent=_Intent("ask_product")))

        # ON
        _enable_bias(monkeypatch)
        layer_on = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(_ready_verdict()),
        )
        d_on = layer_on.decide(_BrainCtx(intent=_Intent("ask_product")))

        # Action is identical in both modes
        assert d_off.action == d_on.action == "search_products"
        # OFF args are exactly what the inner returned (just policy_hint)
        assert set(d_off.args.keys()) == {"policy_hint"}
        # ON args added only the whitelisted bias keys
        new_keys = set(d_on.args.keys()) - set(d_off.args.keys())
        assert new_keys.issubset({
            "preferred_ui_mode",
            "choice_count",
            "recommendation_style",
            "bias_applied",
            "bias_type",
            "bias_reason",
            "bias_intent",
            "bias_industry",
        })

    def test_staging_scope_filters_out_other_industries(self, monkeypatch):
        """The staging rollout is scoped to ``industries='fashion'`` —
        a request from electronics must NOT be biased."""
        from modules.ai.learning import PolicyBiasLayer
        from modules.ai.security import TenantIsolationLayer

        _enable_bias(monkeypatch, industries="fashion")
        tc = TenantIsolationLayer.make_context(
            42, customer_phone="+966500000001", industry="electronics",
        )
        layer = PolicyBiasLayer(
            _StubInner(action="search_products",
                       args={"policy_hint": _make_hint()}),
            registry=_StubRegistry(_ready_verdict()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product"),
                                          tenant_context=tc))
        assert "bias_applied" not in decision.args


# ═══════════════════════════════════════════════════════════════════════════
# 5. Loader robustness
# ═══════════════════════════════════════════════════════════════════════════

class TestLoaderRobustness:
    def test_load_returns_empty_on_db_error(self):
        from modules.ai.learning import load_bias_comparison

        class _BadDB:
            def query(self, *_a, **_k):
                raise RuntimeError("db down")

        report = load_bias_comparison(_BadDB(), intent="ask_product",
                                       industry="fashion")
        assert report.overall.bias_on.n == 0
        assert report.overall.bias_off.n == 0

    def test_load_passes_filters_to_query(self):
        from modules.ai.learning import load_bias_comparison

        captured: List[Any] = []

        class _Q:
            def filter(self, *a, **k):
                captured.append(("filter", a, k))
                return self
            def limit(self, n):
                captured.append(("limit", n))
                return self
            def all(self):
                return []

        class _DB:
            def query(self, *_a, **_k):
                return _Q()

        load_bias_comparison(_DB(), intent="ask_product",
                              industry="fashion", limit=10)
        # at least two filter calls (intent + industry) and a limit
        kinds = [c[0] for c in captured]
        assert kinds.count("filter") >= 2
        assert "limit" in kinds
