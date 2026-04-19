"""
modules/ai/learning/bias_metrics.py
───────────────────────────────────
Phase 1.9 → narrow staging trial — comparative metrics for soft bias.

Purpose
───────
After ``PolicyBiasLayer`` is enabled in staging we need to **prove**
that the bias actually moves the needle before widening the rollout.
This module reads anonymized signals from ``cross_merchant_signals``,
splits them into ``bias_on`` (turn was actually biased) and ``bias_off``
(turn had a hint but bias was not applied — the natural control group),
and computes six rate metrics per group.

Why "hint present + bias not applied" is the control
─────────────────────────────────────────────────────
The cleanest A/B baseline is the population that was *eligible* for
bias (a learned policy existed for the bucket) but received no bias
because of an operator gate (master flag off, env mismatch, component
disabled, readiness not yet reached, etc.).  This holds intent and
industry constant across the two groups so the comparison reflects the
bias itself rather than baseline differences in traffic.

Hard rules
──────────
* Read-only on ``cross_merchant_signals``.
* No raw merchant data is ever returned — outputs are aggregate floats.
* Pure functions; the only side-effect-touching helper is
  ``load_bias_comparison(db)`` which simply runs a single SQLAlchemy
  query and delegates to the pure aggregator.
* Defensive: missing fields default to ``False`` / ``"unknown"`` so a
  partial trace can still be counted toward the totals.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("nahla.ai.learning.bias_metrics")


# ── Outcome / action classifications ────────────────────────────────────────

# Outcomes that mean the customer engaged forward in the funnel.
_PROGRESSING_OUTCOMES = frozenset({
    "product_presented",
    "added_to_cart",
    "checkout_started",
    "payment_sent",
    "conversion",
})

# Outcomes that mean the funnel collapsed (negative signal).
_NEGATIVE_OUTCOMES = frozenset({
    "abandoned",
    "error",
})

# Outcomes that mean checkout was at least *initiated*.
_CHECKOUT_OUTCOMES = frozenset({
    "added_to_cart",
    "checkout_started",
    "payment_sent",
    "conversion",
})

# Outcomes / actions that prove a payment link was requested.
_PAYMENT_OUTCOMES = frozenset({"payment_sent", "conversion"})
_PAYMENT_ACTIONS  = frozenset({"send_payment_link", "propose_draft_order"})

# Actions whose presence is a fallback / clarification rather than a
# productive next step (treat as "confusion").
_FALLBACK_ACTIONS = frozenset({
    "clarify",
    "narrow_choices",
    "llm_reply",
    "handoff",
    "handoff_to_human",
})


# ── Public dataclasses ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class GroupStats:
    """Aggregated rates for a single group (bias_on or bias_off)."""
    n: int = 0
    selection_rate:           float = 0.0  # turn presented options AND user moved
    progression_rate:         float = 0.0  # stage advanced this turn
    checkout_initiation_rate: float = 0.0  # any checkout-related outcome
    payment_link_rate:        float = 0.0  # send_payment_link or PAYMENT_SENT
    conversion_rate:          float = 0.0  # outcome == CONVERSION
    fallback_rate:            float = 0.0  # clarify / handoff / abandoned

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n":                        self.n,
            "selection_rate":           round(self.selection_rate, 4),
            "progression_rate":         round(self.progression_rate, 4),
            "checkout_initiation_rate": round(self.checkout_initiation_rate, 4),
            "payment_link_rate":        round(self.payment_link_rate, 4),
            "conversion_rate":          round(self.conversion_rate, 4),
            "fallback_rate":            round(self.fallback_rate, 4),
        }


@dataclass(frozen=True)
class DeltaStats:
    """Per-metric ``bias_on - bias_off`` deltas (positive = bias helps)."""
    selection_rate:           float = 0.0
    progression_rate:         float = 0.0
    checkout_initiation_rate: float = 0.0
    payment_link_rate:        float = 0.0
    conversion_rate:          float = 0.0
    fallback_rate:            float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {k: round(v, 4) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class BiasComparisonStats:
    """Side-by-side comparison for a single bucket (overall / intent / vertical)."""
    intent: str
    industry: str
    bias_on:  GroupStats = field(default_factory=GroupStats)
    bias_off: GroupStats = field(default_factory=GroupStats)
    deltas:   DeltaStats = field(default_factory=DeltaStats)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent":   self.intent,
            "industry": self.industry,
            "bias_on":  self.bias_on.to_dict(),
            "bias_off": self.bias_off.to_dict(),
            "deltas":   self.deltas.to_dict(),
        }


@dataclass(frozen=True)
class BiasComparisonReport:
    """Full rollup over all signals."""
    overall:            BiasComparisonStats
    by_intent:          Dict[str, BiasComparisonStats]
    by_industry_intent: Dict[Tuple[str, str], BiasComparisonStats]
    generated_at:       datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at":       self.generated_at.isoformat(),
            "overall":            self.overall.to_dict(),
            "by_intent":          {k: v.to_dict() for k, v in self.by_intent.items()},
            "by_industry_intent": {
                f"{ind}|{intent}": v.to_dict()
                for (ind, intent), v in self.by_industry_intent.items()
            },
        }


# ── Aggregation primitives (pure) ───────────────────────────────────────────

@dataclass
class _Counter:
    n:                int = 0
    selection:        int = 0
    progressed:       int = 0
    checkout:         int = 0
    payment:          int = 0
    conversion:       int = 0
    fallback:         int = 0

    def add(self, sig: Any) -> None:
        self.n += 1
        if _is_selection(sig):
            self.selection += 1
        if _is_progression(sig):
            self.progressed += 1
        if _is_checkout(sig):
            self.checkout += 1
        if _is_payment(sig):
            self.payment += 1
        if _is_conversion(sig):
            self.conversion += 1
        if _is_fallback(sig):
            self.fallback += 1

    def to_group(self) -> GroupStats:
        if self.n == 0:
            return GroupStats()
        n = float(self.n)
        return GroupStats(
            n                        = self.n,
            selection_rate           = self.selection / n,
            progression_rate         = self.progressed / n,
            checkout_initiation_rate = self.checkout / n,
            payment_link_rate        = self.payment / n,
            conversion_rate          = self.conversion / n,
            fallback_rate            = self.fallback / n,
        )


def _delta(a: GroupStats, b: GroupStats) -> DeltaStats:
    return DeltaStats(
        selection_rate           = a.selection_rate           - b.selection_rate,
        progression_rate         = a.progression_rate         - b.progression_rate,
        checkout_initiation_rate = a.checkout_initiation_rate - b.checkout_initiation_rate,
        payment_link_rate        = a.payment_link_rate        - b.payment_link_rate,
        conversion_rate          = a.conversion_rate          - b.conversion_rate,
        fallback_rate            = a.fallback_rate            - b.fallback_rate,
    )


# ── Signal feature extractors ───────────────────────────────────────────────

def _outcome(sig: Any) -> str:
    return str(getattr(sig, "outcome", "") or "").strip().lower()


def _action(sig: Any) -> str:
    return str(getattr(sig, "action", "") or "").strip().lower()


def _intent(sig: Any) -> str:
    return str(getattr(sig, "intent", "") or "").strip().lower() or "unknown"


def _industry(sig: Any) -> str:
    val = str(getattr(sig, "industry", "") or "").strip().lower()
    return val or "*"


def _extra(sig: Any) -> Dict[str, Any]:
    e = getattr(sig, "extra", None)
    return e if isinstance(e, dict) else {}


def _had_buttons(sig: Any) -> bool:
    return bool(_extra(sig).get("had_buttons"))


def _stage_changed(sig: Any) -> bool:
    extra = _extra(sig)
    before = str(extra.get("stage_before") or "").strip().lower()
    after  = str(extra.get("stage_after") or "").strip().lower()
    return bool(before and after and before != after)


def _hint_present(sig: Any) -> bool:
    return bool(_extra(sig).get("hint_present"))


def _bias_applied(sig: Any) -> bool:
    return bool(_extra(sig).get("bias_applied"))


# ── Metric predicates ───────────────────────────────────────────────────────

def _is_selection(sig: Any) -> bool:
    """A "selection" turn is one where the user was offered choices
    (buttons) AND the funnel didn't collapse afterwards.

    Without raw inbound text we can't measure clicks directly; the next
    best signal is "options were shown and the customer continued
    forward".  Using ``had_buttons`` keeps this anonymized and stable
    across executors.
    """
    if not _had_buttons(sig):
        return False
    outcome = _outcome(sig)
    return outcome and outcome not in _NEGATIVE_OUTCOMES and outcome != "unknown"


def _is_progression(sig: Any) -> bool:
    if _stage_changed(sig):
        return True
    return _outcome(sig) in _PROGRESSING_OUTCOMES


def _is_checkout(sig: Any) -> bool:
    return _outcome(sig) in _CHECKOUT_OUTCOMES


def _is_payment(sig: Any) -> bool:
    return _action(sig) in _PAYMENT_ACTIONS or _outcome(sig) in _PAYMENT_OUTCOMES


def _is_conversion(sig: Any) -> bool:
    return _outcome(sig) == "conversion"


def _is_fallback(sig: Any) -> bool:
    if _action(sig) in _FALLBACK_ACTIONS:
        return True
    return _outcome(sig) in _NEGATIVE_OUTCOMES


# ── Pure aggregator ─────────────────────────────────────────────────────────

def compute_bias_comparison(signals: Iterable[Any]) -> BiasComparisonReport:
    """Build a ``BiasComparisonReport`` from anonymized signals.

    Only signals where ``hint_present`` is True are counted — turns
    without an eligible policy are noise for this comparison.  Within
    that population:

      * ``bias_applied=True``  → bias_on counter
      * ``bias_applied=False`` → bias_off counter (control)

    The function is fully deterministic and side-effect free.
    """
    overall_on  = _Counter()
    overall_off = _Counter()
    by_intent_on:  Dict[str, _Counter] = defaultdict(_Counter)
    by_intent_off: Dict[str, _Counter] = defaultdict(_Counter)
    by_vert_on:  Dict[Tuple[str, str], _Counter] = defaultdict(_Counter)
    by_vert_off: Dict[Tuple[str, str], _Counter] = defaultdict(_Counter)

    for sig in signals or []:
        try:
            if not _hint_present(sig):
                continue
            intent   = _intent(sig)
            industry = _industry(sig)
            on       = _bias_applied(sig)

            (overall_on if on else overall_off).add(sig)
            (by_intent_on if on else by_intent_off)[intent].add(sig)
            if industry and industry != "*":
                (by_vert_on if on else by_vert_off)[(industry, intent)].add(sig)
        except Exception:
            # A single malformed row must never poison the whole report.
            continue

    overall = BiasComparisonStats(
        intent="*", industry="*",
        bias_on  = overall_on.to_group(),
        bias_off = overall_off.to_group(),
        deltas   = _delta(overall_on.to_group(), overall_off.to_group()),
    )
    by_intent: Dict[str, BiasComparisonStats] = {}
    for intent in set(by_intent_on) | set(by_intent_off):
        on  = by_intent_on.get(intent, _Counter()).to_group()
        off = by_intent_off.get(intent, _Counter()).to_group()
        by_intent[intent] = BiasComparisonStats(
            intent=intent, industry="*",
            bias_on=on, bias_off=off, deltas=_delta(on, off),
        )

    by_vert: Dict[Tuple[str, str], BiasComparisonStats] = {}
    for key in set(by_vert_on) | set(by_vert_off):
        on  = by_vert_on.get(key, _Counter()).to_group()
        off = by_vert_off.get(key, _Counter()).to_group()
        industry, intent = key
        by_vert[key] = BiasComparisonStats(
            intent=intent, industry=industry,
            bias_on=on, bias_off=off, deltas=_delta(on, off),
        )

    return BiasComparisonReport(
        overall=overall,
        by_intent=by_intent,
        by_industry_intent=by_vert,
        generated_at=datetime.now(timezone.utc),
    )


# ── Convenience loader (best-effort) ────────────────────────────────────────

def load_bias_comparison(db: Any, *, intent: Optional[str] = None,
                         industry: Optional[str] = None,
                         since: Optional[datetime] = None,
                         limit: Optional[int] = None) -> BiasComparisonReport:
    """Pull signals from ``cross_merchant_signals`` and aggregate.

    All filters are optional; pass ``intent='ask_product'`` and
    ``industry='fashion'`` to focus on the staging rollout bucket.
    Returns an empty report on any DB error so callers may treat the
    function as best-effort.
    """
    try:
        from database.models import CrossMerchantSignal
    except Exception as exc:
        logger.debug("[BiasMetrics] CrossMerchantSignal import failed: %s", exc)
        return _empty_report()

    try:
        q = db.query(CrossMerchantSignal)
        if intent:
            q = q.filter(CrossMerchantSignal.intent == intent)
        if industry:
            q = q.filter(CrossMerchantSignal.industry == industry)
        if since is not None:
            q = q.filter(CrossMerchantSignal.created_at >= since)
        if limit is not None and limit > 0:
            q = q.limit(int(limit))
        rows: List[Any] = list(q.all())
    except Exception as exc:
        logger.debug("[BiasMetrics] query failed: %s", exc)
        return _empty_report()

    return compute_bias_comparison(rows)


def _empty_report() -> BiasComparisonReport:
    return BiasComparisonReport(
        overall=BiasComparisonStats(intent="*", industry="*"),
        by_intent={},
        by_industry_intent={},
        generated_at=datetime.now(timezone.utc),
    )
