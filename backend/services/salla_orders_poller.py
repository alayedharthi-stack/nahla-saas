"""
services/salla_orders_poller.py
────────────────────────────────
Dedicated background poller that fetches new orders from Salla every 60s.

Why this exists
───────────────
Webhooks from Salla are best-effort: a single dropped webhook means the
merchant never sees the order, never sends the confirmation message, and
loses the customer relationship.  This poller is the **guarantee**.

Diagnostics
───────────
Every step is logged with the `[Salla Orders Poller]` prefix and the
last cycle's full breakdown is exposed in-memory via
`get_poller_state()` so the admin diag endpoints can render it without
touching the DB or scraping logs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.salla_orders_poller")


# ── Configuration ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = int(os.getenv("NAHLA_SALLA_ORDERS_POLL_SECONDS", "60"))
# How far back to look on every cycle. Default raised to 24h while we
# verify production behaviour — once stable we can lower to ~10min.
LOOKBACK_MINUTES = int(os.getenv("NAHLA_SALLA_ORDERS_LOOKBACK_MIN", "1440"))
# Postgres advisory-lock key (random 64-bit-fitting int, unique to this poller).
ADVISORY_LOCK_KEY = int(os.getenv("NAHLA_SALLA_POLLER_LOCK_KEY", "748103219045"))
# Initial delay so we don't race with app startup / migrations.
STARTUP_DELAY_SECONDS = int(os.getenv("NAHLA_SALLA_POLLER_STARTUP_DELAY", "45"))
# Hard kill switch (default off — never set this in prod unless triaging).
DISABLED = os.getenv("NAHLA_SALLA_POLLER_DISABLED", "").lower() in ("1", "true", "yes")


# ── In-memory diagnostic state ────────────────────────────────────────────────
# Updated by every tick / per-tenant scan so the admin diag endpoint can
# render the current health without scraping logs.
_state: Dict[str, Any] = {
    "started_at":            None,
    "last_tick_at":          None,
    "last_tick_duration_ms": None,
    "last_tick_scanned":     0,
    "last_tick_new_orders":  0,
    "last_tick_errors":      0,
    "last_tick_skipped_reason": None,   # set when advisory lock denied
    "ticks_total":           0,
    "tenants":               {},        # tenant_id → last per-tenant scan stats
    "config": {
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "lookback_minutes":      LOOKBACK_MINUTES,
        "advisory_lock_key":     ADVISORY_LOCK_KEY,
        "startup_delay_seconds": STARTUP_DELAY_SECONDS,
        "disabled":              DISABLED,
    },
}


def get_poller_state() -> Dict[str, Any]:
    """Return a deep-ish copy of the poller's in-memory state for diag UI."""
    return {
        **_state,
        "tenants": {tid: dict(stats) for tid, stats in _state["tenants"].items()},
        "config":  dict(_state["config"]),
    }


# ── Public entry point ───────────────────────────────────────────────────────


