"""The shared barrier a handover is actually performed against.

An environment flag is read per process. It cannot stop another replica from
admitting a turn, it cannot be observed by anything but the process that holds
it, and it changes at a moment nobody can name. A handover decided on one is a
handover decided on nothing: the count comes back zero because the replica that
was about to admit had not written its row yet.

So the barrier lives in the database, in the runtime's **own** tables
(``core.commerce_runtime.handover_models``, revision ``0111``). It used to live
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
STATE_RELEASED = hm.STATE_RELEASED
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
    """This process, named the same way for the whole of its life.

    When the platform that runs it names the deployment and the replica
    (Railway sets ``RAILWAY_DEPLOYMENT_ID`` and ``RAILWAY_REPLICA_ID``), the
    identity carries them: ``<deployment>/<replica>@<host>:<pid>``. That is what
    lets a retirement be checked against the deployment the worker actually
    belonged to rather than against whatever the operator typed.
    """
    base = f"{socket.gethostname()}:{os.getpid()}"
    deployment = str(os.environ.get("RAILWAY_DEPLOYMENT_ID", "") or "").strip()
    if not deployment:
        return base
    replica = str(os.environ.get("RAILWAY_REPLICA_ID", "") or "").strip() or "0"
    return f"{deployment}/{replica}@{base}"


def worker_deployment(identity: Any) -> Optional[str]:
    """The deployment a worker id names, or ``None`` for a bare ``host:pid``."""
    text = str(identity or "").strip()
    if "@" not in text:
        return None
    head = text.split("@", 1)[0]
    if "/" not in head:
        return None
    deployment = head.split("/", 1)[0].strip()
    return deployment or None


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
    retirement_evidence: Mapping[str, Any] = dataclasses.field(default_factory=dict)

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
    released_at: Optional[_dt.datetime] = None

    @property
    def draining(self) -> bool:
        return self.state == STATE_DRAINING

    @property
    def settled(self) -> bool:
        return self.state == STATE_SETTLED

    @property
    def released(self) -> bool:
        return self.state == STATE_RELEASED

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


def _engine_of(session: Any) -> Any:
    """The engine behind this session, or ``None`` for the process default."""
    try:
        bind = session.get_bind() if hasattr(session, "get_bind") else session
        return getattr(bind, "engine", bind)
    except Exception:  # noqa: silent-ok — a session that cannot name its engine falls back to the process default, which is the production case
        return None


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
        released_at=_aware(getattr(row, "released_at", None)),
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


def admits_recovery_on(conn: Any, *, tenant_id: int, entry_id: Optional[int] = None,
                       channel_connection_ref: Optional[str] = None,
                       provider_message_id: Optional[str] = None) -> bool:
    """Whether **this accepted entry** may be admitted, read on the caller's connection.

    The form a recovery replay's admission uses. A drain stops new work; an
    inbound the provider was already told we had is not new work, and refusing
    it while draining would leave the customer with nobody able to answer. So
    open *or draining* admits it, ordered against a transition by the same
    shared lock as :func:`admits_new_work_on`. Settled and released do not: a
    settlement has been signed off and a release has been verified, and neither
    may be admitted over.

    The grant that brought the caller here was checked against the durable
    record *before* admission; it is checked **again here, on the admitting
    transaction's own connection**, because what authorises an exception to a
    drain is the pending obligation, and an operator may have disposed of it in
    between. The entry row is locked for the rest of the admission transaction
    and has to name this tenant, this channel connection and this provider
    message id and still be pending; otherwise nothing is admitted. A
    disposition and an admission of the same entry are therefore serialised by
    the database: whichever commits second sees the other. Fails **closed**.
    """
    from sqlalchemy import text as _text  # noqa: PLC0415

    try:
        _take_advisory_lock(conn, tenant_id=int(tenant_id), exclusive=False)
        state = conn.execute(
            _text(f"SELECT state FROM {hm.BARRIER_TABLE} "
                  f"WHERE tenant_id = :tenant AND namespace = :ns"),
            {"tenant": int(tenant_id), "ns": NAMESPACE},
        ).scalar_one_or_none()
        if not (state is None or str(state) in (STATE_OPEN, STATE_DRAINING)):
            return False
        if entry_id is None:
            # No grant named an entry: this is not a recovery admission at all.
            return False
        locking = " FOR UPDATE" if _dialect_of(conn) == "postgresql" else ""
        entry = conn.execute(
            _text(f"SELECT tenant_id, channel_connection_ref, provider_message_id, state "
                  f"FROM {hm.DEFERRED_TABLE} WHERE id = :id AND namespace = :ns{locking}"),
            {"id": int(entry_id), "ns": NAMESPACE},
        ).mappings().first()
        if entry is None:
            return False
        if int(entry["tenant_id"]) != int(tenant_id):
            return False
        if str(entry["channel_connection_ref"]) != str(channel_connection_ref or "").strip():
            return False
        if str(entry["provider_message_id"]) != str(provider_message_id or "").strip():
            return False
        if str(entry["state"]) != hm.DEFERRED_PENDING:
            logger.warning("[COMMERCE_RUNTIME_HANDOVER] recovery grant withdrawn before "
                           "admission tenant=%s entry=%s state=%s — refusing",
                           tenant_id, entry_id, entry["state"])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] barrier or entry unreadable on the "
                       "recovery admission connection tenant=%s error=%s — refusing",
                       tenant_id, type(exc).__name__)
        return False


class BarrierReleased(RuntimeError):
    """The tenant's release has been verified; nothing new is accepted for it."""

    def __init__(self, tenant_id: int) -> None:
        self.tenant_id = int(tenant_id)
        super().__init__(f"tenant {tenant_id} is released: no new pilot work is accepted")


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
        row.released_at = None
        row.evidence = {}
        row.updated_at = _now()
        session.commit()
        barrier = _barrier_from_row(tenant_id, row)
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] drain opened tenant=%s generation=%s",
                   tenant_id, barrier.generation)
    return barrier


