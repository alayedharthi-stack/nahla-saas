"""
services/salla_coupons_poller.py
Dedicated background poller that imports coupons from Salla with adaptive SLA.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.salla_coupons_poller")

POLL_INTERVAL_SECONDS = int(os.getenv("NAHLA_SALLA_COUPONS_POLL_SECONDS", "60"))
ADVISORY_LOCK_KEY = int(os.getenv("NAHLA_SALLA_COUPONS_POLLER_LOCK_KEY", "748103219046"))
STARTUP_DELAY_SECONDS = int(os.getenv("NAHLA_SALLA_COUPONS_POLLER_STARTUP_DELAY", "50"))
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
        "[Salla Coupons Poller] starting interval=%ss advisory_lock_key=%s",
        POLL_INTERVAL_SECONDS,
        ADVISORY_LOCK_KEY,
    )
    while True:
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            logger.info("[Salla Coupons Poller] cancelled")
            raise
        except Exception as exc:
            logger.exception("[Salla Coupons Poller] tick crashed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _run_one_tick() -> Dict[str, Any]:
    from core.database import SessionLocal  # noqa: PLC0415

    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    logger.info("[Salla Coupons Poller] tick started at=%s", started_at.isoformat())

    db: Session = SessionLocal()
    lock_acquired = False
    try:
        try:
            row = db.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": ADVISORY_LOCK_KEY},
            ).scalar()
            lock_acquired = bool(row)
        except Exception as lock_exc:
            logger.debug("[Salla Coupons Poller] advisory lock unsupported (%s)", lock_exc)
            lock_acquired = True

        if not lock_acquired:
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
                "tenant_id": tenant_id,
                "integration_id": intg.id if intg is not None else None,
                "store_id": store_id,
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
            if _retry_after_active(coupon_sync_meta):
                tenant_state["result"] = "skipped_retry_after"
                _state["tenants"][tenant_id] = tenant_state
                continue
            if not tenant_poll_due(coupon_sync_meta):
                tenant_state["result"] = "skipped_not_due"
                _state["tenants"][tenant_id] = tenant_state
                continue

            try:
                stats = await _poll_integration(db, intg)
                scanned += 1
                items_seen_total += stats["items_seen"]
                created_total += stats["created"]
                updated_total += stats["updated"]
                tenant_state.update({"result": "ok", "stats": stats})
                logger.info(
                    "[Salla Coupons Poller] tenant=%s store=%s items_seen=%d created=%d updated=%d duration_ms=%d fetch_ok=%s partial=%s",
                    tenant_id,
                    store_id,
                    stats["items_seen"],
                    stats["created"],
                    stats["updated"],
                    stats["duration_ms"],
                    stats.get("fetch_ok"),
                    stats.get("partial"),
                )
            except Exception as exc:
                errors += 1
                tenant_state["result"] = "error"
                tenant_state["error"] = type(exc).__name__
                logger.exception(
                    "[Salla Coupons Poller] tenant=%s store=%s error=%s",
                    tenant_id,
                    store_id,
                    type(exc).__name__,
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
        if lock_acquired:
            try:
                db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
                db.commit()
            except Exception:  # noqa: silent-ok — advisory unlock cleanup is best-effort
                pass
        try:
            db.close()
        except Exception:  # noqa: silent-ok — session close cleanup is best-effort
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
