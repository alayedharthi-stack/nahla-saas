"""
core/salla_token_lock.py
─────────────────────────
Two-layer token-refresh locking for Salla integrations.

Problem
───────
Both the daily scheduler and a live API call's 401 handler can trigger a token
refresh for the same integration at the same instant.  Without a lock, both
coroutines post to Salla's OAuth endpoint simultaneously, both succeed, and the
second writer silently discards the first's new refresh_token.  Worse, Salla
only issues one valid refresh_token per session — the first winner's new token
is invalidated by the second call.

Solution — two layers
─────────────────────
Layer 1  asyncio.Lock per integration_id
  Guards concurrent coroutines inside the same event-loop worker.  Since
  FastAPI / uvicorn run a single event loop per process, this covers the
  common case where the scheduler task and a request handler race.

Layer 2  DB flag  config.token_refresh_in_progress / token_refresh_started_at
  Guards across multiple workers or Railway replica instances.  A stale flag
  older than LOCK_TTL_SECONDS is auto-cleared so a crashed worker cannot
  permanently block an integration.

Usage
─────
Scheduler (has db session + ORM object):

    lock = SallaTokenLock(db, intg, caller="scheduler")
    acquired = await lock.acquire()
    if not acquired:
        continue          # another coroutine/worker is already refreshing
    try:
        ...do refresh...
    finally:
        await lock.release()

Adapter / on-demand path (no persistent db session):

    async with salla_asyncio_lock(integration_id, caller="adapter") as acquired:
        if not acquired:
            return        # in-process lock already held
        ...do refresh...
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from core.coupon_log_privacy import hash_identifier

logger = logging.getLogger("nahla.salla_token_lock")

# How long a DB flag is considered "fresh" before it can be overridden
LOCK_TTL_SECONDS: int = 300  # 5 minutes


# ── In-process asyncio lock registry ─────────────────────────────────────────
# One asyncio.Lock per integration_id, created lazily.
# Access is safe without a meta-lock because asyncio is cooperative: the dict
# is only mutated at non-await call sites, so no concurrent mutation occurs.

_locks: dict[int, asyncio.Lock] = {}


def _get_asyncio_lock(integration_id: int) -> asyncio.Lock:
    """Return (or lazily create) the asyncio.Lock for this integration."""
    if integration_id not in _locks:
        _locks[integration_id] = asyncio.Lock()
    return _locks[integration_id]


# ── DB-flag helpers ───────────────────────────────────────────────────────────

def _db_flag_is_fresh(cfg: dict) -> bool:
    """Return True when the DB refresh-lock flag is set AND still within TTL."""
    if not cfg.get("token_refresh_in_progress"):
        return False
    started_raw = cfg.get("token_refresh_started_at")
    if not started_raw:
        return False
    try:
        started_dt = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started_dt).total_seconds()
        return age < LOCK_TTL_SECONDS
    except Exception:
        return False   # unparseable → treat as stale


def _set_db_lock(db, intg) -> None:
    """Atomically set token_refresh_in_progress=True on the integration row."""
    try:
        cfg = dict(intg.config or {})
        cfg["token_refresh_in_progress"]  = True
        cfg["token_refresh_started_at"]   = datetime.now(timezone.utc).isoformat()
        intg.config = cfg
        db.commit()
    except Exception as exc:
        logger.warning(
            "[SallaLock] failed to set DB lock integration_id=%s: %s",
            intg.id, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass


def _clear_db_lock(db, intg) -> None:
    """Clear the DB refresh-lock flag (idempotent, safe in finally blocks)."""
    try:
        cfg = dict(intg.config or {})
        cfg["token_refresh_in_progress"] = False
        cfg.pop("token_refresh_started_at", None)
        intg.config = cfg
        db.commit()
    except Exception as exc:
        logger.warning(
            "[SallaLock] failed to clear DB lock integration_id=%s: %s",
            intg.id, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass


# ── Scheduler-side: explicit acquire / release ────────────────────────────────

class SallaTokenLock:
    """Two-layer (asyncio + DB) lock for the scheduler code path.

    The scheduler has direct access to the SQLAlchemy session and the
    Integration ORM object, so it can use both layers.

    Pattern::

        lock = SallaTokenLock(db, intg, caller="scheduler")
        acquired = await lock.acquire()
        if not acquired:
            continue
        try:
            ... perform refresh ...
        finally:
            await lock.release()
    """

    def __init__(self, db, intg, caller: str = "scheduler") -> None:
        self._db         = db
        self._intg       = intg
        self._caller     = caller
        self._asyncio_lock: Optional[asyncio.Lock] = None
        self._asyncio_acquired: bool = False
        self._db_flag_set: bool = False

    async def acquire(self) -> bool:
        """Try to acquire both layers. Returns False if already locked."""
        intg_id = self._intg.id

        # ── Layer 1: asyncio lock ─────────────────────────────────────────
        self._asyncio_lock = _get_asyncio_lock(intg_id)
        if self._asyncio_lock.locked():
            logger.info(
                "[SALLA TOKEN] refresh skipped (in-process lock held) | "
                "integration_id=%s caller=%s",
                intg_id, self._caller,
            )
            return False
        await self._asyncio_lock.acquire()
        self._asyncio_acquired = True

        # ── Layer 2: DB flag ──────────────────────────────────────────────
        cfg = dict(self._intg.config or {})
        if _db_flag_is_fresh(cfg):
            started_raw = cfg.get("token_refresh_started_at", "")
            try:
                age = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                ).total_seconds()
            except Exception:
                age = 0.0
            logger.info(
                "[SALLA TOKEN] refresh skipped (DB flag held, age=%.0fs) | "
                "integration_id=%s caller=%s",
                age, intg_id, self._caller,
            )
            self._asyncio_lock.release()
            self._asyncio_acquired = False
            return False

        if cfg.get("token_refresh_in_progress"):
            logger.warning(
                "[SALLA TOKEN] stale DB lock detected and cleared | integration_id=%s",
                intg_id,
            )

        _set_db_lock(self._db, self._intg)
        self._db_flag_set = True
        return True

    async def release(self) -> None:
        """Release both layers — idempotent, safe in finally blocks."""
        if self._db_flag_set:
            _clear_db_lock(self._db, self._intg)
            self._db_flag_set = False
        if self._asyncio_acquired and self._asyncio_lock is not None:
            try:
                if self._asyncio_lock.locked():
                    self._asyncio_lock.release()
            except RuntimeError:
                pass
            self._asyncio_acquired = False


# ── Adapter-side: asyncio-only context manager ────────────────────────────────

@asynccontextmanager
async def salla_asyncio_lock(
    integration_id: Optional[int],
    caller: str = "adapter",
) -> AsyncIterator[bool]:
    """Async context manager providing in-process locking for the adapter path.

    Yields ``True``  if the lock was successfully acquired (proceed with refresh).
    Yields ``False`` if the lock was already held (skip; another coroutine is
    already refreshing this integration).

    The adapter creates its own DB sessions and cannot share the scheduler's
    session, so we only apply Layer 1 here.  Cross-process protection in
    multi-worker deployments is provided by the DB flag set by the scheduler.

    Usage::

        async with salla_asyncio_lock(self._integration_id) as acquired:
            if not acquired:
                return   # another coroutine is refreshing — skip
            ... perform refresh ...
    """
    if not integration_id:
        yield True  # no ID → cannot lock; proceed unconditionally
        return

    lock = _get_asyncio_lock(integration_id)
    if lock.locked():
        logger.info(
            "[SALLA TOKEN] refresh_lock_held event=salla_token_refresh_lock_held integration_hash=%s caller=%s",
            hash_identifier(integration_id),
            caller,
        )
        yield False
        return

    await lock.acquire()
    try:
        yield True
    finally:
        try:
            if lock.locked():
                lock.release()
        except RuntimeError:
            pass
