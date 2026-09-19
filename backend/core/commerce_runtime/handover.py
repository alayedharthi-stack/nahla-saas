"""The shared barrier a handover is actually performed against.

An environment flag is read per process. It cannot stop another replica from
admitting a turn, it cannot be observed by anything but the process that holds
it, and it changes at a moment nobody can name. A handover decided on one is a
handover decided on nothing: the count comes back zero because the replica that
was about to admit had not written its row yet.

So the barrier lives in the database, where every replica reads it and one
operator writes it. It uses an existing table — the tenant's own settings row,
under one namespaced key — so it needs no migration and no new infrastructure.

    open  →  draining  →  settled  →  open

**draining** is not "new turns go to legacy". While a tenant is draining, an
inbound for an affected recipient is *buffered*: recorded durably, answered by
nobody, and left for the operator to dispose of. Releasing it to the legacy path
would be the one thing a handover must not do — answering, on a second runtime,
a conversation whose first runtime may still have a send in flight.

Three things are recorded, and each exists because a count alone cannot say it:

* **workers** — one row per process that has evaluated a route, with the barrier
  generation it saw. Convergence is then observable: every live worker reporting
  the current generation is evidence that the drain reached the fleet, where an
  attestation is only a statement that it did.
* **buffered** — every inbound refused because of the drain. Nothing is
  acknowledged and quietly dropped; settlement is blocked until each entry has a
  recorded disposition.
* **evidence** — the settlement snapshot, written *before* ingress reopens, so
  what the handover was decided on survives the handover.

Every write takes the tenant's settings row with ``FOR UPDATE``, so two
operators cannot interleave. Every read is ordinary. Nothing here sends
anything.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import logging
import os
import socket
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger("nahla.commerce_runtime.handover")

SETTINGS_KEY = "commerce_runtime_handover"

STATE_OPEN = "open"
STATE_DRAINING = "draining"
STATE_SETTLED = "settled"
STATES: Tuple[str, ...] = (STATE_OPEN, STATE_DRAINING, STATE_SETTLED)

# A worker seen more recently than this is treated as live. One seen before the
# drain opened and still inside this window has an unknown disposition: it may
# be running on the old generation, and that is a concrete reason to stay
# blocked rather than a reason to assume it is gone.
WORKER_LIVE_SECONDS = 900.0

# How often one process writes its heartbeat. The pilot is owner-only, so this
# is a handful of rows; the throttle keeps it off the per-turn write path.
HEARTBEAT_INTERVAL_SECONDS = 30.0

MAX_BUFFERED_ENTRIES = 500

# A tenant-scoped advisory lock, taken for the length of a transaction. It is
# what makes the barrier and an admission *ordered* rather than merely close
# together: a writer takes it exclusively, an admitter takes it shared, and the
# database decides which happened first. An advisory key needs no row, so the
# ordering holds even for a tenant whose settings row does not exist yet —
# which is exactly the tenant a row lock would fail to protect.
#
# PostgreSQL only. On any other dialect the lock is skipped and the barrier is
# still read; the pilot runs on PostgreSQL, and the operator procedure states
# this.
ADVISORY_LOCK_NAMESPACE = 0x6E68_4C56 & 0x7FFF_FFFF      # "nhLV", kept positive

_heartbeat_lock = threading.Lock()
_last_heartbeat: Dict[Tuple[int, int], float] = {}


def worker_id() -> str:
    """This process, named the same way for the whole of its life."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(moment: _dt.datetime) -> str:
    return moment.isoformat()


def _parse(moment: Any) -> Optional[_dt.datetime]:
    try:
        parsed = _dt.datetime.fromisoformat(str(moment))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


