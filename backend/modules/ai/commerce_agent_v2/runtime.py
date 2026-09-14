"""Replay-safe OpenAI runtime policy for Commerce Agent V2."""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal

from agents import ModelRetrySettings
from agents.retry import (
    ModelRetryBackoffSettings,
    RetryDecision,
    RetryPolicyContext,
    retry_policies,
)


ExecutionMode = Literal["shadow", "outbound", "eval"]
RetryObserver = Callable[[dict[str, Any]], None]
_SAFE_REASON_RE = re.compile(r"[^a-z0-9_.:-]+")
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def safe_reason_code(value: Any, *, default: str = "transient_provider_error") -> str:
    """Normalize provider advice without retaining raw error text or customer data."""
    normalized = _SAFE_REASON_RE.sub("_", str(value or "").strip().lower()).strip("_:.")
    return (normalized or default)[:80]


def _network_without_timeout(context: RetryPolicyContext) -> bool:
    return bool(context.normalized.is_network_error and not context.normalized.is_timeout)


def build_model_retry_settings(
    *,
    execution_mode: ExecutionMode,
    max_retries: int,
    observer: RetryObserver | None = None,
    retry_model_timeouts: bool = False,
) -> ModelRetrySettings:
    """Build one bounded retry policy; outbound model timeouts are never retried."""
    bounded_retries = min(1, max(0, int(max_retries)))
    transient_policy = retry_policies.any(
        retry_policies.provider_suggested(),
        retry_policies.retry_after(),
        _network_without_timeout,
        retry_policies.http_status(_RETRYABLE_HTTP_STATUSES),
    )

    async def policy(context: RetryPolicyContext) -> RetryDecision | bool:
        if context.normalized.is_timeout and (
            execution_mode == "outbound" or not retry_model_timeouts
        ):
            decision = RetryDecision(retry=False, reason="model_timeout_no_retry")
        elif context.normalized.is_timeout:
            decision = RetryDecision(retry=True, reason="model_timeout_eval_retry")
        else:
            decision = await transient_policy(context)
        if observer is not None:
            status_code = context.normalized.status_code
            observer(
                {
                    "kind": "model_retry_decision",
                    "attempt": int(context.attempt),
                    "retry": bool(decision.retry),
                    "delay_ms": (
                        int(float(decision.delay) * 1000)
                        if decision.delay is not None
                        else None
                    ),
                    "reason": safe_reason_code(decision.reason),
                    "status_code": (
                        int(status_code) if status_code in _RETRYABLE_HTTP_STATUSES else None
                    ),
                    "network_error": bool(context.normalized.is_network_error),
                    "model_timeout": bool(context.normalized.is_timeout),
                    "response_started": bool(context.response_started),
                    "replay_safety": (
                        context.replay_safety
                        if context.replay_safety in {"safe", "unsafe"}
                        else "unknown"
                    ),
                }
            )
        return decision

    return ModelRetrySettings(
        max_retries=bounded_retries,
        backoff=ModelRetryBackoffSettings(
            initial_delay=0.5,
            max_delay=2.0,
            multiplier=2.0,
            jitter=True,
        ),
        policy=policy,
    )


def classify_model_exception(
    exc: Exception,
    *,
    attempted_retry: bool = False,
    retry_reason: str = "",
) -> str:
    """Return a safe stable failure code without persisting provider error bodies."""
    status_code = getattr(exc, "status_code", None)
    if attempted_retry:
        normalized_reason = safe_reason_code(retry_reason)
        if normalized_reason.startswith("provider_"):
            return "provider_suggested_retry_exhausted"
        if status_code in _RETRYABLE_HTTP_STATUSES:
            normalized_reason = f"http_{status_code}"
        elif type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
            normalized_reason = "network_error"
        return f"model_retry_exhausted:{normalized_reason}"
    if status_code == 408:
        return "model_http_408"
    if status_code == 429:
        return "model_http_429"
    if status_code in {500, 502, 503, 504}:
        return "model_http_5xx"
    class_name = type(exc).__name__
    if class_name in {"APIConnectionError", "APITimeoutError"}:
        return "model_network_error"
    return f"unexpected:{safe_reason_code(class_name, default='provider_exception')}"


__all__ = [
    "ExecutionMode",
    "build_model_retry_settings",
    "classify_model_exception",
    "safe_reason_code",
]
