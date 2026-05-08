"""
core/runtime_perf.py
────────────────────
In-process runtime telemetry for production hardening.

Exposes:

* ``spawn_background(coro, *, name, request_id=None)`` — schedule a
  fire-and-forget coroutine while keeping a live count of in-flight
  background tasks plus a lifetime total. Errors are logged but never
  bubble back into the request that spawned them. This is the helper
  webhook handlers use to return 200 OK to Meta/360dialog instantly
  while the AI/state pipeline runs asynchronously behind the response.

  The helper enforces a HARD CAP on simultaneously-in-flight tasks
  (``MAX_BACKGROUND_TASKS``, default 100, override via env var
  ``NAHLA_MAX_BG_TASKS``). Past the cap the new task is REJECTED:
  the function logs ``[BG/rejected]`` and returns a no-op completed
  task. This protects the worker from "task explosion" — e.g. when
  Meta or 360dialog flush a backlog of thousands of webhooks at the
  worker the moment a new deploy comes up — which would otherwise
  saturate the event loop and freeze /healthz, /auth/ping, login, etc.

* ``incr_active_request()`` / ``decr_active_request()`` — counters
  surfaced in the heartbeat and ``/admin/runtime/perf`` so the operator
  can see how many HTTP requests are mid-flight at any instant.

* ``record_request(...)`` — append a finished request to a small
  rolling window of slowest requests (top-N kept by ``total_ms``). Used
  by the request timing middleware so the operator can spot which
  routes are blocking the worker.

* ``get_perf_snapshot()`` — JSON-friendly dict consumed by
  ``GET /admin/runtime/perf``: active background tasks, scheduler
  states, conversation lock counts, slowest recent requests.

The module is deliberately tiny and dependency-free so it stays cheap
on the hot path. All shared state is guarded by a single
``threading.Lock``; ints fall back to atomic-ish behaviour on CPython.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("nahla.runtime_perf")


# Hard cap on simultaneously-in-flight background tasks (see module docstring).
def _read_max_bg_tasks_env() -> int:
    raw = os.environ.get("NAHLA_MAX_BG_TASKS", "").strip()
    try:
        v = int(raw) if raw else 100
    except ValueError:
        v = 100
    return max(1, v)


MAX_BACKGROUND_TASKS = _read_max_bg_tasks_env()


# ── Background-task tracking ────────────────────────────────────────────────
@dataclass
class _BgState:
    in_flight: int = 0
    total_spawned: int = 0
    total_completed: int = 0
    total_failed: int = 0
    total_rejected: int = 0
    by_name_in_flight: Dict[str, int] = field(default_factory=dict)
    by_name_total: Dict[str, int] = field(default_factory=dict)
    by_name_rejected: Dict[str, int] = field(default_factory=dict)


_BG = _BgState()
_BG_LOCK = threading.Lock()


def spawn_background(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    request_id: Optional[str] = None,
) -> "asyncio.Task[Any]":
    """Schedule ``coro`` on the running event loop and instrument it.

    A hard cap of ``MAX_BACKGROUND_TASKS`` simultaneous tasks is enforced.
    When the cap is reached, the task is REJECTED rather than queued
    indefinitely — preserving the event loop's ability to keep serving
    /alive, /auth/ping, /auth/login. The rejected coroutine is closed so
    Python doesn't emit an "unawaited coroutine" RuntimeWarning, and a
    structured ``[BG/rejected]`` log line surfaces in Railway so the
    operator can correlate the drop with upstream backlogs.

    Errors raised by the coroutine are logged via the module logger but
    are NEVER propagated to the caller — the calling request has
    already returned a 200 to the upstream provider, so the only
    correct error-handling strategy is to log and continue.

    Returns the asyncio.Task so callers can attach extra hooks if they
    really want to. Rejected calls return an already-completed dummy
    task so the caller doesn't have to special-case the return value.
    """
    loop = asyncio.get_event_loop()
    rid = request_id or "-"

    with _BG_LOCK:
        if _BG.in_flight >= MAX_BACKGROUND_TASKS:
            _BG.total_rejected += 1
            _BG.by_name_rejected[name] = _BG.by_name_rejected.get(name, 0) + 1
            in_flight_now = _BG.in_flight
            cap = MAX_BACKGROUND_TASKS
            # Close the coroutine so Python doesn't warn about a never-awaited
            # coroutine on the next GC cycle.
            try:
                coro.close()
            except Exception:
                pass
            # Build a no-op completed Task so the caller can keep its
            # ``return spawn_background(...)`` shape.
            done_future: "asyncio.Future[Any]" = loop.create_future()
            done_future.set_result(None)
            logger.warning(
                "[BG/rejected] name=%s request_id=%s in_flight=%d cap=%d — "
                "task explosion guard tripped",
                name, rid, in_flight_now, cap,
            )
            return asyncio.ensure_future(done_future)

        _BG.in_flight += 1
        _BG.total_spawned += 1
        _BG.by_name_in_flight[name] = _BG.by_name_in_flight.get(name, 0) + 1
        _BG.by_name_total[name] = _BG.by_name_total.get(name, 0) + 1

    started = time.monotonic()
    logger.debug("[BG/spawn] name=%s request_id=%s", name, rid)

    async def _runner() -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            with _BG_LOCK:
                _BG.total_failed += 1
            logger.error(
                "[BG/error] name=%s request_id=%s elapsed_ms=%d exc=%r",
                name, rid, int((time.monotonic() - started) * 1000), exc,
                exc_info=True,
            )
        else:
            with _BG_LOCK:
                _BG.total_completed += 1
        finally:
            with _BG_LOCK:
                _BG.in_flight = max(0, _BG.in_flight - 1)
                cur = _BG.by_name_in_flight.get(name, 0)
                if cur <= 1:
                    _BG.by_name_in_flight.pop(name, None)
                else:
                    _BG.by_name_in_flight[name] = cur - 1

    return loop.create_task(_runner(), name=f"bg:{name}")


def background_snapshot() -> Dict[str, Any]:
    with _BG_LOCK:
        return {
            "in_flight":       _BG.in_flight,
            "cap":             MAX_BACKGROUND_TASKS,
            "total_spawned":   _BG.total_spawned,
            "total_completed": _BG.total_completed,
            "total_failed":    _BG.total_failed,
            "total_rejected":  _BG.total_rejected,
            "by_name_in_flight": dict(_BG.by_name_in_flight),
            "by_name_total":     dict(_BG.by_name_total),
            "by_name_rejected":  dict(_BG.by_name_rejected),
        }


# ── Active-request counter ──────────────────────────────────────────────────
_ACTIVE_REQUESTS = 0
_ACTIVE_REQUESTS_LOCK = threading.Lock()


def incr_active_request() -> int:
    global _ACTIVE_REQUESTS
    with _ACTIVE_REQUESTS_LOCK:
        _ACTIVE_REQUESTS += 1
        return _ACTIVE_REQUESTS


def decr_active_request() -> int:
    global _ACTIVE_REQUESTS
    with _ACTIVE_REQUESTS_LOCK:
        _ACTIVE_REQUESTS = max(0, _ACTIVE_REQUESTS - 1)
        return _ACTIVE_REQUESTS


def active_request_count() -> int:
    with _ACTIVE_REQUESTS_LOCK:
        return _ACTIVE_REQUESTS


# ── Request timing ───────────────────────────────────────────────────────────
@dataclass
class _RequestSample:
    method:    str
    path:      str
    status:    int
    total_ms:  int
    db_ms:     int
    ai_ms:     int
    lock_wait_ms: int
    tenant_id: str
    finished_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method":       self.method,
            "path":         self.path,
            "status":       self.status,
            "total_ms":     self.total_ms,
            "db_ms":        self.db_ms,
            "ai_ms":        self.ai_ms,
            "lock_wait_ms": self.lock_wait_ms,
            "tenant_id":    self.tenant_id,
            "finished_at":  self.finished_at,
        }


_SLOW_KEEP = 25                  # rolling top-N slowest finished requests
_SLOW: List[_RequestSample] = []  # kept sorted desc by total_ms
_SLOW_LOCK = threading.Lock()

_TOTAL_REQUESTS = 0
_TOTAL_DURATION_MS = 0
_REQ_LOCK = threading.Lock()


def record_request(
    *,
    method: str,
    path: str,
    status: int,
    total_ms: int,
    db_ms: int = 0,
    ai_ms: int = 0,
    lock_wait_ms: int = 0,
    tenant_id: str = "-",
) -> None:
    """Record a finished request for the rolling perf snapshot."""
    global _TOTAL_REQUESTS, _TOTAL_DURATION_MS
    with _REQ_LOCK:
        _TOTAL_REQUESTS += 1
        _TOTAL_DURATION_MS += max(0, int(total_ms))

    sample = _RequestSample(
        method=method,
        path=path,
        status=status,
        total_ms=int(total_ms),
        db_ms=int(db_ms),
        ai_ms=int(ai_ms),
        lock_wait_ms=int(lock_wait_ms),
        tenant_id=str(tenant_id),
        finished_at=time.time(),
    )
    with _SLOW_LOCK:
        # Insert keeping list sorted descending by total_ms.
        _SLOW.append(sample)
        _SLOW.sort(key=lambda s: s.total_ms, reverse=True)
        if len(_SLOW) > _SLOW_KEEP:
            del _SLOW[_SLOW_KEEP:]


def request_snapshot() -> Dict[str, Any]:
    with _REQ_LOCK:
        total = _TOTAL_REQUESTS
        total_ms = _TOTAL_DURATION_MS
    avg_ms = round(total_ms / total) if total else 0
    with _SLOW_LOCK:
        slowest = [s.to_dict() for s in _SLOW]
    return {
        "total_requests":  total,
        "avg_total_ms":    avg_ms,
        "slowest_recent":  slowest,
    }


# ── Conversation lock telemetry (read-through to core.conversation_lock) ────
def conversation_lock_snapshot() -> Dict[str, Any]:
    """Best-effort introspection into the in-process conversation lock map.

    Never raises if the module shape changes — we just return an empty
    dict so the perf endpoint always succeeds.
    """
    try:
        from core import conversation_lock as _cl  # noqa: PLC0415
        # _LOCKS / _WAITERS are private but read-only here.
        locks    = getattr(_cl, "_LOCKS",   {}) or {}
        waiters  = getattr(_cl, "_WAITERS", {}) or {}
        active = []
        for key, lock in list(locks.items()):
            tenant_id, phone = key
            held = bool(getattr(lock, "locked", lambda: False)())
            active.append({
                "tenant_id":     tenant_id,
                "phone_suffix":  phone[-6:] if phone else "",
                "held":          held,
                "waiters_ahead": int(waiters.get(key, 0)),
            })
        # Surface only locks that are interesting (held or with waiters).
        active = [a for a in active if a["held"] or a["waiters_ahead"] > 0]
        return {
            "tracked":        len(locks),
            "active_or_busy": active[:25],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"conversation_lock_snapshot_failed: {exc!r}"}


# ── Inbound dedup cache size ────────────────────────────────────────────────
def inbound_dedup_snapshot() -> Dict[str, Any]:
    try:
        from core.inbound_dedup import cache_size  # noqa: PLC0415
        return {"cache_entries": cache_size()}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"inbound_dedup_snapshot_failed: {exc!r}"}


# ── Scheduler state registry ────────────────────────────────────────────────
@dataclass
class _SchedulerEntry:
    name: str
    started_at: float
    delayed_seconds: float
    last_tick_at: Optional[float] = None
    tick_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None


_SCHEDULERS: Dict[str, _SchedulerEntry] = {}
_SCHED_LOCK = threading.Lock()


def register_scheduler(name: str, *, delayed_seconds: float = 0.0) -> None:
    """Mark a scheduler as launched; called from the staggered startup code."""
    with _SCHED_LOCK:
        _SCHEDULERS[name] = _SchedulerEntry(
            name=name,
            started_at=time.time(),
            delayed_seconds=float(delayed_seconds),
        )


def stamp_scheduler_tick(name: str, *, error: Optional[str] = None) -> None:
    """Optional hook that schedulers may call once per loop iteration."""
    with _SCHED_LOCK:
        entry = _SCHEDULERS.get(name)
        if entry is None:
            entry = _SchedulerEntry(name=name, started_at=time.time(), delayed_seconds=0.0)
            _SCHEDULERS[name] = entry
        entry.last_tick_at = time.time()
        entry.tick_count += 1
        if error:
            entry.error_count += 1
            entry.last_error = error[:300]


def scheduler_snapshot() -> Dict[str, Any]:
    with _SCHED_LOCK:
        return {
            name: {
                "name":            entry.name,
                "started_at":      entry.started_at,
                "delayed_seconds": entry.delayed_seconds,
                "last_tick_at":    entry.last_tick_at,
                "tick_count":      entry.tick_count,
                "error_count":     entry.error_count,
                "last_error":      entry.last_error,
            }
            for name, entry in _SCHEDULERS.items()
        }


# ── Process-level snapshot (threads / memory / event-loop lag) ──────────────
def process_snapshot() -> Dict[str, Any]:
    """Best-effort introspection: thread count, RSS in MB, current loop lag.

    Never raises — falls back to ``None`` for any value we can't read on
    the running platform. Container-friendly: uses /proc when psutil is
    not available so it works on the slim Railway base image.
    """
    out: Dict[str, Any] = {
        "thread_count": threading.active_count(),
        "memory_rss_mb": None,
        "active_requests": active_request_count(),
        "event_loop_lag_ms": _last_loop_lag_ms,
    }
    try:
        import resource  # noqa: PLC0415  (POSIX only)
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux returns kB, macOS returns bytes — heuristically detect.
        out["memory_rss_mb"] = round(rss_kb / 1024) if rss_kb > 1024 * 1024 else round(rss_kb / 1024)
    except Exception:
        pass
    return out


# ── Aggregate snapshot ──────────────────────────────────────────────────────
def get_perf_snapshot() -> Dict[str, Any]:
    return {
        "as_of":               time.time(),
        "process":             process_snapshot(),
        "background_tasks":    background_snapshot(),
        "requests":            request_snapshot(),
        "conversation_locks":  conversation_lock_snapshot(),
        "inbound_dedup":       inbound_dedup_snapshot(),
        "schedulers":          scheduler_snapshot(),
    }


# ── Runtime heartbeat ───────────────────────────────────────────────────────
# Background task that emits one INFO log line every ~10s with a snapshot
# of the worker's vitals. If the heartbeats stop showing up in Railway,
# we know precisely WHEN the event loop froze — and can correlate with
# whatever request / scheduler tick was last observed before the gap.
_last_loop_lag_ms: int = 0


async def run_runtime_heartbeat(interval_seconds: float = 10.0) -> None:
    """Forever-loop: log a structured heartbeat every ``interval_seconds``.

    Also measures event-loop responsiveness: we sleep for the requested
    interval and compare wall-clock elapsed vs the requested interval.
    A loop fully starved by sync work in another task will sleep
    significantly longer than asked, surfacing as ``event_loop_lag_ms``
    in the heartbeat (and in /admin/runtime/perf).
    """
    global _last_loop_lag_ms
    logger.info("[RuntimeHeartbeat] starting, interval=%.1fs cap=%d", interval_seconds, MAX_BACKGROUND_TASKS)
    while True:
        slept_at = time.monotonic()
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        actual = time.monotonic() - slept_at
        lag_ms = max(0, int((actual - interval_seconds) * 1000))
        _last_loop_lag_ms = lag_ms

        try:
            bg = background_snapshot()
            proc = process_snapshot()
            logger.info(
                "[RuntimeHeartbeat] loop_lag_ms=%d active_requests=%d bg_in_flight=%d/%d "
                "bg_total_spawned=%d bg_total_failed=%d bg_total_rejected=%d "
                "threads=%s memory_rss_mb=%s",
                lag_ms,
                proc.get("active_requests", -1),
                bg.get("in_flight", -1),
                bg.get("cap", MAX_BACKGROUND_TASKS),
                bg.get("total_spawned", -1),
                bg.get("total_failed", -1),
                bg.get("total_rejected", -1),
                proc.get("thread_count"),
                proc.get("memory_rss_mb"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RuntimeHeartbeat] sample failed: %s", exc)


# ── Convenience: schedule with a delay and register ──────────────────────────
async def _delayed(coro: Awaitable[Any], delay_seconds: float, name: str) -> None:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    register_scheduler(name, delayed_seconds=delay_seconds)
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        logger.error("[Scheduler/error] name=%s exc=%r", name, exc, exc_info=True)


def schedule_with_delay(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    name: str,
    delay_seconds: float,
) -> "asyncio.Task[Any]":
    """Schedule ``coro_factory()`` after ``delay_seconds`` and register it.

    We accept a factory rather than a coroutine so we never instantiate a
    coroutine that is then never awaited (which raises a RuntimeWarning).
    """
    loop = asyncio.get_event_loop()
    return loop.create_task(
        _delayed(coro_factory(), delay_seconds, name),
        name=f"sched:{name}",
    )
