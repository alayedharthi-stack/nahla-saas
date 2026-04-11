"""
backend/modules/ai/orchestrator/providers/resilience.py
────────────────────────────────────────────────────────
Process-local circuit-breaker + timeout protection for AI provider calls.

Public surface:
  call_with_resilience(provider_name, call_fn, timeout)
    → Dict[str, Any] on success, or None on circuit-open / timeout / exception

ProviderCircuitBreaker (internal, one instance per provider name):
  is_open()        → bool
  record_success() → None
  record_failure() → None

Design decisions:
  - Process-local only. No Redis, DB, or external state.
  - Thread-safe via threading.Lock (one lock per breaker).
  - Timeout is enforced via concurrent.futures.ThreadPoolExecutor.
    The thread may continue running until the provider's own httpx timeout
    fires (~25s) after a resilience timeout fires, but this is acceptable
    for AI calls where threads do not hold dangerous locks.
  - The circuit opens after N consecutive call exceptions or timeouts.
    An empty reply_text is NOT counted as a failure — it means the
    provider is reachable but chose not to reply, which is not a
    connectivity failure.

Configuration (env vars):
  AI_PROVIDER_TIMEOUT      : outer call timeout in seconds (default: 20.0)
  AI_PROVIDER_CB_THRESHOLD : failures before circuit opens (default: 3)
  AI_PROVIDER_CB_COOLDOWN  : seconds circuit stays open (default: 60.0)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("nahla.ai.orchestrator.resilience")

# ── Configuration ──────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT: float      = float(os.environ.get("AI_PROVIDER_TIMEOUT",      "20.0"))
_FAILURE_THRESHOLD: int     = int  (os.environ.get("AI_PROVIDER_CB_THRESHOLD", "3"))
_COOLDOWN_SECONDS: float    = float(os.environ.get("AI_PROVIDER_CB_COOLDOWN",  "60.0"))


# ── Circuit breaker ────────────────────────────────────────────────────────────

class ProviderCircuitBreaker:
    """
    Lightweight per-provider circuit breaker (three-state: CLOSED, OPEN, HALF-OPEN).

    CLOSED    — normal operation; calls pass through.
    OPEN      — circuit tripped after failure_threshold consecutive failures;
                calls are skipped until cooldown elapses.
    HALF-OPEN — cooldown elapsed; the next call is allowed as a trial.
                If it succeeds → CLOSED; if it fails → OPEN again.

    Thread-safe.
    """

    def __init__(
        self,
        provider_name: str,
        failure_threshold: int   = _FAILURE_THRESHOLD,
        cooldown_seconds: float  = _COOLDOWN_SECONDS,
    ) -> None:
        self.provider_name     = provider_name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds  = cooldown_seconds
        self._failures: int    = 0
        self._opened_at: Optional[float] = None
        self._lock             = threading.Lock()

    def is_open(self) -> bool:
        """Return True if the circuit is open and the call should be skipped."""
        with self._lock:
            if self._opened_at is None:
                return False
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.cooldown_seconds:
                # Cooldown elapsed → enter HALF-OPEN: allow one trial
                return False
            return True

    def record_success(self) -> None:
        """Reset failure count and close the circuit."""
        with self._lock:
            self._failures  = 0
            self._opened_at = None

    def record_failure(self) -> None:
        """Increment failure count; open the circuit if threshold is reached."""
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                logger.warning(
                    "[resilience] %s circuit opened after %d consecutive failures "
                    "(cooldown: %.0fs)",
                    self.provider_name, self._failures, self.cooldown_seconds,
                )


# ── Singleton breaker registry ─────────────────────────────────────────────────
_BREAKERS: Dict[str, ProviderCircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_circuit_breaker(provider_name: str) -> ProviderCircuitBreaker:
    """Return the shared ProviderCircuitBreaker for this provider name."""
    with _REGISTRY_LOCK:
        if provider_name not in _BREAKERS:
            _BREAKERS[provider_name] = ProviderCircuitBreaker(provider_name)
        return _BREAKERS[provider_name]


# ── Public entry point ─────────────────────────────────────────────────────────

def call_with_resilience(
    provider_name: str,
    call_fn: Callable[[], Dict[str, Any]],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    """
    Execute call_fn under circuit-breaker + timeout protection.

    Parameters
    ----------
    provider_name : canonical provider name (used to look up its breaker)
    call_fn       : zero-argument callable that invokes provider.call(...)
    timeout       : max seconds to wait for call_fn to complete

    Returns
    -------
    The dict returned by call_fn on success.
    None when:
      - the circuit is open (cooldown active) — call is skipped entirely
      - call_fn raises an exception         — failure recorded, None returned
      - call_fn exceeds timeout             — failure recorded, None returned

    Circuit state update:
      SUCCESS: call_fn returns any dict within timeout (even empty reply_text)
               → failure counter reset, circuit stays / returns CLOSED
      FAILURE: exception OR timeout
               → failure counter incremented; circuit opens at threshold
    """
    breaker = get_circuit_breaker(provider_name)

    if breaker.is_open():
        logger.info(
            "[resilience] %s circuit open — skipping provider (cooldown active)",
            provider_name,
        )
        return None

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future: Future[Dict[str, Any]] = executor.submit(call_fn)
            result = future.result(timeout=timeout)

        breaker.record_success()
        return result

    except _FuturesTimeout:
        logger.warning(
            "[resilience] %s timed out after %.1fs — recording failure",
            provider_name, timeout,
        )
        breaker.record_failure()
        return None

    except Exception as exc:
        logger.warning(
            "[resilience] %s call raised %r — recording failure",
            provider_name, exc,
        )
        breaker.record_failure()
        return None
