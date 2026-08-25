"""
services/salla_coupons_poller.py
Dedicated background poller that imports coupons from Salla every 60s.

Salla does not expose a supported coupon CRUD webhook in Nahla's integration
surface, so inbound coupon sync relies on idempotent polling (same guarantee
pattern as ``salla_orders_poller.py``).
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
    },
}


def get_poller_state() -> Dict[str, Any]:
    return {
        **_state,
        "tenants": {tid: dict(stats) for tid, stats in _state["tenants"].items()},
        "config": dict(_state["config"]),
    }


async def run_salla_coupons_poller_scheduler() -> None:
    if DISABLED:
        logger.warning(
            "[Salla Coupons Poller] DISABLED via NAHLA_SALLA_COUPONS_POLLER_DISABLED — exiting",
        )
        return

    _state["started_at"] = datetime.now(timezone.utc).isoformat()
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info(
        "[Salla Coupons Poller] starting — interval=%ss advisory_lock_key=%s",
        POLL_INTERVAL_SECONDS,
        ADVISORY_LOCK_KEY,
    )
    while True:
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            logger.info("[Salla Coupons Poller] cancelled — exiting loop")
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
            logger.debug(
                "[Salla Coupons Poller] advisory lock unsupported (%s) — running anyway",
                lock_exc,
            )
            lock_acquired = True

        if not lock_acquired:
            _state["last_tick_at"] = started_at.isoformat()
            _state["last_tick_skipped_reason"] = "advisory_lock_held_by_other_worker"
            _state["ticks_total"] += 1
            return {"skipped": True, "reason": "advisory_lock_held_by_other_worker"}

        from models import Integration  # noqa: PLC0415
        from store_integration.registry import adapter_for_integration  # noqa: PLC0415

        integrations = (
            db.query(Integration)
            .filter(
                Integration.provider == "salla",
                Integration.enabled == True,  # noqa: E712
            )
            .order_by(Integration.id.asc())
            .all()
        )
        integrations = [
            intg for intg in integrations
            if bool((intg.config or {}).get("api_key"))
            and not bool((intg.config or {}).get("needs_reauth"))
            and adapter_for_integration(intg) is not None
        ]

        scanned = 0
        items_seen_total = 0
        created_total = 0
        updated_total = 0
        errors = 0

        for intg in integrations:
            tenant_id = int(intg.tenant_id)
            cfg = intg.config or {}
            store_id = cfg.get("store_id") or cfg.get("merchant_id") or intg.external_store_id
            tenant_state: Dict[str, Any] = {
                "tenant_id": tenant_id,
                "integration_id": intg.id,
                "store_id": store_id,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
                "error": None,
                "stats": None,
            }

            if bool(cfg.get("needs_reauth")):
                tenant_state["result"] = "skipped_needs_reauth"
                _state["tenants"][tenant_id] = tenant_state
                continue
            if not cfg.get("api_key"):
                tenant_state["result"] = "skipped_no_api_key"
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
                    "[Salla Coupons Poller] tenant=%s store=%s items_seen=%d created=%d updated=%d duration_ms=%d",
                    tenant_id,
                    store_id,
                    stats["items_seen"],
                    stats["created"],
                    stats["updated"],
                    stats["duration_ms"],
                )
            except Exception as exc:
                errors += 1
                tenant_state["result"] = "error"
                tenant_state["error"] = repr(exc)
                logger.exception(
                    "[Salla Coupons Poller] tenant=%s store=%s error=%s",
                    tenant_id,
                    store_id,
                    exc,
                )
                try:
                    db.rollback()
                except Exception:
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
            except Exception:
                pass
        try:
            db.close()
        except Exception:
            pass


async def _poll_integration(db: Session, intg: Any) -> Dict[str, Any]:
    tenant_id = int(intg.tenant_id)
    started = time.monotonic()

    from store_integration.registry import adapter_for_integration  # noqa: PLC0415
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    adapter = adapter_for_integration(intg)
    svc = StoreSyncService(
        db,
        tenant_id,
        integration_connection_id=int(intg.id),
        adapter=adapter,
    )

    items_seen = 0
    api_error: Optional[str] = None
    try:
        adapter = svc._get_adapter()  # noqa: SLF001
        if adapter is not None and hasattr(adapter, "get_coupons"):
            raw_list = await adapter.get_coupons()
            items_seen = len(raw_list or [])
    except Exception as adapter_exc:
        api_error = repr(adapter_exc)
        logger.warning(
            "[Salla Coupons Poller] tenant=%s salla_api_response error=%s",
            tenant_id,
            adapter_exc,
        )

    upserted = await svc.sync_coupons(triggered_by="salla_coupons_poller")
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
    if not meta:
        updated = max(0, upserted - created)

    return {
        "items_seen": items_seen or int(meta.get("items_seen") or 0),
        "created": created,
        "updated": updated,
        "upserted": upserted,
        "api_error": api_error,
        "duration_ms": duration_ms,
    }
