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

Write isolation
───────────────
Telemetry inserts use a dedicated short-lived SQLAlchemy session
(``SessionLocal``) so a failed or oversized write can never poison the
caller's operational transaction.  The optional ``db`` constructor arg is
retained for read aggregations only.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .tenant_isolation import TenantIsolationLayer
from .trace_schema import (
    LearningTier,
    ModelPathTooLongError,
    TraceEvent,
    validate_anonymized,
)

logger = logging.getLogger("nahla.ai.security.cross_merchant_store")


class CrossMerchantLearningStore:
    """Append-only writer for anonymized cross-merchant signals.

    ``db`` is used for read aggregations.  ``record`` always commits through
    its own isolated session so telemetry remains best-effort and cannot
    affect operational state on the caller's session.  The former
    ``commit=False`` mode had no production callers and was removed because a
    short-lived session cannot truthfully return a pending row to its caller.
    """

    def __init__(
        self,
        db: Any,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.db = db
        self._session_factory = session_factory

    # ── Master switch ───────────────────────────────────────────────────

    @staticmethod
    def is_enabled() -> bool:
        try:
            from core.config import CROSS_MERCHANT_LEARNING_ENABLED
            return bool(CROSS_MERCHANT_LEARNING_ENABLED)
        except Exception:
            return False

    def _open_telemetry_session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from database.session import SessionLocal  # noqa: PLC0415

        return SessionLocal()

    # ── Write path ──────────────────────────────────────────────────────

    def record(
        self,
        event: TraceEvent,
    ) -> Optional[int]:
        """Persist ``event`` and return the new row id (or ``None``).

        The method is silent on common failure modes (master switch off,
        importable model missing during a unit test, oversize model_path,
        DB errors).  It only raises when the event itself fails
        ``validate_anonymized`` for reasons other than bounded model_path —
        that is a programming error and must surface immediately.
        """
        if not self.is_enabled():
            logger.debug("[CrossMerchantStore] disabled by config — skipping write")
            return None

        try:
            validated = validate_anonymized(event)
        except ModelPathTooLongError as exc:
            logger.warning(
                "[CrossMerchantStore] model_path_rejected length=%d max=%d",
                exc.actual_length,
                exc.max_length,
            )
            return None

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

        telemetry_db = self._open_telemetry_session()
        try:
            telemetry_db.add(row)
            telemetry_db.commit()
            return getattr(row, "id", None)
        except Exception as exc:
            logger.warning(
                "[CrossMerchantStore] write failed: %s",
                type(exc).__name__,
            )
            try:
                telemetry_db.rollback()
            except Exception:  # noqa: silent-ok — telemetry rollback must not poison caller
                pass
            return None
        finally:
            try:
                telemetry_db.close()
            except Exception:  # noqa: silent-ok — telemetry session close must not poison caller
                pass

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