async def run_salla_orders_poller_scheduler() -> None:
    """Forever-loop. Runs from `main.py` lifespan as a background asyncio task."""
    if DISABLED:
        logger.warning(
            "[Salla Orders Poller] DISABLED via NAHLA_SALLA_POLLER_DISABLED — "
            "exiting without scheduling any ticks",
        )
        return

    _state["started_at"] = datetime.now(timezone.utc).isoformat()
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info(
        "[Salla Orders Poller] starting — interval=%ss lookback=%smin advisory_lock_key=%s",
        POLL_INTERVAL_SECONDS, LOOKBACK_MINUTES, ADVISORY_LOCK_KEY,
    )
    while True:
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            logger.info("[Salla Orders Poller] cancelled — exiting loop")
            raise
        except Exception as exc:
            logger.exception("[Salla Orders Poller] tick crashed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ── One tick ─────────────────────────────────────────────────────────────────


async def _run_one_tick() -> Dict[str, Any]:
    """Single polling cycle across every active Salla integration."""
    from core.database import SessionLocal  # noqa: PLC0415

    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    logger.info("[Salla Orders Poller] tick started at=%s", started_at.isoformat())

    db: Session = SessionLocal()
    lock_acquired = False
    try:
        try:
            row = db.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": ADVISORY_LOCK_KEY},
            ).scalar()
            lock_acquired = bool(row)
        except Exception as _lock_exc:
            logger.debug(
                "[Salla Orders Poller] advisory lock unsupported (%s) — running anyway",
                _lock_exc,
            )
            lock_acquired = True

        if not lock_acquired:
            logger.info(
                "[Salla Orders Poller] tick skipped — another worker holds the lock",
            )
            _state["last_tick_at"]            = started_at.isoformat()
            _state["last_tick_skipped_reason"] = "advisory_lock_held_by_other_worker"
            _state["ticks_total"]            += 1
            return {"skipped": True, "reason": "advisory_lock_held_by_other_worker"}

        from models import Integration  # noqa: PLC0415
        from store_integration.registry import adapter_for_integration  # noqa: PLC0415

        total_salla = db.query(Integration).filter(
            Integration.provider == "salla",
        ).count()

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

        tenant_ids = sorted({int(intg.tenant_id) for intg in integrations if intg.tenant_id})

        logger.info(
            "[Salla Orders Poller] active integrations found count=%d "
            "tenants_with_salla=%d total_salla_rows=%d",
            len(integrations), len(tenant_ids), total_salla,
        )

        scanned = 0
        new_orders_total = 0
        errors = 0
        lookback = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
        lookback_iso = lookback.isoformat()

        for intg in integrations:
            tenant_id = int(intg.tenant_id)
            cfg = intg.config or {}
            store_id      = cfg.get("store_id") or cfg.get("merchant_id") or intg.external_store_id
            api_key       = cfg.get("api_key", "")
            needs_reauth  = bool(cfg.get("needs_reauth"))

            tenant_state: Dict[str, Any] = {
                "tenant_id":     tenant_id,
                "integration_id": intg.id,
                "store_id":      store_id,
                "token_present": bool(api_key),
                "needs_reauth":  needs_reauth,
                "scanned_at":    datetime.now(timezone.utc).isoformat(),
                "result":        None,
                "error":         None,
                "duration_ms":   None,
                "stats":         None,
                "lookback_iso":  lookback_iso,
            }

            if needs_reauth:
                logger.info(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s store_id=%s "
                    "result=skipped reason=needs_reauth",
                    tenant_id, store_id,
                )
                tenant_state["result"] = "skipped_needs_reauth"
                _state["tenants"][tenant_id] = tenant_state
                continue
            if not api_key:
                logger.info(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s store_id=%s "
                    "result=skipped reason=no_api_key",
                    tenant_id, store_id,
                )
                tenant_state["result"] = "skipped_no_api_key"
                _state["tenants"][tenant_id] = tenant_state
                continue

            try:
                stats = await _poll_integration(db, intg, lookback_iso)
                scanned += 1
                new_orders_total += stats["new_orders"]
                tenant_state.update({
                    "result":      "ok",
                    "stats":       stats,
                    "duration_ms": stats["duration_ms"],
                })
                logger.info(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s store_id=%s "
                    "result=ok api_returned=%d created=%d updated=%d emitted=%d duration_ms=%d",
                    tenant_id, store_id,
                    stats["api_returned"],
                    stats["new_orders"], stats["updated_orders"],
                    stats["events_emitted"], stats["duration_ms"],
                )
            except Exception as exc:
                errors += 1
                tenant_state["result"] = "error"
                tenant_state["error"]  = repr(exc)
                logger.exception(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s store_id=%s "
                    "result=error error=%s",
                    tenant_id, store_id, exc,
                )
                try:
                    db.rollback()
                except Exception:
                    pass

            _state["tenants"][tenant_id] = tenant_state

        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "[Salla Orders Poller] tick completed scanned=%d new_orders=%d errors=%d "
            "duration_ms=%d",
            scanned, new_orders_total, errors, duration_ms,
        )

        _state["last_tick_at"]           = started_at.isoformat()
        _state["last_tick_duration_ms"]  = duration_ms
        _state["last_tick_scanned"]      = scanned
        _state["last_tick_new_orders"]   = new_orders_total
        _state["last_tick_errors"]       = errors
        _state["last_tick_skipped_reason"] = None
        _state["ticks_total"]           += 1

        return {
            "skipped":       False,
            "scanned":       scanned,
            "new_orders":    new_orders_total,
            "errors":        errors,
            "duration_ms":   duration_ms,
            "integrations":  len(integrations),
            "lookback_iso":  lookback_iso,
        }

    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": ADVISORY_LOCK_KEY},
                )
                db.commit()
            except Exception:
                pass
        try:
            db.close()
        except Exception:
            pass


# ── Per-tenant work ──────────────────────────────────────────────────────────


