"""
backend/modules/ai/orchestrator/observability.py
─────────────────────────────────────────────────
Lightweight, in-process observability for AI provider-chain execution.

Public surface:
  ChainObserver     — accumulates per-attempt data for one AI call
  ProviderAttempt   — dataclass for a single provider attempt record

Used by:
  backend/modules/ai/orchestrator/engine._call_with_chain

Design:
  - In-process only. No DB writes. No external telemetry.
  - Log-based: emits one structured INFO log per AI call via finalize().
  - Timing is captured by the engine around each call_with_resilience() call.
  - Never raises — all methods are safe to call in any order.

Fields captured per AI call:
  chain_requested   : providers in the chain as requested
  final_provider    : which provider produced the reply (None = all failed)
  fallback_used     : True when self._provider (Anthropic) was the final resort
  total_duration_ms : wall time from ChainObserver creation to finalize()
  attempts          : list of ProviderAttempt, one per provider considered

Fields captured per provider attempt:
  name         : canonical provider name
  status       : one of:
                   succeeded              — non-empty reply returned
                   empty_reply            — call returned but reply_text=""
                   failed                 — call_with_resilience returned None
                                            (covers open circuit, timeout, exception)
                   skipped_not_registered — provider not in registry
                   skipped_not_configured — provider.is_configured() returned False
  duration_ms  : wall time of call_with_resilience() call; 0.0 for skipped
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("nahla.ai.orchestrator.observability")


@dataclass
class ProviderAttempt:
    """Record for a single provider considered during chain execution."""
    name:        str
    status:      str    # see module docstring for valid values
    duration_ms: float  # 0.0 for skipped attempts (no call was made)


class ChainObserver:
    """
    Accumulate observability data for one provider-chain execution.

    Usage pattern in engine._call_with_chain:

        observer = ChainObserver(provider_chain.providers)

        # For each provider that is skipped before attempting a call:
        observer.record_skipped(name, "skipped_not_configured")

        # For each provider whose call is attempted:
        t0 = time.monotonic()
        raw = call_with_resilience(...)
        duration_ms = (time.monotonic() - t0) * 1000
        if raw is None:
            observer.record_call(name, duration_ms, "failed")
        elif raw.get("reply_text"):
            observer.record_call(name, duration_ms, "succeeded")
        else:
            observer.record_call(name, duration_ms, "empty_reply")

        # When chain execution is complete:
        observer.finalize(final_provider="anthropic", fallback_used=False)
    """

    def __init__(self, chain_requested: List[str]) -> None:
        self._chain_requested: List[str] = list(chain_requested)
        self._attempts:        List[ProviderAttempt] = []
        self._started_at:      float = time.monotonic()

    # ── Recording helpers ─────────────────────────────────────────────────────

    def record_skipped(self, name: str, reason: str) -> None:
        """
        Record a provider that was skipped before any call was made.

        reason should be one of:
          skipped_not_registered
          skipped_not_configured
        """
        self._attempts.append(ProviderAttempt(name=name, status=reason, duration_ms=0.0))

    def record_call(self, name: str, duration_ms: float, status: str) -> None:
        """
        Record a provider for which call_with_resilience() was invoked.

        status should be one of:
          succeeded   — non-empty reply returned
          empty_reply — call returned but reply_text=""
          failed      — call_with_resilience returned None (circuit / timeout / exc)
        """
        self._attempts.append(
            ProviderAttempt(name=name, status=status, duration_ms=round(duration_ms, 1))
        )

    # ── Finalization ──────────────────────────────────────────────────────────

    def finalize(
        self,
        *,
        final_provider: Optional[str],
        fallback_used: bool,
    ) -> None:
        """
        Emit one structured INFO log summarising the full chain execution.

        Parameters
        ----------
        final_provider : canonical name of the provider that produced the reply,
                         or None if no provider produced a usable reply.
        fallback_used  : True when self._provider (Anthropic) was invoked as
                         the last-resort fallback after all chain providers failed.
        """
        total_ms = round((time.monotonic() - self._started_at) * 1000, 1)

        attempt_summary = [
            f"{a.name}:{a.status}" + (f"({a.duration_ms:.0f}ms)" if a.duration_ms else "")
            for a in self._attempts
        ]

        logger.info(
            "[chain-obs] chain=%s final=%s fallback=%s total_ms=%.1f attempts=[%s]",
            self._chain_requested,
            final_provider or "none",
            fallback_used,
            total_ms,
            ", ".join(attempt_summary),
        )
