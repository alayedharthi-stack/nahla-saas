"""
modules/ai/learning
───────────────────
Phase 1.7 — First Global & Vertical Learner.

This package layers on top of the anonymized ``cross_merchant_signals``
store (Phase 1.5/1.6) and produces a small, read-only set of
recommended (action, ui_mode) tuples per (intent[, industry]).

Public surface
──────────────
``aggregator``          — pure functions that turn an iterable of
                          signal-shaped dicts into action / outcome /
                          ui-mode statistics.  No DB required.
``learner``             — ``PolicyLearner`` orchestrator: queries the
                          signals table, runs the aggregator, and
                          UPSERTs into ``learned_sales_policies``.
``policy_store``        — ``LearnedPolicyStore`` runtime lookup with
                          short-lived in-memory cache.
``policy_override``     — ``PolicyOverrideLayer`` decorates any
                          ``DecisionMaker`` and attaches a non-binding
                          ``policy_hint`` to its output.

Hard rules
──────────
* The learner is **read-only** w.r.t. ``cross_merchant_signals``.  It
  never inserts / updates / deletes in that table.
* The learner only writes to ``learned_sales_policies``; it never
  touches per-tenant tables.
* The override layer only **augments** decisions; it never overwrites
  the inner ``decision.action`` so behavior is preserved when the
  policy table is empty or when the learner is disabled.
"""
from .aggregator import (  # noqa: F401
    OUTCOME_SENTIMENT,
    ActionStats,
    UIStats,
    aggregate_action_stats,
    aggregate_ui_stats,
    classify_outcome_sentiment,
    pick_recommended_action,
    pick_recommended_ui,
)
from .policy_override import PolicyOverrideLayer  # noqa: F401
from .policy_store import LearnedPolicyStore, PolicyHint  # noqa: F401
from .learner import PolicyLearner, LearnerReport  # noqa: F401

# Phase 1.8 — adoption measurement & readiness gating (read-only).
from .adoption import (  # noqa: F401
    AdoptionReport,
    AdoptionStats,
    compute_adoption_metrics,
    load_adoption_report,
)
from .readiness import (  # noqa: F401
    DEFAULT_SENSITIVE_INTENTS,
    ReadinessGate,
    ReadinessSummary,
    ReadinessVerdict,
)
from .readiness_registry import ReadinessRegistry  # noqa: F401

# Phase 1.9 — Soft Policy Bias (guarded, narrow, reversible).
from .bias import (  # noqa: F401
    PROTECTED_ACTIONS,
    PROTECTED_INTENTS,
    PolicyBiasLayer,
    is_action_protected,
    is_intent_protected,
)

# Phase 1.9 — narrow staging trial: comparative metrics for soft bias.
from .bias_metrics import (  # noqa: F401
    BiasComparisonReport,
    BiasComparisonStats,
    DeltaStats,
    GroupStats,
    compute_bias_comparison,
    load_bias_comparison,
)
