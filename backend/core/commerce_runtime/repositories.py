"""Short-transaction repository of the dormant commerce runtime foundation.

Transaction behaviour (bounded, stated exactly)
===============================================
Every public operation runs **one write transaction**, opened and committed
or rolled back before the operation returns. Two operations may open **one
further read-only transaction** after their write transaction rolled back on
a database conflict, to report the committed truth instead of guessing:
``admit_turn`` after an admission-identity race and ``record_terminal`` after
a terminal primary-key race. No operation opens more than those, and none of
them calls a model, a provider or the network, so no transaction is ever
held across such a call.

Time
====
Lease validity and lease expiry use the database's current wall clock,
``clock_timestamp()``, sampled **after** the conversation row lock has been
acquired and again inside every guarded UPDATE. ``now()`` (transaction start
time) is never used for validity or expiry: a connection that waited on a
row lock across a lease expiry must see that expiry.

Ownership tokens
================
A token is bound to the scope it was issued for (tenant, namespace,
conversation) in addition to owner, fence and epoch; every token-bearing
operation validates the complete binding before touching the database, and
``record_terminal`` compares it with the scope derived from the turn row.

Ordered processing
==================
The *eligible* turn of a conversation is its oldest admitted turn without a
terminal record. State commits and terminal records are bound to the
eligible turn; a claim may name the turn it intends to process and is
refused when that turn is not the eligible one. Nothing here schedules work:
the repository enforces the order for whoever calls it.

PostgreSQL only (row locks, ``clock_timestamp()``, ``ON CONFLICT``, JSONB).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine, Row
from sqlalchemy.exc import IntegrityError

from core.commerce_runtime import contracts as c
from core.commerce_runtime.models import RuntimeConversation, RuntimeTurn, RuntimeTurnTerminal

CONV = RuntimeConversation.__table__
TURN = RuntimeTurn.__table__
TERM = RuntimeTurnTerminal.__table__

# Ledger relations of revision 0109, consulted by name only when they exist,
# so the foundation keeps working on the standalone 0108 schema.
LEDGER_EFFECTS_TABLE = "commerce_runtime_effects"
LEDGER_SEQUENCES_TABLE = "commerce_runtime_delivery_sequences"
# Every relation of the revision 0109 ledger schema. The completion guard
# classifies the database by their presence: none present is the standalone
# 0108 foundation schema, all present applies the ledger-aware completion
# rules, anything in between fails closed.
LEDGER_RELATIONS: Tuple[str, ...] = (
    LEDGER_EFFECTS_TABLE,
    "commerce_runtime_effect_attempts",
    "commerce_runtime_effect_results",
    LEDGER_SEQUENCES_TABLE,
    "commerce_runtime_delivery_attempts",
    "commerce_runtime_delivery_receipts",
)


def _db_now(conn: Connection) -> _dt.datetime:
    """Current database wall time (not the transaction start time)."""
    return conn.execute(select(func.clock_timestamp())).scalar_one()


def _eligible_turn(conn: Connection, conversation_id: int) -> Tuple[Optional[int], Optional[int]]:
    """The oldest admitted turn of the conversation without a terminal record."""
    row = conn.execute(
        select(TURN.c.id, TURN.c.sequence)
        .select_from(TURN.outerjoin(TERM, TERM.c.turn_id == TURN.c.id))
        .where(TURN.c.conversation_id == conversation_id, TERM.c.turn_id.is_(None))
        .order_by(TURN.c.sequence)
        .limit(1)
    ).one_or_none()
    if row is None:
        return None, None
    return int(row.id), int(row.sequence)


def _snapshot(conn: Connection, row: Row) -> c.ConversationSnapshot:
    m = row._mapping
    eligible_turn_id, eligible_sequence = _eligible_turn(conn, int(m["id"]))
    return c.ConversationSnapshot(
        conversation_id=int(m["id"]), tenant_id=int(m["tenant_id"]), namespace=str(m["namespace"]),
        conversation_ref=str(m["conversation_ref"]), next_sequence=int(m["next_sequence"]),
        ownership_epoch=int(m["ownership_epoch"]), lease_owner=m["lease_owner"],
        lease_fence=int(m["lease_fence"]), lease_expires_at=m["lease_expires_at"],
        state_revision=int(m["state_revision"]), state_payload=dict(m["state_payload"] or {}),
        db_now=_db_now(conn), eligible_turn_id=eligible_turn_id, eligible_sequence=eligible_sequence,
    )


def _turn(row: Row, *, duplicate: bool = False) -> c.AdmittedTurn:
    m = row._mapping
    return c.AdmittedTurn(
        turn_id=int(m["id"]), conversation_id=int(m["conversation_id"]), tenant_id=int(m["tenant_id"]),
        namespace=str(m["namespace"]), sequence=int(m["sequence"]),
        channel_connection_ref=str(m["channel_connection_ref"]),
        provider_message_id=str(m["provider_message_id"]), admitted_at=m["admitted_at"],
        payload=dict(m["payload"] or {}), duplicate=duplicate,
    )


def _terminal(row: Row) -> c.TerminalRecord:
    m = row._mapping
    return c.TerminalRecord(
        turn_id=int(m["turn_id"]), conversation_id=int(m["conversation_id"]), tenant_id=int(m["tenant_id"]),
        namespace=str(m["namespace"]), processing_outcome=str(m["processing_outcome"]),
        transport_outcome=str(m["transport_outcome"]), customer_reach=str(m["customer_reach"]),
        recorded_fence=int(m["recorded_fence"]), recorded_epoch=int(m["recorded_epoch"]),
        recorded_by=str(m["recorded_by"]), details=dict(m["details"] or {}), recorded_at=m["recorded_at"],
    )


def _lease_expiry(lease_seconds: int):
    return func.clock_timestamp() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)


class CommerceRuntimeRepository:
    """Tenant-scoped, namespace-scoped persistence operations."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── Reads ────────────────────────────────────────────────────────────────

    def get_conversation(
        self, *, tenant_id: int, namespace: Any, conversation_id: Optional[int] = None,
        conversation_ref: Optional[str] = None,
    ) -> c.ConversationSnapshot:
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        if (conversation_id is None) == (conversation_ref is None):
            raise c.ValidationError("exactly one of conversation_id or conversation_ref is required")
        with self._engine.begin() as conn:
            snap = self._load(conn, tenant_id, ns, conversation_id=conversation_id,
                              conversation_ref=conversation_ref)
        if snap is None:
            raise c.ConversationNotFound(f"conversation not found in tenant {tenant_id}/{ns}")
        return snap

    def list_turns(self, *, tenant_id: int, namespace: Any, conversation_id: int) -> List[c.AdmittedTurn]:
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        with self._engine.begin() as conn:
            rows = conn.execute(
                select(TURN).where(
                    TURN.c.tenant_id == tenant_id, TURN.c.namespace == ns,
                    TURN.c.conversation_id == conversation_id,
                ).order_by(TURN.c.sequence)
            ).all()
        return [_turn(r) for r in rows]

    def get_terminal(self, *, tenant_id: int, namespace: Any, turn_id: int) -> Optional[c.TerminalRecord]:
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        turn_id = c.validate_counter(turn_id, field="turn_id")
        with self._engine.begin() as conn:
            row = conn.execute(
                select(TERM).where(TERM.c.tenant_id == tenant_id, TERM.c.namespace == ns, TERM.c.turn_id == turn_id)
            ).one_or_none()
        return _terminal(row) if row is not None else None

    # ── Admission ────────────────────────────────────────────────────────────

    def admit_turn(
        self, *, tenant_id: int, namespace: Any, conversation_ref: str, channel_connection_ref: str,
        provider_message_id: str, payload: Optional[Mapping[str, Any]] = None,
    ) -> c.AdmittedTurn:
        """Admit one inbound message exactly once and give it the next sequence.

        The identity is (tenant, namespace, channel connection, provider
        message id). A repeated admission returns the existing turn with
        ``duplicate=True`` and consumes no sequence number; the same identity
        presented for a different conversation is an explicit conflict that
        writes nothing. One write transaction; after an identity race that
        rolled it back, one read transaction reports the committed truth.
        """
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_ref = c.validate_ref(conversation_ref, field="conversation_ref", max_length=c.MAX_REF_LENGTH)
        channel = c.validate_ref(channel_connection_ref, field="channel_connection_ref", max_length=c.MAX_REF_LENGTH)
        pmid = c.validate_ref(
            provider_message_id, field="provider_message_id", max_length=c.MAX_PROVIDER_MESSAGE_ID_LENGTH,
        )
        body = c.validate_payload(payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
        identity = (TURN.c.tenant_id == tenant_id, TURN.c.namespace == ns,
                    TURN.c.channel_connection_ref == channel, TURN.c.provider_message_id == pmid)
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    pg_insert(CONV)
                    .values(tenant_id=tenant_id, namespace=ns, conversation_ref=conversation_ref)
                    .on_conflict_do_nothing(constraint="uq_commerce_runtime_conversations_identity")
                )
                conv = conn.execute(
                    select(CONV.c.id).where(
                        CONV.c.tenant_id == tenant_id, CONV.c.namespace == ns,
                        CONV.c.conversation_ref == conversation_ref,
                    ).with_for_update()
                ).one()
                existing = conn.execute(select(TURN).where(*identity)).one_or_none()
                if existing is not None:
                    if int(existing._mapping["conversation_id"]) != int(conv.id):
                        raise c.AdmissionConflict(
                            "provider message already admitted to a different conversation"
                        )
                    return _turn(existing, duplicate=True)
                sequence = conn.execute(
                    update(CONV).where(CONV.c.id == conv.id)
                    .values(next_sequence=CONV.c.next_sequence + 1, updated_at=func.clock_timestamp())
                    .returning(CONV.c.next_sequence)
                ).scalar_one() - 1
                row = conn.execute(
                    insert(TURN).values(
                        tenant_id=tenant_id, namespace=ns, conversation_id=int(conv.id),
                        channel_connection_ref=channel, provider_message_id=pmid,
                        sequence=sequence, payload=body,
                    ).returning(TURN)
                ).one()
                return _turn(row)
        except IntegrityError:
            # Two different conversation rows raced on the same inbound identity:
            # the losing write transaction rolled back (no sequence consumed).
            # One read transaction reports the committed truth.
            with self._engine.begin() as conn:
                existing = conn.execute(select(TURN).where(*identity)).one_or_none()
                conv_id = conn.execute(
                    select(CONV.c.id).where(
                        CONV.c.tenant_id == tenant_id, CONV.c.namespace == ns,
                        CONV.c.conversation_ref == conversation_ref,
                    )
                ).scalar_one_or_none()
            if existing is None:
                raise
            if conv_id is None or int(existing._mapping["conversation_id"]) != int(conv_id):
                raise c.AdmissionConflict("provider message already admitted to a different conversation")
            return _turn(existing, duplicate=True)

    # ── Ownership ────────────────────────────────────────────────────────────

    def claim(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, owner_id: str, lease_seconds: int,
        turn_id: Optional[int] = None,
    ) -> c.Lease:
        """Take exclusive ownership when no unexpired lease exists.

        Validity is judged on the database wall clock read after the row lock
        was acquired, so a claimant that waited on the lock across an expiry
        takes the lease over instead of being told it is held, and the new
        expiry is measured from that same moment. Each successful claim
        issues ``lease_fence + 1``; fences are never reset. Taking over a
        lease that expired without being released also advances the
        ownership epoch. ``turn_id``, when given, must be the eligible turn
        (the oldest turn without a terminal), otherwise ``turn_not_eligible``.
        """
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        owner_id = c.validate_owner_id(owner_id)
        lease_seconds = c.validate_lease_seconds(lease_seconds)
        if turn_id is not None:
            turn_id = c.validate_counter(turn_id, field="turn_id")
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            held = snap.lease_owner is not None and snap.lease_expires_at is not None \
                and snap.lease_expires_at > snap.db_now
            if held:
                raise c.OwnershipRejected(c.RejectReason.LEASE_HELD, snap)
            if turn_id is not None and turn_id != snap.eligible_turn_id:
                raise c.OwnershipRejected(c.RejectReason.TURN_NOT_ELIGIBLE, snap)
            takeover = snap.lease_owner is not None
            row = conn.execute(
                update(CONV)
                .where(
                    CONV.c.id == conversation_id, CONV.c.tenant_id == tenant_id, CONV.c.namespace == ns,
                    CONV.c.lease_fence == snap.lease_fence, CONV.c.ownership_epoch == snap.ownership_epoch,
                )
                .values(
                    lease_owner=owner_id,
                    lease_fence=CONV.c.lease_fence + 1,
                    ownership_epoch=CONV.c.ownership_epoch + (1 if takeover else 0),
                    lease_expires_at=_lease_expiry(lease_seconds),
                    updated_at=func.clock_timestamp(),
                )
                .returning(CONV.c.lease_fence, CONV.c.ownership_epoch, CONV.c.lease_expires_at)
            ).one_or_none()
            if row is None:
                raise c.OwnershipRejected(c.RejectReason.UNCLASSIFIED, snap)
            return c.Lease(
                conversation_id=conversation_id, tenant_id=tenant_id, namespace=ns, owner_id=owner_id,
                fence=int(row.lease_fence), epoch=int(row.ownership_epoch), expires_at=row.lease_expires_at,
                db_now=snap.db_now, takeover=takeover,
                eligible_turn_id=snap.eligible_turn_id, eligible_sequence=snap.eligible_sequence,
            )

    def renew(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, lease_seconds: int,
    ) -> c.Lease:
        """Extend a lease that is still valid on the database clock after the lock."""
        tenant_id, ns, conversation_id, token = self._scoped(tenant_id, namespace, conversation_id, token)
        lease_seconds = c.validate_lease_seconds(lease_seconds)
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            self._require(snap, token)
            row = conn.execute(
                self._guarded_update(conversation_id, tenant_id, ns, token)
                .values(lease_expires_at=_lease_expiry(lease_seconds), updated_at=func.clock_timestamp())
                .returning(CONV.c.lease_expires_at)
            ).one_or_none()
            if row is None:
                self._reject_after_failed_guard(conn, tenant_id, ns, conversation_id, token)
            return c.Lease(
                conversation_id=conversation_id, tenant_id=tenant_id, namespace=ns, owner_id=token.owner_id,
                fence=token.fence, epoch=token.epoch, expires_at=row.lease_expires_at, db_now=snap.db_now,
                takeover=False, eligible_turn_id=snap.eligible_turn_id, eligible_sequence=snap.eligible_sequence,
            )

    def release(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken,
    ) -> c.ConversationSnapshot:
        """Give up a still-valid lease. The fence stays where it is."""
        tenant_id, ns, conversation_id, token = self._scoped(tenant_id, namespace, conversation_id, token)
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            self._require(snap, token)
            result = conn.execute(
                self._guarded_update(conversation_id, tenant_id, ns, token)
                .values(lease_owner=None, lease_expires_at=None, updated_at=func.clock_timestamp())
            )
            if result.rowcount != 1:
                self._reject_after_failed_guard(conn, tenant_id, ns, conversation_id, token)
            return self._load(conn, tenant_id, ns, conversation_id=conversation_id)

    def invalidate_ownership(
        self, *, tenant_id: int, namespace: Any, conversation_id: int,
    ) -> c.ConversationSnapshot:
        """Administrative fencing: advance the ownership epoch and clear the lease.

        Every token issued before this call becomes ``obsolete_epoch``. Fences
        are untouched. Nothing in production calls this; it exists so the
        epoch contract is real and testable.
        """
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            conn.execute(
                update(CONV)
                .where(CONV.c.id == conversation_id, CONV.c.tenant_id == tenant_id, CONV.c.namespace == ns,
                       CONV.c.ownership_epoch == snap.ownership_epoch)
                .values(ownership_epoch=CONV.c.ownership_epoch + 1, lease_owner=None, lease_expires_at=None,
                        updated_at=func.clock_timestamp())
            )
            return self._load(conn, tenant_id, ns, conversation_id=conversation_id)

    # ── State ────────────────────────────────────────────────────────────────

    def commit_state(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, turn_id: int,
        expected_revision: int, payload: Mapping[str, Any],
    ) -> c.StateCommit:
        """Compare-and-set the state for the eligible turn.

        ``turn_id`` must be the conversation's eligible turn (its oldest turn
        without a terminal); a commit for any other turn is refused with
        ``turn_not_eligible``. Revision ``expected`` becomes ``expected + 1``.
        """
        tenant_id, ns, conversation_id, token = self._scoped(tenant_id, namespace, conversation_id, token)
        turn_id = c.validate_counter(turn_id, field="turn_id")
        expected_revision = c.validate_counter(expected_revision, field="expected_revision")
        body = c.validate_payload(payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            self._require(snap, token, expected_revision=expected_revision)
            if snap.eligible_turn_id != turn_id:
                raise c.OwnershipRejected(c.RejectReason.TURN_NOT_ELIGIBLE, snap)
            row = self._apply_state(conn, conversation_id, tenant_id, ns, token, expected_revision, body)
            if row is None:
                self._reject_after_failed_guard(conn, tenant_id, ns, conversation_id, token,
                                                expected_revision=expected_revision)
            return c.StateCommit(
                conversation_id=conversation_id, revision=int(row.state_revision), fence=token.fence,
                epoch=token.epoch, committed_at=row.updated_at,
            )

    # ── Terminal ─────────────────────────────────────────────────────────────

    def record_terminal(
        self, *, tenant_id: int, namespace: Any, turn_id: int, token: c.OwnershipToken,
        processing_outcome: Any, transport_outcome: Any, customer_reach: Any,
        details: Optional[Mapping[str, Any]] = None, state_transition: Optional[c.StateTransition] = None,
        _fault_before_commit: Optional[Callable[[], None]] = None,
    ) -> c.TerminalRecord:
        """Record the single immutable terminal of the eligible turn, atomically
        with an optional state transition, under a valid ownership token.

        The token's scope is compared with the scope derived from the turn
        row. The turn must be the conversation's eligible turn: an older
        unresolved turn refuses the finalisation of a newer one. Lease
        validity is re-checked by a guarded UPDATE on the database clock
        immediately before the terminal row is inserted. Processing outcome,
        transport outcome and customer reach are three separate facts;
        ``transport_outcome='unknown'`` is stored as unknown and grants no
        replay. One write transaction; after a terminal primary-key race that
        rolled it back, one read transaction reports the existing terminal.
        ``_fault_before_commit`` is a test-only fault-injection point.
        """
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        turn_id = c.validate_counter(turn_id, field="turn_id")
        token = c.validate_token(token)
        processing = c.validate_enum(processing_outcome, c.ProcessingOutcome, field="processing_outcome")
        transport = c.validate_enum(transport_outcome, c.TransportOutcome, field="transport_outcome")
        reach = c.validate_enum(customer_reach, c.CustomerReach, field="customer_reach")
        body = c.validate_payload(details, field="details", max_bytes=c.MAX_DETAILS_BYTES)
        expected_revision: Optional[int] = None
        state_body: Optional[Dict[str, Any]] = None
        if state_transition is not None:
            if not isinstance(state_transition, c.StateTransition):
                raise c.ValidationError("state_transition must be a StateTransition")
            expected_revision = c.validate_counter(state_transition.expected_revision, field="expected_revision")
            state_body = c.validate_payload(state_transition.payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
        try:
            with self._engine.begin() as conn:
                return self._record_terminal_in(
                    conn, tenant_id=tenant_id, ns=ns, turn_id=turn_id, token=token, processing=processing,
                    transport=transport, reach=reach, details=body, expected_revision=expected_revision,
                    state_body=state_body, fault_before_commit=_fault_before_commit,
                )
        except IntegrityError:
            # Two completion attempts raced past the existence check: the
            # primary key kept exactly one, this write transaction rolled back
            # whole, and one read transaction reports the existing terminal.
            existing = self.get_terminal(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
            if existing is None:
                raise
            raise c.TerminalAlreadyRecorded(existing) from None

    def _record_terminal_in(
        self, conn: Connection, *, tenant_id: int, ns: str, turn_id: int, token: c.OwnershipToken,
        processing: str, transport: Optional[str], reach: Optional[str], details: Dict[str, Any],
        expected_revision: Optional[int] = None, state_body: Optional[Dict[str, Any]] = None,
        fault_before_commit: Optional[Callable[[], None]] = None,
        resolve: Optional[Callable[[Connection, c.ConversationSnapshot], Tuple[Any, Any, Mapping[str, Any]]]] = None,
    ) -> c.TerminalRecord:
        """Insert the terminal of the eligible turn inside the caller's transaction.

        Inputs are already validated. ``resolve``, when given, runs after the
        conversation lock, the ownership guard, the existence check and the
        eligibility check, and returns the transport outcome, the customer
        reach and the details to record; the ledgers use it to derive those
        facts from rows read under the same lock. Without ``resolve`` the
        given ``transport`` and ``reach`` are recorded as they are.
        """
        turn = conn.execute(
            select(TURN).where(TURN.c.id == turn_id, TURN.c.tenant_id == tenant_id, TURN.c.namespace == ns)
        ).one_or_none()
        if turn is None:
            raise c.TurnNotFound(f"turn not found in tenant {tenant_id}/{ns}")
        conversation_id = int(turn._mapping["conversation_id"])
        c.require_token_scope(token, tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id)
        snap = self._lock(conn, tenant_id, ns, conversation_id)
        self._require(snap, token, expected_revision=expected_revision)
        existing = conn.execute(select(TERM).where(TERM.c.turn_id == turn_id)).one_or_none()
        if existing is not None:
            raise c.TerminalAlreadyRecorded(_terminal(existing))
        if snap.eligible_turn_id != turn_id:
            raise c.OwnershipRejected(c.RejectReason.TURN_NOT_ELIGIBLE, snap)
        self._enforce_ledger_completion(conn, tenant_id, ns, turn_id, derived=resolve is not None)
        if resolve is not None:
            transport, reach, resolved_details = resolve(conn, snap)
            transport = c.validate_enum(transport, c.TransportOutcome, field="transport_outcome")
            reach = c.validate_enum(reach, c.CustomerReach, field="customer_reach")
            details = c.validate_payload(resolved_details, field="details", max_bytes=c.MAX_DETAILS_BYTES)
        if transport is None or reach is None:
            raise c.ValidationError("transport_outcome and customer_reach are required")
        if state_body is not None:
            applied = self._apply_state(conn, conversation_id, tenant_id, ns, token,
                                        expected_revision, state_body)
            if applied is None:
                self._reject_after_failed_guard(conn, tenant_id, ns, conversation_id, token,
                                                expected_revision=expected_revision)
        else:
            touched = conn.execute(
                self._guarded_update(conversation_id, tenant_id, ns, token)
                .values(updated_at=func.clock_timestamp())
            )
            if touched.rowcount != 1:
                self._reject_after_failed_guard(conn, tenant_id, ns, conversation_id, token)
        row = conn.execute(
            insert(TERM).values(
                turn_id=turn_id, tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id,
                processing_outcome=processing, transport_outcome=transport, customer_reach=reach,
                recorded_fence=token.fence, recorded_epoch=token.epoch, recorded_by=token.owner_id,
                details=details,
            ).returning(TERM)
        ).one()
        if fault_before_commit is not None:
            fault_before_commit()
        return _terminal(row)

    # ── Ledger-aware completion (enforced for every terminal entry point) ────

    @staticmethod
    def _ledger_schema_state(conn: Connection) -> Tuple[str, Tuple[str, ...], Tuple[str, ...]]:
        """Classify the ledger schema as ``absent``, ``complete`` or ``partial``.

        Every relation of ``LEDGER_RELATIONS`` is resolved by name in one
        statement; the state is returned with the present and the missing
        relation names.
        """
        clauses = " UNION ALL ".join(
            f"SELECT :name{i} AS relation, to_regclass(:qualified{i}) IS NOT NULL AS present"
            for i in range(len(LEDGER_RELATIONS))
        )
        params: Dict[str, Any] = {}
        for i, name in enumerate(LEDGER_RELATIONS):
            params[f"name{i}"] = name
            params[f"qualified{i}"] = f"public.{name}"
        rows = conn.execute(text(clauses), params).all()
        present = tuple(str(r[0]) for r in rows if r[1])
        missing = tuple(str(r[0]) for r in rows if not r[1])
        if not present:
            return "absent", present, missing
        if not missing:
            return "complete", present, missing
        return "partial", present, missing

    @staticmethod
    def _enforce_ledger_completion(conn: Connection, tenant_id: int, ns: str, turn_id: int, *, derived: bool) -> None:
        """Refuse a terminal that would strand or misstate the turn's ledgers.

        Runs under the conversation lock before any write. The ledger schema
        of revision ``0109`` is classified first: **absent** (no ledger
        relation at all) is the standalone foundation schema and there is
        nothing to consult; **partial** (some relations missing) cannot
        establish whether the turn's obligations are complete, so the
        terminal is refused (``LedgerSchemaIncomplete``) for every turn and
        both entry points, with no automatic repair; **complete** applies the
        rules below. When the turn has effect or delivery records, completion
        is refused while an intent is reserved but not dispatched or an
        attempt has no established outcome; and the foundation entry point,
        which records caller-supplied transport and reach, is refused
        outright for such a turn (``derived`` is False): ledger-bearing turns
        complete only through the ledger-derived path.
        """
        state, present, missing = CommerceRuntimeRepository._ledger_schema_state(conn)
        if state == "absent":
            return
        if state == "partial":
            raise c.LedgerSchemaIncomplete(missing=missing, present=present)
        scope = {"tenant_id": tenant_id, "namespace": ns, "turn_id": turn_id}
        counts = {str(row[0]): int(row[1]) for row in conn.execute(text(
            f"SELECT status, count(*) FROM {LEDGER_EFFECTS_TABLE} "
            "WHERE tenant_id = :tenant_id AND namespace = :namespace AND turn_id = :turn_id GROUP BY status"
        ), scope).all()}
        sequence = conn.execute(text(
            f"SELECT attempt_count, outcome FROM {LEDGER_SEQUENCES_TABLE} "
            "WHERE tenant_id = :tenant_id AND namespace = :namespace AND turn_id = :turn_id"
        ), scope).one_or_none()
        if not counts and sequence is None:
            return
        if not derived:
            raise c.CompletionBlocked("ledger_bearing_turn", [
                f"turn {turn_id} has effect or delivery records; its terminal is recorded through the "
                "ledger-derived path (LedgerRepository.finalize_turn), not with caller-supplied outcomes",
            ])
        blockers: List[str] = []
        if counts.get("reserved"):
            blockers.append(f"{counts['reserved']} effect intent(s) reserved but not dispatched")
        if counts.get("dispatching"):
            blockers.append(f"{counts['dispatching']} effect attempt(s) without an established outcome")
        if sequence is not None:
            if int(sequence[0]) == 0:
                blockers.append("delivery intent reserved but not dispatched")
            elif str(sequence[1]) == "pending":
                blockers.append("delivery attempt without an established outcome")
        if blockers:
            raise c.CompletionBlocked("actionable_work_remains", blockers)

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _scoped(tenant_id: Any, namespace: Any, conversation_id: Any, token: Any) -> Tuple[int, str, int, c.OwnershipToken]:
        """Validate the target scope and the token, and bind them before any database access."""
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        token = c.validate_token(token)
        c.require_token_scope(token, tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id)
        return tenant_id, ns, conversation_id, token

    @staticmethod
    def _load(
        conn: Connection, tenant_id: int, ns: str, *, conversation_id: Optional[int] = None,
        conversation_ref: Optional[str] = None, for_update: bool = False,
    ) -> Optional[c.ConversationSnapshot]:
        stmt = select(CONV).where(CONV.c.tenant_id == tenant_id, CONV.c.namespace == ns)
        if conversation_id is not None:
            stmt = stmt.where(CONV.c.id == conversation_id)
        else:
            stmt = stmt.where(CONV.c.conversation_ref == conversation_ref)
        if for_update:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).one_or_none()
        if row is None:
            return None
        # The clock and the eligibility facts are read after the lock (if any) was acquired.
        return _snapshot(conn, row)

    def _lock(self, conn: Connection, tenant_id: int, ns: str, conversation_id: int) -> c.ConversationSnapshot:
        snap = self._load(conn, tenant_id, ns, conversation_id=conversation_id, for_update=True)
        if snap is None:
            raise c.ConversationNotFound(f"conversation not found in tenant {tenant_id}/{ns}")
        return snap

    @staticmethod
    def _require(
        snap: c.ConversationSnapshot, token: c.OwnershipToken, *, expected_revision: Optional[int] = None,
    ) -> None:
        reason = c.classify_rejection(
            current_owner=snap.lease_owner, current_fence=snap.lease_fence, current_epoch=snap.ownership_epoch,
            current_expires_at=snap.lease_expires_at, current_revision=snap.state_revision, token=token,
            db_now=snap.db_now, expected_revision=expected_revision,
        )
        if reason is c.RejectReason.STALE_REVISION:
            raise c.StateConflict(reason, snap)
        if reason is not None:
            raise c.OwnershipRejected(reason, snap)

    def _reject_after_failed_guard(
        self, conn: Connection, tenant_id: int, ns: str, conversation_id: int, token: c.OwnershipToken,
        *, expected_revision: Optional[int] = None,
    ) -> None:
        """A guarded UPDATE matched no row although the snapshot passed: the
        lease lapsed between the two clock reads. Re-read and name the exact
        reason; if none applies, fail closed as unclassified."""
        snap = self._lock(conn, tenant_id, ns, conversation_id)
        self._require(snap, token, expected_revision=expected_revision)
        raise c.OwnershipRejected(c.RejectReason.UNCLASSIFIED, snap)

    @staticmethod
    def _guarded_update(conversation_id: int, tenant_id: int, ns: str, token: c.OwnershipToken):
        return update(CONV).where(
            CONV.c.id == conversation_id, CONV.c.tenant_id == tenant_id, CONV.c.namespace == ns,
            CONV.c.lease_owner == token.owner_id, CONV.c.lease_fence == token.fence,
            CONV.c.ownership_epoch == token.epoch, CONV.c.lease_expires_at > func.clock_timestamp(),
        )

    def _apply_state(
        self, conn: Connection, conversation_id: int, tenant_id: int, ns: str, token: c.OwnershipToken,
        expected_revision: int, body: Dict[str, Any],
    ) -> Optional[Row]:
        return conn.execute(
            self._guarded_update(conversation_id, tenant_id, ns, token)
            .where(CONV.c.state_revision == expected_revision)
            .values(
                state_revision=CONV.c.state_revision + 1, state_payload=body,
                state_committed_fence=token.fence, state_committed_epoch=token.epoch,
                updated_at=func.clock_timestamp(),
            )
            .returning(CONV.c.state_revision, CONV.c.updated_at)
        ).one_or_none()


__all__ = ["CommerceRuntimeRepository"]
