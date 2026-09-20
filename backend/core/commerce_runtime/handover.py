"""The shared barrier a handover is actually performed against.

An environment flag is read per process. It cannot stop another replica from
admitting a turn, it cannot be observed by anything but the process that holds
it, and it changes at a moment nobody can name. A handover decided on one is a
handover decided on nothing: the count comes back zero because the replica that
was about to admit had not written its row yet.

So the barrier lives in the database, in the runtime's **own** tables
(``core.commerce_runtime.handover_models``, revision ``0110``). It used to live
under a namespaced key in ``tenant_settings.metadata``; that document has other
writers, every one of them read-modify-write over the whole JSON, and any of
them could put back a copy taken before a drain and silently reopen it.
Coordinating all of them would mean rewriting settings code with nothing to do
with this handover. Runtime state belongs where only the runtime writes.

    open  →  draining  →  settled  →  open

**draining** is not "new turns go to legacy". While a tenant is draining, an
inbound for an affected recipient is *deferred*: recorded durably with its own
identity and payload, answered by nobody, and left for the operator to dispose
of. Releasing it to the legacy path would be the one thing a handover must not
do — answering, on a second runtime, a conversation whose first runtime may
still have a send in flight.

Three things are recorded, and each exists because a count alone cannot say it:

* **workers** — one row per process that has evaluated a route, carrying the
  generation and state **it observed**. Convergence is then observable: every
  live worker reporting the current generation is evidence that the drain
  reached the fleet, where an attestation is only a statement that it did. A
  worker that has gone quiet is *stale*, never "gone": only an operator retires
  one, and that decision is recorded with their name on it.
* **deferred inbound** — one row per accepted message nobody has finished, with
  tenant, channel connection, recipient, the provider's own message id and the
  payload a replay needs. Nothing is acknowledged and quietly dropped;
  settlement is blocked until each row has a checked disposition.
* **evidence** — the settlement snapshot, written in the same transaction as
  the transition it describes, so it can never describe a state that was
  already stale when it was recorded.

Every transition validates and applies in **one** transaction under the
tenant's advisory lock: the state and generation it expected, the work still
pending, and the write, decided together. Nothing here sends anything.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import logging
import os
import socket
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import handover_models as hm

logger = logging.getLogger("nahla.commerce_runtime.handover")

NAMESPACE = "live"

STATE_OPEN = hm.STATE_OPEN
STATE_DRAINING = hm.STATE_DRAINING
STATE_SETTLED = hm.STATE_SETTLED
STATES: Tuple[str, ...] = hm.BARRIER_STATES

REASON_ACCEPTED = hm.REASON_ACCEPTED
REASON_DRAIN_BUFFERED = hm.REASON_DRAIN_BUFFERED
REASON_PROCESS_DRAINING = hm.REASON_PROCESS_DRAINING
REASON_ADMISSION_REFUSED = hm.REASON_ADMISSION_REFUSED
REASON_SETTLED_WINDOW = hm.REASON_SETTLED_WINDOW

DISPOSITIONS: Tuple[str, ...] = hm.DISPOSITIONS

# A worker heard from more recently than this is treated as reporting. One last
# heard from before that is **stale**, not gone: it blocks a handover until an
# operator retires it deliberately.
WORKER_LIVE_SECONDS = 900.0

# How often one process writes its heartbeat. The pilot is owner-only, so this
# is a handful of rows; the throttle keeps it off the per-turn write path.
HEARTBEAT_INTERVAL_SECONDS = 30.0

# How many pending deferred rows one tenant may hold before the runtime stops
# accepting new work for it. Disposed and resolved history does not count: the
# pending index is partial, and retention is a separate operator decision.
MAX_PENDING_DEFERRED = 500

# A tenant-scoped advisory lock, taken for the length of a transaction. It is
# what makes the barrier and an admission *ordered* rather than merely close
# together: a transition takes it exclusively, an admission or a deferred write
# takes it shared, and the database decides which happened first. An advisory
# key needs no row, so the ordering holds even for a tenant whose barrier row
# does not exist yet — which is exactly the tenant a row lock would fail to
# protect.
#
# PostgreSQL only. On any other dialect the lock is skipped and the state is
# still read and written; the pilot runs on PostgreSQL, and the operator
# procedure states this.
ADVISORY_LOCK_NAMESPACE = 0x6E68_4C56 & 0x7FFF_FFFF      # "nhLV", kept positive

_heartbeat_lock = threading.Lock()
_last_heartbeat: Dict[Tuple[int, int], float] = {}


def worker_id() -> str:
    """This process, named the same way for the whole of its life."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _aware(moment: Any) -> Optional[_dt.datetime]:
    if moment is None:
        return None
    if isinstance(moment, _dt.datetime):
        return moment if moment.tzinfo else moment.replace(tzinfo=_dt.timezone.utc)
    try:
        parsed = _dt.datetime.fromisoformat(str(moment))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


