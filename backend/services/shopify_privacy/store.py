"""
Durable receipt of signed compliance deliveries.

``record_receipt`` commits before the webhook answers 200; any database error
propagates (the router rolls back and answers 503, so Shopify redelivers).
Nothing here reads or writes ``shopify_connections`` except a plain,
lock-free read of the owning row for attribution.

Identity and attribution
  The shop identity comes from the signed body only. A request is attributed
  to a tenant only when the domain and the shop GID resolve to the **same**
  ownership row (``matched``); none → ``unknown``; two rows, or a row whose
  other half disagrees → ``conflict``. Unknown and conflicting requests carry
  no tenant, and nothing here creates, rebinds or reattributes a connection.
  Ownership rows are permanent and unique per shop, so an unknown request is
  attributed later only to the one row that then owns the exact signed
  identity.

Idempotency, replay and redelivery
  * One **open** request per (topic, signed shop identity, body digest): a
    partial unique index resolves concurrent deliveries to one row. Every
    state but ``completed`` is open, so an identical body — a redelivery, a
    replay, or Shopify sending the same body again after a reinstall —
    always lands on the open request, never on a closed one that could
    swallow it.
  * Each delivery bumps ``delivery_count`` and records the connection
    generation observed at that moment (our own state, not a header). A
    request that was already assessed, blocked or routed to review is made
    due again: the worker re-evaluates it against the current evidence, and
    a worker that is evaluating it meanwhile sees the new count at write
    time and must start over (it can never close away the newer delivery).
  * A completed request is never reused: an identical body after completion
    opens a new request.
  * The unsigned webhook id only deduplicates *delivery log rows* (capped);
    it never suppresses a request.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from sqlalchemy import create_engine, func, inspect, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from services.shopify_connection.models import ShopifyConnection
from services.shopify_privacy.config import (
    DEADLINE_DAYS,
    RECEIPT_CONNECT_TIMEOUT_SECONDS,
    RECEIPT_LOCK_TIMEOUT,
    RECEIPT_POOL_TIMEOUT_SECONDS,
    RECEIPT_STATEMENT_TIMEOUT,
    RECEIPT_STATEMENT_TIMEOUT_MS,
    RECEIPT_WORKERS,
)
from services.shopify_privacy.models import (
    DUE_STATES,
    IDENTITY_CONFLICT,
    IDENTITY_MATCHED,
    IDENTITY_UNKNOWN,
    ALWAYS_RECORDED_ACTIONS,
    MAX_DELIVERY_ROWS_PER_REQUEST,
    MAX_EVENTS_PER_REQUEST,
    OPEN_STATES,
    PRIVACY_TABLE_NAMES,
    STATE_RECEIVED,
    ShopifyPrivacyDelivery,
    ShopifyPrivacyRequest,
    ShopifyPrivacyRequestEvent,
)
from services.shopify_privacy.payloads import PrivacyPayload

logger = logging.getLogger("nahla.shopify_privacy")

OUTCOME_RECEIVED = "received"
OUTCOME_DUPLICATE = "duplicate"
REASON_REDELIVERED = "redelivered"


class ReceiptUnavailable(Exception):
    """The receipt could not be made durable now (retryable; never a 2xx)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def wall_clock() -> datetime:
    """The clock a receipt is stamped with at write time (a seam for tests)."""
    return datetime.now(timezone.utc)


def tables_present(db: Session) -> bool:
    """True only when inspection proves the privacy tables (0123) and the
    connection table exist; an inspection failure is treated as absent.
    Inspects through the session's own connection, so the caller's
    connection-level timeouts bound it."""
    try:
        names = set(inspect(db.connection()).get_table_names())
    except Exception as exc:  # noqa: BLE001 — unknown schema is treated as absent (fail closed)
        logger.warning("[SHOPIFY_PRIVACY] schema inspect failed kind=%s", type(exc).__name__)
        return False
    return set(PRIVACY_TABLE_NAMES) | {"shopify_connections"} <= names


def dedicated_session_factory(database_url: Optional[str] = None) -> Callable[[], Session]:
    """Sessions for the webhook receipt only: the shared database URL with a
    separate, small pool and tight bounds (checkout, connect, statement and
    lock timeouts), so a starved or slow shared pool can never hold a
    delivery past its budget. The shared engine's settings are unchanged."""
    if database_url is None:
        from session import DATABASE_URL as database_url  # noqa: PLC0415,N811
    engine = create_engine(
        database_url,
        future=True,
        pool_size=RECEIPT_WORKERS,
        max_overflow=0,
        pool_timeout=RECEIPT_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        pool_recycle=280,
        connect_args={
            "connect_timeout": RECEIPT_CONNECT_TIMEOUT_SECONDS,
            "options": f"-c statement_timeout={RECEIPT_STATEMENT_TIMEOUT_MS} -c lock_timeout={RECEIPT_LOCK_TIMEOUT}",
        },
    )
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _commit_before(db: Session, deadline: Optional[float]) -> None:
    """Commit only while the delivery's budget still holds; otherwise roll
    back (the sender is answered 503 and redelivers). A commit already in
    flight at the deadline may still complete: the redelivery then attaches
    to the same open request, so a late commit is idempotent, never a loss."""
    if deadline is not None and time.monotonic() >= deadline:
        db.rollback()
        raise ReceiptUnavailable("deadline")
    db.commit()


