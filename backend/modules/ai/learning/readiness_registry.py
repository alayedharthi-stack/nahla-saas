"""
modules/ai/learning/readiness_registry.py
─────────────────────────────────────────
Phase 1.9 — Process-level TTL cache for ``ReadinessVerdict`` lookups.

Why a registry?
───────────────
The ``PolicyBiasLayer`` needs to consult readiness on *every* turn but
recomputing the full ``AdoptionReport`` from ``cross_merchant_signals``
on every turn would dwarf the cost of the entire decision pipeline.
The registry materialises a ``ReadinessSummary`` once and serves it to
every caller until the cached snapshot expires.

Hard rules
──────────
* Read-only w.r.t. the signals store.
* Default-deny: any failure (DB outage, malformed report, missing
  table) returns ``None`` so the bias layer naturally degrades to the
  unbiased path.
* Thread-safe via ``RLock``; the snapshot is immutable while held by a
  caller because lookups only read top-level dicts.
* The registry never decides bias — it only answers "is this bucket
  ready?".  The bias layer owns the policy interpretation.
"""
from __future__ import annotations

import logging
import time
from threading import RLock
from typing import Any, Optional

from .readiness import ReadinessGate, ReadinessSummary, ReadinessVerdict

logger = logging.getLogger("nahla.ai.learning.readiness_registry")


class ReadinessRegistry:
    """TTL-cached ``ReadinessSummary`` provider.

    Construction is cheap; the first ``get`` call lazily loads signals
    and builds the snapshot.  Subsequent calls within ``ttl_seconds``
    return the cached snapshot without touching the database.
    """

    _DEFAULT_TTL = 600.0

    def __init__(
        self,
        db: Any,
        *,
        ttl_seconds: Optional[float] = None,
        gate: Optional[ReadinessGate] = None,
    ) -> None:
        self.db = db
        self._ttl = float(ttl_seconds) if ttl_seconds is not None else self._DEFAULT_TTL
        self._gate = gate
        self._snapshot: Optional[ReadinessSummary] = None
        self._expires_at: float = 0.0
        self._lock = RLock()

    # ── Public API ─────────────────────────────────────────────────────

    def get(self, intent: str, industry: str = "") -> Optional[ReadinessVerdict]:
        """Return the verdict for ``(intent, industry)`` or ``None``.

        Lookup order:

        1. Vertical bucket ``(industry, intent)`` if industry is real
           (not empty / ``"unknown"`` / ``"*"``).
        2. Per-intent global bucket.
        3. ``None`` when the snapshot is empty or the bucket is unknown.
        """
        try:
            self._refresh_if_needed()
        except Exception as exc:
            logger.debug("[ReadinessRegistry] refresh failed: %s", exc)
            return None
        snapshot = self._snapshot
        if snapshot is None:
            return None

        intent_norm   = (intent or "").strip().lower()
        industry_norm = (industry or "").strip().lower()
        if not intent_norm:
            return None

        if industry_norm and industry_norm not in {"unknown", "*"}:
            verdict = snapshot.by_industry_intent.get((industry_norm, intent_norm))
            if verdict is not None:
                return verdict

        return snapshot.by_intent.get(intent_norm)

    def invalidate(self) -> None:
        """Force the next ``get`` to rebuild the snapshot."""
        with self._lock:
            self._snapshot = None
            self._expires_at = 0.0

    @property
    def snapshot(self) -> Optional[ReadinessSummary]:
        return self._snapshot

    # ── Internal ───────────────────────────────────────────────────────

    def _refresh_if_needed(self) -> None:
        now = time.monotonic()
        if self._snapshot is not None and self._expires_at > now:
            return

        with self._lock:
            if self._snapshot is not None and self._expires_at > now:
                return
            try:
                from .adoption import load_adoption_report
                report = load_adoption_report(self.db)
                gate = self._gate or ReadinessGate.from_config()
                self._snapshot = gate.evaluate_report(report)
            except Exception as exc:
                logger.debug("[ReadinessRegistry] build failed: %s", exc)
                # Keep an empty snapshot so future calls don't re-attempt
                # the failing build for every turn within the TTL.
                if self._snapshot is None:
                    self._snapshot = ReadinessSummary()
            self._expires_at = now + self._ttl
