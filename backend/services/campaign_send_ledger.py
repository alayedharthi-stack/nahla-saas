"""
services/campaign_send_ledger.py
────────────────────────────────
Durable execution state for manual marketing campaigns.

The dispatcher used to decide "may I send to this recipient?" from an ORM
object it had loaded minutes earlier, and flipped the row to ``sending``
with an uncommitted flush. Two workers for the same campaign (a second
``dispatch-now`` while the first thread was still running) therefore both
saw ``queued``, both called Meta, and the second write replaced the first
wamid on the single ``provider_message_id`` column. Meta accepted both.

This module replaces that with state that is correct across threads,
processes and replicas:

* **Campaign lease** (``campaign_dispatch_leases``): exactly one live
  worker per campaign. Acquired with one conditional UPDATE; renewed on
  every recipient; a dead worker's lease simply lapses.
* **Atomic recipient claim**: ``UPDATE campaign_send_logs SET
  status='sending' WHERE id=:id AND status='queued'`` — committed before
  anything is sent. A second worker gets ``rowcount == 0`` and moves on.
* **One attempt row per request** (``campaign_send_attempts``) with its
  own wamid and outcome, written in phases (claimed → request_started →
  accepted / rejected / not_sent / uncertain). A crash leaves a row that
  says exactly how far the attempt got.
* **Ambiguity is a state, not a retry**: a timeout after the request left
  the process, a 5xx, a 2xx without a wamid, or a crash after
  ``request_started`` all become ``uncertain``. Nothing re-sends an
  uncertain recipient automatically; an operator decides after
  reconciliation.
* **Webhook inbox** (``campaign_status_event_inbox``): every campaign
  status event is stored once per ``(wamid, status)`` so redeliveries are
  no-ops, and an event that arrives before its attempt is committed is
  applied as soon as the attempt commits.
* **Shared messaging budget**: Meta applies the business-initiated limit
  to the whole business portfolio. Attempts carry the portfolio scope and
  the dispatcher stops (pauses) before the scope's 24h budget is spent,
  instead of pushing thousands of messages Meta will refuse.

Nothing here claims exactly-once delivery: an external HTTP request that
times out cannot be proven un-sent. What this guarantees is that we never
start a *second* request for a recipient while the first one's outcome is
unknown, and never start one concurrently from two workers.
"""
from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    Campaign,
    CampaignDispatchLease,
    CampaignMessagingScope,
    CampaignSendAttempt,
    CampaignSendLog,
    CampaignStatusEventInbox,
)

logger = logging.getLogger("nahla-backend")

# ── Send-log status added by the ledger ────────────────────────────────────
# ``campaign_send_logs.status`` for a recipient whose last request may or
# may not have been accepted. Never re-queued automatically.
LOG_UNCERTAIN = "uncertain"

# ── Attempt states ─────────────────────────────────────────────────────────
ATTEMPT_CLAIMED = "claimed"
ATTEMPT_REQUEST_STARTED = "request_started"
ATTEMPT_ACCEPTED = "accepted"
ATTEMPT_REJECTED = "rejected"
ATTEMPT_NOT_SENT = "not_sent"
ATTEMPT_UNCERTAIN = "uncertain"
ATTEMPT_ABANDONED = "abandoned"

IN_FLIGHT_STATES = frozenset({ATTEMPT_CLAIMED, ATTEMPT_REQUEST_STARTED})
# States where Meta may hold a message for the recipient.
POSSIBLY_SENT_STATES = frozenset({
    ATTEMPT_REQUEST_STARTED, ATTEMPT_ACCEPTED, ATTEMPT_UNCERTAIN,
})
# States that hold a slot of the shared messaging budget: everything that
# may produce a message, plus reservations that have not started yet (so a
# concurrent claim sees them). Abandoned / rejected / not_sent free it.
BUDGET_STATES = POSSIBLY_SENT_STATES | frozenset({ATTEMPT_CLAIMED})
# States that prove no message left for this attempt.
DEFINITELY_NOT_SENT_STATES = frozenset({
    ATTEMPT_REJECTED, ATTEMPT_NOT_SENT, ATTEMPT_ABANDONED,
})

DELIVERY_DELIVERED = "delivered"
DELIVERY_READ = "read"
DELIVERY_FAILED = "failed"

# ── Tunables ───────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# The holder renews on every recipient (≤ HTTP timeout + pacing apart),
# so two minutes is several renewals of headroom.
LEASE_TTL_SECONDS = _env_int("NAHLA_CAMPAIGN_LEASE_TTL_SECONDS", 120)
# An in-flight attempt older than this (and not owned by a live lease)
# belongs to a dead worker. Must exceed LEASE_TTL + provider timeout.
STALE_ATTEMPT_SECONDS = _env_int("NAHLA_CAMPAIGN_STALE_ATTEMPT_SECONDS", 300)
# Meta's starting business-initiated limit. Used whenever the real limit
# is unknown or stale — absence of a reading is never "unlimited".
DEFAULT_MESSAGING_LIMIT = _env_int("NAHLA_META_DEFAULT_MESSAGING_LIMIT", 250)
# A tier reading older than this is treated as unknown.
MESSAGING_LIMIT_MAX_AGE_HOURS = _env_int("NAHLA_META_LIMIT_MAX_AGE_HOURS", 24)
# Share of the limit campaigns may use; the rest is kept for order /
# automation templates this ledger does not count.
CAMPAIGN_BUDGET_PERCENT = _env_int("NAHLA_CAMPAIGN_LIMIT_BUDGET_PERCENT", 90)
MESSAGING_WINDOW = timedelta(hours=24)
# Post-accept throttling breaker (Meta says: stop sending from this number).
POST_ACCEPT_BREAKER_WINDOW = timedelta(
    minutes=_env_int("NAHLA_CAMPAIGN_POST_ACCEPT_WINDOW_MINUTES", 15),
)
POST_ACCEPT_BREAKER_THRESHOLDS: Dict[str, int] = {
    "spam_rate_limit": _env_int("NAHLA_CAMPAIGN_BREAKER_SPAM", 5),
    "rate_limit": _env_int("NAHLA_CAMPAIGN_BREAKER_RATE", 10),
    "marketing_blocked": _env_int("NAHLA_CAMPAIGN_BREAKER_ECOSYSTEM", 25),
}
# Consecutive uncertain outcomes before the dispatcher pauses.
UNCERTAIN_BREAKER_THRESHOLD = _env_int("NAHLA_CAMPAIGN_BREAKER_UNCERTAIN", 3)
# Unmatched inbox rows older than this are dropped (non-campaign sends).
INBOX_RETENTION = timedelta(days=7)