@dataclass(frozen=True)
class ConnectionSnapshot:
    id: int
    tenant_id: int
    shop_domain: str
    shop_gid: str
    status: str
    generation: int
    credential_version: int
    reconcile_request_version: int
    revalidation_pending: bool

    def fingerprint(self) -> tuple:
        return (self.id, self.tenant_id, self.status, self.generation, self.credential_version,
                self.reconcile_request_version, self.revalidation_pending)


@dataclass(frozen=True)
class Identity:
    state: str
    connection: Optional[ConnectionSnapshot] = None

    @property
    def tenant_id(self) -> Optional[int]:
        return self.connection.tenant_id if self.connection is not None else None

    @property
    def connection_id(self) -> Optional[int]:
        return self.connection.id if self.connection is not None else None

    @property
    def generation(self) -> Optional[int]:
        return self.connection.generation if self.connection is not None else None


def snapshot(conn: ShopifyConnection) -> ConnectionSnapshot:
    return ConnectionSnapshot(
        id=int(conn.id), tenant_id=int(conn.tenant_id), shop_domain=conn.shop_domain, shop_gid=conn.shop_gid,
        status=conn.status, generation=int(conn.generation), credential_version=int(conn.credential_version),
        reconcile_request_version=int(conn.reconcile_request_version or 0),
        revalidation_pending=conn.revalidation_requested_at is not None,
    )


def resolve_identity(db: Session, shop_domain: str, shop_gid: str) -> Identity:
    """Exact signed identity → the one ownership row, or unknown / conflict."""
    rows = db.execute(
        select(ShopifyConnection).where(
            or_(ShopifyConnection.shop_domain == shop_domain, ShopifyConnection.shop_gid == shop_gid))
        .execution_options(populate_existing=True)
    ).scalars().all()
    if not rows:
        return Identity(IDENTITY_UNKNOWN)
    if len(rows) == 1 and rows[0].shop_domain == shop_domain and rows[0].shop_gid == shop_gid:
        return Identity(IDENTITY_MATCHED, snapshot(rows[0]))
    return Identity(IDENTITY_CONFLICT)


def event(db: Session, request: ShopifyPrivacyRequest, action: str, *, detail: Optional[dict] = None) -> None:
    """Append one transition, capped per request so signed replays cannot grow
    storage without bound. The first receipt and an execution are always
    recorded; past the cap a single ``event_log_capped`` marker is written
    and later redeliveries / evaluations only move the request's counters
    (``delivery_count``, ``attempts``), which stay authoritative for the
    fences. Every caller holds the request row lock, so the count is exact."""
    if action not in ALWAYS_RECORDED_ACTIONS:
        count = db.scalar(select(func.count()).select_from(ShopifyPrivacyRequestEvent)
                          .where(ShopifyPrivacyRequestEvent.request_id == request.id))
        if count >= MAX_EVENTS_PER_REQUEST:
            return
        if count == MAX_EVENTS_PER_REQUEST - 1:
            action, detail = "event_log_capped", {"delivery_count": int(request.delivery_count)}
    db.add(ShopifyPrivacyRequestEvent(
        request_id=request.id, tenant_id=request.tenant_id, action=action, state=request.state,
        reason=request.reason, detail=detail or None,
    ))


def _log_delivery(db: Session, request_id: int, *, webhook_id: Optional[str], triggered_at: Optional[datetime],
                  generation: Optional[int], now: datetime) -> None:
    """Capped delivery log; a repeated canonical webhook id adds no row."""
    rows = db.scalar(select(func.count()).select_from(ShopifyPrivacyDelivery)
                     .where(ShopifyPrivacyDelivery.request_id == request_id))
    if rows >= MAX_DELIVERY_ROWS_PER_REQUEST:
        return
    db.execute(pg_insert(ShopifyPrivacyDelivery).values(
        request_id=request_id, webhook_id=webhook_id, header_triggered_at=triggered_at,
        connection_generation=generation, received_at=now,
    ).on_conflict_do_nothing())


