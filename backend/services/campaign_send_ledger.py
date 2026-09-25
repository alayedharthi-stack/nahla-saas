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

import json
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
# How often a campaign waiting for capacity is re-checked when the exact
# moment a slot frees is unknown (e.g. usage from outside the ledger).
CAPACITY_RECHECK_INTERVAL = timedelta(minutes=_env_int("NAHLA_CAPACITY_RECHECK_MINUTES", 15))
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
# Meta rejected sends before accepting them with a retryable per-minute
# limit (codes 4 / 17 / 32 / 130429): a temporary backoff that the
# scheduler continues on its own — unlike post-accept delivery blocks.
PAUSE_PROVIDER_RATE_LIMITED = "provider_rate_limited"
# Only Meta's per-minute limit earns a timed backoff; any other retryable
# code repeating (unknown, service errors, …) stops for the merchant.
RATE_LIMIT_BUCKETS = frozenset({"rate_limit"})
PAUSE_PROVIDER_REPEATED_ERROR = "provider_repeated_error"
RATE_LIMIT_BACKOFF_BASE = timedelta(minutes=_env_int("NAHLA_CAMPAIGN_RATE_BACKOFF_MINUTES", 5))
RATE_LIMIT_BACKOFF_MAX = timedelta(minutes=_env_int("NAHLA_CAMPAIGN_RATE_BACKOFF_MAX_MINUTES", 60))
# Continue untouched recipients, never retry an accepted 131049 copy. These
# delays are Nahla policy, not a prediction of when Meta will deliver again.
MARKETING_BACKOFF_BASE = timedelta(hours=1)
MARKETING_BACKOFF_MAX = timedelta(hours=24)
MARKETING_BACKOFF_KEY = "_marketing_continuation_attempt"
# A run that ends for no recorded reason while recipients are still queued
# (e.g. a same-code breaker) is paused, never "completed".
PAUSE_RUN_ENDED_WITH_QUEUE = "run_ended_with_queue"
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
    # claimed | not_queued | lease_lost | stop_requested | conflict |
    # budget_exhausted | prior_send_evidence
    reason: str
    budget: Optional["MessagingBudget"] = None
    evidence: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# No duplicate send
# ═══════════════════════════════════════════════════════════════════════════
#
# A recipient may be sent only when there is affirmative evidence that no
# earlier attempt in this campaign may have reached Meta. The ``queued``
# compare-and-set protects one row; a recipient can have several rows (the
# same number stored as ``+966…``, ``966…``, ``00966…`` or ``05…``), a row
# can be put back to ``queued`` after it was sent, and a copy's only trace
# can be a ``message_events`` row or a delivery receipt. The claim therefore
# checks every trace of the recipient — matched by its validated identity
# (``services.recipient_identity``) — under a per-recipient lock, and
# refuses, durably and with the reason, unless each one proves "never
# accepted". The absence of a delivered/read receipt is never such proof;
# neither is a source that cannot be read or a copy that cannot be placed.

LOG_SKIPPED_SEND_GUARD = "skipped_send_guard"
PRIOR_SEND_ERROR = "prior_send_evidence"
EVIDENCE_UNREADABLE = "evidence_unreadable"
PAUSE_EVIDENCE_UNREADABLE = "evidence_unreadable"
# Evidence that concerns the whole campaign rather than one recipient (a
# copy nobody can be tied to could be anyone's): the run pauses and every
# row stays queued, instead of each recipient being skipped for good.
EVIDENCE_UNRESOLVED = "evidence_unresolved"
PAUSE_EVIDENCE_UNRESOLVED = "evidence_unresolved"
CAMPAIGN_WIDE_EVIDENCE = frozenset({"message_events_unplaceable_copy"})

# Attempt states that prove the request produced no accepted copy: Meta
# answered with an error (rejected), the transport failed before the
# request left (not_sent), or the request never started (abandoned).
PROVEN_NOT_ACCEPTED_STATES = frozenset({ATTEMPT_REJECTED, ATTEMPT_NOT_SENT, ATTEMPT_ABANDONED})
# Row statuses that say a copy may have been accepted.
_ROW_MAY_HAVE_REACHED_META = frozenset({"sending", "sent", "delivered", "read", "uncertain"})


