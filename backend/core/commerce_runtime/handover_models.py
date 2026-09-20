"""Durable tables of the handover: the barrier, the fleet, and deferred work.

Three things a handover needs that a JSON blob on a shared row cannot give it,
and one that it actively took away.

**Its own document.** The barrier previously lived under a namespaced key in
``tenant_settings.metadata`` — a document several unrelated writers read, modify
and write whole. Any of them could put back a copy taken before the drain and
silently reopen it. Nothing coordinates those writers, and coordinating all of
them would mean rewriting settings code that has nothing to do with this. The
runtime's own state belongs in the runtime's own tables, where an unrelated
writer cannot reach it at all.

**A fleet with names.** Convergence is a statement about *workers*, so a worker
is a row: the generation and state **it actually observed**, when it last said
so, and — if it has been taken out of the fleet — who retired it and why.
Silence is not retirement. A worker that stops reporting is *stale*, which
blocks a handover until an operator fences it deliberately and that decision is
recorded.

**Work with an identity.** A deferred inbound is not a line in a list. It is one
message, for one tenant, on one channel connection, from one recipient, with the
provider's own message id and enough payload to be replayed. Disposition is then
per entry and checked against the state that entry is actually in, rather than a
free-text note stamped across everything pending at that moment.

The same table is what makes acceptance honest: a pilot-scoped inbound is
recorded here **before** the webhook acknowledges it, so work the provider has
been told we accepted survives a worker that dies a millisecond later.

Like the foundation and the ledgers, these live on ``RuntimeBase`` metadata,
outside the application's ``models.Base``: production startup materialises
``models.Base`` and pins ``alembic upgrade 0093``, so nothing here reaches a
database until revision ``0111`` is applied on purpose.

PostgreSQL semantics are assumed (``now()``, row locks, JSONB, partial indexes).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from core.commerce_runtime.models import RuntimeBase

# BIGINT on PostgreSQL, where these tables live; INTEGER on SQLite, whose
# autoincrement only applies to ``INTEGER PRIMARY KEY``. The same declaration
# therefore works in the operator tests without weakening the real column.
_SURROGATE_KEY = BigInteger().with_variant(Integer, "sqlite")

BARRIER_TABLE = "commerce_runtime_handover_barrier"
WORKERS_TABLE = "commerce_runtime_handover_workers"
DEFERRED_TABLE = "commerce_runtime_deferred_inbound"

HANDOVER_TABLES = (BARRIER_TABLE, WORKERS_TABLE, DEFERRED_TABLE)

STATE_OPEN = "open"
STATE_DRAINING = "draining"
STATE_SETTLED = "settled"
# The operator has verified, under the lock, that the settlement still holds and
# the fleet is accounted for, and has been told the pilot may be switched off.
# From this instant acceptance refuses new pilot-scoped work outright — the
# provider is answered retryable, nothing is recorded — so nothing accepted can
# be abandoned by the configuration change that follows.
STATE_RELEASED = "released"
BARRIER_STATES = (STATE_OPEN, STATE_DRAINING, STATE_SETTLED, STATE_RELEASED)

# What a deferred inbound is waiting for, and what became of it.
DEFERRED_PENDING = "pending"        # accepted, not yet finished by anybody
DEFERRED_RESOLVED = "resolved"      # the runtime finished this turn
DEFERRED_DISPOSED = "disposed"      # an operator accounted for it, with evidence
DEFERRED_STATES = (DEFERRED_PENDING, DEFERRED_RESOLVED, DEFERRED_DISPOSED)

# Why the record exists. Every one of these is "the provider was told we have
# this message"; they differ in where the turn stopped.
REASON_ACCEPTED = "accepted"                  # acknowledged, processing to follow
REASON_DRAIN_BUFFERED = "drain_buffered"      # the tenant's barrier is draining
REASON_PROCESS_DRAINING = "process_draining"  # this process is out of rotation
REASON_ADMISSION_REFUSED = "admission_refused"   # the barrier closed at admission
REASON_SETTLED_WINDOW = "settled_window"      # arrived after settlement, before reopen
DEFERRED_REASONS = (REASON_ACCEPTED, REASON_DRAIN_BUFFERED, REASON_PROCESS_DRAINING,
                    REASON_ADMISSION_REFUSED, REASON_SETTLED_WINDOW)

# How an operator may account for a deferred entry. Each one names evidence the
# operator has; none of them is "we decided to stop looking".
DISPOSITION_REPLAYED = "replayed"          # re-delivered and handled; evidence names how
DISPOSITION_ANSWERED = "answered"          # the customer was answered by another path
DISPOSITION_SUPERSEDED = "superseded"      # a later message from the same customer replaced it
DISPOSITION_NOT_REQUIRED = "not_required"  # established that no answer was owed
DISPOSITIONS = (DISPOSITION_REPLAYED, DISPOSITION_ANSWERED, DISPOSITION_SUPERSEDED,
                DISPOSITION_NOT_REQUIRED)

_NAMESPACE_SQL = "namespace IN ('live', 'shadow')"
_BARRIER_STATE_SQL = "state IN ('open', 'draining', 'settled', 'released')"
_DEFERRED_STATE_SQL = "state IN ('pending', 'resolved', 'disposed')"
# Written as an equality rather than two OR'd clauses on purpose: with an OR,
# ``state = 'disposed'`` and a NULL disposition evaluates to NULL, and a CHECK
# passes on NULL. A row could then claim to be disposed of while naming nothing.
_DISPOSITION_SQL = (
    "((state = 'disposed') = (disposition IS NOT NULL)) AND "
    "(disposition IS NULL OR disposition IN "
    "('replayed', 'answered', 'superseded', 'not_required'))"
)


class HandoverBarrier(RuntimeBase):
    """One row per tenant: whether new work may start, and on which generation.

    ``generation`` increases on every transition. It is what makes "the fleet
    has seen this" a checkable statement rather than a hope: a worker reports
    the generation it read, and one still reporting an older number is visibly
    behind rather than indistinguishable from a converged one.
    """

    __tablename__ = BARRIER_TABLE

    tenant_id = Column(Integer, primary_key=True)
    namespace = Column(String(16), primary_key=True)
    state = Column(String(16), nullable=False, default=STATE_OPEN,
                   server_default=text("'open'"))
    generation = Column(BigInteger, nullable=False, default=0,
                        server_default=text("0"))
    opened_at = Column(DateTime(timezone=True), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    # When the release was verified and written. Acceptance reads it under the
    # shared lock, so an inbound cannot be accepted after it.
    released_at = Column(DateTime(timezone=True), nullable=True)
    # The snapshot the settlement decision rested on, written in the same
    # transaction as the transition it describes.
    evidence = Column(JSONB, nullable=False, default=dict,
                      server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_handover_barrier_namespace"),
        CheckConstraint(_BARRIER_STATE_SQL, name="ck_commerce_runtime_handover_barrier_state"),
        CheckConstraint("generation >= 0", name="ck_commerce_runtime_handover_barrier_generation"),
    )


class HandoverWorker(RuntimeBase):
    """One row per process that has evaluated a route for this tenant.

    ``observed_generation`` and ``observed_state`` are what the worker **read**,
    passed in by the worker itself. They are deliberately not re-derived when
    the row is written: a heartbeat that stamped the current database generation
    onto an observation made before it would turn "I have not seen the drain"
    into "I am converged", which is the one thing this row exists to prevent.
    """

    __tablename__ = WORKERS_TABLE

    id = Column(_SURROGATE_KEY, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    worker_id = Column(String(200), nullable=False)
    observed_generation = Column(BigInteger, nullable=False)
    observed_state = Column(String(16), nullable=False)
    seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # Retirement is an operator act with a name on it. A worker is never retired
    # by going quiet.
    retired_at = Column(DateTime(timezone=True), nullable=True)
    retired_by = Column(String(200), nullable=True)
    retired_reason = Column(Text, nullable=True)
    # What the operator showed: the deployment identity, how the stop was
    # verified, and when it was observed. Elapsed silence is deliberately not a
    # value this can hold — a quiet worker is one nobody has heard from, which
    # is the case the fleet table exists to keep apart from a stopped one.
    retirement_evidence = Column(JSONB, nullable=False, default=dict,
                                 server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "namespace", "worker_id",
                         name="uq_commerce_runtime_handover_workers_identity"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_handover_workers_namespace"),
        CheckConstraint("observed_generation >= 0",
                        name="ck_commerce_runtime_handover_workers_generation"),
        # Written as an equality on purpose: with OR'd clauses a NULL on one
        # side evaluates to NULL and a CHECK passes on NULL.
        CheckConstraint(
            "((retired_at IS NOT NULL) = (retired_by IS NOT NULL))",
            name="ck_commerce_runtime_handover_workers_retirement"),
        Index("ix_commerce_runtime_handover_workers_active", "tenant_id", "namespace",
              postgresql_where=text("retired_at IS NULL")),
    )


class DeferredInbound(RuntimeBase):
    """One accepted inbound message that nobody has finished yet.

    Written before the webhook acknowledges a pilot-scoped message, so an
    acknowledgement is a promise the database can keep. It carries everything a
    replay needs — tenant, namespace, channel connection, recipient, the
    provider's own message id, and the payload — because an identity without a
    payload is a record of something lost rather than something recoverable.
    """

    __tablename__ = DEFERRED_TABLE

    id = Column(_SURROGATE_KEY, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    channel_connection_ref = Column(String(128), nullable=False)
    phone_number_id = Column(String(64), nullable=False)
    recipient = Column(String(64), nullable=False)
    provider_message_id = Column(String(200), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict,
                     server_default=text("'{}'::jsonb"))
    reason = Column(String(32), nullable=False)
    state = Column(String(16), nullable=False, default=DEFERRED_PENDING,
                   server_default=text("'pending'"))
    barrier_generation = Column(BigInteger, nullable=True)
    # Disposition is per entry, and evidence says what actually happened to it.
    disposition = Column(String(32), nullable=True)
    disposition_evidence = Column(JSONB, nullable=False, default=dict,
                                  server_default=text("'{}'::jsonb"))
    disposed_by = Column(String(200), nullable=True)
    disposed_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "namespace", "channel_connection_ref",
                         "provider_message_id",
                         name="uq_commerce_runtime_deferred_inbound_identity"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_deferred_inbound_namespace"),
        CheckConstraint(_DEFERRED_STATE_SQL, name="ck_commerce_runtime_deferred_inbound_state"),
        CheckConstraint(
            "reason IN ('accepted', 'drain_buffered', 'process_draining', "
            "'admission_refused', 'settled_window')",
            name="ck_commerce_runtime_deferred_inbound_reason"),
        CheckConstraint(_DISPOSITION_SQL, name="ck_commerce_runtime_deferred_inbound_disposition"),
        # Everything the operator and the settlement check ask for is "what is
        # still pending for this tenant", so that is the index. Disposed history
        # stays in the table and out of this index's way.
        Index("ix_commerce_runtime_deferred_inbound_pending", "tenant_id", "namespace",
              postgresql_where=text("state = 'pending'")),
        Index("ix_commerce_runtime_deferred_inbound_created", "tenant_id", "created_at"),
    )


HANDOVER_TABLE_OBJECTS = (
    HandoverBarrier.__table__,
    HandoverWorker.__table__,
    DeferredInbound.__table__,
)


def create_handover_tables(bind) -> None:
    """Create exactly these three relations. Used by tests, never by startup."""
    RuntimeBase.metadata.create_all(bind, tables=list(HANDOVER_TABLE_OBJECTS))


__all__ = [
    "BARRIER_STATES", "BARRIER_TABLE", "DEFERRED_DISPOSED", "DEFERRED_PENDING",
    "DEFERRED_REASONS", "DEFERRED_RESOLVED", "DEFERRED_STATES", "DEFERRED_TABLE",
    "DISPOSITIONS", "DISPOSITION_ANSWERED", "DISPOSITION_NOT_REQUIRED",
    "DISPOSITION_REPLAYED", "DISPOSITION_SUPERSEDED", "DeferredInbound",
    "HANDOVER_TABLES", "HANDOVER_TABLE_OBJECTS", "HandoverBarrier", "HandoverWorker",
    "REASON_ACCEPTED", "REASON_ADMISSION_REFUSED", "REASON_DRAIN_BUFFERED",
    "REASON_PROCESS_DRAINING", "REASON_SETTLED_WINDOW", "STATE_DRAINING", "STATE_OPEN",
    "STATE_RELEASED", "STATE_SETTLED", "WORKERS_TABLE", "create_handover_tables",
]
