"""
services/salla_commerce_reconciler.py
Periodic incremental sync for Salla customers + products (not coupons).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.salla_commerce_reconciler")

POLL_INTERVAL_SECONDS = int(os.getenv("NAHLA_SALLA_COMMERCE_RECONCILE_SECONDS", "900"))
ADVISORY_LOCK_KEY = int(os.getenv("NAHLA_SALLA_COMMERCE_RECONCILE_LOCK_KEY", "748103219047"))
STARTUP_DELAY_SECONDS = int(os.getenv("NAHLA_SALLA_COMMERCE_RECONCILE_STARTUP_DELAY", "60"))
DISABLED = os.getenv("NAHLA_SALLA_COMMERCE_RECONCILE_DISABLED", "").lower() in ("1", "true", "yes")

_state: Dict[str, Any] = {
    "started_at": None,
    "last_tick_at": None,
    "last_tick_duration_ms": None,
    "last_tick_scanned": 0,
    "last_tick_errors": 0,
    "last_tick_skipped_reason": None,
    "ticks_total": 0,
    "tenants": {},
    "config": {
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "advisory_lock_key": ADVISORY_LOCK_KEY,
        "startup_delay_seconds": STARTUP_DELAY_SECONDS,
        "disabled": DISABLED,
    },
}


def get_reconciler_state() -> Dict[str, Any]:
    return {
        **_state,
        "tenants": {tid: dict(stats) for tid, stats in _state["tenants"].items()},
        "config": dict(_state["config"]),
    }


async def run_salla_commerce_reconciler_scheduler() -> None:
    if DISABLED:
        logger.warning("[Salla Commerce Reconciler] DISABLED")
        return
    _state["started_at"] = datetime.now(timezone.utc).isoformat()
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info("[Salla Commerce Reconciler] starting interval=%ss", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[Salla Commerce Reconciler] tick crashed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _run_one_tick() -> Dict[str, Any]:
    from core.database import SessionLocal

    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    db: Session = SessionLocal()
    lock_acquired = False
    try:
        try:
            lock_acquired = bool(db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}).scalar())
        except Exception:
            lock_acquired = True
        if not lock_acquired:
            _state["last_tick_at"] = started_at.isoformat()
            _state["last_tick_skipped_reason"] = "advisory_lock_held_by_other_worker"
            _state["ticks_total"] += 1
            return {"skipped": True}
        from models import Integration
        from store_integration.registry import adapter_for_integration
        integrations = db.query(Integration).filter(Integration.provider == "salla", Integration.enabled == True).order_by(Integration.id.asc()).all()
        integrations = [i for i in integrations if bool((i.config or {}).get("api_key")) and not bool((i.config or {}).get("needs_reauth")) and adapter_for_integration(i) is not None]
        scanned = errors = 0
        for intg in integrations:
            tenant_id = int(intg.tenant_id)
            tenant_state = {"tenant_id": tenant_id, "integration_id": intg.id, "scanned_at": datetime.now(timezone.utc).isoformat()}
            try:
                stats = await _reconcile_integration(db, intg)
                scanned += 1
                tenant_state.update({"result": "ok", "stats": stats})
            except Exception as exc:
                errors += 1
                try:
                    db.rollback()
                except Exception as rb_exc:
                    from core.obs import EVENTS, log_event  # noqa: PLC0415
                    log_event(
                        EVENTS.ORDER_UPSERT_ERROR,
                        err=rb_exc,
                        tenant_id=tenant_id,
                        context="commerce_reconcile_rollback",
                    )
                tenant_state.update({"result": "error", "error": repr(exc)})
            _state["tenants"][tenant_id] = tenant_state
        duration_ms = int((time.monotonic() - started) * 1000)
        _state.update({
            "last_tick_at": started_at.isoformat(),
            "last_tick_duration_ms": duration_ms,
            "last_tick_scanned": scanned,
            "last_tick_errors": errors,
            "last_tick_skipped_reason": None,
            "ticks_total": _state["ticks_total"] + 1,
        })
        return {"scanned": scanned, "errors": errors, "duration_ms": duration_ms}
    finally:
        if lock_acquired:
            try:
                db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            except Exception as unlock_exc:
                from core.obs import EVENTS, log_event  # noqa: PLC0415
                log_event(
                    EVENTS.DISPATCHER_LOOP_ERROR,
                    err=unlock_exc,
                    context="commerce_reconcile_advisory_unlock",
                )
        try:
            db.close()
        except Exception as close_exc:
            from core.obs import EVENTS, log_event  # noqa: PLC0415
            log_event(
                EVENTS.DISPATCHER_LOOP_ERROR,
                err=close_exc,
                context="commerce_reconcile_db_close",
            )


async def _reconcile_integration(db: Session, intg: Any) -> Dict[str, Any]:
    from services.store_sync import StoreSyncService

    started = time.monotonic()
    svc = StoreSyncService(db, int(intg.tenant_id))
    customers_synced = await svc.sync_customers(incremental=True)
    products_synced = await svc.sync_products(incremental=True)
    return {
        "customers_synced": customers_synced,
        "products_synced": products_synced,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