def _suffix_prefilter_sql(db: Session, col: str) -> str:
    """SQL condition that keeps every row whose ``col`` may be the recipient
    (``:tail`` = the recipient's last digits) — a superset; the match itself
    is ``recipient_identity.may_be_recipient``. On PostgreSQL an ASCII value
    is narrowed by its ASCII digits, and any value with a non-ASCII
    character (Arabic-Indic / Persian / fullwidth digits, …) is always kept
    for Python to decide. On SQLite the Python digit function is used."""
    if db.get_bind().dialect.name == "postgresql":
        return (f"(regexp_replace(coalesce({col}, ''), '[^0-9]', '', 'g') LIKE :tail "
                f"OR coalesce({col}, '') ~ '[^\x01-\x7F]')")
    from services.recipient_identity import digits  # noqa: PLC0415
    db.connection().connection.dbapi_connection.create_function(
        "nahla_digits", 1, digits, deterministic=True)
    return f"nahla_digits({col}) LIKE :tail"


def _lock_recipient(db: Session, campaign_id: int, canonical: str) -> None:
    """Serialise every claim for one recipient of one campaign, whichever
    of its rows or spellings (PostgreSQL advisory lock, released at
    commit/rollback)."""
    if db.get_bind().dialect.name != "postgresql" or not canonical:
        return
    from sqlalchemy import text  # noqa: PLC0415
    from services.recipient_identity import lock_key  # noqa: PLC0415
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"),
               {"k": lock_key(f"campaign-recipient:{campaign_id}", canonical)})


def _recipient_rows(db: Session, ctx: Dict[str, Any], table: str, cols: str) -> List[Any]:
    """Rows of ``table`` for this campaign whose phone is, or may be, the
    recipient. SQL narrows by the number's last digits; the identity match
    decides."""
    from sqlalchemy import text  # noqa: PLC0415
    from services.recipient_identity import may_be_recipient  # noqa: PLC0415
    rows = db.execute(text(
        f"SELECT customer_phone_e164, {cols} FROM {table} WHERE campaign_id = :c "
        f"AND {_suffix_prefilter_sql(db, 'customer_phone_e164')}"),
        {"c": ctx["campaign_id"], "tail": f"%{ctx['suffix']}"}).all()
    return [r[1:] for r in rows if may_be_recipient(r[0], ctx["canonical"])]


def _check_attempts(db: Session, ctx: Dict[str, Any]) -> Optional[str]:
    """Every attempt of this campaign for the recipient, on any of its rows.
    The raw fields decide as much as the state label: a wamid, an accepted
    or receipt time, or a started request outside a proven failure blocks."""
    for st, wamid, acc, dl, rd, started in _recipient_rows(
            db, ctx, "campaign_send_attempts",
            "state, provider_message_id, accepted_at, delivered_at, read_at, request_started_at"):
        if rd:
            return "attempt_read"
        if dl:
            return "attempt_delivered"
        if acc or wamid:
            return "attempt_accepted"
        if st not in PROVEN_NOT_ACCEPTED_STATES:
            return f"attempt_{st}"
        if st == ATTEMPT_ABANDONED and started is not None:
            return "attempt_abandoned_after_request_start"
    return None


def _check_rows(db: Session, ctx: Dict[str, Any]) -> Optional[str]:
    """Every row of this campaign for the recipient: a stored wamid,
    ``sent_at`` or receipt, a status that may have reached Meta, or a
    legacy attempt count the ledger cannot account for."""
    rows = _recipient_rows(db, ctx, "campaign_send_logs",
                           "id, status, provider_message_id, sent_at, delivered_at, read_at, "
                           "attempt_count")
    ids = [int(r[0]) for r in rows]
    ledger_attempts = dict(
        db.query(CampaignSendAttempt.send_log_id, func.count(CampaignSendAttempt.id))
        .filter(CampaignSendAttempt.send_log_id.in_(ids))
        .group_by(CampaignSendAttempt.send_log_id)
    ) if ids else {}
    for rid, st, wamid, sent, dl, rd, count in rows:
        own = int(rid) == ctx["log_id"]
        if rd:
            return "row_read"
        if dl:
            return "row_delivered"
        if wamid or sent:
            return "row_accepted"
        if not own and (st or "").lower() in _ROW_MAY_HAVE_REACHED_META:
            return f"duplicate_row_{(st or '').lower()}"
        # Our own claim already added 1 to this row's counter.
        made = int(count or 0) - (1 if own else 0)
        if made > int(ledger_attempts.get(int(rid), 0)):
            return "legacy_attempt_unproven"
    return None