@dataclasses.dataclass(frozen=True)
class WorkerReport:
    """What one process last said about which barrier generation it is running."""

    worker_id: str
    generation: int
    state: str
    seen_at: Optional[_dt.datetime]

    def live(self, *, now: Optional[_dt.datetime] = None,
             window: float = WORKER_LIVE_SECONDS) -> bool:
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
    workers: Tuple[WorkerReport, ...] = ()
    buffered: Tuple[Mapping[str, Any], ...] = ()
    evidence: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def draining(self) -> bool:
        return self.state == STATE_DRAINING

    @property
    def admits_new_work(self) -> bool:
        """Whether a new turn may be claimed and admitted for this tenant."""
        return self.state == STATE_OPEN

    @property
    def undisposed_buffered(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(entry for entry in self.buffered if not entry.get("disposition"))

    def convergence(self, *, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
        """Whether every live worker is running this generation, and the evidence.

        ``converged`` is only true when something positively says so: at least
        one worker has reported *since the drain opened*, every such report
        carries the current generation, and no worker last seen before it is
        still inside the live window with an unknown disposition.
        """
        moment = now or _now()
        current, opened = self.generation, self.opened_at
        on_generation, behind, unknown = [], [], []
        for report in self.workers:
            if not report.live(now=moment):
                continue                                   # gone long enough to be gone
            if opened is not None and report.seen_at is not None and report.seen_at < opened:
                unknown.append(report.worker_id)           # alive, but not heard from since
            elif report.generation == current:
                on_generation.append(report.worker_id)
            else:
                behind.append(report.worker_id)
        converged = bool(on_generation) and not behind and not unknown
        return {"converged": converged, "generation": current,
                "on_generation": sorted(on_generation), "behind": sorted(behind),
                "unknown_disposition": sorted(unknown)}


def _empty(tenant_id: int) -> Barrier:
    return Barrier(tenant_id=int(tenant_id), state=STATE_OPEN, generation=0)


def _from_payload(tenant_id: int, payload: Any) -> Barrier:
    record = payload if isinstance(payload, dict) else {}
    state = str(record.get("state") or STATE_OPEN)
    if state not in STATES:
        state = STATE_OPEN
    workers = []
    for name, raw in (record.get("workers") or {}).items():
        entry = raw if isinstance(raw, dict) else {}
        try:
            generation = int(entry.get("generation", -1))
        except (TypeError, ValueError):
            generation = -1
        workers.append(WorkerReport(worker_id=str(name), generation=generation,
                                    state=str(entry.get("state") or ""),
                                    seen_at=_parse(entry.get("seen_at"))))
    try:
        generation = int(record.get("generation", 0))
    except (TypeError, ValueError):
        generation = 0
    buffered = tuple(entry for entry in (record.get("buffered") or []) if isinstance(entry, dict))
    return Barrier(
        tenant_id=int(tenant_id), state=state, generation=generation,
        opened_at=_parse(record.get("opened_at")), settled_at=_parse(record.get("settled_at")),
        workers=tuple(sorted(workers, key=lambda w: w.worker_id)), buffered=buffered,
        evidence=dict(record.get("evidence") or {}),
    )


def _to_payload(barrier: Barrier) -> Dict[str, Any]:
    return {
        "state": barrier.state,
        "generation": int(barrier.generation),
        "opened_at": _iso(barrier.opened_at) if barrier.opened_at else None,
        "settled_at": _iso(barrier.settled_at) if barrier.settled_at else None,
        "workers": {w.worker_id: {"generation": w.generation, "state": w.state,
                                  "seen_at": _iso(w.seen_at) if w.seen_at else None}
                    for w in barrier.workers},
        "buffered": [dict(entry) for entry in barrier.buffered],
        "evidence": dict(barrier.evidence),
    }


# ── Reading ──────────────────────────────────────────────────────────────────


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
    """Order this transaction against the tenant's other barrier transactions."""
    from sqlalchemy import text as _text  # noqa: PLC0415

    if _dialect_of(bind) != "postgresql":
        return
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    bind.execute(_text(f"SELECT {function}(:ns, :tenant)"),
                 {"ns": ADVISORY_LOCK_NAMESPACE, "tenant": int(tenant_id)})


def admits_new_work_on(conn: Any, *, tenant_id: int) -> bool:
    """Whether new work may be admitted, read **on the caller's connection**.

    This is the form the runtime's admission uses, and the reason the guarantee
    is statable at all. Taking the shared advisory lock inside the admitting
    transaction serialises it against a drain:

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
        row = conn.execute(
            _text("SELECT metadata FROM tenant_settings WHERE tenant_id = :tenant"),
            {"tenant": int(tenant_id)},
        ).scalar_one_or_none()
        payload = row if isinstance(row, dict) else {}
        return _from_payload(tenant_id, payload.get(SETTINGS_KEY)).admits_new_work
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] barrier unreadable on the admission "
                       "connection tenant=%s error=%s — refusing new work",
                       tenant_id, type(exc).__name__)
        return False


def read_barrier(db: Any, *, tenant_id: int) -> Barrier:
    """The tenant's barrier. A row that does not exist is an open barrier."""
    from database.models import TenantSettings  # noqa: PLC0415

    row = (db.query(TenantSettings)
           .filter(TenantSettings.tenant_id == int(tenant_id))
           .first())
    if row is None:
        return _empty(tenant_id)
    return _from_payload(tenant_id, (row.extra_metadata or {}).get(SETTINGS_KEY))


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


# ── Writing ──────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _own_session(db: Any) -> Any:
    """A short transaction of the barrier's own, on the caller's bind.

    A barrier write has to be durable the instant it is made: a drain every
    replica can see, a buffered inbound that outlives the request that was
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
            logger.warning("[COMMERCE_RUNTIME_HANDOVER] barrier session close failed")


def _mutate(db: Any, tenant_id: int, change: Any) -> Optional[Barrier]:
    """Apply ``change`` to the tenant's barrier under a row lock. Commits."""
    from database.models import TenantSettings  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    with _own_session(db) as session:
        _take_advisory_lock(session, tenant_id=int(tenant_id), exclusive=True)
        row = (session.query(TenantSettings)
               .filter(TenantSettings.tenant_id == int(tenant_id))
               .with_for_update()
               .first())
        if row is None:
            row = TenantSettings(tenant_id=int(tenant_id), extra_metadata={})
            session.add(row)
            session.flush()
        metadata = dict(row.extra_metadata or {})
        updated = change(_from_payload(tenant_id, metadata.get(SETTINGS_KEY)))
        if updated is None:
            session.rollback()
            return None
        metadata[SETTINGS_KEY] = _to_payload(updated)
        row.extra_metadata = metadata
        flag_modified(row, "extra_metadata")
        session.commit()
        return updated


def open_drain(db: Any, *, tenant_id: int) -> Barrier:
    """Stop this tenant admitting new work, fleet-wide, from this instant.

    The generation is bumped so a worker still reporting the previous one is
    visibly behind rather than indistinguishable from a converged fleet.
    """
    def change(current: Barrier) -> Barrier:
        return dataclasses.replace(
            current, state=STATE_DRAINING, generation=current.generation + 1,
            opened_at=_now(), settled_at=None, evidence={})

    barrier = _mutate(db, tenant_id, change)
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] drain opened tenant=%s generation=%s",
                   tenant_id, barrier.generation if barrier else None)
    return barrier if barrier is not None else _empty(tenant_id)