# Pause reasons (persisted on the lease; the UI maps them to Arabic).
PAUSE_MERCHANT_STOP = "merchant_stop"
PAUSE_MESSAGING_LIMIT = "messaging_limit_reached"
PAUSE_PROVIDER_THROTTLING = "provider_throttling"
PAUSE_UNCERTAIN = "uncertain_sends"
PAUSE_LEASE_LOST = "lease_lost"


def utcnow() -> datetime:
    """Naive UTC — the ledger's columns are ``timestamp without time zone``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def new_worker_id() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


# ═══════════════════════════════════════════════════════════════════════════
# Campaign lease
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class LeaseResult:
    acquired: bool
    owner: Optional[str] = None
    expires_at: Optional[datetime] = None
    reason: str = ""


LEDGER_TABLES = (
    "campaign_dispatch_leases", "campaign_send_attempts",
    "campaign_status_event_inbox", "campaign_messaging_scopes",
)


def ledger_available(db: Session) -> bool:
    """True when every ledger table can be read.

    The tables are created by the boot-time ``create_all``; until that has
    run (or if it failed) nothing may be sent, because none of the
    guarantees above could be enforced. Runs inside a savepoint so a
    missing table never aborts the caller's transaction.
    """
    from sqlalchemy import text  # noqa: PLC0415
    try:
        with db.begin_nested():
            for t in LEDGER_TABLES:
                db.execute(text(f"SELECT 1 FROM {t} LIMIT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[campaign_ledger] ledger tables unavailable — sends refused: %s",
                     str(exc).splitlines()[0][:200])
        return False


def lease_is_live(lease: Optional[CampaignDispatchLease], *, now: Optional[datetime] = None) -> bool:
    if lease is None or not lease.owner or lease.expires_at is None:
        return False
    return _naive(lease.expires_at) > (now or utcnow())


def get_lease(db: Session, campaign_id: int) -> Optional[CampaignDispatchLease]:
    return db.get(CampaignDispatchLease, campaign_id)


def acquire_lease(
    db: Session,
    *,
    campaign_id: int,
    tenant_id: int,
    owner: str,
    ttl_seconds: int = LEASE_TTL_SECONDS,
) -> LeaseResult:
    """Take the campaign's execution lease, or report who holds it.

    Commits. Refuses while a merchant stop is pending — only an explicit
    resume (:func:`clear_stop`) lifts it.
    """
    now = utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    values = dict(
        owner=owner, acquired_at=now, heartbeat_at=now, expires_at=expires,
        released_at=None, pause_reason=None, pause_detail=None, paused_at=None,
        updated_at=now,
    )
    res = db.execute(
        update(CampaignDispatchLease)
        .where(
            CampaignDispatchLease.campaign_id == campaign_id,
            CampaignDispatchLease.stop_requested_at.is_(None),
            or_(
                CampaignDispatchLease.owner.is_(None),
                CampaignDispatchLease.expires_at.is_(None),
                CampaignDispatchLease.expires_at < now,
                CampaignDispatchLease.owner == owner,
            ),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount == 1:
        db.commit()
        return LeaseResult(True, owner, expires)
    db.rollback()

    existing = db.get(CampaignDispatchLease, campaign_id)
    if existing is None:
        try:
            db.add(CampaignDispatchLease(
                campaign_id=campaign_id, tenant_id=tenant_id, **values,
            ))
            db.commit()
            return LeaseResult(True, owner, expires)
        except IntegrityError:
            # Another worker inserted first — it holds the lease.
            db.rollback()
            existing = db.get(CampaignDispatchLease, campaign_id)

    if existing is not None:
        db.refresh(existing)
        if existing.stop_requested_at is not None:
            return LeaseResult(False, existing.owner, existing.expires_at, "stop_requested")
        return LeaseResult(False, existing.owner, existing.expires_at, "held_by_other_worker")
    return LeaseResult(False, reason="lease_unavailable")


@dataclass
class HeartbeatResult:
    held: bool
    stop_requested: bool = False


def heartbeat(db: Session, *, campaign_id: int, owner: str,
              ttl_seconds: int = LEASE_TTL_SECONDS) -> HeartbeatResult:
    """Renew the lease inside the caller's transaction (no commit).

    ``held=False`` means another worker took over (our lease lapsed):
    the caller must stop claiming immediately.
    """
    now = utcnow()
    res = db.execute(
        update(CampaignDispatchLease)
        .where(
            CampaignDispatchLease.campaign_id == campaign_id,
            CampaignDispatchLease.owner == owner,
        )
        .values(heartbeat_at=now, expires_at=now + timedelta(seconds=ttl_seconds), updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        return HeartbeatResult(held=False)
    stop = (
        db.query(CampaignDispatchLease.stop_requested_at)
        .filter(CampaignDispatchLease.campaign_id == campaign_id)
        .scalar()
    )
    return HeartbeatResult(held=True, stop_requested=stop is not None)


def release_lease(
    db: Session, *, campaign_id: int, owner: str,
    pause_reason: Optional[str] = None, pause_detail: Optional[str] = None,
) -> None:
    now = utcnow()
    values: Dict[str, Any] = dict(owner=None, expires_at=None, released_at=now, updated_at=now)
    if pause_reason:
        values.update(pause_reason=pause_reason, pause_detail=(pause_detail or "")[:1000], paused_at=now)
    db.execute(
        update(CampaignDispatchLease)
        .where(CampaignDispatchLease.campaign_id == campaign_id,
               CampaignDispatchLease.owner == owner)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def request_stop(db: Session, *, campaign_id: int, tenant_id: int,
                 reason: str = PAUSE_MERCHANT_STOP) -> None:
    """Ask the live worker (if any) to stop before its next claim. Commits."""
    now = utcnow()
    lease = db.get(CampaignDispatchLease, campaign_id)
    if lease is None:
        lease = CampaignDispatchLease(campaign_id=campaign_id, tenant_id=tenant_id)
        db.add(lease)
    lease.stop_requested_at = now
    lease.stop_reason = reason
    lease.pause_reason = reason
    lease.paused_at = now
    lease.updated_at = now
    db.commit()


def clear_stop(db: Session, *, campaign_id: int) -> None:
    """Explicit merchant resume: lift a pending stop (no commit)."""
    lease = db.get(CampaignDispatchLease, campaign_id)
    if lease is not None:
        lease.stop_requested_at = None
        lease.stop_reason = None
        lease.pause_reason = None
        lease.pause_detail = None
        lease.paused_at = None
        lease.updated_at = utcnow()


def execution_snapshot(db: Session, campaign_id: int) -> Dict[str, Any]:
    """What the UI needs to say whether a worker is really running."""
    lease = db.get(CampaignDispatchLease, campaign_id)
    now = utcnow()
    live = lease_is_live(lease, now=now)
    last_attempt_at = (
        db.query(func.max(CampaignSendAttempt.claimed_at))
        .filter(CampaignSendAttempt.campaign_id == campaign_id)
        .scalar()
    )
    return {
        "worker_running": live,
        "heartbeat_at": lease.heartbeat_at.isoformat() if lease and lease.heartbeat_at else None,
        "lease_expires_at": lease.expires_at.isoformat() if lease and lease.expires_at and live else None,
        "stop_requested": bool(lease and lease.stop_requested_at),
        "pause_reason": lease.pause_reason if lease else None,
        "pause_detail": lease.pause_detail if lease else None,
        "paused_at": lease.paused_at.isoformat() if lease and lease.paused_at else None,
        "last_attempt_at": last_attempt_at.isoformat() if last_attempt_at else None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Recipient claim + attempt lifecycle
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ClaimResult:
    attempt: Optional[CampaignSendAttempt]
    reason: str  # claimed | not_queued | lease_lost | stop_requested | conflict | budget_exhausted
    budget: Optional["MessagingBudget"] = None


def lock_messaging_scope(db: Session, scope_key: str) -> None:
    """Take the scope row's lock for the rest of the transaction (creating
    the row on first use). Serialises budget check + reservation across
    campaigns, processes and replicas sharing one Meta messaging limit."""
    now = utcnow()
    stmt = (
        update(CampaignMessagingScope)
        .where(CampaignMessagingScope.scope_key == scope_key)
        .values(updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if db.execute(stmt).rowcount == 1:
        return
    try:
        with db.begin_nested():
            db.add(CampaignMessagingScope(scope_key=scope_key, updated_at=now))
    except IntegrityError:
        pass  # noqa: silent-ok — a concurrent claim created it; lock it below
    db.execute(stmt)


def claim_recipient(
    db: Session,
    *,
    log_id: int,
    campaign: Campaign,
    owner: str,
    scope_key: Optional[str],
    phone_number_id: Optional[str],
    wa_conn: Any = None,
) -> ClaimResult:
    """Atomically reserve one queued recipient and record the attempt.

    The compare-and-set on ``status='queued'`` is the cross-worker guard:
    under concurrent claims exactly one UPDATE matches (Postgres re-checks
    the predicate after the row lock is released). Commits, together with
    a lease heartbeat, before returning — the reservation is durable
    before anything is sent.

    With ``wa_conn`` the claim also draws on the shared messaging budget
    atomically: the scope row is locked, usage (including other workers'
    committed reservations) is counted under that lock, and the attempt —
    itself a reservation — is committed before the lock is released.
    Lock order is lease row → scope row → recipient row, the same for
    every worker.
    """
    now = utcnow()
    campaign_id = int(campaign.id)
    tenant_id = int(campaign.tenant_id)
    hb = heartbeat(db, campaign_id=campaign_id, owner=owner)
    if not hb.held:
        db.rollback()
        return ClaimResult(None, "lease_lost")
    if hb.stop_requested:
        db.rollback()
        return ClaimResult(None, "stop_requested")
    budget: Optional[MessagingBudget] = None
    if wa_conn is not None and scope_key:
        budget = messaging_budget(db, wa_conn)
        if budget.budget is not None:
            lock_messaging_scope(db, scope_key)
            # Recount under the lock: every reservation committed before we
            # got it is visible now.
            budget = messaging_budget(db, wa_conn)
            phone_now = (
                db.query(CampaignSendLog.customer_phone_e164)
                .filter(CampaignSendLog.id == log_id)
                .scalar()
            )
            if not budget.allows(phone_now or ""):
                db.rollback()
                return ClaimResult(None, "budget_exhausted", budget)
    res = db.execute(
        update(CampaignSendLog)
        .where(CampaignSendLog.id == log_id, CampaignSendLog.status == "queued")
        .values(
            status="sending",
            attempt_count=func.coalesce(CampaignSendLog.attempt_count, 0) + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        db.rollback()
        return ClaimResult(None, "not_queued")
    prev = (
        db.query(func.max(CampaignSendAttempt.attempt_no))
        .filter(CampaignSendAttempt.send_log_id == log_id)
        .scalar()
    ) or 0
    phone = (
        db.query(CampaignSendLog.customer_phone_e164)
        .filter(CampaignSendLog.id == log_id)
        .scalar()
    )
    attempt = CampaignSendAttempt(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        send_log_id=log_id,
        customer_phone_e164=phone,
        attempt_no=int(prev) + 1,
        worker_id=owner,
        messaging_scope_key=scope_key,
        phone_number_id=phone_number_id,
        state=ATTEMPT_CLAIMED,
        claimed_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(attempt)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return ClaimResult(None, "conflict")
    # The bulk UPDATE bypassed the identity map; drop any stale copy.
    cached = db.get(CampaignSendLog, log_id)
    if cached is not None:
        db.refresh(cached)
    return ClaimResult(attempt, "claimed", budget)


def mark_request_started(db: Session, attempt: CampaignSendAttempt) -> None:
    """Last durable write before the request leaves. From here on a crash
    means *uncertain*, never *not sent*. Commits."""
    now = utcnow()
    attempt.state = ATTEMPT_REQUEST_STARTED
    attempt.request_started_at = now
    attempt.updated_at = now
    db.commit()


def return_to_queue(db: Session, attempt: CampaignSendAttempt, *, reason: str) -> None:
    """The attempt never reached the request phase (e.g. the budget check
    or payload build stopped it). Safe to put the recipient back. Commits."""
    now = utcnow()
    attempt.state = ATTEMPT_ABANDONED
    attempt.error_code = reason[:64]
    attempt.completed_at = now
    attempt.updated_at = now
    row = db.get(CampaignSendLog, attempt.send_log_id)
    # We hold the claim, so nobody else writes this row concurrently.
    if row is not None and row.status == "sending":
        row.status = "queued"
        row.attempt_count = max(0, int(row.attempt_count or 1) - 1)
        row.updated_at = now
    db.commit()


def _send_log(db: Session, attempt: CampaignSendAttempt) -> Optional[CampaignSendLog]:
    return db.get(CampaignSendLog, attempt.send_log_id)


def record_accepted(
    db: Session, attempt: CampaignSendAttempt, *, wamid: str, http_status: Optional[int] = None,
) -> CampaignSendLog:
    """Meta returned a wamid. Stages the attempt + send-log update; the
    caller commits (it adds the MessageEvent in the same transaction)
    and then calls :func:`apply_pending_events_for`."""
    now = utcnow()
    attempt.state = ATTEMPT_ACCEPTED
    attempt.provider_message_id = wamid
    attempt.http_status = http_status
    attempt.accepted_at = now
    attempt.completed_at = now
    attempt.updated_at = now
    row = _send_log(db, attempt)
    if row is not None:
        row.status = "sent"
        # First accepted wamid stays on the anchor row; every wamid lives
        # on its own attempt row.
        if not row.provider_message_id:
            row.provider_message_id = wamid
        row.sent_at = row.sent_at or now
        row.error_code = None
        row.error_message = None
        row.updated_at = now
    return row


def record_rejected(
    db: Session, attempt: CampaignSendAttempt, *,
    error_key: str, raw_code: Any, technical: str, http_status: Optional[int] = None,
    log_status: str = "failed",
) -> None:
    """Meta (or a local guard in front of it) refused the request: no
    message exists. Staged; caller commits."""
    now = utcnow()
    attempt.state = ATTEMPT_REJECTED
    attempt.error_code = (error_key or "unknown")[:64]
    attempt.raw_error_code = (str(raw_code)[:32] if raw_code not in (None, "") else None)
    attempt.error_message = technical[:1000] if technical else None
    attempt.http_status = http_status
    attempt.completed_at = now
    attempt.updated_at = now
    row = _send_log(db, attempt)
    if row is not None:
        row.status = log_status
        row.error_code = attempt.error_code
        row.error_message = (technical or "")[:500]
        row.updated_at = now


def record_not_sent(
    db: Session, attempt: CampaignSendAttempt, *, technical: str,
    error_code: str = "transport_not_sent",
) -> None:
    """Nothing reached Meta: the transport failed before a connection
    existed, or the request could not be built. Staged; caller commits."""
    now = utcnow()
    attempt.state = ATTEMPT_NOT_SENT
    attempt.error_code = error_code[:64]
    attempt.error_message = technical[:1000]
    attempt.completed_at = now
    attempt.updated_at = now
    row = _send_log(db, attempt)
    if row is not None:
        row.status = "failed"
        row.error_code = attempt.error_code
        row.error_message = technical[:500]
        row.updated_at = now


def record_uncertain(
    db: Session, attempt: CampaignSendAttempt, *, reason: str, technical: str,
    http_status: Optional[int] = None,
) -> None:
    """The request may have been accepted. Staged; caller commits."""
    now = utcnow()
    attempt.state = ATTEMPT_UNCERTAIN
    attempt.error_code = reason[:64]
    attempt.error_message = technical[:1000]
    attempt.http_status = http_status
    attempt.completed_at = now
    attempt.updated_at = now
    row = _send_log(db, attempt)
    if row is not None:
        row.status = LOG_UNCERTAIN
        row.error_code = "send_outcome_unknown"
        row.error_message = f"[{reason}] {technical}"[:500]
        row.updated_at = now


# Transport exceptions raised before any byte reached the provider.
_PRE_CONNECT_EXCEPTIONS = (
    "ConnectError", "ConnectTimeout", "PoolTimeout", "UnsupportedProtocol",
    "InvalidURL", "LocalProtocolError",
)


def exception_proves_not_sent(exc: BaseException) -> bool:
    try:
        import httpx  # noqa: PLC0415
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                            httpx.UnsupportedProtocol, httpx.InvalidURL)):
            return True
    except ImportError:
        logger.debug("[campaign_ledger] httpx unavailable; classifying by exception name")
    return type(exc).__name__ in _PRE_CONNECT_EXCEPTIONS


# ═══════════════════════════════════════════════════════════════════════════
# Recovery of attempts left behind by a dead worker
# ═══════════════════════════════════════════════════════════════════════════


def recover_stale_attempts(
    db: Session, campaign_id: int, *, stale_seconds: int = STALE_ATTEMPT_SECONDS,
) -> Dict[str, int]:
    """Resolve rows a crashed/killed worker left in flight. No commit.

    * attempt ``claimed`` (request never started) → ``abandoned``;
      recipient back to ``queued`` — provably nothing was sent.
    * attempt ``request_started`` → ``uncertain``; recipient ``uncertain``.
    * legacy ``sending`` rows with no attempt ledger → ``uncertain`` when
      the legacy counter says a request was made, else ``queued``. The
      pre-ledger code committed ``sending`` before the request, so these
      may well have been accepted.

    Never touches anything while a live lease exists for the campaign
    unless the caller holds it (the caller is expected to hold it).
    """
    now = utcnow()
    cutoff = now - timedelta(seconds=stale_seconds)
    out = {"abandoned": 0, "uncertain": 0, "legacy_uncertain": 0, "legacy_requeued": 0}

    stale = (
        db.query(CampaignSendAttempt)
        .filter(
            CampaignSendAttempt.campaign_id == campaign_id,
            CampaignSendAttempt.state.in_(tuple(IN_FLIGHT_STATES)),
            CampaignSendAttempt.updated_at < cutoff,
        )
        .all()
    )
    for att in stale:
        row = db.get(CampaignSendLog, att.send_log_id)
        if att.state == ATTEMPT_CLAIMED:
            att.state = ATTEMPT_ABANDONED
            att.error_code = "worker_lost_before_request"
            att.completed_at = now
            att.updated_at = now
            if row is not None and row.status == "sending":
                row.status = "queued"
                row.updated_at = now
            out["abandoned"] += 1
        else:
            record_uncertain(
                db, att, reason="worker_lost_after_request_start",
                technical="worker stopped after the request started; outcome unknown",
            )
            out["uncertain"] += 1

    with_ledger = db.query(CampaignSendAttempt.send_log_id).filter(
        CampaignSendAttempt.campaign_id == campaign_id,
    )
    legacy = (
        db.query(CampaignSendLog)
        .filter(
            CampaignSendLog.campaign_id == campaign_id,
            CampaignSendLog.status == "sending",
            CampaignSendLog.updated_at < cutoff,
            ~CampaignSendLog.id.in_(with_ledger),
        )
        .all()
    )
    for row in legacy:
        if int(row.attempt_count or 0) >= 1:
            row.status = LOG_UNCERTAIN
            row.error_code = "send_outcome_unknown"
            row.error_message = (
                "[legacy_sending] left in 'sending' by a pre-ledger worker; "
                "the request may have been accepted"
            )
            out["legacy_uncertain"] += 1
        else:
            row.status = "queued"
            out["legacy_requeued"] += 1
        row.updated_at = now
    if any(out.values()):
        logger.warning("[campaign_ledger] campaign=%d recovered stale in-flight rows %s", campaign_id, out)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Status webhooks
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class StatusApplyResult:
    matched: bool
    duplicate: bool
    attempt_id: Optional[int] = None
    send_log_id: Optional[int] = None
    tenant_id: Optional[int] = None
    campaign_id: Optional[int] = None
    changed: bool = False


def _event_time(provider_timestamp: Any) -> datetime:
    try:
        ts = int(provider_timestamp)
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return utcnow()


def _first_error(errors: Any) -> Dict[str, Any]:
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0]
    return {}


def _apply_to_attempt(att: CampaignSendAttempt, status: str, at: datetime, errors: Any) -> bool:
    """Idempotent, order-independent: each event only fills its own
    timestamp; ``delivery_state`` is derived from all of them."""
    changed = False
    if status == DELIVERY_DELIVERED and att.delivered_at is None:
        att.delivered_at = at
        changed = True
    elif status == DELIVERY_READ:
        if att.read_at is None:
            att.read_at = at
            changed = True
    elif status == DELIVERY_FAILED and att.failed_at is None:
        att.failed_at = at
        e0 = _first_error(errors)
        raw_code = e0.get("code")
        title = str(e0.get("title") or e0.get("message") or "")
        detail = ""
        if isinstance(e0.get("error_data"), dict):
            detail = str(e0["error_data"].get("details") or "")
        try:
            from services.meta_errors import classify_meta_error, format_technical  # noqa: PLC0415
            ce = classify_meta_error(code=raw_code, message=title or detail)
            att.post_accept_error_code = ce.key[:64]
            att.post_accept_error_message = format_technical(
                code=raw_code, subcode=None, error_type=None,
                message=(title + (f" — {detail}" if detail else "")) or "failed",
            )[:1000]
        except Exception:  # noqa: BLE001
            att.post_accept_error_code = "unknown"
            att.post_accept_error_message = title[:1000]
        att.post_accept_raw_error_code = (str(raw_code)[:32] if raw_code not in (None, "") else None)
        changed = True
    if changed:
        if att.read_at is not None:
            att.delivery_state = DELIVERY_READ
        elif att.delivered_at is not None:
            att.delivery_state = DELIVERY_DELIVERED
        elif att.failed_at is not None:
            att.delivery_state = DELIVERY_FAILED
        att.updated_at = utcnow()
        # A late wamid-bearing event for an attempt we had marked
        # uncertain is proof it was accepted.
        if att.state == ATTEMPT_UNCERTAIN:
            att.state = ATTEMPT_ACCEPTED
            att.accepted_at = att.accepted_at or at
    return changed


def refresh_send_log_from_attempts(db: Session, send_log_id: int) -> None:
    """Derive the per-recipient anchor's delivery columns from every
    attempt. ``read`` implies delivered; ``failed_at`` is only kept when
    no attempt has delivery evidence and every accepted attempt failed."""
    row = db.get(CampaignSendLog, send_log_id)
    if row is None:
        return
    atts = db.query(CampaignSendAttempt).filter(CampaignSendAttempt.send_log_id == send_log_id).all()
    accepted = [a for a in atts if a.state == ATTEMPT_ACCEPTED]
    if not accepted:
        return
    delivered_times = [t for a in accepted for t in (a.delivered_at, a.read_at) if t is not None]
    read_times = [a.read_at for a in accepted if a.read_at is not None]
    row.delivered_at = min(delivered_times) if delivered_times else None
    row.read_at = min(read_times) if read_times else None
    all_failed = all(a.failed_at is not None for a in accepted)
    if not delivered_times and all_failed:
        row.failed_at = min(a.failed_at for a in accepted)
        last = max(accepted, key=lambda a: a.failed_at)
        row.error_code = last.post_accept_error_code
        row.error_message = (last.post_accept_error_message or "")[:500]
    else:
        row.failed_at = None
        if row.status == "sent":
            row.error_code = None
            row.error_message = None
    if row.status == LOG_UNCERTAIN:
        row.status = "sent"
        row.sent_at = row.sent_at or min(a.accepted_at or utcnow() for a in accepted)
        if not row.provider_message_id:
            row.provider_message_id = accepted[0].provider_message_id
    row.updated_at = utcnow()


def _find_attempt(db: Session, wamid: str) -> Optional[CampaignSendAttempt]:
    return (
        db.query(CampaignSendAttempt)
        .filter(CampaignSendAttempt.provider_message_id == wamid)
        .first()
    )


def apply_status_event(
    db: Session, *, wamid: str, status: str, provider_timestamp: Any = None,
    recipient_id: Optional[str] = None, errors: Any = None,
    store_if_unmatched: bool = True,
) -> StatusApplyResult:
    """Record one Meta status event and apply it to its attempt. Commits.

    Duplicate redeliveries (same wamid + status) are detected by the
    inbox's unique index and change nothing. An event for a wamid that no
    attempt owns yet is kept pending; after committing, we look for the
    attempt once more so an acceptance that committed concurrently is not
    missed (the dispatcher does the mirror-image check after its commit).
    """
    status = (status or "").lower()
    if status not in (DELIVERY_DELIVERED, DELIVERY_READ, DELIVERY_FAILED) or not wamid:
        return StatusApplyResult(matched=False, duplicate=False)

    att = _find_attempt(db, wamid)
    if att is None and not store_if_unmatched:
        return StatusApplyResult(matched=False, duplicate=False)

    duplicate = False
    inbox = CampaignStatusEventInbox(
        provider_message_id=wamid, status=status,
        provider_timestamp=_safe_int(provider_timestamp),
        recipient_id=(recipient_id or None),
        errors=errors if isinstance(errors, list) else None,
        received_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(inbox)
    except IntegrityError:
        duplicate = True
        inbox = (
            db.query(CampaignStatusEventInbox)
            .filter(CampaignStatusEventInbox.provider_message_id == wamid,
                    CampaignStatusEventInbox.status == status)
            .first()
        )

    if att is None:
        db.commit()
        # Mirror-image of the dispatcher's post-commit drain.
        att = _find_attempt(db, wamid)
        if att is None:
            return StatusApplyResult(matched=False, duplicate=duplicate)

    changed = _apply_pending_for_attempt(db, att)
    db.commit()
    return StatusApplyResult(
        matched=True, duplicate=duplicate and not changed, attempt_id=att.id,
        send_log_id=att.send_log_id, tenant_id=att.tenant_id,
        campaign_id=att.campaign_id, changed=changed,
    )


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _apply_pending_for_attempt(db: Session, att: CampaignSendAttempt) -> bool:
    if not att.provider_message_id:
        return False
    pending = (
        db.query(CampaignStatusEventInbox)
        .filter(CampaignStatusEventInbox.provider_message_id == att.provider_message_id,
                CampaignStatusEventInbox.applied_at.is_(None))
        .all()
    )
    changed = False
    now = utcnow()
    for ev in pending:
        if _apply_to_attempt(att, ev.status, _event_time(ev.provider_timestamp), ev.errors):
            changed = True
        ev.applied_at = now
        ev.attempt_id = att.id
    if changed:
        db.flush()
        refresh_send_log_from_attempts(db, att.send_log_id)
    return changed


def apply_pending_events_for(db: Session, attempt: CampaignSendAttempt) -> bool:
    """Apply events that arrived before this attempt's wamid was
    committed. Call right after the acceptance commit. Commits."""
    changed = _apply_pending_for_attempt(db, attempt)
    db.commit()
    return changed


def apply_pending_events_for_campaign(db: Session, campaign_id: int, *, limit: int = 500) -> int:
    """Sweep: match pending inbox rows to this campaign's attempts. Commits."""
    rows = (
        db.query(CampaignSendAttempt)
        .join(CampaignStatusEventInbox,
              CampaignStatusEventInbox.provider_message_id == CampaignSendAttempt.provider_message_id)
        .filter(CampaignSendAttempt.campaign_id == campaign_id,
                CampaignStatusEventInbox.applied_at.is_(None))
        .limit(limit)
        .all()
    )
    n = 0
    for att in rows:
        if _apply_pending_for_attempt(db, att):
            n += 1
    # Unmatched rows past retention belong to non-campaign sends.
    db.query(CampaignStatusEventInbox).filter(
        CampaignStatusEventInbox.applied_at.is_(None),
        CampaignStatusEventInbox.received_at < utcnow() - INBOX_RETENTION,
    ).delete(synchronize_session=False)
    db.commit()
    return n


