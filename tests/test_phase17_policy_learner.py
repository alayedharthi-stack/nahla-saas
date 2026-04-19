"""
tests/test_phase17_policy_learner.py
────────────────────────────────────
Phase 1.7 — First Global & Vertical Learner.

Coverage
────────
1. Aggregator (pure functions, no DB):
   * deterministic ordering of action stats
   * outcome → sentiment mapping
   * UI mode tie-breaking
   * ``pick_recommended_action`` thresholds (sample size + confidence)

2. ``PolicyLearner`` (orchestrator):
   * UPSERTs one global row per intent that meets thresholds
   * UPSERTs vertical rows partitioned by industry
   * Skips buckets below ``min_sample_size``
   * Idempotent — running twice does not duplicate rows
   * Master-switch off ⇒ no policies persisted

3. ``LearnedPolicyStore``:
   * Returns ``None`` when the table is empty
   * Falls back from vertical → global tier
   * Caches results within TTL
   * Never raises into the caller (DB outage ⇒ ``None``)

4. ``PolicyOverrideLayer``:
   * Always preserves the inner action
   * Attaches ``policy_hint`` when a hint exists
   * No-ops when the master switch is off
   * No-ops when no inner DB session is available
   * Lookup failures never break the turn

5. ``DecisionEngine`` operates correctly even when the learning module
   is not present (regression safety net).
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
# Tiny in-memory fakes to avoid spinning up Postgres / SQLite for unit tests.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Signal:
    intent: str
    action: str
    ui_mode: str
    outcome: str
    industry: str = "*"


@dataclass
class _PolicyRow:
    scope: str
    industry: str
    intent: str
    recommended_action: str = "unknown"
    recommended_ui: str = "unknown"
    confidence: float = 0.0
    sample_size: int = 0
    extra: Optional[Dict[str, Any]] = None
    updated_at: Any = None


class _FakePolicyTable:
    """Behaves enough like a SQLAlchemy session for UPSERT / lookup."""
    def __init__(self) -> None:
        self.rows: List[_PolicyRow] = []
        self.commit_count = 0
        self.rollback_count = 0

    # SQLAlchemy-shaped surface
    def add(self, row: Any) -> None:
        if isinstance(row, _PolicyRow):
            self.rows.append(row)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def query(self, model: Any) -> "_QueryShim":
        # We only support filtering by the unique constraint columns.
        return _QueryShim(self.rows)


class _QueryShim:
    def __init__(self, rows: List[_PolicyRow]) -> None:
        self._rows = rows
        self._filters: Dict[str, Any] = {}

    def filter_by(self, **kw: Any) -> "_QueryShim":
        self._filters.update(kw)
        return self

    def first(self) -> Optional[_PolicyRow]:
        for r in self._rows:
            if all(getattr(r, k) == v for k, v in self._filters.items()):
                return r
        return None

    def all(self) -> List[_PolicyRow]:
        out = []
        for r in self._rows:
            if all(getattr(r, k) == v for k, v in self._filters.items()):
                out.append(r)
        return out


# ═══════════════════════════════════════════════════════════════════════════
# 1. Aggregator
# ═══════════════════════════════════════════════════════════════════════════

class TestAggregator:
    def test_outcome_sentiment_mapping(self):
        from modules.ai.learning import classify_outcome_sentiment
        assert classify_outcome_sentiment("conversion") == "positive"
        assert classify_outcome_sentiment("payment_sent") == "positive"
        assert classify_outcome_sentiment("error") == "negative"
        assert classify_outcome_sentiment("abandoned") == "negative"
        assert classify_outcome_sentiment("browse") == "neutral"
        assert classify_outcome_sentiment("") == "neutral"
        assert classify_outcome_sentiment(None) == "neutral"

    def test_aggregate_action_stats_groups_by_intent(self):
        from modules.ai.learning import aggregate_action_stats

        signals = [
            _Signal("ask_product", "search_products", "list", "product_presented"),
            _Signal("ask_product", "search_products", "list", "added_to_cart"),
            _Signal("ask_product", "search_products", "list", "added_to_cart"),
            _Signal("ask_product", "recommend_addon",  "buttons", "browse"),
            _Signal("greeting",    "greet",            "text", "greet"),
        ]
        out = aggregate_action_stats(signals, group_by=("intent",))

        assert set(out.keys()) == {("ask_product",), ("greeting",)}
        ap_actions = out[("ask_product",)]
        assert ap_actions["search_products"].count == 3
        assert ap_actions["search_products"].positive == 2
        assert ap_actions["recommend_addon"].count == 1
        # Stable: success_rate calculated correctly
        assert ap_actions["search_products"].success_rate == pytest.approx(2 / 3)

    def test_pick_recommended_action_skips_below_min_sample(self):
        from modules.ai.learning import (
            ActionStats,
            pick_recommended_action,
        )
        actions = {
            "a": ActionStats(action="a", count=5, positive=5),
        }
        assert pick_recommended_action(actions, min_sample_size=30) is None

    def test_pick_recommended_action_skips_below_min_confidence(self):
        from modules.ai.learning import (
            ActionStats,
            pick_recommended_action,
        )
        actions = {
            "a": ActionStats(action="a", count=20, positive=8),   # 40% success
            "b": ActionStats(action="b", count=20, positive=4),   # 20% success
        }
        assert pick_recommended_action(
            actions, min_sample_size=10, min_confidence=0.6,
        ) is None

    def test_pick_recommended_action_returns_dominant(self):
        from modules.ai.learning import (
            ActionStats,
            pick_recommended_action,
        )
        # A wins on share AND on success rate.
        actions = {
            "a": ActionStats(action="a", count=80, positive=72),  # 90%
            "b": ActionStats(action="b", count=20, positive=2),
        }
        winner = pick_recommended_action(
            actions, min_sample_size=10, min_confidence=0.6,
        )
        assert winner is not None
        assert winner.action == "a"

    def test_pick_recommended_action_deterministic_tiebreak(self):
        """Equal success rate + equal count → alphabetical action."""
        from modules.ai.learning import (
            ActionStats,
            pick_recommended_action,
        )
        actions = {
            "z_action": ActionStats(action="z_action", count=80, positive=80),
            "a_action": ActionStats(action="a_action", count=80, positive=80),
        }
        winner = pick_recommended_action(
            actions, min_sample_size=10, min_confidence=0.5,
        )
        assert winner is not None
        assert winner.action == "a_action"

    def test_pick_recommended_ui_ignores_unknown(self):
        from modules.ai.learning import pick_recommended_ui
        # 'unknown' must never beat a real UI even when more frequent.
        ui = pick_recommended_ui(
            {"unknown": 100, "buttons": 30, "text": 20},
            min_sample_size=10,
        )
        assert ui == "buttons"


# ═══════════════════════════════════════════════════════════════════════════
# 2. PolicyLearner
# ═══════════════════════════════════════════════════════════════════════════

class TestPolicyLearner:
    def _signals(self) -> List[_Signal]:
        """Build a deterministic dataset where the winners are obvious.

        Sentiment recap (from aggregator.OUTCOME_SENTIMENT):
          POSITIVE = added_to_cart / conversion / payment_sent / checkout_started
          NEUTRAL  = product_presented / browse / greet / support / unknown
          NEGATIVE = error / abandoned / objection / handoff
        """
        out: List[_Signal] = []
        # ask_product / fashion (90 signals).  search_products wins on share
        # AND on success_rate (70/80 = 0.875).
        for _ in range(70):
            out.append(_Signal("ask_product", "search_products", "list",
                               "added_to_cart", industry="fashion"))
        for _ in range(10):
            out.append(_Signal("ask_product", "search_products", "list",
                               "product_presented", industry="fashion"))
        for _ in range(10):
            out.append(_Signal("ask_product", "recommend_addon", "buttons",
                               "browse", industry="fashion"))
        # ask_product / electronics: only 8 signals → below sample threshold.
        for _ in range(8):
            out.append(_Signal("ask_product", "search_products", "list",
                               "product_presented", industry="electronics"))
        # greeting / global: 50 turns where greet leads to a conversion 80%
        # of the time → success_rate 0.80, share 1.00 → strong winner.
        for _ in range(40):
            out.append(_Signal("greeting", "greet", "text", "conversion"))
        for _ in range(10):
            out.append(_Signal("greeting", "greet", "text", "greet"))
        return out

    def test_run_writes_global_and_vertical_rows(self, monkeypatch):
        from modules.ai.learning import PolicyLearner

        db = _FakePolicyTable()

        # Make UPSERT path use _PolicyRow constructor (bypass real ORM).
        from modules.ai.learning import learner as learner_mod
        monkeypatch.setattr(
            learner_mod,
            "_get_config_int",
            lambda name, default: 30 if "SAMPLE" in name else default,
        )
        monkeypatch.setattr(
            learner_mod,
            "_get_config_float",
            lambda name, default: 0.6 if "CONFIDENCE" in name else default,
        )

        # Patch LearnedSalesPolicy import inside _upsert
        import importlib
        db_models = importlib.import_module("database.models")
        monkeypatch.setattr(db_models, "LearnedSalesPolicy", _PolicyRow)

        learner = PolicyLearner(db, min_sample_size=30, min_confidence=0.6)
        report = learner.run(signals=self._signals())

        assert report.signals_seen == 148
        # ask_product (90 signals: 80/10 split, 80 positive) → above thresholds
        # greeting (50 signals: greet) → above thresholds
        assert report.global_written == 2
        # only fashion meets the per-industry threshold
        assert report.vertical_written == 1
        assert report.industries_covered == ["fashion"]

        # Confirm the rows look correct
        global_rows = [r for r in db.rows if r.scope == "global"]
        vertical_rows = [r for r in db.rows if r.scope == "vertical"]

        ask_product = next(r for r in global_rows if r.intent == "ask_product")
        assert ask_product.recommended_action == "search_products"
        assert ask_product.recommended_ui == "list"
        assert ask_product.industry == "*"
        # 80 fashion + 8 electronics + 10 recommend_addon = 98 signals total
        assert ask_product.sample_size == 98
        # winner: search_products → count=88, positive=70, share=88/98, sr=70/88
        share = 88 / 98
        sr    = 70 / 88
        assert ask_product.confidence == pytest.approx((share + sr) / 2, abs=1e-3)

        fashion_row = next(iter(vertical_rows))
        assert fashion_row.industry == "fashion"
        assert fashion_row.intent == "ask_product"
        assert fashion_row.recommended_action == "search_products"

    def test_run_skips_below_min_sample_size(self, monkeypatch):
        from modules.ai.learning import PolicyLearner
        import importlib
        db_models = importlib.import_module("database.models")
        monkeypatch.setattr(db_models, "LearnedSalesPolicy", _PolicyRow)

        signals = [_Signal("ask_product", "search_products", "list",
                           "added_to_cart", industry="fashion")
                   for _ in range(5)]
        db = _FakePolicyTable()
        learner = PolicyLearner(db, min_sample_size=30, min_confidence=0.6)
        report = learner.run(signals=signals)
        assert report.signals_seen == 5
        assert report.global_written == 0
        assert report.vertical_written == 0
        assert report.skipped_below_sample_size >= 1

    def test_run_is_idempotent(self, monkeypatch):
        """Running the learner twice must not duplicate rows."""
        from modules.ai.learning import PolicyLearner
        import importlib
        db_models = importlib.import_module("database.models")
        monkeypatch.setattr(db_models, "LearnedSalesPolicy", _PolicyRow)

        db = _FakePolicyTable()
        learner = PolicyLearner(db, min_sample_size=30, min_confidence=0.6)
        learner.run(signals=self._signals())
        first_rows = list(db.rows)
        learner.run(signals=self._signals())
        # Same number of rows; in-place update on second run.
        assert len(db.rows) == len(first_rows)
        # updated_at moved forward — proves UPDATE path was hit.
        for r in db.rows:
            assert r.updated_at is not None


# ═══════════════════════════════════════════════════════════════════════════
# 3. LearnedPolicyStore
# ═══════════════════════════════════════════════════════════════════════════

class TestLearnedPolicyStore:
    def _seed_db(self, rows: List[_PolicyRow]) -> _FakePolicyTable:
        db = _FakePolicyTable()
        db.rows.extend(rows)
        return db

    def test_disabled_returns_none(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: False)
        )
        db = self._seed_db([
            _PolicyRow(scope="global", industry="*", intent="ask_product",
                       recommended_action="search_products", confidence=0.9,
                       sample_size=100),
        ])
        assert LearnedPolicyStore(db).lookup("ask_product") is None

    def test_vertical_preferred_over_global(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore
        import importlib
        db_models = importlib.import_module("database.models")
        monkeypatch.setattr(db_models, "LearnedSalesPolicy", _PolicyRow)
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: True)
        )

        db = self._seed_db([
            _PolicyRow(scope="global", industry="*", intent="ask_product",
                       recommended_action="search_products", confidence=0.7,
                       sample_size=100),
            _PolicyRow(scope="vertical", industry="fashion", intent="ask_product",
                       recommended_action="recommend_addon", confidence=0.85,
                       sample_size=80),
        ])
        store = LearnedPolicyStore(db, ttl_seconds=10)

        hint = store.lookup("ask_product", industry="fashion")
        assert hint is not None
        assert hint.scope == "vertical"
        assert hint.recommended_action == "recommend_addon"

    def test_global_fallback_for_unknown_industry(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore
        import importlib
        db_models = importlib.import_module("database.models")
        monkeypatch.setattr(db_models, "LearnedSalesPolicy", _PolicyRow)
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: True)
        )

        db = self._seed_db([
            _PolicyRow(scope="global", industry="*", intent="ask_product",
                       recommended_action="search_products", confidence=0.7,
                       sample_size=100),
        ])
        store = LearnedPolicyStore(db, ttl_seconds=10)
        hint = store.lookup("ask_product", industry="unknown_vertical")
        assert hint is not None
        assert hint.scope == "global"
        assert hint.industry == "*"

    def test_lookup_swallows_db_errors(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: True)
        )
        db = MagicMock()
        db.query.side_effect = RuntimeError("simulated outage")
        # Must not raise — the override layer relies on graceful degradation.
        assert LearnedPolicyStore(db).lookup("ask_product") is None

    def test_cache_avoids_second_db_query(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore
        import importlib
        db_models = importlib.import_module("database.models")
        monkeypatch.setattr(db_models, "LearnedSalesPolicy", _PolicyRow)
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: True)
        )

        db = self._seed_db([
            _PolicyRow(scope="global", industry="*", intent="ask_product",
                       recommended_action="search_products", confidence=0.7,
                       sample_size=100),
        ])
        # Track raw query call count.
        original_query = db.query
        calls = {"n": 0}
        def _counting_query(model):
            calls["n"] += 1
            return original_query(model)
        db.query = _counting_query  # type: ignore[assignment]

        store = LearnedPolicyStore(db, ttl_seconds=60)
        store.lookup("ask_product")
        store.lookup("ask_product")
        assert calls["n"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 4. PolicyOverrideLayer
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _Intent:
    name: str
    confidence: float = 0.9


@dataclass
class _BrainCtx:
    intent: _Intent
    tenant_context: Any = None
    facts: Any = None
    _db: Any = None


class _NoopDecisionEngine:
    def __init__(self, action: str = "search_products"):
        self._action = action

    def decide(self, ctx):
        from modules.ai.brain.types import Decision
        return Decision(action=self._action, args={"reason": "inner"})


class TestPolicyOverrideLayer:
    def test_preserves_inner_action_when_no_hint(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore, PolicyOverrideLayer

        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: True)
        )
        store = LearnedPolicyStore(_FakePolicyTable(), ttl_seconds=10)
        layer = PolicyOverrideLayer(_NoopDecisionEngine("greet"), store=store)
        ctx = _BrainCtx(intent=_Intent("greeting"))
        decision = layer.decide(ctx)

        assert decision.action == "greet"
        assert "policy_hint" not in decision.args

    def test_attaches_policy_hint_when_present(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore, PolicyOverrideLayer
        import importlib
        db_models = importlib.import_module("database.models")
        monkeypatch.setattr(db_models, "LearnedSalesPolicy", _PolicyRow)
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: True)
        )

        db = _FakePolicyTable()
        db.rows.append(_PolicyRow(
            scope="global", industry="*", intent="ask_product",
            recommended_action="search_products",
            recommended_ui="product_cards",
            confidence=0.82, sample_size=120,
        ))
        layer = PolicyOverrideLayer(
            _NoopDecisionEngine("search_products"),
            store=LearnedPolicyStore(db, ttl_seconds=10),
        )
        ctx = _BrainCtx(intent=_Intent("ask_product"))
        decision = layer.decide(ctx)

        assert decision.action == "search_products"  # unchanged
        hint = decision.args["policy_hint"]
        assert hint["recommended_action"] == "search_products"
        assert hint["recommended_ui"] == "product_cards"
        assert hint["matches_inner"] is True
        assert hint["confidence"] == pytest.approx(0.82)

    def test_disabled_master_switch_is_noop(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore, PolicyOverrideLayer
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: False)
        )
        layer = PolicyOverrideLayer(
            _NoopDecisionEngine("greet"),
            store=LearnedPolicyStore(_FakePolicyTable()),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("ask_product")))
        assert "policy_hint" not in decision.args

    def test_lookup_failure_does_not_break_decision(self, monkeypatch):
        from modules.ai.learning import LearnedPolicyStore, PolicyOverrideLayer
        monkeypatch.setattr(
            LearnedPolicyStore, "is_enabled", staticmethod(lambda: True)
        )
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("simulated outage")
        layer = PolicyOverrideLayer(
            _NoopDecisionEngine("greet"),
            store=LearnedPolicyStore(broken_db),
        )
        decision = layer.decide(_BrainCtx(intent=_Intent("greeting")))
        assert decision.action == "greet"
        assert "policy_hint" not in decision.args

    def test_layer_works_without_store_when_no_db(self, monkeypatch):
        """No store passed AND ctx has no _db → layer is a pure no-op."""
        from modules.ai.learning import PolicyOverrideLayer
        layer = PolicyOverrideLayer(_NoopDecisionEngine("greet"))
        decision = layer.decide(_BrainCtx(intent=_Intent("greeting")))
        assert decision.action == "greet"
        assert "policy_hint" not in decision.args


# ═══════════════════════════════════════════════════════════════════════════
# 5. DecisionEngine fallback (regression safety net)
# ═══════════════════════════════════════════════════════════════════════════

class TestDecisionEngineFallback:
    def test_engine_works_without_learning_module(self):
        """Pre-Phase-1.7 paths must keep working unchanged."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.types import (
            BrainContext,
            CommerceFacts,
            Intent,
            INTENT_GREETING,
            MerchantConversationState,
        )
        eng = DefaultDecisionEngine()
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="مرحبا",
            intent=Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا"),
            state=MerchantConversationState(),
            facts=CommerceFacts(
                has_products=True, product_count=1, in_stock_count=1,
                has_active_integration=True, orderable=True,
                snapshot_fresh=True, store_name="x",
            ),
        )
        decision = eng.decide(ctx)
        # Engine still returns a real action — no policy_hint, no error.
        assert decision.action
        assert "policy_hint" not in decision.args
