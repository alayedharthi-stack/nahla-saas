"""
core/abandoned_cart_scheduler.py
────────────────────────────────
Dedicated, lightweight scheduler that pulls abandoned carts from every
connected Salla store every few minutes.

Why a separate loop
───────────────────
The existing ``run_store_sync_scheduler`` only fires every **3600 s**
(1 hour) AND runs the full ``StoreSyncService.full_sync`` pipeline —
products, orders, coupons, customers, profile rebuild — which takes
real time per tenant. Two consequences:

  1. New abandoned carts could sit invisible in Nahla for up to 60 min
     after the merchant created them in Salla. Merchants reasonably
     expect "near real-time" because Salla shows them immediately.

  2. Calling ``full_sync`` more often is wasteful — the heavy parts
     (products, customers) genuinely don't change every 5 min.

This loop runs every ``INTERVAL_SECONDS`` (default 300 s = 5 min) and
calls **only** ``StoreSyncService.sync_abandoned_carts()`` per active
Salla integration. That call is cheap: one paginated GET against
``/carts/abandoned`` plus the per-cart upsert. So we get a tight
near-real-time freshness guarantee without paying for a full sync.

Layered design (defence in depth)
─────────────────────────────────
Abandoned-cart visibility now relies on three independent paths,
ranked from fastest to slowest:

  • Webhook  — Salla fires ``abandoned.cart`` on creation. Routed via
    ``core.webhook_dispatcher`` to ``handle_abandoned_cart_webhook``.
    Latency: seconds. Single point of failure: webhook subscription.
  • This 5-min reconciliation loop — safety net. Latency: 5 min worst
    case. Catches everything the webhook missed.
  • The hourly ``full_sync`` — last-resort sweep that also reconciles
    orders/coupons/etc.

Operator visibility
───────────────────
``LAST_RUNS`` is a process-local registry that each tick updates. The
``/debug/scheduler-status`` endpoint reads from it so an operator can
see at a glance: when the loop last fired, what each tenant's last
result was, and how many consecutive failures any tenant has hit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla-abandoned-cart-scheduler")


# ── Knobs ────────────────────────────────────────────────────────────────────
# Both knobs are env-overridable so the merchant can crank the loop
# down to 60 s while triaging without a code deploy. We clamp to a
# sensible floor so a misconfigured value can't DoS Salla.
def _read_int_env(name: str, default: int, *, floor: int) -> int:
    raw = os.environ.get(name, "")
    try:
        v = int(raw) if raw else default
    except ValueError:
        v = default
    return max(floor, v)

INTERVAL_SECONDS = _read_int_env(
    "NAHLA_ABANDONED_CART_SYNC_INTERVAL_SECONDS",
    default=300,  # 5 minutes
    floor=60,     # never hammer Salla harder than once a minute
)
STARTUP_DELAY_SECONDS = _read_int_env(
    "NAHLA_ABANDONED_CART_SYNC_STARTUP_DELAY_SECONDS",
    default=45,
    floor=0,
)


# ── Per-tenant run registry ──────────────────────────────────────────────────

@dataclass
class TenantSyncStatus:
    """Snapshot of the most recent sync attempt for one tenant."""
    tenant_id:           int
    started_at:          str  # ISO-8601 UTC
    finished_at:         Optional[str] = None
    duration_ms:         Optional[int] = None
    status:              str = "running"  # running | ok | error | skipped
    error:               Optional[str] = None
    salla_count:         int = 0
    saved:               int = 0
    updated:             int = 0
    reconciled:          int = 0
    skipped_no_id:       int = 0
    consecutive_failures: int = 0


@dataclass
class SchedulerState:
    """Process-wide snapshot of the scheduler's health."""
    started_at:        Optional[str] = None
    interval_seconds:  int           = INTERVAL_SECONDS
    last_cycle_at:     Optional[str] = None
    last_cycle_ok:     Optional[bool] = None
    next_cycle_at:     Optional[str] = None
    cycles_completed:  int           = 0
    cycle_errors:      int           = 0
    tenants_in_last_cycle: int       = 0
    last_runs:         Dict[int, TenantSyncStatus] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at":            self.started_at,
            "interval_seconds":      self.interval_seconds,
            "last_cycle_at":         self.last_cycle_at,
            "last_cycle_ok":         self.last_cycle_ok,
            "next_cycle_at":         self.next_cycle_at,
            "cycles_completed":      self.cycles_completed,
            "cycle_errors":          self.cycle_errors,
            "tenants_in_last_cycle": self.tenants_in_last_cycle,
            "last_runs": {str(t): asdict(s) for t, s in self.last_runs.items()},
        }