async def _poll_integration(db: Session, intg: Any, lookback_iso: str) -> Dict[str, Any]:
    """Poll a single integration connection; returns rich stats dict."""
    tenant_id = int(intg.tenant_id)
    started = time.monotonic()

    from models import Order  # noqa: PLC0415
    from store_integration.registry import adapter_for_integration  # noqa: PLC0415

    pre_ids = {
        oid for (oid,) in db.query(Order.id).filter(Order.tenant_id == tenant_id).all()
    }

    from services.store_sync import StoreSyncService  # noqa: PLC0415

    adapter = adapter_for_integration(intg)
    svc = StoreSyncService(
        db,
        tenant_id,
        integration_connection_id=int(intg.id),
        adapter=adapter,
    )

    api_returned = -1  # -1 means "did not capture"
    upserted_total = 0
    api_error: Optional[str] = None
    needs_reauth_raised = False

    # We want to also know how many rows the Salla API actually returned
    # for this lookback window — so we call adapter.get_orders directly
    # and then hand the list-shaped sync to sync_orders for upserting.
    try:
        adapter = svc._get_adapter()  # noqa: SLF001
        if adapter is not None:
            try:
                raw_list = await adapter.get_orders(updated_since=lookback_iso)
                api_returned = len(raw_list or [])
                logger.info(
                    "[Salla Orders Poller] tenant=%s salla_api_response orders_returned=%d "
                    "lookback=%s",
                    tenant_id, api_returned, lookback_iso,
                )
            except Exception as adapter_exc:
                # Detect SallaTokenRevokedException without importing it
                # (avoids a hard import-cycle here).
                exc_name = type(adapter_exc).__name__
                if exc_name == "SallaTokenRevokedException":
                    needs_reauth_raised = True
                    api_error = "needs_reauth: token refresh failed or revoked"
                    logger.warning(
                        "[Salla Orders Poller] tenant=%s sync stopped — needs_reauth (%s)",
                        tenant_id, adapter_exc,
                    )
                else:
                    api_error = repr(adapter_exc)
                    logger.warning(
                        "[Salla Orders Poller] tenant=%s salla_api_response error=%s",
                        tenant_id, adapter_exc,
                    )
    except Exception:
        pass

    if not needs_reauth_raised:
        try:
            upserted_total = await svc.sync_orders(
                updated_since=lookback_iso,
                triggered_by="salla_orders_poller",
            )
        except Exception as sync_exc:
            exc_name = type(sync_exc).__name__
            if exc_name == "SallaTokenRevokedException":
                needs_reauth_raised = True
                api_error = api_error or "needs_reauth: token refresh failed or revoked"
                logger.warning(
                    "[Salla Orders Poller] tenant=%s sync_orders stopped — needs_reauth",
                    tenant_id,
                )
            else:
                raise

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    post_rows = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id)
        .all()
    )
    post_ids = {o.id for o in post_rows}
    new_ids = post_ids - pre_ids

    new_orders_count = len(new_ids)
    updated_orders_count = max(0, upserted_total - new_orders_count)

    events_emitted = 0
    if new_ids:
        for o in post_rows:
            if o.id in new_ids:
                logger.info(
                    "[Salla Orders Poller] new order detected tenant_id=%s order_id=%s "
                    "external_id=%s status=%s",
                    tenant_id, o.id, o.external_id, o.status,
                )
                if _emit_for_order(db, tenant_id, o):
                    events_emitted += 1

    return {
        "api_returned":       api_returned,
        "api_error":          api_error,
        "needs_reauth":       needs_reauth_raised,
        "upserted_total":     upserted_total,
        "new_orders":         new_orders_count,
        "updated_orders":     updated_orders_count,
        "events_emitted":     events_emitted,
        "pre_orders_in_db":   len(pre_ids),
        "post_orders_in_db":  len(post_ids),
        "duration_ms":        int((time.monotonic() - started) * 1000),
    }


# ── Per-order emit with idempotency ──────────────────────────────────────────


