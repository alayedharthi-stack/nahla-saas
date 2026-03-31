"""
RateLimiter
───────────
In-memory sliding window rate limiter for single-process deployments.
For multi-worker production: replace _store with Redis ZADD/ZCOUNT.

Usage:
    allowed = check_rate_limit("msg:1:966501234567", max_count=20, window_seconds=60)
    if not allowed:
        raise HTTPException(429, "Too many requests")
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, List

# Sliding window event timestamps per key
_store: Dict[str, List[float]] = defaultdict(list)

# Controls periodic cleanup to prevent unbounded memory growth
_last_cleanup: float = 0.0
_CLEANUP_INTERVAL = 300  # seconds


def check_rate_limit(key: str, max_count: int, window_seconds: int) -> bool:
    """
    Returns True if within the rate limit (request allowed).
    Returns False if the limit is exceeded (request should be rejected).
    """
    global _last_cleanup
    now = time.monotonic()
    cutoff = now - window_seconds

    # Prune expired entries for this key
    _store[key] = [t for t in _store[key] if t > cutoff]

    if len(_store[key]) >= max_count:
        return False

    _store[key].append(now)

    # Periodic full cleanup of stale keys
    if now - _last_cleanup > _CLEANUP_INTERVAL:
        _cleanup()
        _last_cleanup = now

    return True


def _cleanup() -> None:
    """Remove keys with no recent events."""
    now = time.monotonic()
    stale = [k for k, v in _store.items() if not v or now - max(v) > 3600]
    for k in stale:
        del _store[k]