# ═══════════════════════════════════════════════════════════════════════════
# Recipient outcomes, stats and retry eligibility
# ═══════════════════════════════════════════════════════════════════════════

OUTCOME_DELIVERED_ONCE = "delivered_once"
OUTCOME_DELIVERED_MULTIPLE = "delivered_multiple"
OUTCOME_ACCEPTED_MULTIPLE_UNPROVEN = "accepted_multiple_unproven"
OUTCOME_ACCEPTED_PENDING = "accepted_pending"
OUTCOME_FAILED_AFTER_ACCEPT = "failed_after_accept"
OUTCOME_FAILED_BEFORE_ACCEPT = "failed_before_accept"
OUTCOME_UNCERTAIN = "uncertain"
OUTCOME_IN_FLIGHT = "in_flight"
OUTCOME_NOT_STARTED = "not_started"


def classify_recipient(attempts: Iterable[CampaignSendAttempt]) -> str:
    """One recipient's outcome from ALL of its attempts."""
    atts = list(attempts)
    if not atts:
        return OUTCOME_NOT_STARTED
    accepted = [a for a in atts if a.state == ATTEMPT_ACCEPTED]
    delivered = [a for a in accepted if a.delivered_at is not None or a.read_at is not None]
    if len(delivered) >= 2:
        return OUTCOME_DELIVERED_MULTIPLE
    if any(a.state in IN_FLIGHT_STATES for a in atts):
        return OUTCOME_IN_FLIGHT
    if any(a.state == ATTEMPT_UNCERTAIN for a in atts):
        # Unknown extra copy — even with one delivered, we can't say "once".
        return OUTCOME_UNCERTAIN
    if len(delivered) == 1:
        others_unresolved = [a for a in accepted if a not in delivered and a.failed_at is None]
        return OUTCOME_ACCEPTED_MULTIPLE_UNPROVEN if others_unresolved else OUTCOME_DELIVERED_ONCE
    if len(accepted) >= 2:
        if all(a.failed_at is not None for a in accepted):
            return OUTCOME_FAILED_AFTER_ACCEPT
        return OUTCOME_ACCEPTED_MULTIPLE_UNPROVEN
    if len(accepted) == 1:
        return OUTCOME_FAILED_AFTER_ACCEPT if accepted[0].failed_at is not None else OUTCOME_ACCEPTED_PENDING
    return OUTCOME_FAILED_BEFORE_ACCEPT


