"""
modules/ai/learning/learner.py
──────────────────────────────
Phase 1.7 — read-only PolicyLearner.

The learner reads anonymized turns from ``cross_merchant_signals`` and
materialises a small set of recommendations into
``learned_sales_policies``:

* one row per (``scope='global'``, ``industry='*'``, ``intent``)
* one row per (``scope='vertical'``, ``industry``, ``intent``) for every
  industry that crossed the sample-size threshold

Hard contracts
──────────────
* The learner never mutates ``cross_merchant_signals``.
* The learner never reads or writes any per-tenant table.
* Output rows reference only categorical labels already validated by
  ``validate_anonymized``; no raw text / id leaks here.
* ``run`` is idempotent — running it twice produces the same rows
  (UPSERT on the unique constraint).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .aggregator import (
    ActionStats,
    aggregate_action_stats,
    aggregate_ui_stats,
    pick_recommended_action,
    pick_recommended_ui,
)

logger = logging.getLogger("nahla.ai.learning.learner")

SCOPE_GLOBAL   = "global"
SCOPE_VERTICAL = "vertical"
GLOBAL_INDUSTRY = "*"


@dataclass
class LearnerReport:
    """Summary of a single ``PolicyLearner.run`` invocation."""
    signals_seen: int = 0
    global_written: int = 0
    vertical_written: int = 0
    skipped_below_sample_size: int = 0
    skipped_below_confidence: int = 0
    industries_covered: List[str] = field(default_factory=list)


class PolicyLearner:
    """Aggregates ``cross_merchant_signals`` into ``learned_sales_policies``.

    Construction never opens a session; the caller passes a SQLAlchemy
    ``db`` so the learner can be invoked from cron jobs, manual scripts,
    or unit tests with an in-memory engine.
    """

    def __init__(
        self,
        db: Any,
        *,
        min_sample_size: Optional[int] = None,
        min_confidence: Optional[float] = None,
    ) -> None:
        self.db = db
        self.min_sample_size = (
            int(min_sample_size) if min_sample_size is not None
            else _get_config_int("LEARNED_POLICY_MIN_SAMPLE_SIZE", 30)
        )
        self.min_confidence = (
            float(min_confidence) if min_confidence is not None
            else _get_config_float("LEARNED_POLICY_MIN_CONFIDENCE", 0.6)
        )

    # ── Master switch ──────────────────────────────────────────────────

    @staticmethod
    def is_enabled() -> bool:
        try:
            from core.config import LEARNED_POLICY_ENABLED
            return bool(LEARNED_POLICY_ENABLED)
        except Exception:
            return False

    # ── Public entry point ─────────────────────────────────────────────

    def run(self, *, signals: Optional[Iterable[Any]] = None) -> LearnerReport:
        """Compute and persist policies.

        ``signals`` may be supplied for tests; production callers pass
        ``None`` so the learner reads the full cross-merchant table.
        Returns a ``LearnerReport`` regardless of whether anything was
        written.
        """
        report = LearnerReport()

        if signals is None:
            try:
                signals = self._read_signals()
            except Exception as exc:
                logger.warning("[PolicyLearner] signal read failed: %s", exc)
                return report

        # Materialise once so we can iterate twice (global + vertical).
        rows = list(signals or [])
        report.signals_seen = len(rows)
        if not rows:
            return report

        report.global_written, skipped_g_size, skipped_g_conf = self._learn_global(rows)
        report.vertical_written, skipped_v_size, skipped_v_conf, industries = self._learn_vertical(rows)
        report.skipped_below_sample_size = skipped_g_size + skipped_v_size
        report.skipped_below_confidence  = skipped_g_conf + skipped_v_conf
        report.industries_covered = sorted(industries)

        try:
            self.db.commit()
        except Exception as exc:
            logger.warning("[PolicyLearner] commit failed: %s", exc)
            try:
                self.db.rollback()
            except Exception:
                pass

        return report

    # ── Signal read (read-only) ────────────────────────────────────────

    def _read_signals(self) -> List[Any]:
        """Return every anonymized signal currently in the table.

        We do NOT page or window here — Phase 1.7 starts with a simple
        full-table aggregation.  When data volume grows, replace this
        with a windowed query (e.g. last 30 days) without changing the
        aggregator surface.
        """
        from database.models import CrossMerchantSignal
        return list(self.db.query(CrossMerchantSignal).all())

    # ── Tier learners ──────────────────────────────────────────────────

    def _learn_global(self, signals: Iterable[Any]) -> Tuple[int, int, int]:
        """Learn one policy per intent across every merchant."""
        action_stats = aggregate_action_stats(signals, group_by=("intent",))
        ui_stats     = aggregate_ui_stats(signals, group_by=("intent",))

        written = 0
        skipped_size = 0
        skipped_conf = 0
        for (intent,), actions in action_stats.items():
            total = sum(s.count for s in actions.values())
            if total < self.min_sample_size:
                skipped_size += 1
                continue

            winner = pick_recommended_action(
                actions,
                min_sample_size = self.min_sample_size,
                min_confidence  = self.min_confidence,
            )
            if winner is None:
                skipped_conf += 1
                continue

            ui_for_intent = ui_stats.get((intent,), {})
            ui_label = (
                pick_recommended_ui(ui_for_intent, min_sample_size=self.min_sample_size)
                or pick_recommended_ui(winner.ui_modes, min_sample_size=0)
                or "unknown"
            )

            self._upsert(
                scope=SCOPE_GLOBAL,
                industry=GLOBAL_INDUSTRY,
                intent=intent,
                action=winner.action,
                ui=ui_label,
                confidence=_compute_confidence(winner, total),
                sample_size=total,
                extra={
                    "winner_count":      int(winner.count),
                    "winner_share":      round(winner.count / total, 4),
                    "winner_success_rate": round(winner.success_rate, 4),
                    "actions_observed":  len(actions),
                },
            )
            written += 1
        return written, skipped_size, skipped_conf

    def _learn_vertical(self, signals: Iterable[Any]) -> Tuple[int, int, int, List[str]]:
        """Learn one policy per (industry, intent)."""
        action_stats = aggregate_action_stats(signals, group_by=("industry", "intent"))
        ui_stats     = aggregate_ui_stats(signals, group_by=("industry", "intent"))

        written = 0
        skipped_size = 0
        skipped_conf = 0
        industries: set[str] = set()
        for (industry, intent), actions in action_stats.items():
            if not industry or industry == "unknown" or industry == GLOBAL_INDUSTRY:
                # Vertical learning needs a real industry tag.  Unknown
                # industry stays in the global tier.
                continue
            total = sum(s.count for s in actions.values())
            if total < self.min_sample_size:
                skipped_size += 1
                continue

            winner = pick_recommended_action(
                actions,
                min_sample_size = self.min_sample_size,
                min_confidence  = self.min_confidence,
            )
            if winner is None:
                skipped_conf += 1
                continue

            ui_for_bucket = ui_stats.get((industry, intent), {})
            ui_label = (
                pick_recommended_ui(ui_for_bucket, min_sample_size=self.min_sample_size)
                or pick_recommended_ui(winner.ui_modes, min_sample_size=0)
                or "unknown"
            )

            industries.add(industry)
            self._upsert(
                scope=SCOPE_VERTICAL,
                industry=industry,
                intent=intent,
                action=winner.action,
                ui=ui_label,
                confidence=_compute_confidence(winner, total),
                sample_size=total,
                extra={
                    "winner_count":        int(winner.count),
                    "winner_share":        round(winner.count / total, 4),
                    "winner_success_rate": round(winner.success_rate, 4),
                    "actions_observed":    len(actions),
                },
            )
            written += 1
        return written, skipped_size, skipped_conf, list(industries)

    # ── Persistence (UPSERT on unique constraint) ──────────────────────

    def _upsert(
        self,
        *,
        scope: str,
        industry: str,
        intent: str,
        action: str,
        ui: str,
        confidence: float,
        sample_size: int,
        extra: Dict[str, Any],
    ) -> None:
        try:
            from database.models import LearnedSalesPolicy
        except Exception as exc:
            logger.debug("[PolicyLearner] LearnedSalesPolicy unavailable: %s", exc)
            return

        try:
            row = (
                self.db.query(LearnedSalesPolicy)
                .filter_by(scope=scope, industry=industry, intent=intent)
                .first()
            )
            now = datetime.now(timezone.utc)
            if row is None:
                row = LearnedSalesPolicy(
                    scope              = scope,
                    industry           = industry,
                    intent             = intent,
                    recommended_action = action,
                    recommended_ui     = ui,
                    confidence         = float(confidence),
                    sample_size        = int(sample_size),
                    extra              = dict(extra or {}),
                    updated_at         = now,
                )
                self.db.add(row)
            else:
                row.recommended_action = action
                row.recommended_ui     = ui
                row.confidence         = float(confidence)
                row.sample_size        = int(sample_size)
                row.extra              = dict(extra or {})
                row.updated_at         = now
        except Exception as exc:
            logger.warning("[PolicyLearner] upsert failed for %s/%s/%s: %s",
                           scope, industry, intent, exc)


# ── Module helpers ──────────────────────────────────────────────────────────

def _get_config_int(name: str, default: int) -> int:
    try:
        from core import config
        return int(getattr(config, name, default))
    except Exception:
        return default


def _get_config_float(name: str, default: float) -> float:
    try:
        from core import config
        return float(getattr(config, name, default))
    except Exception:
        return default


def _compute_confidence(winner: ActionStats, total: int) -> float:
    """Combine winner share and success rate into a single 0..1 score.

    The mean is intentionally pessimistic: a 100% share with only 50%
    success rate scores 0.75, not 1.0.  This stops a low-quality "least
    bad" action from looking authoritative.
    """
    if total <= 0:
        return 0.0
    share = winner.count / total
    sr    = winner.success_rate
    return round(min(1.0, max(0.0, (share + sr) / 2.0)), 4)
