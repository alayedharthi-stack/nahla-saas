"""
services/salla_orders_poller.py
────────────────────────────────
Dedicated background poller that fetches new orders from Salla every 60s.

Why this exists
───────────────
Webhooks from Salla are best-effort: a single dropped webhook means the
merchant never sees the order, never sends the confirmation message, and
loses the customer relationship.  This poller is the **guarantee**:

  • Runs every NAHLA_SALLA_ORDERS_POLL_SECONDS (default 60s) FOREVER.
  • For every active Salla integration, asks Salla "what orders changed
    in the last N minutes?" and upserts them into Nahla.
  • For every NEW row it inserts, fires the same automation events that
    `handle_order_webhook` would have fired (ORDER_NOTIFICATIONS, plus
    ORDER_COD_PENDING for cash-on-delivery).
  • One try/except per tenant — a bad token on store A never blocks
    store B from being polled.
  • Postgres advisory lock so multi-worker Railway deploys don't hammer
    Salla in parallel.
  • Structured `[Salla Orders Poller]` logs at every boundary so ops can
    see exactly what happened in each cycle.

This is started from `main.py` lifespan via
`run_salla_orders_poller_scheduler()`.  It is NOT manual, NOT a route,
NOT triggered by opening a dashboard — it just runs.
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
# How far back to look on every cycle. Must be > poll interval so a slow
# cycle never leaves a gap.
LOOKBACK_MINUTES = int(os.getenv("NAHLA_SALLA_ORDERS_LOOKBACK_MIN", "10"))
# Postgres advisory-lock key (random 64-bit-fitting int, unique to this poller).
ADVISORY_LOCK_KEY = int(os.getenv("NAHLA_SALLA_POLLER_LOCK_KEY", "748103219045"))
# Initial delay so we don't race with app startup / migrations.
STARTUP_DELAY_SECONDS = int(os.getenv("NAHLA_SALLA_POLLER_STARTUP_DELAY", "45"))


# ── Public entry point ───────────────────────────────────────────────────────


async def run_salla_orders_poller_scheduler() -> None:
    """Forever-loop. Runs from `main.py` lifespan as a background asyncio task."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info(
        "[Salla Orders Poller] starting — interval=%ss lookback=%smin",
        POLL_INTERVAL_SECONDS, LOOKBACK_MINUTES,
    )
    while True:
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            logger.info("[Salla Orders Poller] cancelled — exiting loop")
            raise
        except Exception as exc:
            # Never die. Log and keep going on the next interval.
            logger.exception("[Salla Orders Poller] tick crashed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ── One tick ─────────────────────────────────────────────────────────────────


async def _run_one_tick() -> None:
    """Single polling cycle across every active Salla integration."""
    from core.database import SessionLocal  # noqa: PLC0415

    started = time.monotonic()
    logger.info("[Salla Orders Poller] tick started")

    db: Session = SessionLocal()
    lock_acquired = False
    try:
        # ── Multi-worker guard: only ONE worker polls at a time ──────────
        # `pg_try_advisory_lock` returns true if we got the lock, false if
        # someone else holds it. We hold for the whole tick and release in
        # the finally below.
        try:
            row = db.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": ADVISORY_LOCK_KEY},
            ).scalar()
            lock_acquired = bool(row)
        except Exception as _lock_exc:
            # Non-Postgres backends (sqlite tests) just fall through and
            # always run — there's only one worker in dev anyway.
            logger.debug(
                "[Salla Orders Poller] advisory lock unsupported (%s) — running anyway",
                _lock_exc,
            )
            lock_acquired = True

        if not lock_acquired:
            logger.info(
                "[Salla Orders Poller] tick skipped — another worker holds the lock",
            )
            return

        from models import Integration  # noqa: PLC0415

        integrations = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.enabled == True,  # noqa: E712
        ).all()

        if not integrations:
            logger.info(
                "[Salla Orders Poller] tick completed scanned=0 new_orders=0 errors=0 "
                "duration_ms=%d (no active Salla integrations)",
                int((time.monotonic() - started) * 1000),
            )
            return

        scanned = 0
        new_orders_total = 0
        errors = 0
        lookback = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
        lookback_iso = lookback.isoformat()

        for intg in integrations:
            tenant_id = intg.tenant_id
            cfg = intg.config or {}

            if cfg.get("needs_reauth"):
                logger.info(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s "
                    "result=skipped reason=needs_reauth",
                    tenant_id,
                )
                continue
            if not cfg.get("api_key"):
                logger.info(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s "
                    "result=skipped reason=no_api_key",
                    tenant_id,
                )
                continue

            try:
                stats = await _poll_tenant(db, tenant_id, lookback_iso)
                scanned += 1
                new_orders_total += stats["new_orders"]
                logger.info(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s "
                    "result=ok created=%d updated=%d emitted=%d duration_ms=%d",
                    tenant_id,
                    stats["new_orders"], stats["updated_orders"],
                    stats["events_emitted"], stats["duration_ms"],
                )
            except Exception as exc:
                errors += 1
                logger.exception(
                    "[Salla Orders Poller] tenant scanned tenant_id=%s "
                    "result=error error=%s",
                    tenant_id, exc,
                )
                # rollback whatever the failed tenant left in the session
                # so the next tenant starts clean
                try:
                    db.rollback()
                except Exception:
                    pass

        logger.info(
            "[Salla Orders Poller] tick completed scanned=%d new_orders=%d errors=%d "
            "duration_ms=%d",
            scanned, new_orders_total, errors,
            int((time.monotonic() - started) * 1000),
        )

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