def _open_request(db: Session, payload: PrivacyPayload, digest: str) -> Optional[ShopifyPrivacyRequest]:
    return db.execute(
        select(ShopifyPrivacyRequest).where(
            ShopifyPrivacyRequest.topic == payload.topic,
            ShopifyPrivacyRequest.shop_domain == payload.shop_domain,
            ShopifyPrivacyRequest.shop_gid == payload.shop_gid,
            ShopifyPrivacyRequest.body_sha256 == digest,
            ShopifyPrivacyRequest.state.in_(OPEN_STATES),
        ).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _attach(db: Session, request: ShopifyPrivacyRequest, identity: Identity, now: datetime) -> Optional[int]:
    """Record another delivery of the same signed body on its open request
    and make it due again. Returns the generation observed for this delivery."""
    if request.connection_id is None and identity.state == IDENTITY_MATCHED:
        request.identity_state = IDENTITY_MATCHED
        request.tenant_id = identity.tenant_id
        request.connection_id = identity.connection_id
    observed = (identity.generation if identity.state == IDENTITY_MATCHED
                and identity.connection_id == request.connection_id else None)
    request.delivery_count = int(request.delivery_count) + 1
    request.last_delivery_at = now
    request.last_delivery_generation = observed
    if request.state in DUE_STATES:
        # Keep any lease: its holder is fenced by delivery_count at write time.
        if request.next_attempt_at is None or request.next_attempt_at > now:
            request.next_attempt_at = now
    else:
        request.state = STATE_RECEIVED
        request.reason = REASON_REDELIVERED
        request.next_attempt_at = now
        request.eligible_generation = None
        request.evaluated_delivery_count = None
    request.updated_at = now
    return observed


def record_receipt(
    db: Session,
    *,
    payload: PrivacyPayload,
    body_sha256: str,
    webhook_id: Optional[str],
    triggered_at: Optional[datetime],
    now: Optional[datetime] = None,
    deadline: Optional[float] = None,
) -> Tuple[str, int]:
    """(outcome, request id) after the receipt is committed. Raises on any
    database error (the caller rolls back and answers non-2xx).

    Timestamps are taken at write time, after any lock wait: a delivery that
    started earlier but waited behind a later one is stamped no earlier than
    the row it attaches to (``last_delivery_at >= received_at`` always holds).
    """
    db.execute(text(f"SET LOCAL lock_timeout = '{RECEIPT_LOCK_TIMEOUT}'"))
    db.execute(text(f"SET LOCAL statement_timeout = '{RECEIPT_STATEMENT_TIMEOUT}'"))
    identity = resolve_identity(db, payload.shop_domain, payload.shop_gid)
    request = _open_request(db, payload, body_sha256)
    if request is None:
        current = now or wall_clock()
        generation = identity.generation if identity.state == IDENTITY_MATCHED else None
        new_id = db.execute(pg_insert(ShopifyPrivacyRequest).values(
            topic=payload.topic, shop_domain=payload.shop_domain, shop_gid=payload.shop_gid,
            body_sha256=body_sha256, identity_state=identity.state, tenant_id=identity.tenant_id,
            connection_id=identity.connection_id, receipt_generation=generation,
            last_delivery_generation=generation, customer_ref_kind=payload.customer_ref_kind,
            orders_count=payload.orders_count, data_request_id=payload.data_request_id,
            state=STATE_RECEIVED, attempts=0, next_attempt_at=current, delivery_count=1,
            received_at=current, last_delivery_at=current, deadline_at=current + timedelta(days=DEADLINE_DAYS),
            created_at=current, updated_at=current,
        ).on_conflict_do_nothing().returning(ShopifyPrivacyRequest.id)).scalar_one_or_none()
        if new_id is not None:
            request = db.get(ShopifyPrivacyRequest, new_id)
            _log_delivery(db, new_id, webhook_id=webhook_id, triggered_at=triggered_at, generation=generation,
                          now=current)
            event(db, request, "received", detail={"identity": identity.state})
            _commit_before(db, deadline)
            return OUTCOME_RECEIVED, new_id
        # A concurrent delivery of the same body committed first.
        request = _open_request(db, payload, body_sha256)
        if request is None:
            raise ReceiptUnavailable("open request vanished")
    # Under the request lock: never earlier than the row's own timestamps.
    stamp = max(now or wall_clock(), request.received_at, request.last_delivery_at)
    observed = _attach(db, request, identity, stamp)
    _log_delivery(db, request.id, webhook_id=webhook_id, triggered_at=triggered_at, generation=observed,
                  now=stamp)
    event(db, request, "redelivered", detail={"delivery_count": int(request.delivery_count)})
    request_id = int(request.id)
    _commit_before(db, deadline)
    return OUTCOME_DUPLICATE, request_id