def settle(db: Any, *, tenant_id: int, expected_generation: Optional[int] = None,
           validate: Optional[Callable[[Any, Barrier], Tuple[List[str], Dict[str, Any]]]] = None,
           expected_workers: Optional[Sequence[str]] = None,
           ) -> SettlementResult:
    """Validate and settle in **one** transaction, under the tenant's lock.

    The reviewed shape inspected first and wrote afterwards, so a deferred entry
    committing in between was settled over and the evidence described a state
    that had already changed. Here the barrier is re-read under the lock, the
    generation the caller decided on is re-checked, the fleet is read on this
    same session and reconciled against the deployment inventory the operator
    states, ``validate`` recounts the work on this same session, and the
    transition and its evidence are written from what was just read — or
    nothing is written at all.

    ``expected_workers`` is required for a settlement to succeed: convergence
    can only see processes that wrote a row, and the replica that never reported
    is exactly the one still admitting. An unstated inventory is a blocker, not
    a warning, whichever command asked.
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
        fleet_found, evidence = fleet_blockers(current, fleet_on(session, tenant_id),
                                               expected_workers)
        blockers.extend(fleet_found)
        if validate is not None:
            found, more = validate(session, current)
            blockers.extend(found)
            evidence = dict(evidence, **dict(more or {}))

        if blockers:
            session.rollback()
            return SettlementResult(settled=False, barrier=current, blockers=tuple(blockers),
                                    evidence=evidence)

        settled_at = _now()
        row.state = STATE_SETTLED
        row.settled_at = settled_at
        row.evidence = dict(evidence, settled_generation=current.generation,
                            settled_at=settled_at.isoformat())
        row.updated_at = settled_at
        session.commit()
        return SettlementResult(settled=True, barrier=_barrier_from_row(tenant_id, row),
                                evidence=dict(row.evidence))


def reopen(db: Any, *, tenant_id: int, expected_generation: Optional[int] = None,
           validate: Optional[Callable[[Any, Barrier], Tuple[List[str], Dict[str, Any]]]] = None,
           ) -> Optional[Barrier]:
    """Admit new work again, on a fresh generation, keeping the audit trail.

    Checked and applied together: a drain that started between an operator's
    precheck and this call would otherwise be reopened on the strength of a
    reading that was already stale. State, generation **and** the work still
    outstanding are all read inside the transaction that writes — an entry that
    arrived during the settled window is an obligation nobody has met, and
    reopening over it would bury it under new traffic.

    Returns ``None`` when the barrier is not the settled one the caller decided
    about, or when ``validate`` reports blockers, and writes nothing.
    """
    with _locked(db, tenant_id) as session:
        row = _barrier_row(session, tenant_id, for_update=True)
        current = _barrier_from_row(tenant_id, row)
        if row is None or current.state not in (STATE_SETTLED, STATE_RELEASED):
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
        if validate is not None:
            blockers, _evidence = validate(session, current)
            if blockers:
                session.rollback()
                logger.warning("[COMMERCE_RUNTIME_HANDOVER] reopen refused tenant=%s "
                               "blockers=%s", tenant_id, blockers)
                return None
        row.state = STATE_OPEN
        row.generation = int(row.generation) + 1
        row.opened_at = _now()
        row.settled_at = None
        row.released_at = None
        row.updated_at = _now()
        session.commit()
        barrier = _barrier_from_row(tenant_id, row)
    # The fleet must re-observe the new generation before anything is claimed
    # converged again; the rows stay so the audit trail survives the reopen.
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] ingress reopened tenant=%s generation=%s",
                   tenant_id, barrier.generation)
    return barrier


# ── The fleet ────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ReleaseState:
    """Whether the pilot may be switched off for this tenant, right now."""

    tenant_id: int
    barrier: Barrier
    pending: int
    arrived_after_settlement: int
    blockers: Tuple[str, ...] = ()

    @property
    def released(self) -> bool:
        return not self.blockers

    def as_log_fields(self) -> Dict[str, Any]:
        return {"tenant_id": self.tenant_id, "state": self.barrier.state,
                "generation": self.barrier.generation, "pending": self.pending,
                "arrived_after_settlement": self.arrived_after_settlement,
                "released": self.released, "blockers": list(self.blockers)}


@dataclasses.dataclass(frozen=True)
class ReleaseResult:
    """The outcome of one release attempt, and why."""

    released: bool
    barrier: Barrier
    blockers: Tuple[str, ...] = ()
    evidence: Mapping[str, Any] = dataclasses.field(default_factory=dict)


def release(db: Any, *, tenant_id: int, expected_generation: Optional[int] = None,
            expected_workers: Optional[Sequence[str]] = None,
            validate: Optional[Callable[[Any, Barrier], Tuple[List[str], Dict[str, Any]]]] = None,
            ) -> ReleaseResult:
    """Verify the settlement still holds and write ``released``, in one transaction.

    A settlement is evidence about the instant it was taken, and the operator
    switches the pilot off some time later. In between, an inbound can be
    accepted — recorded, and then abandoned by the configuration change. So the
    release is not a verdict the operator reads and acts on; it is a
    **transition** written under the tenant's exclusive lock, and acceptance
    takes the shared lock and reads it: from the instant this commits, no new
    pilot-scoped inbound is accepted for the tenant — the provider is answered
    retryable and nothing is recorded — and once the switch is off the same
    request is the legacy path's, as it is today. Nothing accepted can fall
    between the two.

    Written only when, on this same session: the barrier is settled on the
    generation the operator decided about, nothing is pending, nothing arrived
    after ``settled_at``, the fleet is converged and reconciled against the
    stated inventory, and ``validate`` (the work counts) finds nothing.
    """
    with _locked(db, tenant_id) as session:
        row = _barrier_row(session, tenant_id, for_update=True)
        current = _barrier_from_row(tenant_id, row)
        blockers: List[str] = []
        evidence: Dict[str, Any] = {}
        if row is None or current.state != STATE_SETTLED:
            blockers.append(f"barrier_is_{current.state}_not_settled")
        if expected_generation is not None and int(expected_generation) != current.generation:
            blockers.append(
                f"generation_moved:expected={expected_generation},found={current.generation}")
        pending = pending_count_on(session, tenant_id=int(tenant_id))
        if pending:
            blockers.append(f"deferred_pending={pending}")
        after = _arrived_after(session, tenant_id=int(tenant_id), moment=current.settled_at)
        if after:
            blockers.append(f"arrived_after_settlement={after}")
        fleet_found, evidence = fleet_blockers(current, fleet_on(session, tenant_id),
                                               expected_workers)
        blockers.extend(fleet_found)
        if validate is not None:
            found, more = validate(session, current)
            blockers.extend(found)
            evidence = dict(evidence, **dict(more or {}))
        if blockers:
            session.rollback()
            return ReleaseResult(released=False, barrier=current, blockers=tuple(blockers),
                                 evidence=evidence)
        moment = _now()
        row.state = STATE_RELEASED
        row.released_at = moment
        row.evidence = dict(row.evidence or {}, release=dict(
            evidence, released_at=moment.isoformat(), released_generation=current.generation,
            pending=pending, arrived_after_settlement=after))
        row.updated_at = moment
        session.commit()
        barrier = _barrier_from_row(tenant_id, row)
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] released tenant=%s generation=%s — no new "
                   "pilot work is accepted; switch the pilot off now",
                   tenant_id, barrier.generation)
    return ReleaseResult(released=True, barrier=barrier, evidence=dict(barrier.evidence))


def _arrived_after(session: Any, *, tenant_id: int, moment: Any) -> int:
    if moment is None:
        return 0
    return int(session.query(hm.DeferredInbound)
               .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                       hm.DeferredInbound.namespace == NAMESPACE,
                       hm.DeferredInbound.created_at > moment)
               .count())


def release_state(db: Any, *, tenant_id: int) -> ReleaseState:
    """Whether the settlement verdict still holds, computed now — a **report**.

    :func:`release` is the transition; this is the same question asked without
    writing, for ``status``. A barrier already released reports no blockers.

    A settlement is evidence about the instant it was taken. Switching the pilot
    off on the strength of one taken ten minutes ago abandons everything that
    arrived since — and something *does* arrive: an inbound in the settled
    window is accepted and recorded rather than lost, which is exactly the case
    the old verdict cannot see.

    So the verdict is never stored and never cached. It is derived, every time
    it is asked, from the barrier and from the entries this tenant holds: an
    entry created after ``settled_at`` invalidates the release on its own, even
    if an operator has since disposed of it, because the settlement that was
    signed off did not account for it.
    """
    barrier = read_barrier(db, tenant_id=int(tenant_id))
    pending = pending_count(db, tenant_id=int(tenant_id))
    after = _arrived_after(db, tenant_id=int(tenant_id), moment=barrier.settled_at)
    blockers: List[str] = []
    if barrier.released:
        return ReleaseState(tenant_id=int(tenant_id), barrier=barrier, pending=pending,
                            arrived_after_settlement=after, blockers=())
    if barrier.state != STATE_SETTLED:
        blockers.append(f"barrier_is_{barrier.state}_not_settled")
    if pending:
        blockers.append(f"deferred_pending={pending}")
    if after:
        blockers.append(f"arrived_after_settlement={after}")
    return ReleaseState(tenant_id=int(tenant_id), barrier=barrier, pending=pending,
                        arrived_after_settlement=after, blockers=tuple(blockers))


def _worker_from_row(row: Any) -> WorkerReport:
    return WorkerReport(
        worker_id=str(row.worker_id), observed_generation=int(row.observed_generation),
        observed_state=str(row.observed_state), seen_at=_aware(row.seen_at),
        retired_at=_aware(row.retired_at), retired_by=row.retired_by,
        retired_reason=row.retired_reason,
        retirement_evidence=dict(getattr(row, "retirement_evidence", None) or {}),
    )


def fleet_on(session: Any, tenant_id: int) -> Tuple[WorkerReport, ...]:
    """The fleet, read on a session the caller already holds — the form a
    transition uses, so the workers it judges are the ones in its transaction."""
    rows = (session.query(hm.HandoverWorker)
            .filter(hm.HandoverWorker.tenant_id == int(tenant_id),
                    hm.HandoverWorker.namespace == NAMESPACE)
            .order_by(hm.HandoverWorker.worker_id)
            .all())
    return tuple(_worker_from_row(row) for row in rows)


BLOCKER_INVENTORY_UNSTATED = "expected_worker_inventory_unstated"


def fleet_blockers(barrier: Barrier, workers: Sequence[WorkerReport],
                   inventory: Optional[Sequence[str]]) -> Tuple[List[str], Dict[str, Any]]:
    """Every reason the fleet is not accounted for, and the evidence read.

    The authoritative form: a transition calls this itself on the workers it
    read inside its own transaction, so no command can settle or release
    without it. Convergence is what the workers reported; the inventory is
    what the deployment is supposed to contain, stated by the operator. A
    worker in the inventory that never wrote a row is the blocker
    ``workers_expected_but_never_reported``; an inventory that was never stated
    is ``expected_worker_inventory_unstated`` — silence about the fleet is not
    evidence about the fleet.
    """
    reasons: List[str] = []
    converged = convergence(barrier, workers)
    if not converged["converged"]:
        if converged["behind"]:
            reasons.append(f"workers_behind:{','.join(converged['behind'])}")
        if converged["stale"]:
            reasons.append(f"workers_stale_retire_or_wait:{','.join(converged['stale'])}")
        if not converged["on_generation"]:
            reasons.append("no_worker_has_reported_this_generation")
    stated = [str(name).strip() for name in (inventory or ()) if str(name).strip()]
    reconciled = expected_fleet(workers, stated)
    if not stated:
        reasons.append(BLOCKER_INVENTORY_UNSTATED)
    elif reconciled["missing_from_fleet"]:
        reasons.append("workers_expected_but_never_reported:"
                       + ",".join(reconciled["missing_from_fleet"]))
    evidence = {
        "convergence": converged,
        "fleet_inventory": reconciled,
        "retired_workers": [
            {"worker_id": w.worker_id, "by": w.retired_by, "reason": w.retired_reason,
             "evidence": dict(w.retirement_evidence)}
            for w in workers if w.retired
        ],
    }
    return reasons, evidence


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
                row.retirement_evidence = {}
                row.updated_at = _now()
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] heartbeat failed tenant=%s error=%s",
                       tenant_id, type(exc).__name__)


# What an operator has to be able to show before a worker leaves the expected
# set. A name and a sentence are not evidence a process stopped.
RETIREMENT_EVIDENCE_KEYS: Tuple[str, ...] = (
    # The deployment or process this worker belonged to, as the platform that
    # runs it names it — not the worker id, which is what we are retiring.
    "deployment",
    # How the stop was established: the command run, the console state read,
    # the fencing applied. A sentence an on-call engineer can re-check.
    "stop_verified_by",
    # When that was observed, ISO-8601. A report after this moment means the
    # process is not stopped and the retirement is refused.
    "observed_at",
    # The platform's own record of the stop, captured and retained: the output
    # of the deployment listing or the process fencing, verbatim. It has to
    # name the deployment above, and it is stored with its digest. A sentence
    # typed by an operator is a statement; this is what the statement rests on.
    "stop_record",
)

# A stop record is retained on the worker row, so it is bounded.
STOP_RECORD_MAX_BYTES = 64 * 1024

# What the platform's own record of a stop has to contain before it counts. It
# is a structured document — the operator captures the platform's answer and
# the operator tooling shapes it — because a free-text listing can say
# "RUNNING" about the very deployment it is offered as proof of stopping.
STOP_RECORD_REQUIRED_KEYS: Tuple[str, ...] = (
    "deployment",       # the exact deployment identity the record is about
    "incarnation",      # the process/replica incarnation of that deployment
    "state",            # the platform's state word for it, as captured
    "active_replicas",  # how many replicas of it the platform reports running
    "observed_at",      # when the platform was asked, ISO-8601
    "source",           # how it was asked: the command or API query
)
# A state word the platform uses for a deployment or process that is no longer
# running. Anything else — running, deploying, sleeping, an unknown word — is
# not evidence of a stop and is refused by name.
STOP_RECORD_INACTIVE_STATES = frozenset({
    "stopped", "removed", "exited", "crashed", "failed", "terminated", "inactive",
    "dead", "skipped",
})


class RetirementRefused(ValueError):
    """The evidence offered does not establish that this worker stopped."""


def _validated_stop_record(record: str, *, deployment: str,
                           observed: _dt.datetime) -> Dict[str, Any]:
    """The platform's stop record, checked for what it actually says.

    A digest proves which bytes were retained, not that a process stopped. So
    the record is read: it has to be a structured document about **this**
    deployment, naming the incarnation, carrying a state word the platform uses
    for something no longer running, reporting **zero** active replicas, and
    observed at the moment the retirement claims. A record that says the
    deployment is running is a contradiction and is refused as one.
    """
    import json  # noqa: PLC0415

    try:
        document = json.loads(record)
    except ValueError as exc:
        raise RetirementRefused("stop_record_not_structured") from exc
    if not isinstance(document, dict):
        raise RetirementRefused("stop_record_not_structured")
    missing = [key for key in STOP_RECORD_REQUIRED_KEYS
               if document.get(key) is None or str(document.get(key)).strip() == ""]
    if missing:
        raise RetirementRefused(f"stop_record_missing:{','.join(missing)}")
    if str(document["deployment"]).strip() != deployment:
        # The record has to be *about* this deployment. A capture of some
        # other deployment proves nothing about this one.
        raise RetirementRefused("stop_record_does_not_name_the_deployment")
    state = str(document["state"]).strip().lower()
    if state not in STOP_RECORD_INACTIVE_STATES:
        raise RetirementRefused(f"stop_record_state_is_not_inactive:{state}")
    try:
        replicas = int(document["active_replicas"])
    except (TypeError, ValueError) as exc:
        raise RetirementRefused("stop_record_active_replicas_not_an_integer") from exc
    if replicas != 0:
        raise RetirementRefused(f"stop_record_reports_active_replicas:{replicas}")
    seen = _parse_moment(str(document["observed_at"]))
    if seen is None:
        raise RetirementRefused("stop_record_observed_at_invalid")
    if seen != observed:
        raise RetirementRefused("stop_record_observation_differs_from_observed_at")
    raw = document.get("raw")
    if raw is not None and not isinstance(raw, str):
        raise RetirementRefused("stop_record_raw_must_be_text")
    return {"state": state, "incarnation": str(document["incarnation"]).strip()}


def retire_worker(db: Any, *, tenant_id: int, name: str, by: str, reason: str,
                  evidence: Optional[Mapping[str, Any]] = None) -> bool:
    """Take one worker out of the expected set, on evidence, on the record.

    This is the only way a worker leaves the fleet, and it is the one place the
    handover trusts a human instead of a reading — so what the human has to show
    is spelled out rather than implied:

    * **deployment** — the exact deployment or process identity, as the platform
      that runs it names it. "some replica" retires nothing.
    * **stop_verified_by** — how the stop or the fencing was actually
      established. Elapsed silence is not in this set and never will be: a quiet
      worker is a worker nobody has heard from, which is the case this whole
      mechanism exists to distinguish from a stopped one.
    * **observed_at** — when that was seen. If the worker has reported *since*
      that moment it is demonstrably running, and the retirement is refused
      rather than recorded.

    Raises :class:`RetirementRefused` with the reason when the evidence does not
    hold. Returns ``False`` only when there is no such worker for this tenant.
    """
    identity = str(name or "").strip()
    who = str(by or "").strip()
    why = str(reason or "").strip()
    if not identity or not who or not why:
        raise RetirementRefused("a retirement needs a worker, an operator and a reason")
    supplied = dict(evidence or {})
    missing = [key for key in RETIREMENT_EVIDENCE_KEYS
               if not str(supplied.get(key) or "").strip()]
    if missing:
        raise RetirementRefused(f"evidence_missing:{','.join(missing)}")
    observed = _parse_moment(supplied["observed_at"])
    if observed is None:
        raise RetirementRefused("observed_at must be an ISO-8601 moment")
    record = str(supplied["stop_record"])
    encoded = record.encode("utf-8")
    if len(encoded) > STOP_RECORD_MAX_BYTES:
        raise RetirementRefused(f"stop_record_too_large:{len(encoded)}>{STOP_RECORD_MAX_BYTES}")
    deployment = str(supplied["deployment"]).strip()
    bound = worker_deployment(identity)
    if bound is not None and bound != deployment:
        # The worker named the deployment it belonged to when it reported.
        # Evidence about a different deployment retires nothing here.
        raise RetirementRefused(f"deployment_does_not_match_the_worker_s_own:{bound}")
    structured = _validated_stop_record(record, deployment=deployment, observed=observed)
    import hashlib  # noqa: PLC0415

    supplied["stop_record_sha256"] = hashlib.sha256(encoded).hexdigest()
    supplied["stop_record_bytes"] = len(encoded)
    supplied["stop_record_state"] = structured["state"]
    supplied["stop_record_incarnation"] = structured["incarnation"]

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
        seen = _aware(row.seen_at)
        if seen is not None and seen > observed:
            session.rollback()
            raise RetirementRefused(
                f"worker reported at {seen.isoformat()}, after the stop was observed at "
                f"{observed.isoformat()} — it is running")
        moment = _now()
        row.retired_at = moment
        row.retired_by = who
        row.retired_reason = why
        row.retirement_evidence = dict(
            supplied, recorded_at=moment.isoformat(),
            last_report_at=None if seen is None else seen.isoformat())
        row.updated_at = moment
        session.commit()
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] worker retired tenant=%s worker=%s by=%s "
                   "deployment=%s", tenant_id, identity, who, supplied.get("deployment"))
    return True


def _parse_moment(value: Any) -> Optional[_dt.datetime]:
    """An ISO-8601 moment, or ``None``. Never guesses a timezone but UTC."""
    if isinstance(value, _dt.datetime):
        return _aware(value)
    try:
        return _aware(_dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def expected_fleet(workers: Sequence[WorkerReport], inventory: Sequence[str]
                   ) -> Dict[str, Any]:
    """Reconcile the workers that reported against the deployment inventory.

    The barrier can only see processes that wrote a row. An expected worker that
    never reported is invisible to convergence — it is exactly the replica that
    would still be admitting — so the operator states what the deployment is
    supposed to contain and this names both gaps.
    """
    active = {w.worker_id for w in workers if not w.retired}
    expected = {str(name).strip() for name in inventory if str(name).strip()}
    return {
        "expected": sorted(expected),
        "reporting": sorted(active),
        "missing_from_fleet": sorted(expected - active),
        "unexpected_in_fleet": sorted(active - expected) if expected else [],
        "reconciled": bool(expected) and not (expected - active) and not (active - expected),
    }


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

        # Read under the shared lock this transaction already holds, so it is
        # ordered against the release: either the release committed first and
        # this refuses, or this commits first and the release sees the row.
        gate = _barrier_row(session, tenant_id)
        if gate is not None and str(gate.state) == STATE_RELEASED:
            session.rollback()
            raise BarrierReleased(int(tenant_id))

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


def accepted_inbound(db: Any, *, tenant_id: int, channel_connection_ref: str,
                     provider_message_id: str) -> Optional[DeferredRecord]:
    """The durable record for one inbound identity, whatever state it is in.

    Read on the caller's session. "Accepted" is the question — was the provider
    told we had this message — not "is it still outstanding", so a resolved or
    disposed row answers too.
    """
    identity = str(provider_message_id or "").strip()
    connection = str(channel_connection_ref or "").strip()
    if not identity or not connection:
        return None
    row = (db.query(hm.DeferredInbound)
           .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                   hm.DeferredInbound.namespace == NAMESPACE,
                   hm.DeferredInbound.channel_connection_ref == connection,
                   hm.DeferredInbound.provider_message_id == identity)
           .first())
    return None if row is None else _deferred_from_row(row)


def resolve_inbound(db: Any, *, tenant_id: int, channel_connection_ref: str,
                    provider_message_id: str,
                    evidence: Optional[Mapping[str, Any]] = None) -> bool:
    """Mark one deferred inbound finished, against the record that finished it.

    Called when the runtime's turn for this exact identity has an authoritative
    terminal. It is never resolved because time passed, because a turn id
    exists, or because a caller reported something: ``evidence`` is the terminal
    the caller checked, and it is stored so the resolution can be audited.
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
            # The caller's word is not what closes the row. The runtime's own
            # turn for this identity is looked up on this database and has to
            # be bound to this entry and to have **answered** it — the same
            # validator an operator's disposition is held to. A terminal that
            # records a failed or never-sent reply leaves the obligation
            # pending, where settlement will count it and an operator can name
            # honestly what happened to it.
            ok, why, verified = _verified_answer(session, row, identity)
            if not ok:
                session.rollback()
                logger.warning("[COMMERCE_RUNTIME_HANDOVER] deferred inbound stays pending "
                               "tenant=%s entry=%s reason=%s", tenant_id, row.id, why)
                return False
            moment = _now()
            row.state = hm.DEFERRED_RESOLVED
            row.resolved_at = moment
            row.updated_at = moment
            row.disposition_evidence = dict(evidence or {}, verified=verified,
                                            verified_at=moment.isoformat())
            session.commit()
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] could not resolve deferred inbound "
                       "tenant=%s error=%s", tenant_id, type(exc).__name__)
        return False


