"""
modules/ai/learning/readiness.py
────────────────────────────────
Phase 1.8 — Soft-bias readiness gates.

The decision of *when* to promote a learned ``policy_hint`` from a
passive observation to an active soft bias is a one-way operation:
once the brain starts steering on a hint, the resulting traces are no
longer a clean baseline and the uplift signal becomes
self-reinforcing.

This module defines the gates that an automated tool (or an operator
running a checklist) must clear before any phase >= 1.9 may flip a
hint into the active bias path.

The gates are intentionally pure: they take an ``AdoptionReport`` and
return a structured ``ReadinessVerdict``.  No side-effects, no DB
access — so the same evaluator powers tests, dashboards and the
upcoming bias engine.

Default thresholds
──────────────────
* ``min_sample_size``      = 100 hinted turns per (intent, industry)
* ``min_observed_uplift``  = +0.05 (5 percentage points conversion)
* ``min_alignment_rate``   = 0.30 (the hint matched the inner decision
  often enough to actually have a baseline to compare against)
* ``sensitive_intents``    — intents where any *negative* observed
  uplift blocks readiness even if all other gates pass.  These are the
  intents where an over-eager bias could lose money or send a customer
  away (checkout / payment / objection / handoff / support_handoff /
  abandon).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from .adoption import AdoptionReport, AdoptionStats


# ── Defaults ────────────────────────────────────────────────────────────────

# Intents where a *negative* observed uplift blocks readiness regardless
# of sample size — losing checkouts to a wrong bias is unacceptable.
DEFAULT_SENSITIVE_INTENTS: FrozenSet[str] = frozenset({
    "checkout",
    "payment",
    "complete_order",
    "objection",
    "complaint",
    "handoff",
    "support_handoff",
    "abandon",
    "abandoned",
})


@dataclass(frozen=True)
class ReadinessVerdict:
    """Per-bucket evaluation result."""
    intent: str
    industry: str
    ready: bool
    reasons: Tuple[str, ...] = ()
    sample_size: int = 0
    hint_alignment_rate: float = 0.0
    observed_uplift: float = 0.0


@dataclass
class ReadinessSummary:
    """Aggregate readiness evaluation across an entire ``AdoptionReport``."""
    by_intent: Dict[str, ReadinessVerdict] = field(default_factory=dict)
    by_industry_intent: Dict[Tuple[str, str], ReadinessVerdict] = field(default_factory=dict)

    @property
    def ready_intents(self) -> List[str]:
        return sorted(k for k, v in self.by_intent.items() if v.ready)

    @property
    def blocked_intents(self) -> List[str]:
        return sorted(k for k, v in self.by_intent.items() if not v.ready)

    def to_dict(self) -> Dict[str, object]:
        return {
            "ready_intents":     self.ready_intents,
            "blocked_intents":   self.blocked_intents,
            "by_intent": {
                k: {
                    "ready":               v.ready,
                    "reasons":             list(v.reasons),
                    "sample_size":         v.sample_size,
                    "hint_alignment_rate": round(v.hint_alignment_rate, 4),
                    "observed_uplift":     round(v.observed_uplift, 4),
                }
                for k, v in self.by_intent.items()
            },
            "by_industry_intent": [
                {
                    "industry":            ind,
                    "intent":              it,
                    "ready":               v.ready,
                    "reasons":             list(v.reasons),
                    "sample_size":         v.sample_size,
                    "hint_alignment_rate": round(v.hint_alignment_rate, 4),
                    "observed_uplift":     round(v.observed_uplift, 4),
                }
                for (ind, it), v in self.by_industry_intent.items()
            ],
        }


# ── Gate ────────────────────────────────────────────────────────────────────

@dataclass
class ReadinessGate:
    """Pure evaluator that turns ``AdoptionStats`` into a verdict."""
    min_sample_size: int = 100
    min_observed_uplift: float = 0.05
    min_alignment_rate: float = 0.30
    sensitive_intents: FrozenSet[str] = field(default_factory=lambda: DEFAULT_SENSITIVE_INTENTS)

    @classmethod
    def from_config(cls) -> "ReadinessGate":
        """Build a gate using ``core.config`` values when available.

        Falls back to defaults silently — never raises so import-time
        misconfiguration cannot prevent the metric pipeline from running.
        """
        try:
            from core import config
            return cls(
                min_sample_size      = int(getattr(config, "LEARNED_POLICY_BIAS_MIN_SAMPLE_SIZE", 100)),
                min_observed_uplift  = float(getattr(config, "LEARNED_POLICY_BIAS_MIN_UPLIFT", 0.05)),
                min_alignment_rate   = float(getattr(config, "LEARNED_POLICY_BIAS_MIN_ALIGNMENT", 0.30)),
            )
        except Exception:
            return cls()

    # ── Per-bucket evaluation ──────────────────────────────────────────

    def evaluate(self, stats: AdoptionStats) -> ReadinessVerdict:
        """Return a verdict for a single ``(intent, industry)`` bucket."""
        intent_norm = (stats.intent or "").strip().lower() or "unknown"
        industry    = (stats.industry or "*").strip().lower() or "*"

        reasons: List[str] = []

        if stats.hint_present_count < int(self.min_sample_size):
            reasons.append(
                f"insufficient_sample:{stats.hint_present_count}<{self.min_sample_size}"
            )
        if stats.hint_alignment_rate < float(self.min_alignment_rate):
            reasons.append(
                f"low_alignment:{round(stats.hint_alignment_rate, 4)}<{self.min_alignment_rate}"
            )
        if stats.observed_uplift < float(self.min_observed_uplift):
            reasons.append(
                f"insufficient_uplift:{round(stats.observed_uplift, 4)}<{self.min_observed_uplift}"
            )
        # Hard regression guard — sensitive intents lose readiness on
        # ANY negative uplift, even if the previous gate would pass.
        if intent_norm in self.sensitive_intents and stats.observed_uplift < 0:
            reasons.append("sensitive_regression")

        return ReadinessVerdict(
            intent              = intent_norm,
            industry            = industry,
            ready               = not reasons,
            reasons             = tuple(reasons),
            sample_size         = stats.hint_present_count,
            hint_alignment_rate = stats.hint_alignment_rate,
            observed_uplift     = stats.observed_uplift,
        )

    # ── Whole-report evaluation ────────────────────────────────────────

    def evaluate_report(self, report: AdoptionReport) -> ReadinessSummary:
        summary = ReadinessSummary()
        for intent, stats in report.by_intent.items():
            summary.by_intent[intent] = self.evaluate(stats)
        for key, stats in report.by_industry_intent.items():
            summary.by_industry_intent[key] = self.evaluate(stats)
        return summary

    # ── Convenience for callers that pass dicts ────────────────────────

    def evaluate_many(self, stats_iter: Iterable[AdoptionStats]) -> List[ReadinessVerdict]:
        return [self.evaluate(s) for s in stats_iter or []]
