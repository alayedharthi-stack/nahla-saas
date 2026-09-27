"""
services/salla_coupons_poller.py
Dedicated background poller that imports coupons from Salla with adaptive SLA.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from core.coupon_log_privacy import hash_identifier, safe_exception_class
from core.pg_advisory_lock import DedicatedAdvisoryLock

logger = logging.getLogger("nahla.salla_coupons_poller")

POLL_INTERVAL_SECONDS = int(os.getenv("NAHLA_SALLA_COUPONS_POLL_SECONDS", "60"))
TICK_INTERVAL_SECONDS = int(os.getenv("NAHLA_SALLA_COUPONS_TICK_SECONDS", "5"))
RECENT_POLL_SECONDS = int(os.getenv("NAHLA_SALLA_COUPONS_RECENT_POLL_SECONDS", "10"))
ADVISORY_LOCK_KEY = int(os.getenv("NAHLA_SALLA_COUPONS_POLLER_LOCK_KEY", "748103219046"))
STARTUP_DELAY_SECONDS = int(os.getenv("NAHLA_SALLA_COUPONS_POLLER_STARTUP_DELAY", "5"))
DISABLED = os.getenv("NAHLA_SALLA_COUPONS_POLLER_DISABLED", "").lower() in ("1", "true", "yes")

_state: Dict[str, Any] = {
    "started_at": None,
    "last_tick_at": None,
    "last_tick_duration_ms": None,
    "last_tick_scanned": 0,
    "last_tick_items_seen": 0,
    "last_tick_created": 0,
    "last_tick_updated": 0,
    "last_tick_errors": 0,
    "last_tick_skipped_reason": None,
    "ticks_total": 0,
    "tenants": {},
    "config": {
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "tick_interval_seconds": TICK_INTERVAL_SECONDS,
        "recent_poll_seconds": RECENT_POLL_SECONDS,
        "advisory_lock_key": ADVISORY_LOCK_KEY,
        "startup_delay_seconds": STARTUP_DELAY_SECONDS,
        "disabled": DISABLED,
        "adaptive_sla": {
            "small_catalog_max": 120,
            "small_catalog_seconds": 60,
            "medium_catalog_max": 600,
            "medium_catalog_seconds": 300,
            "large_catalog_seconds": 900,
        },
    },
}


def get_poller_state() -> Dict[str, Any]:
    return {
        **_state,
        "tenants": {tid: dict(stats) for tid, stats in _state["tenants"].items()},
        "config": dict(_state["config"]),
    }


def _retry_after_active(meta: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    raw = meta.get("retry_after_until")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return now < until.astimezone(timezone.utc)
    except ValueError:
        return False


async def run_salla_coupons_poller_scheduler() -> None:
    if DISABLED:
        logger.warning("[Salla Coupons Poller] DISABLED via NAHLA_SALLA_COUPONS_POLLER_DISABLED")
        return

    _state["started_at"] = datetime.now(timezone.utc).isoformat()
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info(
        "[Salla Coupons Poller] starting interval=%ss recent_interval=%ss advisory_lock_key=%s",
        TICK_INTERVAL_SECONDS, RECENT_POLL_SECONDS, ADVISORY_LOCK_KEY,
    )
    while True:
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            logger.info("[Salla Coupons Poller] cancelled")
            raise
        except Exception as exc:
            logger.warning(
                '[Salla Coupons Poller] tick_failed event=coupon_poller_tick_failed error_class=%s',
                safe_exception_class(exc),
            )
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


async def _run_one_tick() -> Dict[str, Any]:
    from core.database import SessionLocal  # noqa: PLC0415

    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    logger.info("[Salla Coupons Poller] tick started at=%s", started_at.isoformat())

    db: Session = SessionLocal()
    lock = DedicatedAdvisoryLock(db, key=ADVISORY_LOCK_KEY)
    try:
        try:
            acquired = lock.try_acquire()
        except Exception as lock_exc:
            logger.warning(
                "[Salla Coupons Poller] advisory lock acquire failed error_class=%s",
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

        from models import Integration  # noqa: PLC0415
        from services.salla_coupon_fetch import tenant_poll_due  # noqa: PLC0415
        from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415

        integrations = (
            db.query(Integration)
            .filter(
                Integration.provider == "salla",
                Integration.enabled == True,  # noqa: E712
            )
            .all()
        )
        tenant_ids = sorted({int(i.tenant_id) for i in integrations if i.tenant_id})

        scanned = 0
        items_seen_total = 0
        created_total = 0
        updated_total = 0
        errors = 0

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
                "store_present": bool(store_id),
                "store_hash": hash_identifier(store_id) if store_id else "",
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
                "error": None,
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

            coupon_sync_meta = cfg.get("coupon_sync_meta") or {}
            recent_sync_meta = cfg.get("coupon_recent_sync_meta") or {}
            full_backoff = _retry_after_active(coupon_sync_meta)
            full_due = not full_backoff and tenant_poll_due(coupon_sync_meta)
            recent_backoff = _retry_after_active(recent_sync_meta)
            provider_failures = ("rate_limited", "auth_error", "needs_reauth")
            provider_backoff = (
                (recent_backoff and recent_sync_meta.get("failure_class") in provider_failures)
                or (full_backoff and coupon_sync_meta.get("failure_class") in provider_failures)
            )
            if provider_backoff:
                tenant_state["result"] = "skipped_provider_backoff"
                _state["tenants"][tenant_id] = tenant_state
                continue
            recent_due = (
                not recent_backoff
                and tenant_poll_due(recent_sync_meta, interval_seconds=RECENT_POLL_SECONDS)
            )
            if not full_due and not recent_due:
                tenant_state["result"] = "skipped_not_due"
                _state["tenants"][tenant_id] = tenant_state
                continue

            try:
                recent_stats = None
                if recent_due:
                    recent_stats = await _poll_recent_integration(db, intg)
                    tenant_state["recent_stats"] = recent_stats
                    logger.info(
                        "[Salla Coupons Poller] tenant_recent_poll event=coupon_recent_poll tenant_hash=%s store_hash=%s items_seen=%d upserted=%d fetch_ok=%s failure_class=%s duration_ms=%d",
                        hash_identifier(tenant_id), hash_identifier(store_id),
                        recent_stats["items_seen"], recent_stats["upserted"],
                        recent_stats["fetch_ok"], recent_stats["failure_class"],
                        recent_stats["duration_ms"],
                    )
                if recent_stats is not None and not recent_stats["fetch_ok"] and (
                    not full_due or recent_stats["failure_class"] in provider_failures
                ):
                    tenant_state["result"] = "recent_fetch_failed"
                    errors += 1
                elif full_due:
                    stats = await _poll_integration(db, intg)
                    scanned += 1
                    items_seen_total += stats["items_seen"]
                    created_total += stats["created"]
                    updated_total += stats["updated"]
                    tenant_state.update({"result": "ok", "stats": stats})
                    logger.info(
                        "[Salla Coupons Poller] tenant_poll_ok event=coupon_poller_tenant_ok tenant_hash=%s store_hash=%s items_seen=%d created=%d updated=%d duration_ms=%d fetch_ok=%s partial=%s",
                        hash_identifier(tenant_id), hash_identifier(store_id),
                        stats["items_seen"], stats["created"], stats["updated"],
                        stats["duration_ms"], stats.get("fetch_ok"), stats.get("partial"),
                    )
                else:
                    tenant_state["result"] = "ok_recent"
            except Exception as exc:
                errors += 1
                tenant_state["result"] = "error"
                tenant_state["error"] = safe_exception_class(exc)
                logger.warning(
                    '[Salla Coupons Poller] tenant_poll_failed event=coupon_poller_tenant_failed tenant_hash=%s store_hash=%s error_class=%s',
                    hash_identifier(tenant_id),
                    hash_identifier(store_id),
                    safe_exception_class(exc),
                )
                try:
                    db.rollback()
                except Exception:  # noqa: silent-ok — best-effort rollback after tenant poll error
                    pass

            _state["tenants"][tenant_id] = tenant_state

        duration_ms = int((time.monotonic() - started) * 1000)
        _state.update({
            "last_tick_at": started_at.isoformat(),
            "last_tick_duration_ms": duration_ms,
            "last_tick_scanned": scanned,
            "last_tick_items_seen": items_seen_total,
            "last_tick_created": created_total,
            "last_tick_updated": updated_total,
            "last_tick_errors": errors,
            "last_tick_skipped_reason": None,
        })
        _state["ticks_total"] += 1

        logger.info(
            "[Salla Coupons Poller] tick completed scanned=%d items_seen=%d created=%d updated=%d errors=%d duration_ms=%d",
            scanned,
            items_seen_total,
            created_total,
            updated_total,
            errors,
            duration_ms,
        )
        return {
            "skipped": False,
            "scanned": scanned,
            "items_seen": items_seen_total,
            "created": created_total,
            "updated": updated_total,
            "errors": errors,
            "duration_ms": duration_ms,
        }
    finally:
        if lock.held:
            lock.release()
        try:
            db.close()
        except Exception:  # noqa: silent-ok
            pass



async def _poll_integration(db: Session, intg: Any) -> Dict[str, Any]:
    tenant_id = int(intg.tenant_id)
    started = time.monotonic()

    from store_integration.registry import adapter_for_integration  # noqa: PLC0415
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    adapter = adapter_for_integration(intg)
    if adapter is None or not hasattr(adapter, "fetch_coupons_paginated"):
        raise RuntimeError("missing_fetch_coupons_paginated")

    fetch_result = await adapter.fetch_coupons_paginated(per_page=60)
    svc = StoreSyncService(
        db,
        tenant_id,
        integration_connection_id=int(intg.id),
        adapter=adapter,
    )
    upserted = await svc.sync_coupons(
        triggered_by="salla_coupons_poller",
        raw_list=list(fetch_result.get("items") or []),
        fetch_result=fetch_result,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    try:
        db.commit()
        db.refresh(intg)
    except Exception:
        db.rollback()
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    cfg = dict(intg.config or {})
    meta = cfg.get("coupon_sync_meta") or {}
    created = int(meta.get("created") or 0)
    updated = int(meta.get("updated") or 0)

    return {
        "items_seen": int(meta.get("items_seen") or fetch_result.get("items_seen") or 0),
        "created": created,
        "updated": updated,
        "upserted": upserted,
        "duration_ms": duration_ms,
        "fetch_ok": bool(fetch_result.get("ok")),
        "partial": bool(fetch_result.get("partial")),
        "failure_class": fetch_result.get("failure_class"),
        "pages_fetched": fetch_result.get("pages_fetched"),
        "poll_interval_seconds": meta.get("poll_interval_seconds"),
    }


async def _poll_recent_integration(db: Session, intg: Any) -> Dict[str, Any]:
    """Quickly import new Salla coupons without resetting full-scan cadence."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    from store_integration.registry import adapter_for_integration  # noqa: PLC0415
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    started = time.monotonic()
    adapter = adapter_for_integration(intg)
    if adapter is None or not hasattr(adapter, "fetch_recent_coupons_paginated"):
        raise RuntimeError("missing_fetch_recent_coupons_paginated")

    fetch_result = await adapter.fetch_recent_coupons_paginated(per_page=60)
    upserted = 0
    if fetch_result.get("ok"):
        svc = StoreSyncService(
            db, int(intg.tenant_id),
            integration_connection_id=int(intg.id), adapter=adapter,
        )
        upserted = await svc.sync_coupons(
            triggered_by="salla_coupons_recent_poller",
            raw_list=list(fetch_result.get("items") or []),
            fetch_result=fetch_result,
            record_sync_meta=False,
        )

    now = datetime.now(timezone.utc)
    cfg = dict(intg.config or {})
    old_meta = dict(cfg.get("coupon_recent_sync_meta") or {})
    recent_meta = {
        "last_attempt_at": now.isoformat(),
        "last_poll_at": now.isoformat(),
        "last_success_at": now.isoformat() if fetch_result.get("ok") else old_meta.get("last_success_at"),
        "items_seen": int(fetch_result.get("items_seen") or 0),
        "upserted": upserted,
        "pages_fetched": int(fetch_result.get("pages_fetched") or 0),
        "failure_class": fetch_result.get("failure_class"),
    }
    retry_after = fetch_result.get("retry_after")
    if not fetch_result.get("ok"):
        # Failed recent scans must not hammer Salla every ten seconds.
        # Keep the normal full reconciliation available after the backoff.
        failure = str(fetch_result.get("failure_class") or "")
        if failure == "recent_window_too_large":
            retry_after = 900
        elif failure in ("auth_error", "needs_reauth"):
            retry_after = 300
        elif retry_after is None:
            retry_after = 60
    if retry_after:
        try:
            recent_meta["retry_after_until"] = (now + timedelta(seconds=int(retry_after))).isoformat()
        except (TypeError, ValueError):
            pass
    cfg["coupon_recent_sync_meta"] = recent_meta
    intg.config = cfg
    flag_modified(intg, "config")
    db.commit()
    db.refresh(intg)

    return {
        "items_seen": recent_meta["items_seen"],
        "upserted": upserted,
        "fetch_ok": bool(fetch_result.get("ok")),
        "failure_class": fetch_result.get("failure_class"),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