@dataclass
class _CopyIndex:
    """This campaign's outbound ``message_events`` copies, placed on
    recipients. Built inside the claim's own transaction and never reused
    across claims: attribution depends on rows that can change in place
    (an event's conversation, a conversation's customer, a customer's
    phone, a row's or attempt's wamid), so no cheap fingerprint can prove a
    cached copy is still right."""
    placed: Dict[str, str] = field(default_factory=dict)     # identity -> reason
    possible: List[Tuple[str, str]] = field(default_factory=list)  # (significant digits, reason)
    unplaceable: int = 0


def _campaign_copies_sql(db: Session) -> Tuple[str, str]:
    if db.get_bind().dialect.name == "postgresql":
        return "(me.metadata->>'campaign_id')", "conv.metadata->>'customer_phone'"
    return ("CAST(json_extract(me.metadata, '$.campaign_id') AS TEXT)",
            "json_extract(conv.metadata, '$.customer_phone')")


def _build_copy_index(db: Session, ctx: Dict[str, Any]) -> _CopyIndex:
    """Place every copy on a recipient: by its conversation's customer
    (phone, normalised phone, ``conversation.metadata.customer_phone``) and
    by whoever owns its wamid in this campaign (an attempt, a row, or a
    receipt linked to a row). One identity → that recipient. Two or more,
    or a link that contradicts the owner → every one of them is blocked
    (``message_events_conflict``). None, and nothing that could be a
    spelling of someone → ``unplaceable``: nobody in the campaign can be
    proven clean."""
    from sqlalchemy import text  # noqa: PLC0415
    from services.recipient_identity import canonical_recipient, digits  # noqa: PLC0415
    cid, conv_phone = _campaign_copies_sql(db)
    p = {"t": ctx["tenant_id"], "c": str(ctx["campaign_id"]), "ci": ctx["campaign_id"]}
    owners: Dict[str, set] = {}
    for w, ph in db.execute(text(
            "SELECT provider_message_id, customer_phone_e164 FROM campaign_send_attempts "
            "WHERE campaign_id = :ci AND provider_message_id IS NOT NULL "
            "UNION ALL SELECT provider_message_id, customer_phone_e164 FROM campaign_send_logs "
            "WHERE campaign_id = :ci AND provider_message_id IS NOT NULL "
            "UNION ALL SELECT mde.wamid, sl.customer_phone_e164 FROM message_delivery_events mde "
            "JOIN campaign_send_logs sl ON sl.id = mde.campaign_send_log_id WHERE sl.campaign_id = :ci"),
            p).all():
        owners.setdefault(w, set()).add(ph)
    idx = _CopyIndex()
    for md, cu_phone, cu_norm, cv_phone in db.execute(text(
            f"SELECT me.metadata, cu.phone, cu.normalized_phone, {conv_phone} FROM message_events me "
            "LEFT JOIN conversations conv ON conv.id = me.conversation_id AND conv.tenant_id = me.tenant_id "
            "LEFT JOIN customers cu ON cu.id = conv.customer_id AND cu.tenant_id = me.tenant_id "
            "WHERE me.tenant_id = :t AND me.direction = 'outbound' AND me.event_type = 'campaign' "
            f"AND {cid} = :c"), p).all():
        if isinstance(md, str):          # JSON returned as text (SQLite)
            try:
                md = json.loads(md)
            except ValueError:
                md = None
        md = md if isinstance(md, dict) else {}
        ps = md.get("provider_send")
        wamid = md.get("wa_message_id") or (ps.get("wamid") if isinstance(ps, dict) else None)
        # The event's own record of whom it was sent to counts as a link too.
        linked = [x for x in (cu_phone, cu_norm, cv_phone, md.get("customer_phone"),
                              md.get("phone")) if x]
        owned = sorted(owners.get(wamid, ())) if wamid else []
        linked_ids = {canonical_recipient(x) for x in linked} - {None}
        owned_ids = {canonical_recipient(x) for x in owned} - {None}
        loose = {digits(x).lstrip("0") for x in linked + owned if canonical_recipient(x) is None}
        loose = {d for d in loose if len(d) >= 7}
        # Every identity any link or owner names is a candidate. One is a
        # copy; more than one — however they overlap — is a conflict, and
        # every candidate is blocked.
        ids = linked_ids | owned_ids
        reason = "message_events_copy" if len(ids) == 1 else "message_events_conflict"
        for i in ids:
            if reason == "message_events_conflict" or idx.placed.get(i) == "message_events_conflict":
                idx.placed[i] = "message_events_conflict"
            else:
                idx.placed[i] = "message_events_copy"
        for d in loose:
            idx.possible.append((d, "message_events_possible_copy"))
        if not ids and not loose:
            idx.unplaceable += 1
    return idx


