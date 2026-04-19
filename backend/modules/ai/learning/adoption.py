"""
modules/ai/learning/adoption.py
───────────────────────────────
Phase 1.8 — Policy hint adoption & uplift measurement.

Pure aggregation primitives that turn anonymized turn signals
(``CrossMerchantSignal`` rows or dict-shaped fixtures) into:

* ``hint_alignment_rate`` — share of turns where the inner decision
  matched the recommended action.
* ``conversion_when_aligned`` — conversion rate on turns where decision
  and hint agreed.
* ``conversion_when_not_aligned`` — conversion rate on turns where the
  decision diverged from the hint.
* ``observed_uplift`` — the (signed) difference between the two rates.

The module is intentionally side-effect-free:
* It never queries or writes to the database.
* It never derives raw merchant identity from signals — only the
  whitelisted, already-anonymized fields ``intent``, ``industry``,
  ``outcome`` and the ``extra.hint_*`` keys are consumed.

The companion ``readiness.py`` module turns these reports into a
go / no-go verdict for the upcoming Soft Bias phase.

Signal contract
───────────────
A "signal" is anything with these keys / attributes:
    intent, industry, outcome, extra
where ``extra`` is a mapping containing the Phase 1.8 keys::

    {
        "hint_present": bool,
        "hint_aligned": bool,        # set only when hint_present is True
        "hint_action":  str,         # categorical, optional
        ...
    }

Signals where ``extra`` is missing or ``hint_present`` is falsy are
counted in ``sample_size`` but contribute zero to the alignment /
conversion bucket (they sit in ``hint_absent_count`` instead).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .aggregator import OUTCOME_POSITIVE


# ── Helpers ─────────────────────────────────────────────────────────────────

def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _safe_extra(signal: Any) -> Dict[str, Any]:
    extra = _attr(signal, "extra")
    if isinstance(extra, dict):
        return extra
    return {}


def _is_positive_outcome(outcome: Optional[str]) -> bool:
    return (outcome or "").strip().lower() in OUTCOME_POSITIVE


# ── Stats containers ────────────────────────────────────────────────────────

@dataclass
class AdoptionStats:
    """Adoption + conversion counters for a single grouping bucket.

    ``intent`` and ``industry`` together form the primary key.  For the
    "global" slice the industry is set to ``"*"``; for an "intent-only"
    rollup the industry slot is filled with ``"*"`` as well.
    """
    intent: str
    industry: str = "*"
    sample_size: int = 0
    hint_present_count: int = 0
    hint_absent_count: int = 0
    aligned_count: int = 0
    aligned_conversion_count: int = 0
    not_aligned_count: int = 0
    not_aligned_conversion_count: int = 0

    # ── Derived rates ──────────────────────────────────────────────────

    @property
    def hint_alignment_rate(self) -> float:
        """Fraction of *hinted* turns where the decision matched the hint.

        Returns 0.0 when there were no hinted turns — a non-existent
        denominator must never appear as ``inf`` / ``nan`` in metrics.
        """
        if self.hint_present_count <= 0:
            return 0.0
        return self.aligned_count / self.hint_present_count

    @property
    def conversion_when_aligned(self) -> float:
        if self.aligned_count <= 0:
            return 0.0
        return self.aligned_conversion_count / self.aligned_count

    @property
    def conversion_when_not_aligned(self) -> float:
        if self.not_aligned_count <= 0:
            return 0.0
        return self.not_aligned_conversion_count / self.not_aligned_count

    @property
    def observed_uplift(self) -> float:
        """Signed delta between aligned / not-aligned conversion rates."""
        return self.conversion_when_aligned - self.conversion_when_not_aligned

    # ── Mutation ───────────────────────────────────────────────────────

    def merge(self, signal: Any) -> None:
        self.sample_size += 1
        extra = _safe_extra(signal)
        hint_present = bool(extra.get("hint_present", False))
        if not hint_present:
            self.hint_absent_count += 1
            return

        self.hint_present_count += 1
        aligned = bool(extra.get("hint_aligned", False))
        converted = _is_positive_outcome(_attr(signal, "outcome"))
        if aligned:
            self.aligned_count += 1
            if converted:
                self.aligned_conversion_count += 1
        else:
            self.not_aligned_count += 1
            if converted:
                self.not_aligned_conversion_count += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent":                       self.intent,
            "industry":                     self.industry,
            "sample_size":                  self.sample_size,
            "hint_present_count":           self.hint_present_count,
            "hint_absent_count":            self.hint_absent_count,
            "aligned_count":                self.aligned_count,
            "not_aligned_count":            self.not_aligned_count,
            "aligned_conversion_count":     self.aligned_conversion_count,
            "not_aligned_conversion_count": self.not_aligned_conversion_count,
            "hint_alignment_rate":          round(self.hint_alignment_rate, 4),
            "conversion_when_aligned":      round(self.conversion_when_aligned, 4),
            "conversion_when_not_aligned":  round(self.conversion_when_not_aligned, 4),
            "observed_uplift":              round(self.observed_uplift, 4),
        }


@dataclass
class AdoptionReport:
    """Top-level result returned by ``compute_adoption_metrics``."""
    overall: AdoptionStats
    by_intent: Dict[str, AdoptionStats] = field(default_factory=dict)
    by_industry_intent: Dict[Tuple[str, str], AdoptionStats] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall":             self.overall.to_dict(),
            "by_intent":           {k: v.to_dict() for k, v in self.by_intent.items()},
            "by_industry_intent": [
                {"industry": ind, "intent": it, **stats.to_dict()}
                for (ind, it), stats in self.by_industry_intent.items()
            ],
        }


# ── Public API ──────────────────────────────────────────────────────────────

def compute_adoption_metrics(signals: Iterable[Any]) -> AdoptionReport:
    """Aggregate ``signals`` into an ``AdoptionReport``.

    The function performs three rollups in one pass:

    * ``overall`` — every signal counted regardless of intent / industry.
    * ``by_intent`` — one ``AdoptionStats`` per ``intent``.
    * ``by_industry_intent`` — one ``AdoptionStats`` per ``(industry, intent)``
      where ``industry`` is non-empty and not ``"unknown"`` / ``"*"``.

    Iteration is deterministic (input order is preserved) and the function
    never raises on malformed signals — bad rows are silently skipped so a
    single corrupt entry cannot break the whole report.
    """
    overall = AdoptionStats(intent="*", industry="*")
    by_intent: Dict[str, AdoptionStats] = {}
    by_ii: Dict[Tuple[str, str], AdoptionStats] = {}

    for sig in signals or []:
        try:
            intent   = (_attr(sig, "intent")   or "unknown").strip().lower() or "unknown"
            industry = (_attr(sig, "industry") or "unknown").strip().lower() or "unknown"
        except Exception:
            continue

        overall.merge(sig)

        bucket = by_intent.setdefault(intent, AdoptionStats(intent=intent))
        bucket.merge(sig)

        if industry and industry not in {"unknown", "*"}:
            key = (industry, intent)
            ii_bucket = by_ii.setdefault(key, AdoptionStats(intent=intent, industry=industry))
            ii_bucket.merge(sig)

    return AdoptionReport(
        overall            = overall,
        by_intent          = by_intent,
        by_industry_intent = by_ii,
    )


# ── Read path (best-effort) ─────────────────────────────────────────────────

def load_adoption_report(db: Any, *, limit: Optional[int] = None) -> AdoptionReport:
    """Read signals from the cross-merchant store and build a report.

    Provided for ops scripts / dashboards.  Returns an empty
    ``AdoptionReport`` on any failure — callers must handle ``overall.sample_size == 0``.
    """
    try:
        from database.models import CrossMerchantSignal
    except Exception:
        return AdoptionReport(overall=AdoptionStats(intent="*"))

    try:
        q = db.query(CrossMerchantSignal)
        if limit:
            q = q.limit(int(limit))
        rows: List[Any] = list(q.all())
    except Exception:
        return AdoptionReport(overall=AdoptionStats(intent="*"))

    return compute_adoption_metrics(rows)
