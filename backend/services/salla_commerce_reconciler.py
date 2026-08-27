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

from sqlalchemy.orm import Session

from core.coupon_log_privacy import hash_identifier, safe_exception_class
from core.pg_advisory_lock import DedicatedAdvisoryLock

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
            logger.exception(
                "[Salla Commerce Reconciler] tick crashed error_class=%s",
                safe_exception_class(exc),
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _run_one_tick() -> Dict[str, Any]:
    from core.database import SessionLocal

    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    db: Session = SessionLocal()
    lock = DedicatedAdvisoryLock(db, key=ADVISORY_LOCK_KEY)
    try:
        try:
            acquired = lock.try_acquire()
        except Exception as lock_exc:
            logger.warning(
                "[Salla Commerce Reconciler] advisory lock acquire failed error_class=%s",
                safe_exception_class(lock_exc),
            )
            _state["last_tick_at"] = started_at.isoformat()
            _state["last_tick_skipped_reason"] = "advisory_lock_unavailable"
            _state["ticks_total"] += 1
            return {"skipped": True, "reason": "advisory_lock_unavailable"}

        if not acquired:
            _state["last_tick_at"] = started_at.isoformat()
            _state["last_tick_skipped_reason"] = "advisory_lock_held_by_other_worker"
            _state["ticks_total"] += 1
            return {"skipped": True, "reason": "advisory_lock_held_by_other_worker"}

        from models import Integration
        from store_integration.registry import pick_active_salla_integration

        tenant_ids = sorted({
            int(row.tenant_id)
            for row in db.query(Integration).filter_by(provider="salla", enabled=True).all()
            if row.tenant_id is not None
        })

        scanned = errors = 0
        for tenant_id in tenant_ids:
            intg = pick_active_salla_integration(db, tenant_id)
            cfg = (intg.config or {}) if intg is not None else {}
            store_id = (
                cfg.get("store_id")
                or cfg.get("merchant_id")
                or (intg.external_store_id if intg is not None else None)
            )
            tenant_state: Dict[str, Any] = {
                "tenant_hash": hash_identifier(tenant_id),
                "integration_id": intg.id if intg is not None else None,
                "store_hash": hash_identifier(store_id) if store_id else "",
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
                "error_code": None,
                "stats": None,
            }

            if intg is None:
                tenant_state["result"] = "skipped_no_integration"
                _state["tenants"][tenant_id] = tenant_state
                continue
            if bool(cfg.get("needs_reauth")):
                tenant_state["result"] = "skipped_needs_reauth"
                _state["tenants"][tenant_id] = tenant_state
                continue
            if not cfg.get("api_key"):
                tenant_state["result"] = "skipped_no_api_key"
                _state["tenants"][tenant_id] = tenant_state
                continue

            try:
                stats = await _reconcile_integration(db, intg)
                scanned += 1
                tenant_state.update({"result": "ok", "stats": stats})
            except Exception as exc:
                errors += 1
                try:
                    db.rollback()
                except Exception as rb_exc:
                    logger.warning(
                        "[Salla Commerce Reconciler] rollback_failed tenant_hash=%s error_class=%s",
                        hash_identifier(tenant_id),
                        safe_exception_class(rb_exc),
                    )
                tenant_state.update({
                    "result": "error",
                    "error_code": safe_exception_class(exc),
                })
                logger.warning(
                    "[Salla Commerce Reconciler] tenant_reconcile_failed tenant_hash=%s store_hash=%s error_class=%s",
                    hash_identifier(tenant_id),
                    hash_identifier(store_id),
                    safe_exception_class(exc),
                )
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
        if lock.held:
            lock.release()
        try:
            db.close()
        except Exception as close_exc:
            logger.warning(
                "[Salla Commerce Reconciler] db_close_failed error_class=%s",
                safe_exception_class(close_exc),
            )


async def _reconcile_integration(db: Session, intg: Any) -> Dict[str, Any]:
    from services.store_sync import StoreSyncService

    started = time.monotonic()
    svc = StoreSyncService(db, int(intg.tenant_id))
    customers_synced = await svc.sync_customers(incremental=True, strict=True)
    products_synced = await svc.sync_products(incremental=True, strict=True)
    return {
        "customers_synced": customers_synced,
        "products_synced": products_synced,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