def recipient_outcomes(db: Session, campaign_id: int) -> Dict[int, str]:
    """``{send_log_id: outcome}`` for recipients that have attempts."""
    by_log: Dict[int, List[CampaignSendAttempt]] = {}
    for a in db.query(CampaignSendAttempt).filter(CampaignSendAttempt.campaign_id == campaign_id):
        by_log.setdefault(int(a.send_log_id), []).append(a)
    return {lid: classify_recipient(atts) for lid, atts in by_log.items()}


def ledger_stats(db: Session, campaign_ids: List[int]) -> Dict[int, Dict[str, int]]:
    """Message-level and recipient-level counts from the attempt ledger.

    Messages and recipients are different scopes and are never mixed:
    ``messages_*`` count attempts/wamids, ``recipients_*`` count people.
    """
    out: Dict[int, Dict[str, int]] = {}
    if not campaign_ids:
        return out
    atts = db.query(CampaignSendAttempt).filter(CampaignSendAttempt.campaign_id.in_(campaign_ids)).all()
    grouped: Dict[int, Dict[int, List[CampaignSendAttempt]]] = {}
    for a in atts:
        grouped.setdefault(int(a.campaign_id), {}).setdefault(int(a.send_log_id), []).append(a)
    for cid, logs in grouped.items():
        s = {
            "messages_attempted": 0, "messages_accepted": 0, "messages_delivered": 0,
            "messages_read": 0, "messages_failed_after_accept": 0,
            "messages_rejected": 0, "messages_uncertain": 0,
            "recipients_with_attempts": len(logs),
        }
        for key in (OUTCOME_DELIVERED_ONCE, OUTCOME_DELIVERED_MULTIPLE,
                    OUTCOME_ACCEPTED_MULTIPLE_UNPROVEN, OUTCOME_ACCEPTED_PENDING,
                    OUTCOME_FAILED_AFTER_ACCEPT, OUTCOME_FAILED_BEFORE_ACCEPT,
                    OUTCOME_UNCERTAIN, OUTCOME_IN_FLIGHT):
            s[f"recipients_{key}"] = 0
        for lst in logs.values():
            for a in lst:
                if a.state in POSSIBLY_SENT_STATES:
                    s["messages_attempted"] += 1
                if a.state == ATTEMPT_ACCEPTED:
                    s["messages_accepted"] += 1
                    if a.delivered_at is not None or a.read_at is not None:
                        s["messages_delivered"] += 1
                    if a.read_at is not None:
                        s["messages_read"] += 1
                    if a.failed_at is not None and a.delivered_at is None and a.read_at is None:
                        s["messages_failed_after_accept"] += 1
                elif a.state == ATTEMPT_REJECTED:
                    s["messages_rejected"] += 1
                elif a.state == ATTEMPT_UNCERTAIN:
                    s["messages_uncertain"] += 1
            outcome = classify_recipient(lst)
            if outcome != OUTCOME_NOT_STARTED:
                s[f"recipients_{outcome}"] += 1
        out[cid] = s
    return out


