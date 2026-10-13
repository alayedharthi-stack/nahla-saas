"""Erase expired install secrets even when all Shopify capability gates are off.

This local housekeeping path never reads configuration/keys, constructs an API
client, refreshes tokens, creates tables or writes outside the install flows.
Uncertain exchange phases remain durable until gated recovery reconciles them.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, inspect, or_, select

from services.shopify_connection.install_models import ShopifyInstallFlow as Flow

logger = logging.getLogger("nahla.shopify_connection")
UNCERTAIN = ("exchanging_online", "exchanging_offline")
RETENTION_SECONDS = 900
BATCH_LIMIT = 100
TICK_SECONDS = 60


def erase_expired(db, *, now=None, limit=BATCH_LIMIT):
    now = now or datetime.now(timezone.utc)
    retention = now - timedelta(seconds=RETENTION_SECONDS)
    # Rows already scrubbed but still uncertain must not starve later rows.
    needs_work = or_(Flow.online_token_enc.isnot(None), Flow.handoff_hash.isnot(None),
                     and_(Flow.phase.notin_(UNCERTAIN),
                          or_(Flow.phase != "failed", Flow.created_at <= retention)))
    rows = db.execute(select(Flow).where(Flow.expires_at <= now, needs_work)
                      .order_by(Flow.expires_at, Flow.id).limit(max(1, min(int(limit), BATCH_LIMIT)))
                      .with_for_update(skip_locked=True)).scalars().all()
    for row in rows:
        row.online_token_enc = row.handoff_hash = None
        if row.phase not in UNCERTAIN:
            row.phase = "failed"
            if row.created_at <= retention:
                db.delete(row)
    db.commit()
    return len(rows)


async def run_cleanup_tick(*, session_factory=None, now=None, limit=BATCH_LIMIT):
    if session_factory is None:
        from core.database import SessionLocal as session_factory  # noqa: PLC0415,N813
    with session_factory() as db:
        # No startup DDL, including on a pre-0124 database. Bound catalog/read
        # and DML waits independently of the capability configuration.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(select(func.set_config("lock_timeout", "250ms", True)))
            db.execute(select(func.set_config("statement_timeout", "1000ms", True)))
        if not inspect(db.connection()).has_table(Flow.__tablename__):
            return {"skipped": "storage_unavailable"}
        return {"erased": erase_expired(db, now=now, limit=limit)}


async def run_cleanup_scheduler():
    while True:
        try:
            summary = await run_cleanup_tick()
            if summary.get("erased"):
                logger.info("[SHOPIFY_CONNECTION] expired install cleanup rows=%s", summary["erased"])
        except Exception as exc:  # noqa: BLE001
            logger.error("[SHOPIFY_CONNECTION] install cleanup failed kind=%s", type(exc).__name__)
        await asyncio.sleep(TICK_SECONDS)