# ── Values ───────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class WorkerReport:
    """What one process last said about the barrier **it** read."""

    worker_id: str
    observed_generation: int
    observed_state: str
    seen_at: Optional[_dt.datetime]
    retired_at: Optional[_dt.datetime] = None
    retired_by: Optional[str] = None
    retired_reason: Optional[str] = None

    @property
    def retired(self) -> bool:
        return self.retired_at is not None

    def reporting(self, *, now: Optional[_dt.datetime] = None,
                  window: float = WORKER_LIVE_SECONDS) -> bool:
        """Whether this worker has been heard from recently enough to count."""
        if self.seen_at is None:
            return False
        return ((now or _now()) - self.seen_at).total_seconds() <= window


@dataclasses.dataclass(frozen=True)
class Barrier:
    """The tenant's handover state, as every replica reads it."""

    tenant_id: int
    state: str
    generation: int
    opened_at: Optional[_dt.datetime] = None
    settled_at: Optional[_dt.datetime] = None
    evidence: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    exists: bool = True

    @property
    def draining(self) -> bool:
        return self.state == STATE_DRAINING

    @property
    def settled(self) -> bool:
        return self.state == STATE_SETTLED

    @property
    def admits_new_work(self) -> bool:
        """Whether a new turn may be claimed and admitted for this tenant."""
        return self.state == STATE_OPEN


@dataclasses.dataclass(frozen=True)
class DeferredRecord:
    """One accepted inbound nobody has finished, by identity."""

    id: int
    tenant_id: int
    channel_connection_ref: str
    phone_number_id: str
    recipient: str
    provider_message_id: str
    payload: Mapping[str, Any]
    reason: str
    state: str
    barrier_generation: Optional[int]
    disposition: Optional[str]
    disposition_evidence: Mapping[str, Any]
    created_at: Optional[_dt.datetime]

    @property
    def pending(self) -> bool:
        return self.state == hm.DEFERRED_PENDING


@dataclasses.dataclass(frozen=True)
class DispositionResult:
    """What a disposition attempt actually did, per entry."""

    disposed: Tuple[int, ...] = ()
    refused: Mapping[int, str] = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refused


@dataclasses.dataclass(frozen=True)
class SettlementResult:
    """The outcome of one settle attempt, and why."""

    settled: bool
    barrier: Barrier
    blockers: Tuple[str, ...] = ()
    evidence: Mapping[str, Any] = dataclasses.field(default_factory=dict)