def log_ids_blocking_retry(db: Session, log_ids: List[int]) -> set:
    """Recipients that must NOT be re-queued: any attempt that may have
    produced a message (accepted, uncertain, in flight)."""
    if not log_ids:
        return set()
    rows = (
        db.query(CampaignSendAttempt.send_log_id)
        .filter(CampaignSendAttempt.send_log_id.in_(log_ids),
                CampaignSendAttempt.state.in_(tuple(POSSIBLY_SENT_STATES | IN_FLIGHT_STATES)))
        .distinct()
        .all()
    )
    return {int(r[0]) for r in rows}


def log_ids_with_ledger(db: Session, log_ids: List[int]) -> set:
    if not log_ids:
        return set()
    rows = (
        db.query(CampaignSendAttempt.send_log_id)
        .filter(CampaignSendAttempt.send_log_id.in_(log_ids))
        .distinct()
        .all()
    )
    return {int(r[0]) for r in rows}


def log_ids_proven_undelivered(db: Session, log_ids: List[int]) -> set:
    """Recipients the ledger proves never received a message: every
    attempt was refused / never sent, or was accepted and then reported
    ``failed`` without any delivered/read evidence.

    Rows without a ledger (pre-ledger history) are never in the result:
    the old dispatcher could overwrite a delivered wamid with a failed
    one, so their ``failed_at`` proves nothing about the recipient.
    """
    if not log_ids:
        return set()
    by_log: Dict[int, List[CampaignSendAttempt]] = {}
    for a in db.query(CampaignSendAttempt).filter(CampaignSendAttempt.send_log_id.in_(log_ids)):
        by_log.setdefault(int(a.send_log_id), []).append(a)
    out = set()
    for lid, atts in by_log.items():
        if classify_recipient(atts) in (OUTCOME_FAILED_AFTER_ACCEPT, OUTCOME_FAILED_BEFORE_ACCEPT):
            out.add(lid)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Shared Meta messaging limit