def verify_handling(db: Any, *, tenant_id: int, entry_id: int) -> Tuple[bool, str, Dict[str, Any]]:
    """Read-only: would this entry's own runtime turn resolve it right now?

    The same validator :func:`resolve_inbound` applies before it writes, run
    without writing — for a dry run, or for a report that has to say *why* an
    entry with a finished turn is still pending.
    """
    with _own_session(db) as session:
        row = (session.query(hm.DeferredInbound)
               .filter(hm.DeferredInbound.tenant_id == int(tenant_id),
                       hm.DeferredInbound.namespace == NAMESPACE,
                       hm.DeferredInbound.id == int(entry_id))
               .first())
        if row is None:
            return False, "not_this_tenant_s_entry", {}
        if str(row.state) != hm.DEFERRED_PENDING:
            return False, f"already_{row.state}", {}
        return _verified_answer(session, row, str(row.provider_message_id))


def _verified_answer(session: Any, row: Any, identity: str) -> Tuple[bool, str, Dict[str, Any]]:
    """The runtime turn for ``identity``, checked as an answer to ``row``."""
    from core.commerce_runtime import recovery  # noqa: PLC0415

    phone_number_id = str(row.channel_connection_ref).split(":", 1)[-1]
    try:
        # The same database this resolution is being written to, not whatever
        # the process default points at.
        found = recovery.admitted_turn_for(
            tenant_id=int(row.tenant_id), phone_number_id=phone_number_id,
            provider_message_id=identity, engine=_engine_of(session))
    except Exception as exc:  # noqa: BLE001 - unverifiable is not verified
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] handling evidence unverifiable "
                       "tenant=%s error=%s", row.tenant_id, type(exc).__name__)
        return False, "evidence_could_not_be_verified", {}
    return handling_verdict(session, row, found)