# Single shared instance — the scheduler task writes, debug endpoints read.
# Protected by a Lock because asyncio + thread-pool callers (test suite,
# Starlette sync routes) may both poke it.
_state_lock = threading.Lock()
STATE = SchedulerState()


def get_state_snapshot() -> Dict[str, Any]:
    """Thread-safe read of the registry. Returns a deep-ish dict copy."""
    with _state_lock:
        return STATE.to_dict()


def get_last_run_for_tenant(tenant_id: int) -> Optional[Dict[str, Any]]:
    """Return the most recent :class:`TenantSyncStatus` for ``tenant_id``."""
    with _state_lock:
        s = STATE.last_runs.get(int(tenant_id))
        return asdict(s) if s else None


def _record_start(tenant_id: int) -> TenantSyncStatus:
    now = datetime.now(timezone.utc).isoformat()
    with _state_lock:
        prior = STATE.last_runs.get(tenant_id)
        # Carry the prior consecutive_failures count forward — this
        # tick will reset it to 0 if it succeeds.
        prior_fail = prior.consecutive_failures if prior else 0
        s = TenantSyncStatus(
            tenant_id=tenant_id, started_at=now, status="running",
            consecutive_failures=prior_fail,
        )
        STATE.last_runs[tenant_id] = s
        return s


def _record_finish(
    tenant_id: int,
    *,
    status: str,
    started_at_iso: str,
    error: Optional[str] = None,
    sync_result: Optional[Dict[str, Any]] = None,
) -> None:
    finished = datetime.now(timezone.utc)
    started = datetime.fromisoformat(started_at_iso)
    duration_ms = int((finished - started).total_seconds() * 1000)
    with _state_lock:
        prior = STATE.last_runs.get(tenant_id)
        consec = (prior.consecutive_failures if prior else 0)
        if status == "error":
            consec += 1
        else:
            consec = 0
        s = TenantSyncStatus(
            tenant_id=tenant_id,
            started_at=started_at_iso,
            finished_at=finished.isoformat(),
            duration_ms=duration_ms,
            status=status,
            error=error,
            salla_count=int((sync_result or {}).get("salla_count", 0)),
            saved=int((sync_result or {}).get("saved", 0)),
            updated=int((sync_result or {}).get("updated", 0)),
            reconciled=int((sync_result or {}).get("reconciled", 0)),
            skipped_no_id=int((sync_result or {}).get("skipped_no_id", 0)),
            consecutive_failures=consec,
        )
        STATE.last_runs[tenant_id] = s


# ── Cycle implementation ─────────────────────────────────────────────────────

async def _sync_one_tenant(tenant_id: int) -> None:
    """Run ``sync_abandoned_carts()`` for a single tenant with full
    error isolation + per-tenant status recording.

    Never raises — failures are recorded in :data:`STATE` and logged
    so a single broken tenant cannot stop the whole loop.
    """
    snap = _record_start(tenant_id)
    started_at = snap.started_at
    try:
        # Local imports keep this module importable in test contexts
        # that monkeypatch the DB / sync service.
        from core.database import SessionLocal  # noqa: PLC0415
        from services.store_sync import StoreSyncService  # noqa: PLC0415

        db = SessionLocal()
        try:
            svc = StoreSyncService(db, tenant_id)
            sync_result = await svc.sync_abandoned_carts()
        finally:
            db.close()

        _record_finish(
            tenant_id, status="ok", started_at_iso=started_at,
            sync_result=sync_result,
        )
        logger.info(
            "[AbandonedCartScheduler] tenant=%s OK "
            "salla_count=%d saved=%d updated=%d reconciled=%d skipped_no_id=%d",
            tenant_id,
            int(sync_result.get("salla_count", 0)),
            int(sync_result.get("saved", 0)),
            int(sync_result.get("updated", 0)),
            int(sync_result.get("reconciled", 0)),
            int(sync_result.get("skipped_no_id", 0)),
        )
    except Exception as exc:
        _record_finish(
            tenant_id, status="error", started_at_iso=started_at,
            error=f"{type(exc).__name__}: {exc}",
        )
        logger.exception(
            "[AbandonedCartScheduler] tenant=%s FAILED: %s",
            tenant_id, exc,
        )


