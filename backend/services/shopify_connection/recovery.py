"""
Durable recovery of pending uninstall reconciliations.

``record_uninstall`` commits the event and a durable reconciliation request
(``reconcile_next_at``) before the webhook is acknowledged. The webhook
handler then tries one reconciliation in the background, but nothing depends
on that attempt: this runner drains every connection that is quarantined or
has a revalidation requested, whatever happened to the first attempt (worker
crash after the 200, transient Shopify failure, configuration unavailable at
that moment, duplicate redelivery).

Multi-worker safety
  * a due row is claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` and a short
    ``reconcile_lease_id`` / ``reconcile_lease_expires_at`` in one
    transaction, so competing workers never run the same connection at once;
  * a crashed worker's lease simply expires and the row becomes due again;
  * the reconciliation itself only writes through generation +
    credential-version compare-and-swap (``lifecycle.reconcile``), so a
    reinstall, disconnect or refresh that happens meanwhile supersedes it;
  * the schedule is updated only while the lease is still ours.

Backoff: ``30 s · 2^(attempts-1)``, capped at one hour. A quarantine is never
lifted by giving up: an unreachable Shopify keeps the credential unused
(fail safe) until a probe succeeds or the merchant reconnects.

Dormant by default: ``run_recovery_tick`` returns without touching the
database unless the feature flag **and** every required configuration item
(``config.evaluate_availability``) are present, and the scheduler loop is only
queued at startup under the same condition (``main.py``). It reads and writes
Shopify lifecycle tables only.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from services.shopify_connection import crypto, lifecycle
from services.shopify_connection.config import ShopifyConfig, evaluate_availability
from services.shopify_connection.models import STATUS_ACTIVE, STATUS_QUARANTINED, ShopifyConnection
from services.shopify_connection.oauth import ShopifyApi

logger = logging.getLogger("nahla.shopify_connection")

RECONCILE_LEASE_SECONDS = 120
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
TICK_SECONDS = 60
BATCH_LIMIT = 10


def backoff_seconds(attempts: int) -> int:
    attempts = max(1, int(attempts))
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(attempts - 1, 16)))


def _pending_clause(now: datetime):
    c = ShopifyConnection
    return and_(
        or_(c.status == STATUS_QUARANTINED,
            and_(c.status == STATUS_ACTIVE, c.revalidation_requested_at.isnot(None))),
        or_(c.reconcile_next_at.is_(None), c.reconcile_next_at <= now),
        or_(c.reconcile_lease_expires_at.is_(None), c.reconcile_lease_expires_at < now),
    )


def claim_due(db: Session, *, now: datetime, limit: int = BATCH_LIMIT,
              connection_id: Optional[int] = None) -> List[Tuple[int, str]]:
    """Claim up to *limit* due connections (optionally one specific id). Commits."""
    query = select(ShopifyConnection.id).where(_pending_clause(now))
    if connection_id is not None:
        query = query.where(ShopifyConnection.id == int(connection_id))
    query = (query.order_by(ShopifyConnection.reconcile_next_at.asc().nulls_first(), ShopifyConnection.id)
             .limit(int(limit)).with_for_update(skip_locked=True))
    ids = list(db.execute(query).scalars().all())
    claimed: List[Tuple[int, str]] = []
    for cid in ids:
        lease = secrets.token_hex(16)
        db.execute(
            update(ShopifyConnection).where(ShopifyConnection.id == cid).values(
                reconcile_lease_id=lease,
                reconcile_lease_expires_at=now + timedelta(seconds=RECONCILE_LEASE_SECONDS),
            ).execution_options(synchronize_session=False)
        )
        claimed.append((int(cid), lease))
    db.commit()
    return claimed


def _finalize(db: Session, connection_id: int, lease: str, claimed_at: datetime, now: datetime) -> None:
    """Reset or back off the schedule — only while our lease is still on the row."""
    conn = db.get(ShopifyConnection, connection_id, with_for_update=True, populate_existing=True)
    if conn is None or conn.reconcile_lease_id != lease:
        # Tombstoned, reinstalled or re-claimed after our lease expired.
        db.rollback()
        return
    still_pending = conn.status == STATUS_QUARANTINED or (
        conn.status == STATUS_ACTIVE and conn.revalidation_requested_at is not None)
    conn.reconcile_lease_id = None
    conn.reconcile_lease_expires_at = None
    if not still_pending:
        conn.reconcile_attempts = 0
        conn.reconcile_next_at = None
    elif conn.reconcile_next_at is not None and conn.reconcile_next_at > claimed_at:
        pass  # a new request arrived while we ran: stay due now, no backoff
    else:
        conn.reconcile_attempts = int(conn.reconcile_attempts or 0) + 1
        conn.reconcile_next_at = now + timedelta(seconds=backoff_seconds(conn.reconcile_attempts))
    db.commit()


async def run_claimed(
    db: Session,
    *,
    connection_id: int,
    lease: str,
    api: ShopifyApi,
    cipher: crypto.TokenCipher,
    claimed_at: datetime,
    now: Optional[datetime] = None,
) -> str:
    try:
        result = await lifecycle.reconcile(db, connection_id=connection_id, api=api, cipher=cipher, now=now)
    except Exception as exc:  # noqa: BLE001 — the quarantine stays in place (fail safe)
        logger.error("[SHOPIFY_CONNECTION] reconcile failed connection=%s kind=%s",
                     connection_id, type(exc).__name__)
        try:
            db.rollback()
        except Exception:  # noqa: silent-ok — best-effort rollback before the schedule update
            pass
        result = "deferred"
    _finalize(db, connection_id, lease, claimed_at, now or lifecycle.utcnow())
    return result


def _api_for(config: ShopifyConfig) -> ShopifyApi:
    return ShopifyApi(client_id=config.client_id, client_secret=config.client_secret,
                      api_version=config.api_version)


async def run_recovery_tick(
    *,
    session_factory: Optional[Callable[[], Session]] = None,
    env: Optional[Mapping[str, str]] = None,
    api: Optional[ShopifyApi] = None,
    now: Optional[datetime] = None,
    limit: int = BATCH_LIMIT,
    connection_id: Optional[int] = None,
) -> Dict[str, Any]:
    """One drain pass. Does nothing (no database access) unless available."""
    availability = evaluate_availability(env)
    if not availability.available:
        return {"skipped": availability.reason}
    config = availability.config
    if session_factory is None:
        from core.database import SessionLocal as session_factory  # noqa: PLC0415,N813
    db = session_factory()
    counts: Dict[str, Any] = {"claimed": 0}
    try:
        if not lifecycle.tables_present(db):
            return {"skipped": "storage_unavailable"}
        claimed_at = now or lifecycle.utcnow()
        claimed = claim_due(db, now=claimed_at, limit=limit, connection_id=connection_id)
        counts["claimed"] = len(claimed)
        cipher = crypto.TokenCipher(config.encryption_key)
        client = api or _api_for(config)
        for cid, lease in claimed:
            result = await run_claimed(db, connection_id=cid, lease=lease, api=client, cipher=cipher,
                                       claimed_at=claimed_at, now=now)
            counts[result] = counts.get(result, 0) + 1
        return counts
    finally:
        db.close()


async def reconcile_now(connection_id: int) -> Dict[str, Any]:
    """Webhook background attempt for one connection; the runner covers any miss."""
    try:
        return await run_recovery_tick(connection_id=connection_id, limit=1)
    except Exception as exc:  # noqa: BLE001 — the durable schedule remains
        logger.error("[SHOPIFY_CONNECTION] background reconcile failed kind=%s", type(exc).__name__)
        return {"error": type(exc).__name__}


async def run_recovery_scheduler() -> None:
    """Periodic drain. Each tick re-checks the flag and configuration first."""
    logger.info("[SHOPIFY_CONNECTION] uninstall recovery runner started — every %ss", TICK_SECONDS)
    while True:
        try:
            summary = await run_recovery_tick()
            if summary.get("claimed"):
                logger.info("[SHOPIFY_CONNECTION] uninstall recovery %s",
                            {k: v for k, v in summary.items() if isinstance(v, int)})
        except Exception as exc:  # noqa: BLE001
            logger.error("[SHOPIFY_CONNECTION] uninstall recovery tick failed kind=%s", type(exc).__name__)
        await asyncio.sleep(TICK_SECONDS)