def convergence(barrier: Barrier, workers: Sequence[WorkerReport], *,
                now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """Whether every worker in the expected set is running this generation.

    The **expected set** is every worker row that has not been retired. A
    retired worker is excluded because an operator said so, with a reason on
    the record; nothing is excluded for having gone quiet.

    ``converged`` is only true when something positively says so: at least one
    worker has reported *since the drain opened*, every reporting worker carries
    the current generation, and no expected worker is stale.
    """
    moment = now or _now()
    current, opened = barrier.generation, barrier.opened_at
    on_generation: List[str] = []
    behind: List[str] = []
    stale: List[str] = []
    for report in workers:
        if report.retired:
            continue                                   # retired on the record, by name
        if not report.reporting(now=moment):
            stale.append(report.worker_id)             # quiet is not gone
        elif opened is not None and report.seen_at is not None and report.seen_at < opened:
            stale.append(report.worker_id)             # alive, but not heard from since
        elif report.observed_generation == current:
            on_generation.append(report.worker_id)
        else:
            behind.append(report.worker_id)
    converged = bool(on_generation) and not behind and not stale
    return {"converged": converged, "generation": current,
            "expected": sorted(r.worker_id for r in workers if not r.retired),
            "retired": sorted(r.worker_id for r in workers if r.retired),
            "on_generation": sorted(on_generation), "behind": sorted(behind),
            "stale": sorted(stale)}


# ── Sessions and locking ─────────────────────────────────────────────────────


def _dialect_of(bind: Any) -> str:
    """The dialect behind a Connection or a Session, whichever this is."""
    try:
        dialect = getattr(bind, "dialect", None)
        if dialect is None and hasattr(bind, "get_bind"):
            dialect = getattr(bind.get_bind(), "dialect", None)
        return str(getattr(dialect, "name", "") or "")
    except Exception:  # noqa: BLE001 - an unnameable dialect takes no advisory lock
        return ""


def _take_advisory_lock(bind: Any, *, tenant_id: int, exclusive: bool) -> None:
    """Order this transaction against the tenant's other handover transactions."""
    from sqlalchemy import text as _text  # noqa: PLC0415

    if _dialect_of(bind) != "postgresql":
        return
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    bind.execute(_text(f"SELECT {function}(:ns, :tenant)"),
                 {"ns": ADVISORY_LOCK_NAMESPACE, "tenant": int(tenant_id)})


@contextlib.contextmanager
def _own_session(db: Any) -> Any:
    """A short transaction of the handover's own, on the caller's bind.

    A handover write has to be durable the instant it is made: a drain every
    replica can see, a deferred inbound that outlives the request that was
    refused. Committing the *caller's* session to get that would commit whatever
    else it had staged and end a transaction it still owns, so the write is made
    on a session of its own and the caller's is left exactly as it was.
    """
    from sqlalchemy.orm import Session  # noqa: PLC0415

    bind = db.get_bind() if hasattr(db, "get_bind") else db
    session = Session(bind=bind)
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            logger.warning("[COMMERCE_RUNTIME_HANDOVER] session close failed")


@contextlib.contextmanager
def _locked(db: Any, tenant_id: int, *, exclusive: bool = True) -> Any:
    """One transaction, holding the tenant's advisory lock for its whole length."""
    with _own_session(db) as session:
        _take_advisory_lock(session, tenant_id=int(tenant_id), exclusive=exclusive)
        yield session


# ── Barrier: reading ─────────────────────────────────────────────────────────


def _barrier_from_row(tenant_id: int, row: Any) -> Barrier:
    if row is None:
        return Barrier(tenant_id=int(tenant_id), state=STATE_OPEN, generation=0, exists=False)
    return Barrier(
        tenant_id=int(tenant_id), state=str(row.state), generation=int(row.generation),
        opened_at=_aware(row.opened_at), settled_at=_aware(row.settled_at),
        evidence=dict(row.evidence or {}),
    )


def _barrier_row(session: Any, tenant_id: int, *, for_update: bool = False) -> Any:
    query = (session.query(hm.HandoverBarrier)
             .filter(hm.HandoverBarrier.tenant_id == int(tenant_id),
                     hm.HandoverBarrier.namespace == NAMESPACE))
    if for_update:
        query = query.with_for_update()
    return query.first()


def read_barrier(db: Any, *, tenant_id: int) -> Barrier:
    """The tenant's barrier. A row that does not exist is an open barrier.

    Read on the caller's own session: a read has nothing to commit, so it neither
    needs a transaction of its own nor may end the caller's.
    """
    return _barrier_from_row(tenant_id, _barrier_row(db, tenant_id))


def barrier_admits_new_work(db: Any, *, tenant_id: int) -> bool:
    """Whether a new turn may be started for this tenant. Fails **closed**.

    A barrier that cannot be read is not permission to admit: during a handover
    the whole point is that nothing new starts, and an unreadable barrier is
    exactly when a stale process would otherwise carry on.
    """
    try:
        return read_barrier(db, tenant_id=tenant_id).admits_new_work
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] barrier unreadable tenant=%s error=%s "
                       "— refusing new work", tenant_id, type(exc).__name__)
        return False