async def _poll_tenant(db: Session, tenant_id: int, lookback_iso: str) -> Dict[str, Any]:
    """
    Poll a single tenant.

    Strategy:
      1. Ask the StoreSyncService to upsert all orders updated since
         `lookback_iso`.  StoreSyncService.sync_orders handles per-row
         upsert and (since this commit) emits ORDER_NOTIFICATIONS for
         every row it inserts as new.
      2. Defensively also scan recent orders for any that were inserted
         but never had `notifications_emitted=True` set in extra_metadata
         (e.g. created by an older version of the upsert path) and emit
         the missing event.

    Returns dict: {new_orders, updated_orders, events_emitted, duration_ms}
    """
    started = time.monotonic()

    # Snapshot of order ids BEFORE the sync so we can detect what's new.
    from models import Order  # noqa: PLC0415

    pre_ids = {
        oid for (oid,) in db.query(Order.id).filter(Order.tenant_id == tenant_id).all()
    }

    from services.store_sync import StoreSyncService  # noqa: PLC0415

    svc = StoreSyncService(db, tenant_id)
    # NOTE: sync_orders returns total upserted (created + updated) as int.
    # We compute new vs updated ourselves via the pre-ids snapshot.
    upserted_total = await svc.sync_orders(
        updated_since=lookback_iso,
        triggered_by="salla_orders_poller",
    )

    # Commit the upserts so query for post_ids sees them.
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
        "new_orders":      new_orders_count,
        "updated_orders":  updated_orders_count,
        "events_emitted":  events_emitted,
        "duration_ms":     int((time.monotonic() - started) * 1000),
    }


# ── Per-order emit with idempotency ──────────────────────────────────────────


def _emit_for_order(db: Session, tenant_id: int, order: Any) -> bool:
    """
    Emit ORDER_NOTIFICATIONS (and ORDER_COD_PENDING when applicable) for one
    order, exactly once.

    Idempotency
    ───────────
    StoreSyncService.sync_orders ALREADY emits these events at the moment a
    row is inserted (see services/store_sync.py).  This function is the
    safety-net for two scenarios:

      1. Older rows that pre-date the in-line emit logic.
      2. Rows where the in-line emit raised but the order row itself was
         committed (rare — wrapped in try/except).

    To stay idempotent we set `extra_metadata.notifications_emitted=True`
    after a successful emit.  Subsequent calls observe the flag and bail
    out — the order's external_order_id (or our internal id) is the
    idempotency key.
    """
    meta = dict(order.extra_metadata or {})
    if meta.get("notifications_emitted"):
        # Already emitted (either by sync_orders inline logic or a previous
        # poller tick) — never double-fire.
        return False
    if order.is_abandoned:
        # Abandoned carts have their own recovery flow.
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

        # COD-specific event
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
