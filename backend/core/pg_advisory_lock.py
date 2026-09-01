"""Connection-safe PostgreSQL advisory locks.

PostgreSQL session advisory locks are bound to the physical connection that
acquired them. SQLAlchemy `Session` operations may commit or rollback and
return the underlying connection to the pool while the lock remains held on
the discarded connection. `DedicatedAdvisoryLock` pins a dedicated physical
connection for the full lock lifetime and invalidates it when unlock fails.

On non-PostgreSQL backends (e.g. SQLite unit tests) a process-local
``threading.Lock`` provides equivalent mutual exclusion without opening a
dedicated DB connection.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Tuple, Union

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

logger = logging.getLogger('nahla.pg_advisory_lock')

_ThreadLockKey = Union[Tuple[str, int], Tuple[str, int, int]]
_thread_lock_registry: Dict[_ThreadLockKey, threading.Lock] = {}
_thread_lock_registry_guard = threading.Lock()


def _thread_lock_identity(
    *,
    key: Optional[int],
    namespace: Optional[int],
    level_key: Optional[int],
) -> _ThreadLockKey:
    if key is not None:
        return ('key', key)
    return ('ns', int(namespace or 0), int(level_key or 0))


def _shared_thread_lock(identity: _ThreadLockKey) -> threading.Lock:
    with _thread_lock_registry_guard:
        lock = _thread_lock_registry.get(identity)
        if lock is None:
            lock = threading.Lock()
            _thread_lock_registry[identity] = lock
        return lock


class DedicatedAdvisoryLock:
    """Own a PostgreSQL advisory lock on a dedicated physical connection."""

    def __init__(
        self,
        db: Session,
        *,
        key: Optional[int] = None,
        namespace: Optional[int] = None,
        level_key: Optional[int] = None,
    ) -> None:
        if key is not None and (namespace is not None or level_key is not None):
            raise ValueError('Specify either key or namespace+level_key, not both')
        if key is None and (namespace is None or level_key is None):
            raise ValueError('Specify key or both namespace and level_key')
        self._db = db
        self._key = key
        self._namespace = namespace
        self._level_key = level_key
        self._conn: Optional[Connection] = None
        self._owns_connection = False
        self._held = False
        self._invalidated = False
        self._thread_lock: Optional[threading.Lock] = None
        self._use_thread_lock = False

    @property
    def held(self) -> bool:
        return self._held

    @property
    def invalidated(self) -> bool:
        return self._invalidated

    def _bind_is_postgresql(self) -> bool:
        bind = self._db.get_bind()
        return bind.dialect.name == 'postgresql'

    def _engine_from_bind(self):
        bind = self._db.get_bind()
        if isinstance(bind, Connection):
            return bind.engine
        return bind

    def _acquire_sql(self) -> Tuple[str, Dict[str, Any]]:
        if self._key is not None:
            return 'SELECT pg_try_advisory_lock(:k)', {'k': self._key}
        return (
            'SELECT pg_try_advisory_lock(:namespace, :lock_key)',
            {'namespace': self._namespace, 'lock_key': self._level_key},
        )

    def _blocking_acquire_sql(self) -> Tuple[str, Dict[str, Any]]:
        if self._key is not None:
            return 'SELECT pg_advisory_lock(:k)', {'k': self._key}
        return (
            'SELECT pg_advisory_lock(:namespace, :lock_key)',
            {'namespace': self._namespace, 'lock_key': self._level_key},
        )

    def _release_sql(self) -> Tuple[str, Dict[str, Any]]:
        if self._key is not None:
            return 'SELECT pg_advisory_unlock(:k)', {'k': self._key}
        return (
            'SELECT pg_advisory_unlock(:namespace, :lock_key)',
            {'namespace': self._namespace, 'lock_key': self._level_key},
        )

    def try_acquire(self) -> bool:
        """Try to acquire the lock.

        Returns `True` when this connection owns the lock, `False` when
        another session already holds it. Raises on unsupported environments.
        """
        if self._held:
            return True

        if not self._bind_is_postgresql():
            self._use_thread_lock = True
            self._thread_lock = _shared_thread_lock(
                _thread_lock_identity(
                    key=self._key,
                    namespace=self._namespace,
                    level_key=self._level_key,
                )
            )
            acquired = self._thread_lock.acquire(blocking=False)
            if acquired:
                self._held = True
            return acquired

        if self._conn is not None:
            self._safe_close()

        self._conn = self._engine_from_bind().connect()
        self._owns_connection = True
        sql, params = self._acquire_sql()
        try:
            acquired = bool(self._conn.execute(text(sql), params).scalar())
        except Exception:
            self._invalidate_connection()
            raise

        if acquired:
            self._held = True
            return True

        self._safe_close()
        return False

    def acquire_blocking(self, *, timeout_seconds: Optional[float] = 30.0) -> None:
        """Block until this connection owns the lock.

        Pool refill and campaign/autopilot callers keep using ``try_acquire``.
        This blocking path is for same-customer customer-request issuance.
        """
        if self._held:
            return

        if not self._bind_is_postgresql():
            self._use_thread_lock = True
            self._thread_lock = _shared_thread_lock(
                _thread_lock_identity(
                    key=self._key,
                    namespace=self._namespace,
                    level_key=self._level_key,
                )
            )
            acquire_kwargs: Dict[str, Any] = {"blocking": True}
            if timeout_seconds is not None:
                acquire_kwargs["timeout"] = timeout_seconds
            acquired = self._thread_lock.acquire(**acquire_kwargs)
            if not acquired:
                self._thread_lock = None
                self._use_thread_lock = False
                raise RuntimeError("advisory lock timed out")
            self._held = True
            return

        if self._conn is not None:
            self._safe_close()

        self._conn = self._engine_from_bind().connect()
        self._owns_connection = True
        sql, params = self._blocking_acquire_sql()
        try:
            if timeout_seconds is not None:
                timeout_ms = max(1, int(float(timeout_seconds) * 1000))
                self._conn.execute(text(f"SET lock_timeout = '{timeout_ms}'"))
            self._conn.execute(text(sql), params)
        except Exception:
            self._invalidate_connection()
            raise
        self._held = True

    def release(self) -> bool:
        """Release the lock and close the dedicated connection.

        Returns `False` when unlock fails; the physical connection is
        invalidated so it cannot return to the pool while still holding a lock.
        """
        if not self._held:
            return True

        if self._use_thread_lock:
            lock = self._thread_lock
            self._held = False
            self._thread_lock = None
            self._use_thread_lock = False
            if lock is None:
                return True
            try:
                lock.release()
            except RuntimeError:
                logger.warning('[pg_advisory_lock] thread lock release failed')
                return False
            return True

        if self._conn is None:
            self._held = False
            return True

        sql, params = self._release_sql()
        try:
            released = bool(self._conn.execute(text(sql), params).scalar())
        except Exception as exc:
            logger.warning(
                '[pg_advisory_lock] unlock exception error_class=%s',
                type(exc).__name__,
            )
            self._invalidate_connection()
            return False

        self._held = False
        if not released:
            logger.warning('[pg_advisory_lock] pg_advisory_unlock returned false')
            self._invalidate_connection()
            return False

        self._safe_close()
        return True

    def _invalidate_connection(self) -> None:
        self._invalidated = True
        self._held = False
        if self._conn is None:
            return
        if self._owns_connection:
            try:
                self._conn.invalidate()
            except Exception:  # noqa: silent-ok -- invalidate cleanup must not raise
                pass
            try:
                self._conn.close()
            except Exception:  # noqa: silent-ok -- close cleanup must not raise
                pass
        self._conn = None
        self._owns_connection = False

    def _safe_close(self) -> None:
        if self._conn is None:
            return
        if self._owns_connection:
            try:
                self._conn.close()
            except Exception:  # noqa: silent-ok -- close cleanup must not raise
                pass
        self._conn = None
        self._owns_connection = False

    def __enter__(self) -> 'DedicatedAdvisoryLock':
        if not self.try_acquire():
            raise RuntimeError('advisory lock not acquired')
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._held:
            self.release()


__all__ = ['DedicatedAdvisoryLock']