def admits_new_work_on(conn: Any, *, tenant_id: int) -> bool:
    """Whether new work may be admitted, read **on the caller's connection**.

    This is the form the runtime's admission uses, and the reason the guarantee
    is statable at all. Taking the shared advisory lock inside the admitting
    transaction serialises it against a transition:

    * the drain got the exclusive lock first — this read blocks until the drain
      commits, then sees ``draining`` and the admission is refused;
    * this transaction got the shared lock first — the drain blocks until the
      admission commits, so the turn is already visible to everything the drain
      does afterwards, the settlement count included.

    There is no third ordering, so no turn can be admitted that a check made
    after the drain would not see. Fails **closed**.
    """
    from sqlalchemy import text as _text  # noqa: PLC0415

    try:
        _take_advisory_lock(conn, tenant_id=int(tenant_id), exclusive=False)
        state = conn.execute(
            _text(f"SELECT state FROM {hm.BARRIER_TABLE} "
                  f"WHERE tenant_id = :tenant AND namespace = :ns"),
            {"tenant": int(tenant_id), "ns": NAMESPACE},
        ).scalar_one_or_none()
        return state is None or str(state) == STATE_OPEN
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] barrier unreadable on the admission "
                       "connection tenant=%s error=%s — refusing new work",
                       tenant_id, type(exc).__name__)
        return False


# ── Barrier: transitions ─────────────────────────────────────────────────────


def _ensure_barrier(session: Any, tenant_id: int) -> Any:
    row = _barrier_row(session, tenant_id, for_update=True)
    if row is None:
        row = hm.HandoverBarrier(tenant_id=int(tenant_id), namespace=NAMESPACE,
                                 state=STATE_OPEN, generation=0, evidence={})
        session.add(row)
        session.flush()
    return row


def open_drain(db: Any, *, tenant_id: int) -> Barrier:
    """Stop this tenant admitting new work, fleet-wide, from this instant.

    The generation is bumped so a worker still reporting the previous one is
    visibly behind rather than indistinguishable from a converged fleet.
    """
    with _locked(db, tenant_id) as session:
        row = _ensure_barrier(session, tenant_id)
        row.state = STATE_DRAINING
        row.generation = int(row.generation) + 1
        row.opened_at = _now()
        row.settled_at = None
        row.evidence = {}
        row.updated_at = _now()
        session.commit()
        barrier = _barrier_from_row(tenant_id, row)
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] drain opened tenant=%s generation=%s",
                   tenant_id, barrier.generation)
    return barrier


def settle(db: Any, *, tenant_id: int, expected_generation: Optional[int] = None,
           validate: Optional[Callable[[Any, Barrier], Tuple[List[str], Dict[str, Any]]]] = None,
           ) -> SettlementResult:
    """Validate and settle in **one** transaction, under the tenant's lock.

    The reviewed shape inspected first and wrote afterwards, so a deferred entry
    committing in between was settled over and the evidence described a state
    that had already changed. Here the barrier is re-read under the lock, the
    generation the caller decided on is re-checked, ``validate`` recounts the
    work on this same session, and the transition and its evidence are written
    from what was just read — or nothing is written at all.
    """
    with _locked(db, tenant_id) as session:
        row = _ensure_barrier(session, tenant_id)
        current = _barrier_from_row(tenant_id, row)
        blockers: List[str] = []
        evidence: Dict[str, Any] = {}

        if current.state != STATE_DRAINING:
            blockers.append(f"barrier_is_{current.state}_not_draining")
        if expected_generation is not None and int(expected_generation) != current.generation:
            blockers.append(
                f"generation_moved:expected={expected_generation},found={current.generation}")
        if validate is not None:
            found, evidence = validate(session, current)
            blockers.extend(found)

        if blockers:
            session.rollback()
            return SettlementResult(settled=False, barrier=current, blockers=tuple(blockers))

        settled_at = _now()
        row.state = STATE_SETTLED
        row.settled_at = settled_at
        row.evidence = dict(evidence, settled_generation=current.generation,
                            settled_at=settled_at.isoformat())
        row.updated_at = settled_at
        session.commit()
        return SettlementResult(settled=True, barrier=_barrier_from_row(tenant_id, row),
                                evidence=dict(row.evidence))