def _list_active_salla_tenants() -> list[int]:
    """Find every tenant with an enabled, non-revoked Salla integration.

    We deliberately query inside this helper (not at module load) so a
    DB outage at startup doesn't kill the scheduler task — the next
    tick will just retry.
    """
    from core.database import SessionLocal  # noqa: PLC0415
    from models import Integration  # noqa: PLC0415

    db = SessionLocal()
    try:
        rows = (
            db.query(Integration)
            .filter(
                Integration.provider == "salla",
                Integration.enabled == True,  # noqa: E712
            )
            .all()
        )
        out: list[int] = []
        for intg in rows:
            cfg = intg.config or {}
            # Mirror the same skip rules as the hourly full sync so
            # we don't repeatedly hammer a known-broken integration.
            if cfg.get("needs_reauth"):
                continue
            if not cfg.get("api_key"):
                continue
            out.append(int(intg.tenant_id))
        return out
    finally:
        db.close()


async def _tick() -> None:
    """One scheduler cycle — sync every active tenant, sequentially.

    Sequential (not gather) on purpose: keeps Salla rate-limit pressure
    flat, makes log output deterministic, and lets a single
    expensive tenant degrade gracefully instead of blowing the
    request budget all at once.
    """
    cycle_started = datetime.now(timezone.utc)
    try:
        tenants = _list_active_salla_tenants()
    except Exception as exc:
        logger.exception(
            "[AbandonedCartScheduler] tenant lookup failed — skipping cycle: %s", exc,
        )
        with _state_lock:
            STATE.cycle_errors += 1
            STATE.last_cycle_at = cycle_started.isoformat()
            STATE.last_cycle_ok = False
        return

    logger.info(
        "[AbandonedCartScheduler] cycle started — %d active Salla tenant(s)",
        len(tenants),
    )

    for tenant_id in tenants:
        await _sync_one_tenant(tenant_id)

    with _state_lock:
        STATE.cycles_completed += 1
        STATE.last_cycle_at = cycle_started.isoformat()
        STATE.last_cycle_ok = True
        STATE.tenants_in_last_cycle = len(tenants)

    logger.info(
        "[AbandonedCartScheduler] cycle done in %.1fs — next in %ds",
        (datetime.now(timezone.utc) - cycle_started).total_seconds(),
        INTERVAL_SECONDS,
    )


# ── Public entrypoint ────────────────────────────────────────────────────────

async def run_abandoned_cart_scheduler() -> None:
    """Run the scheduler forever. Wired in from ``backend.main`` startup."""
    with _state_lock:
        STATE.started_at = datetime.now(timezone.utc).isoformat()
        STATE.interval_seconds = INTERVAL_SECONDS

    if STARTUP_DELAY_SECONDS > 0:
        logger.info(
            "[AbandonedCartScheduler] sleeping %ds before first cycle",
            STARTUP_DELAY_SECONDS,
        )
        await asyncio.sleep(STARTUP_DELAY_SECONDS)

    logger.info(
        "[AbandonedCartScheduler] started — interval=%ds", INTERVAL_SECONDS,
    )

    while True:
        try:
            await _tick()
        except Exception as exc:
            # _tick() catches per-tenant failures itself, but any
            # outer exception (bad import etc.) must not kill the loop.
            logger.exception(
                "[AbandonedCartScheduler] cycle raised — staying alive: %s", exc,
            )
            with _state_lock:
                STATE.cycle_errors += 1
                STATE.last_cycle_ok = False

        # Compute and stamp the next scheduled cycle so debug endpoints
        # can show "next run in 2m 14s" without guessing.
        next_at = datetime.now(timezone.utc).timestamp() + INTERVAL_SECONDS
        with _state_lock:
            STATE.next_cycle_at = datetime.fromtimestamp(
                next_at, tz=timezone.utc,
            ).isoformat()

        await asyncio.sleep(INTERVAL_SECONDS)
