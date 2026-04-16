"""
core/webhook_dispatcher.py
──────────────────────────
Async worker that drains the `webhook_events` table.

For each claimed row we invoke a provider-specific handler that performs the
actual business side-effects (tenant resolution, order upsert, coupon
generation, OAuth token persistence, ...). Success → status='processed'.
Failure → status='failed' with exponential backoff, eventually 'dead_letter'.

Run from `main.py` lifespan:

    from core.webhook_dispatcher import run_dispatcher_loop
    asyncio.create_task(run_dispatcher_loop())
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from core.obs import EVENTS, log_event
from core.webhook_events import (
    claim_next_batch,
    mark_failed,
    mark_processed,
)

logger = logging.getLogger("nahla.webhook_dispatcher")


# ── Configuration ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = float(os.getenv("NAHLA_DISPATCHER_POLL_SECONDS", "2"))
BATCH_SIZE = int(os.getenv("NAHLA_DISPATCHER_BATCH_SIZE", "25"))


# ── Salla-specific dispatch ───────────────────────────────────────────────────


async def _dispatch_salla(db: Session, event) -> None:
    """
    Execute the business handler for a single Salla webhook_event row.

    Raises on failure so mark_failed(...) records the error.
    """
    from routers.webhooks import (  # noqa: PLC0415
        _handle_salla_authorize,
        _disable_salla_integration,
        _resolve_tenant_from_store,
    )

    payload = event.parsed_payload or {}
    if not isinstance(payload, dict):
        raise RuntimeError("parsed_payload_missing_or_invalid")

    event_type = event.event_type or payload.get("event") or "unknown"
    store_id = event.store_id or str(payload.get("merchant") or payload.get("store_id") or "")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    # ── OAuth / install / uninstall — don't need tenant to exist yet ────────
    if event_type in ("app.store.authorize", "app.store.token", "app.installed"):
        await _handle_salla_authorize(db, store_id, data, payload)
        return

    if event_type == "app.uninstalled":
        _disable_salla_integration(db, str(store_id))
        return

    # ── Event types that need a mapped tenant ───────────────────────────────
    tenant_id = _resolve_tenant_from_store(db, store_id)
    if tenant_id is None:
        # This is a real failure that will retry on backoff. If the merchant
        # is integrating right now the authorize row will land soon and the
        # retry will succeed. If not, it lands in DLQ and admin can investigate.
        log_event(
            EVENTS.DISPATCHER_TENANT_UNRESOLVED,
            provider="salla",
            store_id=store_id,
            event_type=event_type,
            webhook_event_id=event.id,
        )
        raise RuntimeError(f"tenant_unresolved: store_id={store_id}")

    # Stamp tenant_id on the event for observability / admin UI.
    if event.tenant_id != tenant_id:
        event.tenant_id = tenant_id
        db.add(event)
        db.flush()

    from services.store_sync import StoreSyncService  # noqa: PLC0415
    svc = StoreSyncService(db, tenant_id)

    if event_type in ("order.created", "order.updated"):
        await svc.handle_order_webhook(data)
        return

    if event_type in ("product.created", "product.updated"):
        await svc.handle_product_webhook(data)
        return

    if event_type == "product.deleted":
        await svc.handle_product_deleted(str(data.get("id", "")))
        return

    if event_type in ("customer.created", "customer.updated"):
        await svc.handle_customer_webhook(data)
        return

    # Unknown event — mark processed so it does not retry forever.
    logger.info(
        "[Dispatcher] Unhandled salla event_type=%s webhook_event_id=%s — marking processed",
        event_type, event.id,
    )


# ── Provider registry ─────────────────────────────────────────────────────────

_DISPATCHERS: Dict[str, Callable[[Session, Any], Any]] = {
    "salla": _dispatch_salla,
}


# ── Main loop ─────────────────────────────────────────────────────────────────


async def _process_event(db: Session, event) -> None:
    """Dispatch one event; on exception, mark_failed; on success, mark_processed."""
    dispatcher = _DISPATCHERS.get(event.provider)
    if dispatcher is None:
        # Unknown provider — mark as dead letter since we'll never know how to handle it.
        mark_failed(db, event, f"unknown_provider:{event.provider}")
        # Force straight to DLQ by re-failing until exhausted? No — simpler:
        # just log and bail; remaining retries will all fail the same way.
        return

    try:
        result = dispatcher(db, event)
        if asyncio.iscoroutine(result):
            await result
    except BaseException as exc:
        try:
            db.rollback()
        except Exception:
            pass
        mark_failed(db, event, exc)
        return

    try:
        mark_processed(db, event)
    except Exception as exc:
        # Could not record completion — rare, but still treat as retryable.
        try:
            db.rollback()
        except Exception:
            pass
        mark_failed(db, event, exc)


async def _run_one_batch() -> int:
    """Claim and process a single batch. Returns number processed."""
    from core.database import SessionLocal  # noqa: PLC0415

    db: Session = SessionLocal()
    try:
        events = claim_next_batch(db, limit=BATCH_SIZE)
        if not events:
            return 0
        for ev in events:
            # Each event gets its own fresh-start transaction boundary: after
            # processing we commit (mark_processed) or rollback (mark_failed).
            try:
                await _process_event(db, ev)
            except Exception as exc:
                logger.exception(
                    "[Dispatcher] Unhandled error processing event id=%s: %s",
                    ev.id, exc,
                )
                try:
                    mark_failed(db, ev, exc)
                except Exception:
                    logger.exception(
                        "[Dispatcher] Could not even mark_failed on event id=%s", ev.id
                    )
        return len(events)
    finally:
        try:
            db.close()
        except Exception:
            pass


async def run_dispatcher_loop() -> None:
    """
    Forever-loop that drives the dispatcher. Errors at the loop level are
    logged and the loop continues — this must never die silently.
    """
    logger.info(
        "[Dispatcher] starting: poll_interval=%.2fs batch_size=%s",
        POLL_INTERVAL_SECONDS, BATCH_SIZE,
    )
    while True:
        try:
            processed = await _run_one_batch()
            if processed == 0:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            # If we processed a full batch, loop again immediately so we can
            # drain a backlog quickly.
            elif processed < BATCH_SIZE:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("[Dispatcher] cancelled — exiting loop.")
            raise
        except Exception as exc:
            log_event(
                EVENTS.DISPATCHER_LOOP_ERROR,
                err=exc,
            )
            # Slow down on catastrophic failures so we don't spin.
            await asyncio.sleep(max(POLL_INTERVAL_SECONDS * 5, 10))