def _emit_for_order(db: Session, tenant_id: int, order: Any) -> bool:
    """Emit ORDER_NOTIFICATIONS (and ORDER_COD_PENDING when COD) exactly once."""
    meta = dict(order.extra_metadata or {})
    if meta.get("notifications_emitted"):
        return False
    if order.is_abandoned:
        return False

    try:
        from core.automation_engine   import emit_automation_event   # noqa: PLC0415
        from core.automation_triggers import AutomationTrigger        # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified            # noqa: PLC0415

        pm     = str(meta.get("payment_method") or "").lower()
        status = str(order.status or "").lower()
        ext_id = order.external_id

        emit_automation_event(
            db, tenant_id,
            AutomationTrigger.ORDER_NOTIFICATIONS.value,
            payload={
                "external_id":           ext_id,
                "order_id":              order.id,
                "order_internal_id":     order.id,
                "status":                status,
                "total":                 order.total,
                "order_number":          order.external_order_number or ext_id,
                "external_order_number": order.external_order_number,
                "checkout_url":          order.checkout_url or "",
                "payment_url":           order.checkout_url or "",
                "payment_method":        pm,
                "source":                "salla_orders_poller",
            },
            commit=False,
        )
        logger.info(
            "[Salla Orders Poller] ORDER_NOTIFICATIONS emitted tenant_id=%s order_id=%s",
            tenant_id, order.id,
        )

        cod_methods = {"cod", "cash_on_delivery", "cash", "الدفع عند الاستلام"}
        is_cod = bool(pm and any(pm == m or m in pm for m in cod_methods))
        if is_cod:
            emit_automation_event(
                db, tenant_id,
                AutomationTrigger.ORDER_COD_PENDING.value,
                payload={
                    "external_id":           ext_id,
                    "order_id":              order.id,
                    "order_number":          order.external_order_number or ext_id,
                    "status":                status,
                    "total":                 order.total,
                    "payment_method":        pm,
                    "checkout_url":          order.checkout_url or "",
                    "payment_url":           order.checkout_url or "",
                    "source":                "salla_orders_poller",
                    "step_idx":              0,
                    "message_type":          "initial_confirmation",
                },
                commit=False,
            )
            logger.info(
                "[Salla Orders Poller] ORDER_COD_PENDING emitted tenant_id=%s order_id=%s",
                tenant_id, order.id,
            )

        meta["notifications_emitted"]    = True
        meta["notifications_emitted_at"] = datetime.now(timezone.utc).isoformat()
        meta["notifications_emitted_by"] = "salla_orders_poller"
        order.extra_metadata = meta
        flag_modified(order, "extra_metadata")
        db.commit()
        return True
    except Exception as exc:
        logger.exception(
            "[Salla Orders Poller] emit failed tenant_id=%s order_id=%s: %s",
            tenant_id, getattr(order, "id", "?"), exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False


# ── Manual run-once entry point (for /admin/salla/orders-poller/run-once) ────


async def run_once_for_tenant(
    tenant_id: int,
    lookback_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Poll every enabled Salla integration for a tenant (per-connection).

    Does not use pick_active — each integration row is polled explicitly.
    """
    from core.database import SessionLocal  # noqa: PLC0415
    from models import Integration  # noqa: PLC0415
    from store_integration.registry import adapter_for_integration  # noqa: PLC0415

    db: Session = SessionLocal()
    try:
        integrations = (
            db.query(Integration)
            .filter(
                Integration.tenant_id == int(tenant_id),
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

        if not integrations:
            logger.warning(
                "[Salla Orders Poller] run_once tenant_id=%s — no pollable Salla integrations",
                tenant_id,
            )
            return {
                "ok": False,
                "reason": "no_salla_integration",
                "tenant_id": tenant_id,
            }

        lb_min = lookback_minutes if lookback_minutes is not None else LOOKBACK_MINUTES
        lookback = datetime.now(timezone.utc) - timedelta(minutes=lb_min)
        lookback_iso = lookback.isoformat()

        per_integration: list[Dict[str, Any]] = []
        errors = 0
        scanned = 0
        new_orders_total = 0

        for intg in integrations:
            cfg = intg.config or {}
            ctx = {
                "tenant_id":             tenant_id,
                "integration_id":        intg.id,
                "enabled":               intg.enabled,
                "store_id":              cfg.get("store_id") or cfg.get("merchant_id"),
                "external_store_id":     getattr(intg, "external_store_id", None),
                "token_present":         bool(cfg.get("api_key")),
                "refresh_token_present": bool(cfg.get("refresh_token")),
                "needs_reauth":          bool(cfg.get("needs_reauth")),
            }
            try:
                stats = await _poll_integration(db, intg, lookback_iso)
                scanned += 1
                new_orders_total += int(stats.get("new_orders") or 0)
                per_integration.append({"ok": True, **ctx, **stats})
            except Exception as exc:
                errors += 1
                logger.exception(
                    "[Salla Orders Poller] run_once tenant_id=%s integration_id=%s — %s",
                    tenant_id, intg.id, exc,
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                per_integration.append({"ok": False, "reason": "exception", "error": repr(exc), **ctx})

        result = {
            "ok": errors == 0,
            "lookback_minutes": lb_min,
            "lookback_iso": lookback_iso,
            "tenant_id": tenant_id,
            "integrations_polled": len(per_integration),
            "scanned": scanned,
            "new_orders": new_orders_total,
            "errors": errors,
            "per_integration": per_integration,
        }
        logger.info(
            "[Salla Orders Poller] run_once done tenant_id=%s result=%s",
            tenant_id, result,
        )
        _state["tenants"][tenant_id] = {
            "tenant_id":     tenant_id,
            "scanned_at":    datetime.now(timezone.utc).isoformat(),
            "result":        "ok_run_once",
            "integrations_polled": len(per_integration),
            "new_orders":    new_orders_total,
            "lookback_iso":  lookback_iso,
        }
        return result
    finally:
        try:
            db.close()
        except Exception:
            pass