def reopen(db: Any, *, tenant_id: int,
           expected_generation: Optional[int] = None) -> Optional[Barrier]:
    """Admit new work again, on a fresh generation, keeping the audit trail.

    Checked and applied together: a drain that started between an operator's
    precheck and this call would otherwise be reopened on the strength of a
    reading that was already stale. Returns ``None`` when the barrier is not the
    settled one the caller decided about, and writes nothing.
    """
    with _locked(db, tenant_id) as session:
        row = _barrier_row(session, tenant_id, for_update=True)
        current = _barrier_from_row(tenant_id, row)
        if row is None or current.state != STATE_SETTLED:
            session.rollback()
            logger.warning("[COMMERCE_RUNTIME_HANDOVER] reopen refused tenant=%s state=%s",
                           tenant_id, current.state)
            return None
        if expected_generation is not None and int(expected_generation) != current.generation:
            session.rollback()
            logger.warning("[COMMERCE_RUNTIME_HANDOVER] reopen refused tenant=%s "
                           "generation moved expected=%s found=%s",
                           tenant_id, expected_generation, current.generation)
            return None
        row.state = STATE_OPEN
        row.generation = int(row.generation) + 1
        row.opened_at = _now()
        row.settled_at = None
        row.updated_at = _now()
        session.commit()
        barrier = _barrier_from_row(tenant_id, row)
    # The fleet must re-observe the new generation before anything is claimed
    # converged again; the rows stay so the audit trail survives the reopen.
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] ingress reopened tenant=%s generation=%s",
                   tenant_id, barrier.generation)
    return barrier


# ── The fleet ────────────────────────────────────────────────────────────────


def _worker_from_row(row: Any) -> WorkerReport:
    return WorkerReport(
        worker_id=str(row.worker_id), observed_generation=int(row.observed_generation),
        observed_state=str(row.observed_state), seen_at=_aware(row.seen_at),
        retired_at=_aware(row.retired_at), retired_by=row.retired_by,
        retired_reason=row.retired_reason,
    )


def fleet(db: Any, *, tenant_id: int) -> Tuple[WorkerReport, ...]:
    """Every worker known for this tenant, retired ones included."""
    rows = (db.query(hm.HandoverWorker)
            .filter(hm.HandoverWorker.tenant_id == int(tenant_id),
                    hm.HandoverWorker.namespace == NAMESPACE)
            .order_by(hm.HandoverWorker.worker_id)
            .all())
    return tuple(_worker_from_row(row) for row in rows)


def note_worker(db: Any, *, tenant_id: int, observed_generation: int, observed_state: str,
                name: Optional[str] = None, force: bool = False) -> None:
    """Record the barrier this process **observed**, exactly as it observed it.

    The generation and state are the caller's reading, passed in. They are
    deliberately not re-derived here: stamping the row with whatever the
    database says *now* would turn "I have not seen the drain" into "I am
    converged", which is the one thing this row exists to prevent.

    Throttled per process and per tenant, and never fatal: a heartbeat that
    cannot be written makes the fleet look unconverged, which blocks a handover
    rather than waving one through.
    """
    import time  # noqa: PLC0415

    key = (int(tenant_id), os.getpid())
    if not force:
        with _heartbeat_lock:
            last = _last_heartbeat.get(key, 0.0)
            if (time.monotonic() - last) < HEARTBEAT_INTERVAL_SECONDS:
                return
            _last_heartbeat[key] = time.monotonic()
    identity = str(name or worker_id())

    try:
        with _own_session(db) as session:
            row = (session.query(hm.HandoverWorker)
                   .filter(hm.HandoverWorker.tenant_id == int(tenant_id),
                           hm.HandoverWorker.namespace == NAMESPACE,
                           hm.HandoverWorker.worker_id == identity)
                   .with_for_update()
                   .first())
            if row is None:
                row = hm.HandoverWorker(
                    tenant_id=int(tenant_id), namespace=NAMESPACE, worker_id=identity,
                    observed_generation=int(observed_generation),
                    observed_state=str(observed_state), seen_at=_now())
                session.add(row)
            else:
                row.observed_generation = int(observed_generation)
                row.observed_state = str(observed_state)
                row.seen_at = _now()
                # A worker that reports again is back in the fleet. Retirement
                # is a statement about a process that is gone; this one is not.
                row.retired_at = None
                row.retired_by = None
                row.retired_reason = None
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] heartbeat failed tenant=%s error=%s",
                       tenant_id, type(exc).__name__)


def retire_worker(db: Any, *, tenant_id: int, name: str, by: str, reason: str) -> bool:
    """Take one worker out of the expected set, on the record.

    This is the only way a worker leaves the fleet. It is a statement an
    operator makes — "this process is stopped, and here is how I know" — and it
    is stored with their name and their reason so the settlement evidence can
    carry it.
    """
    identity = str(name or "").strip()
    who = str(by or "").strip()
    why = str(reason or "").strip()
    if not identity or not who or not why:
        return False
    with _locked(db, tenant_id) as session:
        row = (session.query(hm.HandoverWorker)
               .filter(hm.HandoverWorker.tenant_id == int(tenant_id),
                       hm.HandoverWorker.namespace == NAMESPACE,
                       hm.HandoverWorker.worker_id == identity)
               .with_for_update()
               .first())
        if row is None:
            session.rollback()
            return False
        row.retired_at = _now()
        row.retired_by = who
        row.retired_reason = why
        session.commit()
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] worker retired tenant=%s worker=%s by=%s",
                   tenant_id, identity, who)
    return True


