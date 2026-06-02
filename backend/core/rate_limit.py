"""
core/rate_limit.py
──────────────────
High-level rate-limit helpers used by sensitive routers (login, password
reset, JWT revocation lookup). Built on top of ``core.redis_client`` with
an in-process fallback so a missing Redis never breaks the surface — it
only weakens cross-worker precision.

Algorithm
─────────
Fixed window via ``INCR`` + ``EXPIRE NX``:

    key   = rl:<bucket>:<identifier>
    count = INCR key
    if count == 1:
        EXPIRE key window_seconds NX

Pros: O(1), atomic enough for our threat model (credential stuffing /
brute force / spam), no Lua needed, predictable memory footprint.
Cons: window edges allow up to 2× the cap in a worst-case adversary
burst — which is fine: an attacker doing 10/min on a 5/15min cap is
still trivially blocked at the second window.

When Redis is absent we use a per-process sliding window
(``_local_check``) so dev / single-worker setups still get useful
protection. The shared Redis path is the production default.

Public API
──────────
* ``check_rate_limit_or_429(bucket, key, max_count, window_s)`` — raises
  ``HTTPException(429)`` when the limit is exceeded. Always returns
  cleanly when allowed. Login buckets (``login_ip`` / ``login_email``)
  audit under ``login_rate_limited``; all other buckets use
  ``rate_limit_exceeded``.
* ``hash_email(email)`` — opaque, stable identifier for per-email keys
  that does NOT leak the address into Redis (audit-friendly).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import defaultdict
from typing import Dict, List, Tuple

from fastapi import HTTPException

from core.audit import audit
from core.redis_client import get_redis

logger = logging.getLogger("nahla.rate_limit")

# Salt for the per-email rate-limit key. Keys are stored in Redis (shared
# infrastructure), so we hash the email + a stable salt to keep PII out
# of the rate-limit namespace. JWT_SECRET is reused as the salt so we
# don't introduce a new secret to manage.
_HASH_SALT_ENV = "JWT_SECRET"

# Buckets that represent the /auth/login* surface — Phase 1A spec asks
# for a dedicated ``login_rate_limited`` audit event (IP + email hash).
_LOGIN_RATE_BUCKETS = frozenset({"login_ip", "login_email"})


# ── In-process fallback (sliding window) ───────────────────────────────────────
_local_store: Dict[str, List[float]] = defaultdict(list)
_local_last_cleanup = 0.0
_LOCAL_CLEANUP_INTERVAL = 300.0


def _local_check(key: str, max_count: int, window_seconds: int) -> Tuple[bool, int]:
    """In-process sliding window. Returns ``(allowed, retry_after_seconds)``."""
    global _local_last_cleanup  # noqa: PLW0603
    now = time.monotonic()
    cutoff = now - window_seconds
    bucket = _local_store[key] = [t for t in _local_store[key] if t > cutoff]
    if len(bucket) >= max_count:
        retry_after = max(1, int(round(bucket[0] + window_seconds - now)))
        return False, retry_after
    bucket.append(now)
    if now - _local_last_cleanup > _LOCAL_CLEANUP_INTERVAL:
        stale = [k for k, v in _local_store.items() if not v or now - max(v) > 3600]
        for k in stale:
            del _local_store[k]
        _local_last_cleanup = now
    return True, 0


# ── Redis-backed window (production) ───────────────────────────────────────────
def _redis_check(key: str, max_count: int, window_seconds: int) -> Tuple[bool, int]:
    """Fixed-window via INCR. Returns ``(allowed, retry_after_seconds)``."""
    r = get_redis()
    if r is None:
        return _local_check(key, max_count, window_seconds)
    try:
        pipe = r.pipeline()
        pipe.incr(key, 1)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        count = int(count)
        ttl = int(ttl)
        # First hit in this window: install the TTL. We use the explicit
        # ``ex`` form because some Redis versions don't accept ``EXPIRE
        # ... NX``. Race-safe enough — at worst we re-set the TTL on
        # concurrent first hits, which keeps the window honest.
        if ttl < 0:
            r.expire(key, window_seconds)
            ttl = window_seconds
        if count > max_count:
            retry_after = max(1, ttl)
            return False, retry_after
        return True, 0
    except Exception as exc:  # noqa: BLE001 — never let RL break the request
        logger.warning("[rate_limit] redis error on key=%s: %s — falling back to local", key, exc)
        return _local_check(key, max_count, window_seconds)


# ── Public helpers ─────────────────────────────────────────────────────────────
def hash_email(email: str) -> str:
    """Stable, opaque per-email identifier. 16 hex chars (~8 bytes)."""
    salt = os.environ.get(_HASH_SALT_ENV, "") or "nahla-rl-fallback-salt"
    h = hmac.new(salt.encode("utf-8"), (email or "").strip().lower().encode("utf-8"), hashlib.sha256)
    return h.hexdigest()[:16]


def check_rate_limit_or_429(
    *,
    bucket: str,
    key: str,
    max_count: int,
    window_seconds: int,
    audit_metadata: dict | None = None,
) -> None:
    """
    Raise ``HTTPException(429)`` if the per-key counter exceeds ``max_count``
    within the rolling ``window_seconds``. Always returns cleanly otherwise.

    ``bucket`` is a short label used for the Redis key namespace AND the
    audit event payload (e.g. ``"login_ip"``, ``"login_email"``).
    """
    full_key = f"rl:{bucket}:{key}"
    allowed, retry_after = _redis_check(full_key, max_count, window_seconds)
    if allowed:
        return

    metadata = dict(audit_metadata or {})
    metadata.update({
        "bucket": bucket,
        "max_count": max_count,
        "window_seconds": window_seconds,
        "retry_after": retry_after,
    })
    try:
        event = "login_rate_limited" if bucket in _LOGIN_RATE_BUCKETS else "rate_limit_exceeded"
        audit(event, **metadata)
    except Exception:  # noqa: silent-ok — audit emission is best-effort; the 429 below is the user-visible signal
        pass

    raise HTTPException(
        status_code=429,
        detail=(
            "تم تجاوز الحد المسموح به من المحاولات. "
            f"حاول مرة أخرى بعد {retry_after} ثانية."
        ),
        headers={"Retry-After": str(retry_after)},
    )