def _check_message_events(db: Session, ctx: Dict[str, Any]) -> Optional[str]:
    from services.recipient_identity import digits  # noqa: PLC0415
    idx = _build_copy_index(db, ctx)
    if ctx["canonical"] in idx.placed:
        return idx.placed[ctx["canonical"]]
    mine = digits(ctx["canonical"])
    for significant, reason in idx.possible:
        if mine.endswith(significant):
            return reason
    if idx.unplaceable:
        return "message_events_unplaceable_copy"
    return None


def _check_delivery_events(db: Session, ctx: Dict[str, Any]) -> Optional[str]:
    """A provider receipt (delivered / read / failed) for any row of the
    recipient means Meta accepted that copy."""
    from sqlalchemy import text  # noqa: PLC0415
    from services.recipient_identity import may_be_recipient  # noqa: PLC0415
    for phone, status in db.execute(text(
            "SELECT sl.customer_phone_e164, mde.status FROM message_delivery_events mde "
            "JOIN campaign_send_logs sl ON sl.id = mde.campaign_send_log_id "
            "WHERE sl.campaign_id = :c AND mde.wamid NOT LIKE 'synth:%' "
            f"AND {_suffix_prefilter_sql(db, 'sl.customer_phone_e164')}"),
            {"c": ctx["campaign_id"], "tail": f"%{ctx['suffix']}"}).all():
        if may_be_recipient(phone, ctx["canonical"]):
            return f"delivery_event_{status}"
    return None


PRIOR_SEND_CHECKS = (_check_attempts, _check_rows, _check_message_events, _check_delivery_events)


