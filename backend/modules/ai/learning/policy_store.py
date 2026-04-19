"""
modules/ai/learning/policy_store.py
───────────────────────────────────
Runtime read path for ``learned_sales_policies``.

The store is intentionally tiny:
* one method (``lookup``) returning at most one ``PolicyHint`` per call;
* a small process-local TTL cache so the brain pipeline does not query
  the table on every turn;
* a defensive fallback chain ``vertical → global → None`` so a hint
  exists whenever any tier has data, but never crosses tiers it
  shouldn't (a vertical hint for industry X is never returned for
  industry Y).

The class never writes; persistence is owned exclusively by
``PolicyLearner``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.ai.learning.policy_store")


@dataclass(frozen=True)
class PolicyHint:
    """Read-only view of a single ``LearnedSalesPolicy`` row."""
    scope: str
    industry: str
    intent: str
    recommended_action: str
    recommended_ui: str
    confidence: float
    sample_size: int
    extra: Dict[str, Any] = field(default_factory=dict)


class LearnedPolicyStore:
    """Thread-safe TTL cache around ``learned_sales_policies``.

    The cache key is ``(intent, industry_or_global)`` and entries expire
    after ``ttl_seconds`` (default 300).  A cache miss falls through to a
    single point-lookup; on success the hint is also written back under
    a plain ``intent`` key so the global tier can satisfy other
    industries without re-querying.
    """

    _DEFAULT_TTL = 300.0

    def __init__(self, db: Any, *, ttl_seconds: Optional[float] = None) -> None:
        self.db = db
        self._ttl = float(ttl_seconds) if ttl_seconds is not None else self._DEFAULT_TTL
        self._cache: Dict[Tuple[str, str], Tuple[Optional[PolicyHint], float]] = {}
        self._lock = RLock()

    # ── Public API ─────────────────────────────────────────────────────

    @staticmethod
    def is_enabled() -> bool:
        try:
            from core.config import LEARNED_POLICY_ENABLED
            return bool(LEARNED_POLICY_ENABLED)
        except Exception:
            return False

    def lookup(self, intent: str, *, industry: str = "") -> Optional[PolicyHint]:
        """Return the most specific hint that exists for ``(intent, industry)``.

        Lookup order:

        1. ``(intent, industry)`` if ``industry`` is provided and resolves
           to a vertical row.
        2. ``(intent, '*')`` global tier as a fallback.

        Returns ``None`` when the master switch is off, when the table is
        empty for this intent, or when any DB error occurs (logged at
        debug level).  The store NEVER raises into the caller because
        the override layer must be a pure best-effort augmentation.
        """
        if not self.is_enabled():
            return None

        intent_norm   = (intent or "").strip().lower() or "unknown"
        industry_norm = (industry or "").strip().lower()

        if industry_norm and industry_norm not in {"unknown", "*"}:
            hit = self._lookup_one(intent_norm, industry_norm)
            if hit is not None:
                return hit

        return self._lookup_one(intent_norm, "*")

    def invalidate(self) -> None:
        """Drop every cached entry — call after ``PolicyLearner.run``."""
        with self._lock:
            self._cache.clear()

    # ── Internal ───────────────────────────────────────────────────────

    def _lookup_one(self, intent: str, industry: str) -> Optional[PolicyHint]:
        key = (intent, industry)
        now = time.monotonic()

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                hint, expires_at = cached
                if expires_at > now:
                    return hint

        hint = self._fetch(intent, industry)

        with self._lock:
            self._cache[key] = (hint, now + self._ttl)
        return hint

    def _fetch(self, intent: str, industry: str) -> Optional[PolicyHint]:
        try:
            from database.models import LearnedSalesPolicy
        except Exception as exc:
            logger.debug("[LearnedPolicyStore] model unavailable: %s", exc)
            return None

        try:
            row = (
                self.db.query(LearnedSalesPolicy)
                .filter_by(intent=intent, industry=industry)
                .first()
            )
        except Exception as exc:
            logger.debug("[LearnedPolicyStore] query failed: %s", exc)
            return None

        if row is None:
            return None

        try:
            return PolicyHint(
                scope              = str(row.scope or "global"),
                industry           = str(row.industry or "*"),
                intent             = str(row.intent or "unknown"),
                recommended_action = str(row.recommended_action or "unknown"),
                recommended_ui     = str(row.recommended_ui or "unknown"),
                confidence         = float(row.confidence or 0.0),
                sample_size        = int(row.sample_size or 0),
                extra              = dict(row.extra or {}),
            )
        except Exception as exc:
            logger.debug("[LearnedPolicyStore] row coerce failed: %s", exc)
            return None