# What a terminal has to say before it counts as having answered a customer.
# "The turn finished" is a statement about the runtime; "the customer was
# answered" is a statement about a reply the provider accepted. Only the second
# closes an obligation, and a confirmed delivery is recorded as a third thing —
# ``customer_reach`` — never inferred from either.
ANSWER_PROCESSING_OUTCOME = "completed"
ANSWER_TRANSPORT_OUTCOME = "accepted"
CUSTOMER_REACHED = "reached"


def handling_verdict(session: Any, row: Any, found: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """Whether runtime turn ``found`` **answered** deferred entry ``row``.

    One validator for every path that closes an obligation on the strength of
    a turn — normal resolution, recovery's finished-turn branch and an
    operator's ``answered``/``replayed`` disposition all come through here. A
    turn counts only when it exists, reached a terminal, is bound to this
    entry's tenant, channel connection and customer, was recorded after the
    inbound arrived, and its terminal records a completed turn whose reply the
    provider **accepted**. A failed turn, a reply never attempted, a definitive
    rejection or an abandoned turn is a terminal, not an answer, and it is named
    as such so the caller can say what actually happened.
    """
    if found is None:
        return False, "no_runtime_turn_for_that_identity", {}
    if not getattr(found, "finished", False):
        return False, "that_turn_has_no_terminal", {}
    bound, why, binding = _turn_binding(session, row, found)
    if not bound:
        return False, why, {}
    own = str(getattr(found, "provider_message_id", "") or "") == str(row.provider_message_id)
    if not own:
        # Another turn is claimed to have answered this inbound: an answer
        # recorded before this message arrived did not answer it. The entry's
        # **own** turn is the handling of this very inbound whenever it was
        # recorded, so chronology against the bookkeeping row says nothing.
        terminal_at = _aware(binding.get("terminal_recorded_at"))
        arrived_at = _aware(row.created_at)
        if terminal_at is None or arrived_at is None or terminal_at < arrived_at:
            return False, "answering_terminal_predates_this_inbound", {}
    processing = str(binding.get("processing_outcome") or "")
    transport = str(binding.get("transport_outcome") or "")
    reach = str(binding.get("customer_reach") or "")
    if processing != ANSWER_PROCESSING_OUTCOME or transport != ANSWER_TRANSPORT_OUTCOME:
        return False, (f"terminal_is_not_an_accepted_reply:processing={processing},"
                       f"transport={transport},customer_reach={reach}"), {
                           "turn_id": int(found.turn_id), "processing_outcome": processing,
                           "transport_outcome": transport, "customer_reach": reach}
    return True, "", dict(binding, turn_id=int(found.turn_id),
                          verified_against="commerce_runtime_turn_terminals",
                          reply_accepted_by_provider=True,
                          # Provider acceptance is not delivery. What the
                          # terminal knows about the customer is carried as it
                          # is, and "confirmed" is true only when it says so.
                          customer_delivery_confirmed=(reach == CUSTOMER_REACHED))


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


# What each disposition has to be able to show. ``replayed`` and ``answered``
# are claims about the customer's conversation and are checked against the
# runtime's own terminal records; ``superseded`` is checked against another
# entry this platform holds; ``not_required`` is the one operator judgement,
# and it carries its authorisation instead of a delivery claim.
DISPOSITION_EVIDENCE_KEYS: Mapping[str, Tuple[str, ...]] = {
    "replayed": ("replayed_as_provider_message_id",),
    "answered": ("answered_by_provider_message_id",),
    "superseded": ("superseded_by_provider_message_id",),
    "not_required": ("authorized_by", "why"),
    # The customer was not answered and the operator closes the obligation
    # knowing that. It asserts no delivery; it is refused when the runtime did
    # in fact answer, because then the honest disposition is ``answered``.
    "unanswered": ("authorized_by", "why"),
}


def _verified_handling(session: Any, row: Any, kind: str,
                       evidence: Mapping[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Whether the operator's evidence is real, checked against the records.

    A note is not proof of anything. ``replayed`` and ``answered`` each name a
    provider message id; that identity is resolved against **this tenant and
    this channel connection**, and it has to be a runtime turn that reached a
    terminal. ``superseded`` names a later inbound for the same recipient that
    this platform actually holds. ``not_required`` claims no delivery at all —
    it is an operator's decision, stored with who authorised it and why, and it
    is the only disposition that asserts nothing about the customer.
    """
    from core.commerce_runtime import recovery  # noqa: PLC0415

    keys = DISPOSITION_EVIDENCE_KEYS.get(kind, ())
    missing = [key for key in keys if not str(evidence.get(key) or "").strip()]
    if missing:
        return False, f"evidence_missing:{','.join(missing)}", {}

    if kind == "not_required":
        # No delivery was owed — an operator's judgement, which cannot stand
        # when the runtime's own turn did in fact answer this inbound: then
        # the record is ``answered``, whatever anyone decided about it. Nor
        # when a send's outcome is unknown: it may have arrived.
        ok, _why, found = _verified_answer(session, row, str(row.provider_message_id))
        if ok:
            return False, "the_runtime_answered_this_inbound", {}
        if found.get("transport_outcome") == "unknown":
            return False, "delivery_outcome_unknown", {}
        return True, "", {"authorized_by": str(evidence["authorized_by"]).strip(),
                          "why": str(evidence["why"]).strip()}

    if kind == "superseded":
        identity = str(evidence["superseded_by_provider_message_id"]).strip()
        # The same tenant, the same channel connection **and** the same
        # recipient: another customer's later message, or the same customer on
        # another connection, supersedes nothing here.
        later = (session.query(hm.DeferredInbound)
                 .filter(hm.DeferredInbound.tenant_id == int(row.tenant_id),
                         hm.DeferredInbound.namespace == NAMESPACE,
                         hm.DeferredInbound.channel_connection_ref == str(row.channel_connection_ref),
                         hm.DeferredInbound.recipient == str(row.recipient),
                         hm.DeferredInbound.provider_message_id == identity)
                 .first())
        if later is None:
            return False, "superseding_inbound_not_found", {}
        if int(later.id) == int(row.id):
            return False, "an_entry_cannot_supersede_itself", {}
        earlier_at, later_at = _aware(row.created_at), _aware(later.created_at)
        # Chronology is part of the claim: an older message does not replace a
        # newer one, whatever it says. Two entries recorded in the same clock
        # tick are ordered by arrival — the surrogate key is assigned in
        # insertion order and never reused.
        is_later = (earlier_at is not None and later_at is not None
                    and (later_at > earlier_at
                         or (later_at == earlier_at and int(later.id) > int(row.id))))
        if not is_later:
            return False, "superseding_inbound_is_not_later", {}
        return True, "", {"superseded_by_entry_id": int(later.id),
                          "superseded_by_provider_message_id": identity,
                          "superseded_by_created_at": later_at.isoformat()}

    if kind == "unanswered":
        # The one disposition that says the customer was **not** answered. It
        # is refused when the runtime's own turn for this inbound did answer —
        # then the truthful record is ``answered`` — and otherwise stores what
        # the runtime's terminal, if any, actually recorded.
        ok, why, found = _verified_answer(session, row, str(row.provider_message_id))
        if ok:
            return False, "the_runtime_answered_this_inbound", {}
        if found.get("transport_outcome") == "unknown":
            # A send nobody established the outcome of may have arrived.
            # "Unanswered" would be a claim nobody can make; it stays pending.
            return False, "delivery_outcome_unknown", {}
        return True, "", {"authorized_by": str(evidence["authorized_by"]).strip(),
                          "why": str(evidence["why"]).strip(),
                          "runtime_terminal": why,
                          **({"runtime_turn_id": found["turn_id"]} if found.get("turn_id") else {})}

    key = keys[0]
    identity = str(evidence[key]).strip()
    if kind == "replayed" and identity != str(row.provider_message_id):
        # A replay handles *this* inbound, under its own identity. Naming
        # another message is an "answered" claim, and is checked as one.
        return False, "replayed_identity_is_not_this_inbound", {}
    # ``answered`` and ``replayed`` both claim a runtime turn answered this
    # customer. The claim is checked by the one validator every closing path
    # uses: bound to this entry, recorded after it arrived, and a completed
    # turn whose reply the provider accepted. A terminal alone is not an answer.
    ok, why, verified = _verified_answer(session, row, identity)
    if not ok:
        return False, why, {}
    return True, "", dict(verified, **{key: identity})


def _unfinished_turn_for(session: Any, row: Any) -> Optional[int]:
    """The id of a runtime turn admitted for this entry's identity that has no
    terminal yet, or ``None``. Read on the caller's own transaction. Raises
    when it cannot be established — the caller must not treat that as "none".
    """
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    try:
        turn = session.execute(sa_text(
            "SELECT t.id FROM commerce_runtime_turns t "
            "LEFT JOIN commerce_runtime_turn_terminals x "
            "ON x.turn_id = t.id AND x.tenant_id = t.tenant_id AND x.namespace = t.namespace "
            "WHERE t.tenant_id = :tenant AND t.namespace = :ns "
            "AND t.channel_connection_ref = :ref AND t.provider_message_id = :pmid "
            "AND x.turn_id IS NULL"),
            {"tenant": int(row.tenant_id), "ns": NAMESPACE,
             "ref": str(row.channel_connection_ref), "pmid": str(row.provider_message_id)},
        ).scalar()
    except Exception:
        # A database without the runtime's turn tables cannot hold an
        # admitted turn at all — the runtime refuses to admit without them —
        # so that one case is "none". Anything else is unverifiable and is
        # raised to the caller, which must not read it as "none".
        session.rollback()
        bind = session.get_bind() if hasattr(session, "get_bind") else session
        if not sa_inspect(bind).has_table("commerce_runtime_turns"):
            return None
        raise
    return None if turn is None else int(turn)


def _turn_binding(session: Any, row: Any, found: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """Whether a runtime turn belongs to **this entry's** conversation.

    A terminal proves a turn was handled. It says nothing about *whose* turn
    unless the turn is bound to the same tenant, the same channel connection
    and the same customer as the entry: another conversation's terminal cannot
    account for this one. The runtime conversation names the application
    conversation it was admitted for, and that names the customer.
    """
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    from core.commerce_runtime import conversation_link as cl  # noqa: PLC0415
    from core.commerce_runtime import pilot_guard as pg  # noqa: PLC0415

    engine = _engine_of(session)
    try:
        with engine.connect() as conn:
            turn = conn.execute(sa_text(
                "SELECT channel_connection_ref, conversation_id FROM commerce_runtime_turns "
                "WHERE id = :id AND tenant_id = :tenant"),
                {"id": int(found.turn_id), "tenant": int(row.tenant_id)}).mappings().first()
            if turn is None:
                return False, "no_runtime_turn_for_that_identity", {}
            if str(turn["channel_connection_ref"]) != str(row.channel_connection_ref):
                return False, "turn_is_on_another_connection", {}
            ref = conn.execute(sa_text(
                "SELECT conversation_ref FROM commerce_runtime_conversations "
                "WHERE id = :id AND tenant_id = :tenant"),
                {"id": int(turn["conversation_id"]), "tenant": int(row.tenant_id)}).scalar()
            parsed = cl.parse_conversation_ref(ref)
            if parsed is None:
                return False, "turn_conversation_is_not_bound_to_an_application_conversation", {}
            _channel, app_conversation_id = parsed
            phones = conn.execute(sa_text(
                "SELECT c.normalized_phone, c.phone FROM conversations v "
                "JOIN customers c ON c.id = v.customer_id "
                "WHERE v.id = :id AND v.tenant_id = :tenant"),
                {"id": int(app_conversation_id), "tenant": int(row.tenant_id)}).first()
            if phones is None:
                return False, "turn_conversation_has_no_customer", {}
            candidates = {pg.normalize_recipient(p) for p in phones if p}
            if pg.normalize_recipient(row.recipient) not in candidates:
                return False, "turn_belongs_to_another_customer", {}
            terminal = conn.execute(sa_text(
                "SELECT recorded_at, processing_outcome, transport_outcome, customer_reach "
                "FROM commerce_runtime_turn_terminals "
                "WHERE turn_id = :id AND tenant_id = :tenant"),
                {"id": int(found.turn_id), "tenant": int(row.tenant_id)}).mappings().first()
    except Exception as exc:  # noqa: BLE001 - unverifiable is not verified
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] turn binding unverifiable tenant=%s "
                       "error=%s", row.tenant_id, type(exc).__name__)
        return False, "evidence_could_not_be_verified", {}
    if terminal is None:
        return False, "that_turn_has_no_terminal", {}
    moment = _aware(terminal["recorded_at"])
    return True, "", {"app_conversation_id": int(app_conversation_id),
                      "terminal_recorded_at": None if moment is None else moment.isoformat(),
                      "processing_outcome": str(terminal["processing_outcome"] or ""),
                      "transport_outcome": str(terminal["transport_outcome"] or ""),
                      "customer_reach": str(terminal["customer_reach"] or "")}


def dispose_inbound(db: Any, *, tenant_id: int, entry_ids: Sequence[int], disposition: str,
                    evidence: Mapping[str, Any], by: str,
                    not_after: Optional[_dt.datetime] = None) -> DispositionResult:
    """Account for named entries, each checked against the state it is in.

    The reviewed shape stamped one free-text note across everything pending at
    that moment, which meant an entry that arrived while the operator was
    looking was disposed of without anyone having seen it, and the note itself
    was the only evidence that anything had been done. Here the operator names
    the entries, the disposition is one of a closed set, and evidence travels
    with each row. An id that is not pending is refused by name rather than
    silently included.

    ``evidence`` is checked, not merely recorded: a disposition that claims the
    customer was replayed or answered has to name the identity that did it, and
    that identity is resolved against this tenant's own terminal records before
    the row is closed. ``not_after`` is the moment the operator inspected; an
    entry created after it is refused even if its id was passed, so a selection
    can never grow to include something nobody looked at.
    """
    who = str(by or "").strip()
    kind = str(disposition or "").strip()
    wanted = [int(entry) for entry in entry_ids]
    if not who or kind not in DISPOSITIONS or not wanted:
        return DispositionResult(refused={entry: "invalid_request" for entry in wanted}
                                 or {0: "invalid_request"})

    disposed: List[int] = []
    refused: Dict[int, str] = {}
    cutoff = _aware(not_after)
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
            created = _aware(row.created_at)
            if cutoff is not None and created is not None and created > cutoff:
                refused[entry] = "arrived_after_the_inspection"
                continue
            # Read under the tenant's exclusive lock this transaction holds, so
            # it is ordered against a recovery admission: an admission that
            # committed first has a turn here, and this refuses; one that has
            # not committed yet waits on the lock and then finds the row
            # disposed. Nothing is disposed of underneath a running turn.
            try:
                running = _unfinished_turn_for(session, row)
            except Exception as exc:  # noqa: BLE001 - unverifiable is not disposable
                logger.warning("[COMMERCE_RUNTIME_HANDOVER] turn state unverifiable "
                               "tenant=%s entry=%s error=%s", tenant_id, entry,
                               type(exc).__name__)
                refused[entry] = "turn_state_could_not_be_verified"
                continue
            if running is not None:
                refused[entry] = f"turn_admitted_and_unfinished:{running}"
                continue
            ok, why, verified = _verified_handling(session, row, kind, dict(evidence or {}))
            if not ok:
                refused[entry] = why
                continue
            row.state = hm.DEFERRED_DISPOSED
            row.disposition = kind
            row.disposition_evidence = dict(evidence or {}, verified=verified,
                                            verified_at=moment.isoformat())
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
    "DISPOSITION_EVIDENCE_KEYS", "RETIREMENT_EVIDENCE_KEYS", "ReleaseState",
    "RetirementRefused", "expected_fleet", "release_state",
    "BLOCKER_INVENTORY_UNSTATED", "BarrierReleased", "ReleaseResult", "STATE_RELEASED",
    "STOP_RECORD_MAX_BYTES", "STOP_RECORD_REQUIRED_KEYS", "STOP_RECORD_INACTIVE_STATES",
    "ANSWER_PROCESSING_OUTCOME", "ANSWER_TRANSPORT_OUTCOME", "CUSTOMER_REACHED",
    "handling_verdict", "verify_handling", "worker_deployment",
    "admits_recovery_on", "fleet_blockers", "fleet_on", "release",
    "accepted_inbound", "admits_new_work_on", "barrier_admits_new_work", "convergence",
    "dispose_inbound",
    "fleet", "note_worker", "open_drain", "pending_count", "pending_count_on",
    "pending_inbound", "read_barrier", "record_inbound", "reopen", "resolve_inbound",
    "retire_worker", "settle", "worker_id",
]