# ── Deferred inbound ─────────────────────────────────────────────────────────


def _deferred_from_row(row: Any) -> DeferredRecord:
    return DeferredRecord(
        id=int(row.id), tenant_id=int(row.tenant_id),
        channel_connection_ref=str(row.channel_connection_ref),
        phone_number_id=str(row.phone_number_id), recipient=str(row.recipient),
        provider_message_id=str(row.provider_message_id), payload=dict(row.payload or {}),
        reason=str(row.reason), state=str(row.state),
        barrier_generation=None if row.barrier_generation is None else int(row.barrier_generation),
        disposition=row.disposition, disposition_evidence=dict(row.disposition_evidence or {}),
        created_at=_aware(row.created_at),
    )


def record_inbound(db: Any, *, tenant_id: int, phone_number_id: str,
                   channel_connection_ref: str, recipient: str, provider_message_id: str,
                   payload: Mapping[str, Any], reason: str,
                   barrier_generation: Optional[int] = None) -> Optional[DeferredRecord]:
    """Record one accepted inbound durably, by identity. Idempotent.

    This is what makes an acknowledgement a promise: the row exists before the
    provider is told we have the message, so a worker that dies a millisecond
    later has left something recoverable behind rather than a gap.

    Returns the record (existing or new), or ``None`` when it could not be
    written — which the caller must treat as **not accepted**.
    """
    identity = str(provider_message_id or "").strip()
    connection = str(channel_connection_ref or "").strip()
    if not identity or not connection:
        return None
    if str(reason) not in hm.DEFERRED_REASONS:
        raise ValueError(f"unknown deferred reason {reason!r}")

    with _locked(db, tenant_id, exclusive=False) as session:
        existing = (session.query(hm.DeferredInbound)
                    .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                            hm.DeferredInbound.namespace == NAMESPACE,
                            hm.DeferredInbound.channel_connection_ref == connection,
                            hm.DeferredInbound.provider_message_id == identity)
                    .first())
        if existing is not None:
            session.rollback()
            return _deferred_from_row(existing)

        pending = (session.query(hm.DeferredInbound)
                   .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                           hm.DeferredInbound.namespace == NAMESPACE,
                           hm.DeferredInbound.state == hm.DEFERRED_PENDING)
                   .count())
        if pending >= MAX_PENDING_DEFERRED:
            session.rollback()
            logger.error("[COMMERCE_RUNTIME_HANDOVER] pending deferred work is at its limit "
                         "tenant=%s pending=%s — refusing to accept more",
                         tenant_id, pending)
            return None

        row = hm.DeferredInbound(
            tenant_id=int(tenant_id), namespace=NAMESPACE, channel_connection_ref=connection,
            phone_number_id=str(phone_number_id or ""), recipient=str(recipient or ""),
            provider_message_id=identity, payload=dict(payload or {}), reason=str(reason),
            state=hm.DEFERRED_PENDING, barrier_generation=barrier_generation)
        session.add(row)
        session.commit()
        return _deferred_from_row(row)


def resolve_inbound(db: Any, *, tenant_id: int, channel_connection_ref: str,
                    provider_message_id: str) -> bool:
    """Mark one deferred inbound finished by the runtime itself.

    Called when the turn reaches a terminal: the record has done its job and
    stops counting against settlement. It is never resolved because time passed.
    """
    identity = str(provider_message_id or "").strip()
    connection = str(channel_connection_ref or "").strip()
    if not identity or not connection:
        return False
    try:
        with _own_session(db) as session:
            row = (session.query(hm.DeferredInbound)
                   .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                           hm.DeferredInbound.namespace == NAMESPACE,
                           hm.DeferredInbound.channel_connection_ref == connection,
                           hm.DeferredInbound.provider_message_id == identity,
                           hm.DeferredInbound.state == hm.DEFERRED_PENDING)
                   .with_for_update()
                   .first())
            if row is None:
                session.rollback()
                return False
            row.state = hm.DEFERRED_RESOLVED
            row.resolved_at = _now()
            row.updated_at = _now()
            session.commit()
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] could not resolve deferred inbound "
                       "tenant=%s error=%s", tenant_id, type(exc).__name__)
        return False


