"""
core/redis_client.py
────────────────────
Lazy, process-wide Redis client used by rate limiting, JWT revocation, and
webhook replay protection (Phase 1B+). The client is intentionally
optional — when ``REDIS_URL`` is empty the helpers in this module return
``None`` and callers fall back to an in-process implementation. This keeps
local dev frictionless while letting production opt into a shared,
multi-worker store.

Public API
──────────
* ``get_redis()``        — sync ``redis.Redis`` instance or ``None``.
* ``redis_available()``  — quick boolean probe (used by routers / health).

Notes
─────
* We use the synchronous ``redis-py`` client because the auth and webhook
  hot paths run in a thread pool (sync ``def`` routes) and a brief blocking
  Redis call is cheaper than the async overhead.
* Connection pool is shared per-process via ``redis.Redis.from_url``. The
  pool transparently reconnects on transient network errors.
* All Redis errors are swallowed by the higher-level helpers — Redis
  outages MUST NOT break login, only weaken rate-limit precision.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("nahla.redis")

_REDIS_CLIENT = None  # type: ignore[assignment]
_REDIS_PROBED = False
_REDIS_REASON = ""


def _redis_url() -> str:
    return (os.environ.get("REDIS_URL") or "").strip()


def get_redis():
    """
    Return a process-wide ``redis.Redis`` instance, or ``None`` when
    ``REDIS_URL`` is not configured / the import fails.

    The first call probes the import path and the URL; subsequent calls
    reuse the cached client. We never raise — callers MUST treat the
    return value as best-effort.
    """
    global _REDIS_CLIENT, _REDIS_PROBED, _REDIS_REASON  # noqa: PLW0603

    if _REDIS_PROBED:
        return _REDIS_CLIENT

    _REDIS_PROBED = True
    url = _redis_url()
    if not url:
        _REDIS_REASON = "REDIS_URL not set"
        logger.info(
            "[redis] disabled — REDIS_URL is empty. "
            "Rate limiting falls back to in-process counters "
            "(not shared across workers)."
        )
        return None

    try:
        import redis  # noqa: PLC0415
    except ImportError as exc:
        _REDIS_REASON = f"redis-py not installed: {exc}"
        logger.warning("[redis] %s — falling back to in-process counters", _REDIS_REASON)
        return None

    try:
        _REDIS_CLIENT = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            retry_on_timeout=False,
            health_check_interval=30,
        )
        _REDIS_CLIENT.ping()
        logger.info("[redis] connected at %s", _mask_url(url))
    except Exception as exc:  # noqa: BLE001
        _REDIS_REASON = f"connect failed: {type(exc).__name__}: {exc}"
        logger.warning(
            "[redis] could not reach %s — falling back to in-process counters: %s",
            _mask_url(url), exc,
        )
        _REDIS_CLIENT = None
    return _REDIS_CLIENT



def redis_supports_getdel() -> dict:
    """Probe whether the configured Redis client/server supports atomic GETDEL.

    Returns a safe status dict — never includes REDIS_URL or credentials.
    """
    r = get_redis()
    if r is None:
        return {
            "configured": False,
            "getdel_supported": False,
            "reason": _REDIS_REASON or "not_configured",
        }

    if not hasattr(r, "getdel"):
        return {
            "configured": True,
            "getdel_supported": False,
            "reason": "client_missing_getdel",
        }

    try:
        info = r.execute_command("COMMAND", "INFO", "GETDEL")
        supported = bool(info)
        return {
            "configured": True,
            "getdel_supported": supported,
            "reason": "ok" if supported else "server_missing_getdel",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "configured": True,
            "getdel_supported": False,
            "reason": f"probe_failed:{type(exc).__name__}",
        }

def redis_available() -> bool:
    """True iff a live Redis connection is usable right now."""
    return get_redis() is not None


def _mask_url(url: str) -> str:
    """Return ``redis://user:***@host:port/db`` for safe logging."""
    try:
        if "@" not in url:
            return url
        scheme, rest = url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://***@{host}"
    except Exception:
        return "redis://***"
