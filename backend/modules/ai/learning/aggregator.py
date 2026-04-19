"""
modules/ai/learning/aggregator.py
─────────────────────────────────
Pure aggregation primitives for the cross-merchant policy learner.

Every function in this module is a stateless transformation over a
collection of signal-shaped objects (either ORM rows or plain dicts).
The functions intentionally never touch the database, so they are
trivially unit-testable and can be reused by future learners (vertical
sub-learner, channel-specific learner, …) without duplication.

Signal shape contract
─────────────────────
The aggregator only reads these attributes / dict keys:

    intent, action, ui_mode, outcome, industry, value_bucket

Any other attribute is ignored — the schema-level guarantee that no
raw data lives on a signal row is enforced upstream by
``modules.ai.security.trace_schema.validate_anonymized``.

Outcome → sentiment mapping
───────────────────────────
The aggregator collapses the rich ``OutcomeKind`` set into three
buckets so a "winning action" can be picked deterministically:

    POSITIVE → conversion / payment_sent / checkout_started / added_to_cart
    NEUTRAL  → product_presented / browse / greet / support / unknown
    NEGATIVE → abandoned / objection / error / handoff

Mapping is exposed as ``OUTCOME_SENTIMENT`` so downstream tooling can
render or extend it without re-deriving.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple


# ── Outcome → sentiment ─────────────────────────────────────────────────────

OUTCOME_POSITIVE = frozenset({
    "conversion",
    "payment_sent",
    "checkout_started",
    "added_to_cart",
})
OUTCOME_NEGATIVE = frozenset({
    "abandoned",
    "objection",
    "error",
    "handoff",
})
# Anything not in either set is treated as neutral (browse, greet,
# product_presented, support, unknown, …).

OUTCOME_SENTIMENT: Dict[str, str] = {}
for _o in OUTCOME_POSITIVE:
    OUTCOME_SENTIMENT[_o] = "positive"
for _o in OUTCOME_NEGATIVE:
    OUTCOME_SENTIMENT[_o] = "negative"


def classify_outcome_sentiment(outcome: Optional[str]) -> str:
    """Return ``"positive"`` / ``"negative"`` / ``"neutral"`` for an outcome.

    Unknown / empty values are treated as ``"neutral"`` so noisy signals
    don't bias the learner toward a particular action.
    """
    o = (outcome or "").strip().lower()
    if o in OUTCOME_POSITIVE:
        return "positive"
    if o in OUTCOME_NEGATIVE:
        return "negative"
    return "neutral"


# ── Aggregate dataclasses ───────────────────────────────────────────────────

@dataclass
class ActionStats:
    """Counts for a single (group_key, action) pair."""
    action: str
    count: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    ui_modes: Dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """Fraction of signals for this action with positive sentiment.

        Returns ``0.0`` for empty buckets so divide-by-zero is impossible
        for callers.
        """
        if self.count <= 0:
            return 0.0
        return self.positive / self.count

    def merge(self, signal: Any) -> None:
        sentiment = classify_outcome_sentiment(_attr(signal, "outcome"))
        self.count += 1
        if sentiment == "positive":
            self.positive += 1
        elif sentiment == "negative":
            self.negative += 1
        else:
            self.neutral += 1
        ui = (_attr(signal, "ui_mode") or "unknown").strip().lower() or "unknown"
        self.ui_modes[ui] = self.ui_modes.get(ui, 0) + 1


@dataclass
class UIStats:
    """Counts for a single (group_key, ui_mode) pair."""
    ui_mode: str
    count: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0

    @property
    def success_rate(self) -> float:
        if self.count <= 0:
            return 0.0
        return self.positive / self.count

    def merge(self, signal: Any) -> None:
        sentiment = classify_outcome_sentiment(_attr(signal, "outcome"))
        self.count += 1
        if sentiment == "positive":
            self.positive += 1
        elif sentiment == "negative":
            self.negative += 1
        else:
            self.neutral += 1


# ── Aggregation functions ───────────────────────────────────────────────────

def aggregate_action_stats(
    signals: Iterable[Any],
    *,
    group_by: Tuple[str, ...] = ("intent",),
) -> Dict[Tuple[str, ...], Dict[str, ActionStats]]:
    """Group signals by ``group_by`` keys and aggregate per ``action``.

    The result is a nested mapping::

        { group_key_tuple → { action → ActionStats } }

    ``group_by`` is sorted-tolerant; pass ``("intent",)`` for the global
    learner and ``("industry", "intent")`` for the vertical learner.
    The function never mutates input signals.
    """
    out: Dict[Tuple[str, ...], Dict[str, ActionStats]] = defaultdict(dict)
    for sig in signals or []:
        key = _group_key(sig, group_by)
        action = (_attr(sig, "action") or "unknown").strip().lower() or "unknown"
        bucket = out[key].setdefault(action, ActionStats(action=action))
        bucket.merge(sig)
    # Normalise the outer dict so callers can rely on plain dict semantics.
    return {k: dict(v) for k, v in out.items()}


def aggregate_ui_stats(
    signals: Iterable[Any],
    *,
    group_by: Tuple[str, ...] = ("intent",),
) -> Dict[Tuple[str, ...], Dict[str, UIStats]]:
    """Group signals by ``group_by`` keys and aggregate per ``ui_mode``."""
    out: Dict[Tuple[str, ...], Dict[str, UIStats]] = defaultdict(dict)
    for sig in signals or []:
        key = _group_key(sig, group_by)
        ui = (_attr(sig, "ui_mode") or "unknown").strip().lower() or "unknown"
        bucket = out[key].setdefault(ui, UIStats(ui_mode=ui))
        bucket.merge(sig)
    return {k: dict(v) for k, v in out.items()}


# ── Selectors ───────────────────────────────────────────────────────────────

def pick_recommended_action(
    actions: Dict[str, ActionStats],
    *,
    min_sample_size: int = 30,
    min_confidence: float = 0.6,
) -> Optional[ActionStats]:
    """Return the dominant action for a single group bucket, or ``None``.

    Selection rules
    ───────────────
    1. Total sample size across all actions in the bucket must reach
       ``min_sample_size``.  Below that threshold the bucket is ignored
       entirely — not just the winning action.
    2. The action with the highest ``success_rate`` (ties broken by
       ``count``) is the candidate.
    3. The candidate's ``count / total`` share AND its ``success_rate``
       must both reach ``min_confidence`` for it to be returned.

    The double threshold protects against two failure modes:
      * Many actions, none dominant → no recommendation worth pushing.
      * Only one action seen → wins by default; rejected unless its
        success_rate is also high.
    """
    if not actions:
        return None
    total = sum(s.count for s in actions.values())
    if total < int(max(min_sample_size, 0)):
        return None

    # Stable ordering: success_rate desc, count desc, action asc.
    candidates = sorted(
        actions.values(),
        key=lambda s: (-s.success_rate, -s.count, s.action),
    )
    winner = candidates[0]
    if winner.count <= 0:
        return None
    share = winner.count / total
    if share < float(min_confidence):
        return None
    if winner.success_rate < float(min_confidence):
        return None
    return winner


def pick_recommended_ui(
    ui_modes: Dict[str, int] | Dict[str, UIStats],
    *,
    min_sample_size: int = 30,
) -> Optional[str]:
    """Return the dominant ``ui_mode`` label for a bucket or ``None``.

    Accepts either the ``ActionStats.ui_modes`` shape (``{ui: count}``)
    or a ``UIStats`` mapping (``{ui: UIStats}``); the latter is preferred
    because it carries success-rate information that breaks ties more
    informatively.
    """
    if not ui_modes:
        return None

    items: list[tuple[str, int, float]] = []
    total = 0
    for key, value in ui_modes.items():
        if isinstance(value, UIStats):
            count = value.count
            sr    = value.success_rate
        else:
            count = int(value or 0)
            sr    = 0.0
        if not key or key == "unknown":
            continue
        items.append((str(key), count, sr))
        total += count

    if total < int(max(min_sample_size, 0)) or not items:
        return None

    items.sort(key=lambda t: (-t[2], -t[1], t[0]))
    winner_ui, winner_count, _ = items[0]
    if winner_count <= 0:
        return None
    return winner_ui


# ── Internal helpers ────────────────────────────────────────────────────────

def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _group_key(signal: Any, keys: Tuple[str, ...]) -> Tuple[str, ...]:
    parts = []
    for k in keys:
        v = _attr(signal, k)
        parts.append((str(v).strip().lower() or "unknown") if v is not None else "unknown")
    return tuple(parts)