# ═══════════════════════════════════════════════════════════════════════════


def messaging_scope_key(conn: Any) -> Optional[str]:
    """The scope Meta's business-initiated limit applies to.

    Since October 2025 Meta applies the limit per business portfolio,
    shared by every number in it. We key on the portfolio (business
    manager) id when known, else the WABA, else the phone number — the
    narrower keys are only fallbacks for connections that never stored
    the portfolio id, and are documented as such.
    """
    if conn is None:
        return None
    for prefix, attr in (
        ("bm", "business_manager_id"),
        ("bm", "meta_business_account_id"),
        ("waba", "whatsapp_business_account_id"),
        ("phone", "phone_number_id"),
    ):
        v = str(getattr(conn, attr, "") or "").strip()
        if v:
            return f"{prefix}:{v}"[:160]
    return None


def parse_messaging_tier(raw: Any) -> Optional[int]:
    """``TIER_250`` / ``TIER_1K`` / ``TIER_2K`` / ``TIER_10K`` /
    ``TIER_100K`` / ``TIER_1000`` / ``TIER_UNLIMITED`` / ``UNLIMITED``.
    Returns -1 for unlimited, None when unparseable."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if "UNLIMITED" in s:
        return -1
    s = s.replace("TIER_", "").replace(",", "")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


@dataclass
class MessagingBudget:
    scope_key: Optional[str]
    limit: Optional[int]           # None = unlimited
    limit_source: str              # meta_fresh | meta_stale_fallback | unknown_fallback | unlimited
    budget: Optional[int]          # campaign share of the limit
    used: int = 0
    tier_raw: Optional[str] = None
    tier_updated_at: Optional[str] = None
    contacted_phones: set = field(default_factory=set)

    @property
    def remaining(self) -> Optional[int]:
        if self.budget is None:
            return None
        return max(0, self.budget - self.used)

    def allows(self, phone: str) -> bool:
        # A recipient already messaged inside the window does not consume
        # a new unique-user slot.
        if self.budget is None or phone in self.contacted_phones:
            return True
        return self.used < self.budget

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope_key": self.scope_key, "limit": self.limit, "limit_source": self.limit_source,
            "budget": self.budget, "used_24h": self.used, "remaining": self.remaining,
            "tier_raw": self.tier_raw, "tier_updated_at": self.tier_updated_at,
        }


def _scope_connections(db: Session, conn: Any, scope_key: Optional[str]) -> List[Any]:
    from models import WhatsAppConnection  # noqa: PLC0415
    if not scope_key or ":" not in scope_key:
        return [conn]
    prefix, value = scope_key.split(":", 1)
    col = {
        "bm": or_(WhatsAppConnection.business_manager_id == value,
                  WhatsAppConnection.meta_business_account_id == value),
        "waba": WhatsAppConnection.whatsapp_business_account_id == value,
        "phone": WhatsAppConnection.phone_number_id == value,
    }.get(prefix)
    if col is None:
        return [conn]
    rows = db.query(WhatsAppConnection).filter(col).all()
    return rows or [conn]


def messaging_budget(db: Session, conn: Any, *, now: Optional[datetime] = None) -> MessagingBudget:
    """Current 24h budget for the connection's messaging scope.

    The limit comes from the stored Meta tier only when it is fresh; a
    missing, unparseable or stale reading falls back to Meta's starting
    limit (250) — never to "unlimited". When several numbers share the
    scope, the smallest fresh reading wins.

    ``used`` counts distinct recipients of campaign attempts in the scope
    that may have produced a message (started, accepted, uncertain) in
    the last 24h. Other business-initiated templates (orders, automations)
    are not in this ledger — the budget percentage leaves room for them.
    """
    now = now or utcnow()
    scope = messaging_scope_key(conn)
    conns = _scope_connections(db, conn, scope)
    max_age = timedelta(hours=MESSAGING_LIMIT_MAX_AGE_HOURS)
    fresh: List[Tuple[int, Any, Any]] = []
    stale_raw: Optional[str] = None
    for c in conns:
        raw = getattr(c, "meta_messaging_limit", None)
        updated = _naive(getattr(c, "meta_tier_updated_at", None))
        val = parse_messaging_tier(raw)
        if val is None:
            continue
        if updated is not None and now - updated <= max_age:
            fresh.append((val, raw, updated))
        else:
            stale_raw = stale_raw or str(raw)

    if fresh:
        finite = [f for f in fresh if f[0] >= 0]
        if not finite:
            b = MessagingBudget(scope, None, "unlimited", None,
                                tier_raw=str(fresh[0][1]), tier_updated_at=fresh[0][2].isoformat())
            return b
        val, raw, updated = min(finite, key=lambda f: f[0])
        limit, source, tier_raw, tier_at = val, "meta_fresh", str(raw), updated.isoformat()
    else:
        limit = DEFAULT_MESSAGING_LIMIT
        source = "meta_stale_fallback" if stale_raw else "unknown_fallback"
        tier_raw, tier_at = stale_raw, None

    budget = max(0, (limit * max(0, min(100, CAMPAIGN_BUDGET_PERCENT))) // 100)
    since = now - MESSAGING_WINDOW
    phones = set()
    if scope:
        phones = {
            r[0] for r in (
                db.query(CampaignSendAttempt.customer_phone_e164)
                .filter(
                    CampaignSendAttempt.messaging_scope_key == scope,
                    CampaignSendAttempt.state.in_(tuple(BUDGET_STATES)),
                    func.coalesce(CampaignSendAttempt.request_started_at,
                                  CampaignSendAttempt.claimed_at) >= since,
                )
                .distinct()
                .all()
            )
        }
    # Recipient rows of every tenant whose number is in this scope —
    # covers sends made before the attempt ledger existed.
    tenant_ids = {int(t) for t in (getattr(c, "tenant_id", None) for c in conns) if t}
    if tenant_ids:
        phones.update(
            r[0] for r in (
                db.query(CampaignSendLog.customer_phone_e164)
                .filter(
                    CampaignSendLog.tenant_id.in_(tenant_ids),
                    or_(
                        CampaignSendLog.sent_at >= since,
                        and_(CampaignSendLog.status.in_((LOG_UNCERTAIN, "sending")),
                             CampaignSendLog.updated_at >= since),
                    ),
                )
                .distinct()
                .all()
            )
        )
    return MessagingBudget(scope, limit, source, budget, used=len(phones),
                           tier_raw=tier_raw, tier_updated_at=tier_at, contacted_phones=phones)


def post_accept_throttle(db: Session, scope_key: Optional[str], *,
                         now: Optional[datetime] = None) -> Optional[Tuple[str, int]]:
    """``(error_key, count)`` when Meta's post-accept failures in this
    messaging scope say to stop sending (e.g. 131048 spam rate limit),
    else None."""
    if not scope_key:
        return None
    since = (now or utcnow()) - POST_ACCEPT_BREAKER_WINDOW
    rows = (
        db.query(CampaignSendAttempt.post_accept_error_code, func.count(CampaignSendAttempt.id))
        .filter(
            CampaignSendAttempt.messaging_scope_key == scope_key,
            CampaignSendAttempt.failed_at >= since,
            CampaignSendAttempt.post_accept_error_code.in_(tuple(POST_ACCEPT_BREAKER_THRESHOLDS)),
        )
        .group_by(CampaignSendAttempt.post_accept_error_code)
        .all()
    )
    for key, cnt in rows:
        if int(cnt) >= POST_ACCEPT_BREAKER_THRESHOLDS.get(key, 1 << 30):
            return str(key), int(cnt)
    return None


__all__ = [name for name in dir() if not name.startswith("_")]