def prior_send_evidence(db: Session, *, campaign_id: int, tenant_id: int, log_id: int,
                        phone: Any) -> Optional[str]:
    """Why this recipient must not be sent, or ``None`` when every trace of
    it in the campaign proves no copy was accepted. A read error propagates:
    the caller refuses the claim."""
    from services.recipient_identity import canonical_recipient, match_suffix  # noqa: PLC0415
    canonical = canonical_recipient(phone)
    if not canonical:
        return "recipient_identity_unresolved"
    ctx = {"campaign_id": int(campaign_id), "tenant_id": int(tenant_id), "log_id": int(log_id),
           "canonical": canonical, "suffix": match_suffix(canonical)}
    for check in PRIOR_SEND_CHECKS:
        why = check(db, ctx)
        if why:
            return why
    return None


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
    phone_key = (
        db.query(CampaignSendLog.customer_phone_e164)
        .filter(CampaignSendLog.id == log_id)
        .scalar()
    )
    from services.recipient_identity import canonical_recipient  # noqa: PLC0415
    _lock_recipient(db, campaign_id, canonical_recipient(phone_key) or "")
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
    try:
        why = prior_send_evidence(db, campaign_id=campaign_id, tenant_id=tenant_id,
                                  log_id=log_id, phone=phone_key)
    except Exception as exc:  # noqa: BLE001, silent-ok — refused and logged; the run pauses on it
        # A source that cannot be read — or any error while reading it — is
        # never permission to send: the claim is rolled back (the row stays
        # queued, nothing half-written), refused, and the run stops.
        db.rollback()
        logger.error("[campaign_ledger] campaign=%d row=%d evidence unreadable: %s",
                     campaign_id, log_id, type(exc).__name__)
        return ClaimResult(None, EVIDENCE_UNREADABLE, budget, evidence=type(exc).__name__)
    if why in CAMPAIGN_WIDE_EVIDENCE:
        db.rollback()
        logger.error("[campaign_ledger] campaign=%d paused: %s", campaign_id, why)
        return ClaimResult(None, EVIDENCE_UNRESOLVED, budget, evidence=why)
    if why:
        # Refused durably: the row leaves the queue with its reason, so no
        # later run, resume or retry picks it up again.
        db.execute(
            update(CampaignSendLog)
            .where(CampaignSendLog.id == log_id)
            .values(
                status=LOG_SKIPPED_SEND_GUARD,
                attempt_count=func.greatest(func.coalesce(CampaignSendLog.attempt_count, 1) - 1, 0)
                if db.get_bind().dialect.name == "postgresql"
                else func.max(func.coalesce(CampaignSendLog.attempt_count, 1) - 1, 0),
                error_code=PRIOR_SEND_ERROR,
                skip_reason=why[:64],
                error_message=f"[{PRIOR_SEND_ERROR}] {why}: not sent — nothing proves this "
                              "recipient has not already received this campaign",
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        logger.warning("[campaign_ledger] campaign=%d row=%d not sent: %s", campaign_id,
                       log_id, why)
        cached = db.get(CampaignSendLog, log_id)
        if cached is not None:
            db.refresh(cached)
        return ClaimResult(None, PRIOR_SEND_ERROR, budget, evidence=why)
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
    limit_source: str              # meta_fresh | meta_stale_fallback | unknown_fallback | unlimited | portfolio_unknown
    budget: Optional[int]          # campaign share of the limit
    used: int = 0
    tier_raw: Optional[str] = None
    tier_updated_at: Optional[str] = None
    contacted_phones: set = field(default_factory=set)
    # When the next slot frees up: the moment ``used`` drops below
    # ``budget`` as counted recipients age out of the 24h window (None
    # when there is room now, or nothing to wait for).
    next_slot_at: Optional[datetime] = None
    window_since: Optional[datetime] = None

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
            "next_slot_at": self.next_slot_at.isoformat() if self.next_slot_at else None,
            "window_since": self.window_since.isoformat() if self.window_since else None,
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
    if not (scope or "").startswith("bm:"):
        # Meta's limit is shared by every WABA and number in the business
        # portfolio. Without the portfolio identity a narrower (WABA /
        # number) budget could let two WABAs of one portfolio each admit
        # their own share — so nothing is admitted until it is known (the
        # tier sync resolves it from Meta; the campaign waits meanwhile).
        source, budget = "portfolio_unknown", 0
    since = now - MESSAGING_WINDOW
    usage = messaging_usage(db, scope, conns, since=since)
    last_seen = {p: max((v["ts"] for v in rows if v["ts"] is not None), default=None)
                 for p, rows in usage.items()}
    used = len(last_seen)
    next_slot = None
    if budget is not None and used >= budget and last_seen:
        # The (used - budget + 1)-th oldest recipient to age out frees
        # the first slot.
        expiries = sorted(t for t in last_seen.values() if t is not None)
        k = used - budget
        if k < len(expiries):
            next_slot = expiries[k] + MESSAGING_WINDOW
    return MessagingBudget(scope, limit, source, budget, used=used,
                           tier_raw=tier_raw, tier_updated_at=tier_at,
                           contacted_phones=set(last_seen), next_slot_at=next_slot,
                           window_since=since)


def scope_key_aliases(scope: Optional[str], conns: List[Any]) -> List[str]:
    """Every scope key an attempt of these connections may have been
    recorded under: the scope itself plus each connection's portfolio,
    WABA and number keys — so a connection whose portfolio was resolved
    later still counts the attempts it recorded under a narrower key."""
    keys = {scope} if scope else set()
    for c in conns:
        for prefix, attr in (("bm", "business_manager_id"), ("bm", "meta_business_account_id"),
                             ("waba", "whatsapp_business_account_id"),
                             ("phone", "phone_number_id")):
            v = str(getattr(c, attr, "") or "").strip()
            if v:
                keys.add(f"{prefix}:{v}"[:160])
    return sorted(keys)


def messaging_usage(db: Session, scope: Optional[str], conns: List[Any], *,
                    since: datetime) -> Dict[str, List[Dict[str, Any]]]:
    """Recipient phones that may have taken one of Meta's unique-user
    slots since ``since``, each with the evidence that counted it.

    Meta limits the unique users a business *delivers* business-initiated
    messages to in a moving 24h window. What we can see:

    * ledger attempts (``source='attempt'``) in the scope that may have
      produced a message — started, accepted or uncertain. An accepted
      attempt Meta later reported failed, with no delivered/read receipt,
      did not reach the user and does not count;
    * send-log rows of every tenant with a number in the scope
      (``source='send_log'``) for sends the ledger does not cover — rows
      from before the attempt ledger. Their ``failed_at`` cannot be tied
      to one copy (a duplicate may have been delivered), so they always
      count; a row whose ``sent_at`` comes from its own ledger attempts is
      left to the ledger.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    keys = scope_key_aliases(scope, conns)
    if keys:
        rows = (
            db.query(CampaignSendAttempt.customer_phone_e164, CampaignSendAttempt.campaign_id,
                     CampaignSendAttempt.tenant_id, CampaignSendAttempt.state,
                     CampaignSendAttempt.request_started_at, CampaignSendAttempt.claimed_at,
                     CampaignSendAttempt.failed_at, CampaignSendAttempt.delivered_at,
                     CampaignSendAttempt.read_at)
            .filter(
                CampaignSendAttempt.messaging_scope_key.in_(keys),
                CampaignSendAttempt.state.in_(tuple(BUDGET_STATES)),
                func.coalesce(CampaignSendAttempt.request_started_at,
                              CampaignSendAttempt.claimed_at) >= since,
            )
            .all()
        )
        for phone, cid, tid, state, started, claimed, failed, dl, rd in rows:
            if (state == ATTEMPT_ACCEPTED and failed is not None
                    and dl is None and rd is None):
                continue                      # Meta said it never reached the user
            out.setdefault(phone, []).append({
                "source": "attempt", "campaign_id": cid, "tenant_id": tid,
                "ts": _naive(started or claimed),
            })
    tenant_ids = {int(t) for t in (getattr(c, "tenant_id", None) for c in conns) if t}
    if tenant_ids:
        logs = (
            db.query(CampaignSendLog.id, CampaignSendLog.customer_phone_e164,
                     CampaignSendLog.campaign_id, CampaignSendLog.tenant_id,
                     CampaignSendLog.status, CampaignSendLog.sent_at, CampaignSendLog.updated_at)
            .filter(
                CampaignSendLog.tenant_id.in_(tenant_ids),
                or_(
                    CampaignSendLog.sent_at >= since,
                    and_(CampaignSendLog.status.in_((LOG_UNCERTAIN, "sending")),
                         CampaignSendLog.updated_at >= since),
                ),
            )
            .all()
        )
        ids = [int(r[0]) for r in logs]
        first_claim: Dict[int, datetime] = {}
        for i in range(0, len(ids), 1000):
            for lid, first in (
                db.query(CampaignSendAttempt.send_log_id, func.min(CampaignSendAttempt.claimed_at))
                .filter(CampaignSendAttempt.send_log_id.in_(ids[i:i + 1000]))
                .group_by(CampaignSendAttempt.send_log_id)
                .all()
            ):
                first_claim[int(lid)] = _naive(first)
        for lid, phone, cid, tid, status, sent_at, updated_at in logs:
            fc = first_claim.get(int(lid))
            if fc is not None and sent_at is not None and _naive(sent_at) >= fc:
                continue                      # this send is the ledger's to count
            if fc is not None and sent_at is None:
                continue                      # uncertain/sending row owned by the ledger
            out.setdefault(phone, []).append({
                "source": "send_log", "campaign_id": cid, "tenant_id": tid,
                "ts": _naive(sent_at if sent_at is not None and _naive(sent_at) >= since
                             else updated_at),
            })
    return out


CAPACITY_WAIT_KEY = "_capacity_wait"
# Who may write a wait that the scheduler acts on. Only a dispatch run of
# this version that was itself stopped by the shared limit records it. A
# campaign that is merely ``paused`` with ``pause_reason=messaging_limit_reached``
# -- e.g. paused by an older build, before this record existed -- carries no
# authority and continues only on the merchant's explicit resume.
CAPACITY_WAIT_AUTHORITY = "dispatch_run_v2"


def record_capacity_wait(campaign: Any, budget: Optional[MessagingBudget], *,
                         now: Optional[datetime] = None) -> Dict[str, Any]:
    """Persist on the campaign when it may continue after Meta's shared
    messaging limit stopped it (stage only; caller commits). Durable
    across restarts and deploys: the scheduler reads it from the DB."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    now = now or utcnow()
    next_at = budget.next_slot_at if budget is not None else None
    wait = {
        "reason": PAUSE_MESSAGING_LIMIT,
        "authority": CAPACITY_WAIT_AUTHORITY,
        "since": now.isoformat(),
        "next_eligible_at": (next_at or now + CAPACITY_RECHECK_INTERVAL).isoformat(),
        "next_eligible_exact": next_at is not None,
        "scope_key": budget.scope_key if budget else None,
        "used_24h": budget.used if budget else None,
        "budget": budget.budget if budget else None,
        "limit": budget.limit if budget else None,
        "limit_source": budget.limit_source if budget else None,
    }
    tv = dict(campaign.template_variables or {})
    tv[CAPACITY_WAIT_KEY] = wait
    campaign.template_variables = tv
    flag_modified(campaign, "template_variables")
    return wait


def store_capacity_wait(campaign: Any, wait: Dict[str, Any]) -> None:
    """Write back a (modified) wait record (stage only; caller commits)."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    tv = dict(campaign.template_variables or {})
    tv[CAPACITY_WAIT_KEY] = dict(wait)
    campaign.template_variables = tv
    flag_modified(campaign, "template_variables")


def clear_capacity_wait(campaign: Any) -> None:
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    tv = dict(campaign.template_variables or {})
    if CAPACITY_WAIT_KEY in tv:
        tv.pop(CAPACITY_WAIT_KEY, None)
        campaign.template_variables = tv
        flag_modified(campaign, "template_variables")


def capacity_wait(campaign: Any) -> Optional[Dict[str, Any]]:
    tv = campaign.template_variables or {}
    w = tv.get(CAPACITY_WAIT_KEY) if isinstance(tv, dict) else None
    return w if isinstance(w, dict) else None


# Pauses the scheduler may continue on its own, once their recorded wait
# has passed. provider_throttling additionally requires an explicit, typed
# marketing-only wait below. Spam/uncertain/legacy pauses remain manual.
AUTO_RESUME_REASONS = frozenset({
    PAUSE_MESSAGING_LIMIT, PAUSE_PROVIDER_RATE_LIMITED, PAUSE_PROVIDER_THROTTLING,
})


def record_marketing_wait(campaign: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Durable, increasing cooldown for untouched recipients after 131049.

    The counter survives intervening capacity/rate waits and restarts. Never
    requeue accepted/uncertain copies; normal claim guards still own admission.
    """
    now = now or utcnow()
    tv = dict(campaign.template_variables or {})
    try:
        attempt = max(0, min(24, int(tv.get(MARKETING_BACKOFF_KEY, 0)))) + 1
    except (ValueError, TypeError, OverflowError):
        attempt = 25  # malformed state can lengthen a wait, never shorten it
    delay = min(MARKETING_BACKOFF_BASE * (2 ** min(attempt - 1, 5)), MARKETING_BACKOFF_MAX)
    wait = {
        "reason": PAUSE_PROVIDER_THROTTLING,
        "authority": CAPACITY_WAIT_AUTHORITY,
        "error_key": "marketing_blocked",
        "continuation": "untouched_recipients_only",
        "since": now.isoformat(),
        "next_eligible_at": (now + delay).isoformat(),
        "next_eligible_exact": False,
        "attempt": attempt,
    }
    prev = capacity_wait(campaign) or {}
    if (prev.get("reason") == PAUSE_PROVIDER_RATE_LIMITED
            or prev.get("requeue_rate_limited")):
        wait["requeue_rate_limited"] = True
    tv[MARKETING_BACKOFF_KEY] = attempt
    campaign.template_variables = tv
    store_capacity_wait(campaign, wait)
    return wait


def record_rate_limit_wait(campaign: Any, *, detail: str,
                           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Persist a backoff after Meta's per-minute limit rejected sends:
    ``RATE_LIMIT_BACKOFF_BASE`` doubling for each consecutive backoff, capped
    at ``RATE_LIMIT_BACKOFF_MAX`` (stage only; caller commits)."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    now = now or utcnow()
    prev = capacity_wait(campaign) or {}
    # A stale record left from an earlier backoff can only lengthen this
    # one (never shorten it or re-send anything) — the safe direction.
    attempt = int(prev.get("attempt", 0)) + 1 if prev.get("reason") == PAUSE_PROVIDER_RATE_LIMITED else 1
    delay = min(RATE_LIMIT_BACKOFF_BASE * (2 ** (attempt - 1)), RATE_LIMIT_BACKOFF_MAX)
    wait = {
        "reason": PAUSE_PROVIDER_RATE_LIMITED,
        "authority": CAPACITY_WAIT_AUTHORITY,
        "since": now.isoformat(),
        "next_eligible_at": (now + delay).isoformat(),
        "next_eligible_exact": False,
        "attempt": attempt,
        "detail": (detail or "")[:200],
    }
    tv = dict(campaign.template_variables or {})
    tv[CAPACITY_WAIT_KEY] = wait
    campaign.template_variables = tv
    flag_modified(campaign, "template_variables")
    return wait


def authorized_capacity_wait(template_variables: Any,
                             reason: str = PAUSE_MESSAGING_LIMIT) -> Optional[Dict[str, Any]]:
    """The recorded wait, only when it authorises an automatic resume: written
    by a dispatch run of this version (``CAPACITY_WAIT_AUTHORITY``) for
    ``reason`` (one of ``AUTO_RESUME_REASONS``), with a parseable
    ``next_eligible_at``. Anything else -- no record, a record from an
    older build, another reason, or a malformed one -- returns ``None``:
    the campaign waits for the merchant."""
    w = template_variables.get(CAPACITY_WAIT_KEY) if isinstance(template_variables, dict) else None
    if not isinstance(w, dict):
        return None
    if reason not in AUTO_RESUME_REASONS:
        return None
    if w.get("authority") != CAPACITY_WAIT_AUTHORITY or w.get("reason") != reason:
        return None
    if reason == PAUSE_PROVIDER_THROTTLING and (
        w.get("error_key") != "marketing_blocked"
        or w.get("continuation") != "untouched_recipients_only"
    ):
        return None
    try:
        datetime.fromisoformat(str(w.get("next_eligible_at")))
    except ValueError:
        return None
    return w


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
    # A concurrent spam/rate block must never be masked by a 131049 block
    # and accidentally earn automatic marketing continuation.
    for key, cnt in sorted(rows, key=lambda row: (row[0] == "marketing_blocked", row[0])):
        if int(cnt) >= POST_ACCEPT_BREAKER_THRESHOLDS.get(key, 1 << 30):
            return str(key), int(cnt)
    return None


def post_accept_throttle_status(db: Session, scope_key: Optional[str], *,
                                now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The post-accept breaker as it stands now, or None when it is clear.

    The breaker counts Meta's post-accept failures of the whole messaging
    scope over a sliding ``POST_ACCEPT_BREAKER_WINDOW`` — not per run — so
    a run started while it is tripped stops at its first recipient
    without sending. ``clears_at`` is when enough of the counted failures
    leave the window for the count to fall below the threshold (receipts
    that arrive later can push it back)."""
    if not scope_key:
        return None
    now = now or utcnow()
    since = now - POST_ACCEPT_BREAKER_WINDOW
    tripped = post_accept_throttle(db, scope_key, now=now)
    if tripped is None:
        return None
    key, count = tripped
    threshold = POST_ACCEPT_BREAKER_THRESHOLDS[key]
    times = [
        _naive(t) for (t,) in
        db.query(CampaignSendAttempt.failed_at)
        .filter(
            CampaignSendAttempt.messaging_scope_key == scope_key,
            CampaignSendAttempt.failed_at >= since,
            CampaignSendAttempt.post_accept_error_code == key,
        )
        .order_by(CampaignSendAttempt.failed_at)
        .all()
    ]
    # Below the threshold once count - k < threshold, i.e. after the
    # (count - threshold + 1) oldest have left the window.
    k = max(1, len(times) - threshold + 1)
    # The window includes its lower bound, so the count drops only just
    # after this instant.
    clears_at = (times[k - 1] + POST_ACCEPT_BREAKER_WINDOW + timedelta(seconds=1)
                 if len(times) >= k else now)
    return {"key": key, "count": count, "threshold": threshold,
            "window_minutes": int(POST_ACCEPT_BREAKER_WINDOW.total_seconds() // 60),
            "clears_at": clears_at.isoformat()}


__all__ = [name for name in dir() if not name.startswith("_")]
