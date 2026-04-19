"""
modules/ai/security/cross_merchant_store.py
───────────────────────────────────────────
Single safe writer for the cross-merchant learning signal store.

Read paths can issue plain SQL aggregations on ``cross_merchant_signals``;
they never need this class.  Writes, however, MUST go through
``CrossMerchantLearningStore.record`` which:

1. Confirms the cross-merchant learning master switch is on.
2. Runs ``validate_anonymized`` which raises if the event still carries
   any raw / identifying field.
3. Verifies the target ORM model is intentionally non-tenant-scoped via
   ``TenantIsolationLayer.is_cross_tenant_safe``.
4. Persists with a small, fixed column set — no JSONB free-for-all.

The class is intentionally tiny.  It is not the place to compute
recommendations or apply business logic — it only enforces "anonymized
in, anonymized out".
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .tenant_isolation import TenantIsolationLayer
from .trace_schema import (
    LearningTier,
    TraceEvent,
    validate_anonymized,
)

logger = logging.getLogger("nahla.ai.security.cross_merchant_store")


class CrossMerchantLearningStore:
    """Append-only writer for anonymized cross-merchant signals.

    The store wraps a SQLAlchemy ``db`` session but does not own it; the
    caller is responsible for the surrounding lifecycle (commit / close)
    when ``commit=False`` is used.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # ── Master switch ───────────────────────────────────────────────────

    @staticmethod
    def is_enabled() -> bool:
        try:
            from core.config import CROSS_MERCHANT_LEARNING_ENABLED
            return bool(CROSS_MERCHANT_LEARNING_ENABLED)
        except Exception:
            return False

    # ── Write path ──────────────────────────────────────────────────────

    def record(
        self,
        event: TraceEvent,
        *,
        commit: bool = True,
    ) -> Optional[int]:
        """Persist ``event`` and return the new row id (or ``None``).

        The method is silent on common failure modes (master switch off,
        importable model missing during a unit test).  It only raises when
        the event itself fails ``validate_anonymized`` — that is a
        programming error and must surface immediately.
        """
        if not self.is_enabled():
            logger.debug("[CrossMerchantStore] disabled by config — skipping write")
            return None

        validated = validate_anonymized(event)

        try:
            from database.models import CrossMerchantSignal
        except Exception as exc:
            logger.debug(
                "[CrossMerchantStore] CrossMerchantSignal model not available: %s",
                exc,
            )
            return None

        if not TenantIsolationLayer.is_cross_tenant_safe(CrossMerchantSignal):
            # Defensive double-check: the model was somehow re-classified.
            # We refuse the write rather than risk leaking a tenant_id.
            logger.error(
                "[CrossMerchantStore] CrossMerchantSignal is not marked "
                "cross-tenant-safe — refusing to write"
            )
            return None

        row = CrossMerchantSignal(
            tenant_hash  = validated.tenant_hash,
            industry     = validated.industry,
            intent       = validated.intent,
            action       = validated.action,
            ui_mode      = validated.ui_mode,
            outcome      = validated.outcome,
            value_bucket = validated.value_bucket,
            turn_index   = validated.turn_index,
            model_path   = validated.model_path,
            latency_ms   = validated.latency_ms,
            tier         = validated.tier,
            extra        = dict(validated.extra or {}),
        )
        try:
            self.db.add(row)
            if commit:
                self.db.commit()
            return getattr(row, "id", None)
        except Exception as exc:
            logger.warning("[CrossMerchantStore] write failed: %s", exc)
            try:
                if commit:
                    self.db.rollback()
            except Exception:
                pass
            return None

    # ── Read paths (small built-ins for tests / dashboards) ─────────────

    def aggregate_global_action_distribution(
        self,
        action: str,
        *,
        limit_outcomes: int = 8,
    ) -> List[Dict[str, Any]]:
        """Return a coarse outcome distribution for a given action across
        every merchant.  Used by the global-policy learner.
        """
        from sqlalchemy import func
        try:
            from database.models import CrossMerchantSignal
        except Exception:
            return []

        rows = (
            self.db.query(
                CrossMerchantSignal.outcome,
                func.count(CrossMerchantSignal.id).label("n"),
            )
            .filter(
                CrossMerchantSignal.action == (action or "").lower(),
                CrossMerchantSignal.tier == LearningTier.GLOBAL,
            )
            .group_by(CrossMerchantSignal.outcome)
            .order_by(func.count(CrossMerchantSignal.id).desc())
            .limit(limit_outcomes)
            .all()
        )
        return [{"outcome": r[0], "count": int(r[1])} for r in rows]

    def aggregate_vertical_outcomes(
        self,
        industry: str,
        *,
        limit_actions: int = 16,
    ) -> List[Dict[str, Any]]:
        """Return per-action outcome counts inside a single vertical.

        Used by the vertical-policy learner.  No raw merchant identifier
        is exposed in the result.
        """
        from sqlalchemy import func
        try:
            from database.models import CrossMerchantSignal
        except Exception:
            return []

        rows = (
            self.db.query(
                CrossMerchantSignal.action,
                CrossMerchantSignal.outcome,
                func.count(CrossMerchantSignal.id).label("n"),
            )
            .filter(
                CrossMerchantSignal.industry == (industry or "").lower(),
                CrossMerchantSignal.tier.in_([LearningTier.VERTICAL, LearningTier.GLOBAL]),
            )
            .group_by(CrossMerchantSignal.action, CrossMerchantSignal.outcome)
            .order_by(func.count(CrossMerchantSignal.id).desc())
            .limit(limit_actions)
            .all()
        )
        return [
            {"action": r[0], "outcome": r[1], "count": int(r[2])}
            for r in rows
        ]