def note_worker(db: Any, *, tenant_id: int, state: str, force: bool = False) -> None:
    """Record that this process is running the barrier it just read.

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
    name = worker_id()

    def change(current: Barrier) -> Barrier:
        workers = {w.worker_id: w for w in current.workers}
        workers[name] = WorkerReport(worker_id=name, generation=current.generation,
                                     state=str(state), seen_at=_now())
        return dataclasses.replace(current, workers=tuple(
            sorted(workers.values(), key=lambda w: w.worker_id)))

    try:
        _mutate(db, tenant_id, change)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME_HANDOVER] heartbeat failed tenant=%s error=%s",
                       tenant_id, type(exc).__name__)


def buffer_inbound(db: Any, *, tenant_id: int, provider_message_id: str, recipient: str,
                   reason: str) -> bool:
    """Record an inbound this tenant refused because it is draining.

    Buffering is what makes refusing honest: the message is not answered and is
    not forgotten either. Settlement is blocked until every entry has a recorded
    disposition, so nothing here is acknowledged and quietly dropped.
    """
    identity = str(provider_message_id or "").strip()
    if not identity:
        return False

    def change(current: Barrier) -> Optional[Barrier]:
        if any(entry.get("provider_message_id") == identity for entry in current.buffered):
            return None                                    # already recorded
        if len(current.buffered) >= MAX_BUFFERED_ENTRIES:
            logger.error("[COMMERCE_RUNTIME_HANDOVER] buffer full tenant=%s; refusing to drop "
                         "the oldest entry", tenant_id)
            return None
        entry = {"provider_message_id": identity, "recipient": str(recipient or ""),
                 "reason": str(reason), "buffered_at": _iso(_now()), "disposition": None}
        return dataclasses.replace(current, buffered=current.buffered + (entry,))

    try:
        return _mutate(db, tenant_id, change) is not None
    except Exception as exc:  # noqa: BLE001
        # The inbound is still withheld — releasing it to another owner while a
        # send may be in flight is the worse failure — but it is now withheld
        # *unrecorded*, so settlement cannot see it. This line is the record:
        # an operator must account for this message by hand.
        logger.error("[COMMERCE_RUNTIME_HANDOVER] could not buffer inbound tenant=%s "
                     "provider_message_id=%s error=%s — withheld but UNRECORDED; "
                     "account for it by hand before settling",
                     tenant_id, identity, type(exc).__name__)
        return False


def dispose_buffered(db: Any, *, tenant_id: int, disposition: str, by: str) -> int:
    """Record what the operator did with the buffered inbounds. Returns the count."""
    stamped = {"disposition": str(disposition), "disposed_by": str(by),
               "disposed_at": _iso(_now())}

    def change(current: Barrier) -> Optional[Barrier]:
        pending = current.undisposed_buffered
        if not pending:
            return None
        buffered = tuple(dict(entry, **stamped) if not entry.get("disposition") else entry
                         for entry in current.buffered)
        return dataclasses.replace(current, buffered=buffered)

    before = len(read_barrier(db, tenant_id=tenant_id).undisposed_buffered)
    _mutate(db, tenant_id, change)
    return before


def record_settlement(db: Any, *, tenant_id: int, evidence: Mapping[str, Any]) -> Barrier:
    """Preserve what the handover was decided on, then mark it settled.

    Written before ingress reopens, so the evidence outlives the state it
    describes.
    """
    def change(current: Barrier) -> Barrier:
        return dataclasses.replace(current, state=STATE_SETTLED, settled_at=_now(),
                                   evidence=dict(evidence))

    barrier = _mutate(db, tenant_id, change)
    return barrier if barrier is not None else _empty(tenant_id)


def reopen(db: Any, *, tenant_id: int) -> Barrier:
    """Admit new work again, on a fresh generation, keeping the audit trail."""
    def change(current: Barrier) -> Barrier:
        return dataclasses.replace(current, state=STATE_OPEN,
                                   generation=current.generation + 1,
                                   opened_at=_now(), settled_at=None, workers=())

    barrier = _mutate(db, tenant_id, change)
    logger.warning("[COMMERCE_RUNTIME_HANDOVER] ingress reopened tenant=%s generation=%s",
                   tenant_id, barrier.generation if barrier else None)
    return barrier if barrier is not None else _empty(tenant_id)


__all__ = [
    "ADVISORY_LOCK_NAMESPACE", "Barrier", "HEARTBEAT_INTERVAL_SECONDS", "MAX_BUFFERED_ENTRIES", "SETTINGS_KEY",
    "STATES", "STATE_DRAINING", "STATE_OPEN", "STATE_SETTLED", "WORKER_LIVE_SECONDS",
    "WorkerReport", "admits_new_work_on", "barrier_admits_new_work", "buffer_inbound",
    "dispose_buffered",
    "note_worker", "open_drain", "read_barrier", "record_settlement", "reopen", "worker_id",
]
