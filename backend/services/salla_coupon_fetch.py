"""Structured Salla coupon list fetch + adaptive poll SLA helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def empty_fetch_result(*, failure_class: str, http_status: Optional[int] = None, retry_after: Optional[int] = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "items": [],
        "pages_fetched": 0,
        "items_seen": 0,
        "partial": False,
        "http_status": http_status,
        "failure_class": failure_class,
        "retry_after": retry_after,
    }


def classify_fetch_exception(exc: Exception) -> Dict[str, Optional[Any]]:
    http_status: Optional[int] = None
    retry_after: Optional[int] = None
    failure_class = type(exc).__name__

    response = getattr(exc, "response", None)
    if response is not None:
        http_status = getattr(response, "status_code", None)
        if http_status in (401, 403):
            failure_class = "auth_error"
        elif http_status == 429:
            failure_class = "rate_limited"
            headers = getattr(response, "headers", {}) or {}
            raw_retry = headers.get("Retry-After") or headers.get("retry-after")
            if raw_retry:
                try:
                    retry_after = int(str(raw_retry).strip())
                except (TypeError, ValueError):
                    retry_after = None
        elif http_status is not None and 400 <= int(http_status) < 500:
            failure_class = "client_error"
        elif http_status is not None and int(http_status) >= 500:
            failure_class = "server_error"
    elif exc.__class__.__name__ == "SallaTokenRevokedException":
        failure_class = "needs_reauth"
    else:
        failure_class = "network_error"

    return {
        "http_status": http_status,
        "failure_class": failure_class,
        "retry_after": retry_after,
    }


def poll_interval_seconds_for_catalog(items_seen: int) -> int:
    """Adaptive inbound coupon poll SLA based on last successful catalog size."""
    count = max(0, int(items_seen or 0))
    if count <= 120:
        return 60
    if count <= 600:
        return 300
    return 900


def tenant_poll_due(
    coupon_sync_meta: Optional[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    meta = coupon_sync_meta or {}
    last_poll_raw = meta.get("last_poll_at") or meta.get("last_attempt_at")
    if not last_poll_raw:
        return True
    try:
        last_poll = datetime.fromisoformat(str(last_poll_raw).replace("Z", "+00:00"))
        if last_poll.tzinfo is None:
            last_poll = last_poll.replace(tzinfo=timezone.utc)
    except ValueError:
        return True

    interval = int(meta.get("poll_interval_seconds") or poll_interval_seconds_for_catalog(meta.get("items_seen") or 0))
    elapsed = (now - last_poll.astimezone(timezone.utc)).total_seconds()
    return elapsed >= interval