def pending_inbound(db: Any, *, tenant_id: int, limit: int = 200) -> Tuple[DeferredRecord, ...]:
    """Every deferred inbound still pending for this tenant, oldest first."""
    rows = (db.query(hm.DeferredInbound)
            .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                    hm.DeferredInbound.namespace == NAMESPACE,
                    hm.DeferredInbound.state == hm.DEFERRED_PENDING)
            .order_by(hm.DeferredInbound.created_at, hm.DeferredInbound.id)
            .limit(int(limit))
            .all())
    return tuple(_deferred_from_row(row) for row in rows)


def pending_count_on(session: Any, *, tenant_id: int) -> int:
    """Pending deferred inbounds, counted on a session the caller already holds."""
    return int(session.query(hm.DeferredInbound)
               .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                       hm.DeferredInbound.namespace == NAMESPACE,
                       hm.DeferredInbound.state == hm.DEFERRED_PENDING)
               .count())


def pending_count(db: Any, *, tenant_id: int) -> int:
    return pending_count_on(db, tenant_id=tenant_id)


def dispose_inbound(db: Any, *, tenant_id: int, entry_ids: Sequence[int], disposition: str,
                    evidence: Mapping[str, Any], by: str) -> DispositionResult:
    """Account for named entries, each checked against the state it is in.

    The reviewed shape stamped one free-text note across everything pending at
    that moment, which meant an entry that arrived while the operator was
    looking was disposed of without anyone having seen it, and the note itself
    was the only evidence that anything had been done. Here the operator names
    the entries, the disposition is one of a closed set, and evidence travels
    with each row. An id that is not pending is refused by name rather than
    silently included.
    """
    who = str(by or "").strip()
    kind = str(disposition or "").strip()
    wanted = [int(entry) for entry in entry_ids]
    if not who or kind not in DISPOSITIONS or not wanted:
        return DispositionResult(refused={entry: "invalid_request" for entry in wanted}
                                 or {0: "invalid_request"})

    disposed: List[int] = []
    refused: Dict[int, str] = {}
    with _locked(db, tenant_id) as session:
        rows = {int(row.id): row for row in
                session.query(hm.DeferredInbound)
                .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                        hm.DeferredInbound.namespace == NAMESPACE,
                        hm.DeferredInbound.id.in_(wanted))
                .with_for_update()
                .all()}
        moment = _now()
        for entry in wanted:
            row = rows.get(entry)
            if row is None:
                refused[entry] = "not_this_tenant_s_entry"
                continue
            if str(row.state) != hm.DEFERRED_PENDING:
                refused[entry] = f"already_{row.state}"
                continue
            row.state = hm.DEFERRED_DISPOSED
            row.disposition = kind
            row.disposition_evidence = dict(evidence or {})
            row.disposed_by = who
            row.disposed_at = moment
            row.updated_at = moment
            disposed.append(entry)
        if disposed:
            session.commit()
        else:
            session.rollback()
    return DispositionResult(disposed=tuple(disposed), refused=refused)


__all__ = [
    "ADVISORY_LOCK_NAMESPACE", "Barrier", "DISPOSITIONS", "DeferredRecord",
    "DispositionResult", "HEARTBEAT_INTERVAL_SECONDS", "MAX_PENDING_DEFERRED", "NAMESPACE",
    "REASON_ACCEPTED", "REASON_ADMISSION_REFUSED", "REASON_DRAIN_BUFFERED",
    "REASON_PROCESS_DRAINING", "REASON_SETTLED_WINDOW", "STATES", "STATE_DRAINING",
    "STATE_OPEN", "STATE_SETTLED", "SettlementResult", "WORKER_LIVE_SECONDS", "WorkerReport",
    "admits_new_work_on", "barrier_admits_new_work", "convergence", "dispose_inbound",
    "fleet", "note_worker", "open_drain", "pending_count", "pending_count_on",
    "pending_inbound", "read_barrier", "record_inbound", "reopen", "resolve_inbound",
    "retire_worker", "settle", "worker_id",
]
