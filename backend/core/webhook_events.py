"""
core/webhook_events.py
──────────────────────
Durable inbound-webhook queue.

Every provider webhook endpoint calls `persist_event(...)` before doing any
business work. The async dispatcher (`core/webhook_dispatcher.py`) later
claims rows and invokes the per-provider handler, advancing the row through
the `received → processing → processed | failed | dead_letter` FSM.

Design goals:
  • Zero silent failures — a `received` row always reaches a terminal state.
  • Idempotent re-processing on retry.
  • Admin-visible DLQ.
  • Works with Postgres SKIP LOCKED and falls back gracefully elsewhere.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import WebhookEvent  # noqa: E402
from core.obs import EVENTS, log_event

logger = logging.getLogger("nahla.webhook_events")


# ── Configuration ─────────────────────────────────────────────────────────────
# Exponential backoff for the dispatcher: attempts 1..N wait this many seconds
# before the next retry. After the last entry → dead_letter.
BACKOFF_SECONDS: tuple[int, ...] = (
    60,        # 1 min
    5 * 60,    # 5 min
    15 * 60,   # 15 min
    60 * 60,   # 1 h
    6 * 60 * 60,   # 6 h
)
MAX_ATTEMPTS = len(BACKOFF_SECONDS)

# Heartbeat window: any row stuck in `processing` for longer than this is
# considered abandoned (worker died) and reclaimable.
PROCESSING_STALE_SECONDS = 15 * 60


# ── Enum-like constants ───────────────────────────────────────────────────────
STATUS_RECEIVED    = "received"
STATUS_PROCESSING  = "processing"
STATUS_PROCESSED   = "processed"
STATUS_FAILED      = "failed"
STATUS_DEAD_LETTER = "dead_letter"

TERMINAL_STATUSES = (STATUS_PROCESSED, STATUS_DEAD_LETTER)


# ── Core API ──────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json(value: Any) -> Optional[dict]:
    if value is None:
        return None
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return None


def _headers_dict(headers: Any) -> dict:
    """Starlette Headers → dict, skipping values that are non-serialisable."""
    out: Dict[str, str] = {}
    try:
        for key, val in dict(headers).items():
            try:
                out[str(key)] = str(val)
            except Exception:
                continue
    except Exception:
        pass
    return out


def persist_event(
    db: Session,
    *,
    provider: str,
    raw_body: bytes | str | None,
    headers: Any = None,
    parsed_payload: Optional[dict] = None,
    event_type: Optional[str] = None,
    external_event_id: Optional[str] = None,
    store_id: Optional[str] = None,
    tenant_id: Optional[int] = None,
    signature_valid: Optional[bool] = None,
    initial_status: str = STATUS_RECEIVED,
    initial_error: Optional[str] = None,
) -> WebhookEvent:
    """
    Persist a webhook event and COMMIT immediately. Returns the row.

    `initial_status=received`   — ready for the dispatcher.
    `initial_status=failed`     — persisted but skipped (e.g. invalid JSON,
                                  invalid signature). Admin can replay.
    """
    if isinstance(raw_body, bytes):
        try:
            raw_body_str = raw_body.decode("utf-8", errors="replace")
        except Exception:
            raw_body_str = None
    else:
        raw_body_str = raw_body

    now = _utcnow()
    ev = WebhookEvent(
        tenant_id=tenant_id,
        provider=provider,
        event_type=event_type,
        external_event_id=str(external_event_id) if external_event_id else None,
        store_id=str(store_id) if store_id else None,
        raw_headers=_headers_dict(headers) if headers is not None else None,
        raw_body=raw_body_str,
        parsed_payload=_safe_json(parsed_payload),
        signature_valid=signature_valid,
        status=initial_status,
        attempts=0,
        last_error=initial_error,
        last_error_at=now if initial_error else None,
        next_retry_at=now if initial_status == STATUS_RECEIVED else None,
        received_at=now,
    )
    try:
        db.add(ev)
        db.commit()
    except IntegrityError as exc:
        # Duplicate (provider, external_event_id) — same event already stored.
        db.rollback()
        existing = (
            db.query(WebhookEvent)
            .filter(
                WebhookEvent.provider == provider,
                WebhookEvent.external_event_id == (str(external_event_id) if external_event_id else None),
            )
            .first()
        )
        if existing is not None:
            log_event(
                EVENTS.WEBHOOK_PERSISTED,
                webhook_event_id=existing.id,
                provider=provider,
                event_type=event_type,
                external_event_id=external_event_id,
                store_id=store_id,
                duplicate=True,
            )
            return existing
        # If we couldn't recover, log and re-raise so the caller knows.
        log_event(
            EVENTS.WEBHOOK_PERSIST_FAILED,
            provider=provider,
            event_type=event_type,
            err=exc,
        )
        raise
    except Exception as exc:
        db.rollback()
        log_event(
            EVENTS.WEBHOOK_PERSIST_FAILED,
            provider=provider,
            event_type=event_type,
            err=exc,
        )
        raise

    log_event(
        EVENTS.WEBHOOK_PERSISTED,
        webhook_event_id=ev.id,
        provider=provider,
        event_type=event_type,
        external_event_id=external_event_id,
        store_id=store_id,
        tenant_id=tenant_id,
        signature_valid=signature_valid,
        initial_status=initial_status,
    )
    return ev


# ── Dispatcher helpers ────────────────────────────────────────────────────────


def _is_postgres(db: Session) -> bool:
    try:
        return db.bind.dialect.name == "postgresql"
    except Exception:
        return False


def claim_next_batch(db: Session, *, limit: int = 25) -> List[WebhookEvent]:
    """
    Atomically claim up to `limit` events for processing.

    On Postgres we use FOR UPDATE SKIP LOCKED so multiple workers are safe.
    Elsewhere (sqlite, tests) we fall back to a non-atomic update that is
    good enough for a single-worker test run.

    Also reclaims rows stuck in `processing` for longer than
    PROCESSING_STALE_SECONDS (worker died while holding them).
    """
    now = _utcnow()
    stale_before = now - timedelta(seconds=PROCESSING_STALE_SECONDS)

    if _is_postgres(db):
        sql = sa_text(
            """
            WITH claimed AS (
                SELECT id
                FROM webhook_events
                WHERE (
                        status IN ('received', 'failed')
                        AND (next_retry_at IS NULL OR next_retry_at <= :now)
                      )
                   OR (
                        status = 'processing'
                        AND updated_at < :stale
                      )
                ORDER BY received_at
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
            )
            UPDATE webhook_events
            SET status = 'processing',
                updated_at = :now
            FROM claimed
            WHERE webhook_events.id = claimed.id
            RETURNING webhook_events.id
            """
        )
        rows = db.execute(sql, {"now": now, "stale": stale_before, "lim": limit}).fetchall()
        db.commit()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        events = (
            db.query(WebhookEvent)
            .filter(WebhookEvent.id.in_(ids))
            .order_by(WebhookEvent.received_at.asc())
            .all()
        )
    else:
        q = (
            db.query(WebhookEvent)
            .filter(
                (
                    (WebhookEvent.status.in_((STATUS_RECEIVED, STATUS_FAILED)))
                    & ((WebhookEvent.next_retry_at == None) | (WebhookEvent.next_retry_at <= now))  # noqa: E711
                )
                | (
                    (WebhookEvent.status == STATUS_PROCESSING)
                    & (WebhookEvent.updated_at < stale_before)
                )
            )
            .order_by(WebhookEvent.received_at.asc())
            .limit(limit)
        )
        events = q.all()
        for ev in events:
            ev.status = STATUS_PROCESSING
            ev.updated_at = now
        db.commit()

    for ev in events:
        log_event(
            EVENTS.EVENT_CLAIMED,
            webhook_event_id=ev.id,
            provider=ev.provider,
            event_type=ev.event_type,
            attempts=ev.attempts,
        )
    return events


def mark_processed(db: Session, event: WebhookEvent) -> None:
    now = _utcnow()
    event.status = STATUS_PROCESSED
    event.processed_at = now
    event.updated_at = now
    event.last_error = None
    db.add(event)
    db.commit()
    log_event(
        EVENTS.EVENT_DISPATCHED,
        webhook_event_id=event.id,
        provider=event.provider,
        event_type=event.event_type,
        attempts=event.attempts,
        status=STATUS_PROCESSED,
    )


def mark_failed(db: Session, event: WebhookEvent, err: BaseException | str) -> None:
    """
    Increment attempts and schedule next retry — or dead_letter if exhausted.
    """
    now = _utcnow()
    event.attempts = int(event.attempts or 0) + 1
    event.last_error = _format_error(err)[:4000]
    event.last_error_at = now
    event.updated_at = now

    if event.attempts >= MAX_ATTEMPTS:
        event.status = STATUS_DEAD_LETTER
        event.next_retry_at = None
        db.add(event)
        db.commit()
        log_event(
            EVENTS.EVENT_DEAD_LETTER,
            webhook_event_id=event.id,
            provider=event.provider,
            event_type=event.event_type,
            attempts=event.attempts,
            err=err,
        )
        return

    # Schedule retry
    backoff = BACKOFF_SECONDS[min(event.attempts - 1, len(BACKOFF_SECONDS) - 1)]
    event.status = STATUS_FAILED
    event.next_retry_at = now + timedelta(seconds=backoff)
    db.add(event)
    db.commit()
    log_event(
        EVENTS.EVENT_FAILED,
        webhook_event_id=event.id,
        provider=event.provider,
        event_type=event.event_type,
        attempts=event.attempts,
        next_retry_at=event.next_retry_at.isoformat(),
        err=err,
    )


def _format_error(err: BaseException | str) -> str:
    if isinstance(err, BaseException):
        return f"{type(err).__name__}: {err}"
    return str(err)


def replay(db: Session, event_id: int) -> Optional[WebhookEvent]:
    """
    Reset an event back to `received` so the dispatcher picks it up again.

    Admin-facing. Callable for any current status except `processing`
    (that would race with a live worker).
    """
    ev = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
    if ev is None:
        return None
    if ev.status == STATUS_PROCESSING:
        # Don't race an active worker. Admin can wait for it to finish or
        # time out into stale reclaim.
        log_event(
            EVENTS.EVENT_REPLAYED,
            webhook_event_id=event_id,
            status="skipped_still_processing",
        )
        return ev

    now = _utcnow()
    ev.status = STATUS_RECEIVED
    ev.attempts = 0
    ev.next_retry_at = now
    ev.last_error = None
    ev.last_error_at = None
    ev.processed_at = None
    ev.updated_at = now
    db.add(ev)
    db.commit()
    log_event(
        EVENTS.EVENT_REPLAYED,
        webhook_event_id=event_id,
        provider=ev.provider,
        event_type=ev.event_type,
    )
    return ev


def replay_bulk(
    db: Session,
    *,
    status: str = STATUS_DEAD_LETTER,
    provider: Optional[str] = None,
    limit: int = 500,
) -> int:
    """Bulk-reset events matching filters back to `received`. Returns count."""
    q = db.query(WebhookEvent).filter(WebhookEvent.status == status)
    if provider:
        q = q.filter(WebhookEvent.provider == provider)
    rows = q.limit(limit).all()
    n = 0
    for ev in rows:
        now = _utcnow()
        ev.status = STATUS_RECEIVED
        ev.attempts = 0
        ev.next_retry_at = now
        ev.last_error = None
        ev.last_error_at = None
        ev.processed_at = None
        ev.updated_at = now
        db.add(ev)
        n += 1
    db.commit()
    log_event(
        EVENTS.EVENT_REPLAYED,
        count=n,
        status_from=status,
        provider=provider,
        bulk=True,
    )
    return n


def count_by_status(db: Session, *, provider: Optional[str] = None) -> Dict[str, int]:
    q = db.query(WebhookEvent.status)
    if provider:
        q = q.filter(WebhookEvent.provider == provider)
    out: Dict[str, int] = {
        STATUS_RECEIVED: 0,
        STATUS_PROCESSING: 0,
        STATUS_PROCESSED: 0,
        STATUS_FAILED: 0,
        STATUS_DEAD_LETTER: 0,
    }
    for (status,) in q.all():
        if status in out:
            out[status] += 1
        else:
            out.setdefault(status, 0)
            out[status] += 1
    return out
