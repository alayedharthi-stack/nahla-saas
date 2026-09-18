"""Short-transaction repository of the dormant commerce runtime foundation.

Every public method opens exactly one database transaction and closes it
before returning; none of them calls a model, a provider or the network, and
no transaction is ever held across such a call. Every conflict is explicit:
an update that cannot be applied raises with the exact reason and the row as
the database saw it, and nothing is discarded or replayed on the caller's
behalf.

PostgreSQL only (row locks, ``now()``, ``ON CONFLICT``, JSONB).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine, Row
from sqlalchemy.exc import IntegrityError

from core.commerce_runtime import contracts as c
from core.commerce_runtime.models import RuntimeConversation, RuntimeTurn, RuntimeTurnTerminal

CONV = RuntimeConversation.__table__
TURN = RuntimeTurn.__table__
TERM = RuntimeTurnTerminal.__table__


def _db_now(conn: Connection) -> _dt.datetime:
    return conn.execute(select(func.now())).scalar_one()


def _snapshot(row: Row, db_now: _dt.datetime) -> c.ConversationSnapshot:
    m = row._mapping
    return c.ConversationSnapshot(
        conversation_id=int(m["id"]), tenant_id=int(m["tenant_id"]), namespace=str(m["namespace"]),
        conversation_ref=str(m["conversation_ref"]), next_sequence=int(m["next_sequence"]),
        ownership_epoch=int(m["ownership_epoch"]), lease_owner=m["lease_owner"],
        lease_fence=int(m["lease_fence"]), lease_expires_at=m["lease_expires_at"],
        state_revision=int(m["state_revision"]), state_payload=dict(m["state_payload"] or {}),
        db_now=db_now,
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
        presented for a different conversation is an explicit conflict.
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
                    .values(next_sequence=CONV.c.next_sequence + 1, updated_at=func.now())
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
            # Only reachable when two different conversation rows raced on the
            # same inbound identity: the losing transaction rolled back, so no
            # sequence was consumed. Resolve against the committed truth.
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
    ) -> c.Lease:
        """Take exclusive ownership when no unexpired lease exists.

        Each successful claim issues ``lease_fence + 1``; fences are never
        reset. Taking over a lease that expired without being released also
        advances the ownership epoch, so work issued under the old lease can
        never commit.
        """
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        owner_id = c.validate_owner_id(owner_id)
        lease_seconds = c.validate_lease_seconds(lease_seconds)
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            held = snap.lease_owner is not None and snap.lease_expires_at is not None \
                and snap.lease_expires_at > snap.db_now
            if held:
                raise c.OwnershipRejected(c.RejectReason.LEASE_HELD, snap)
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
                    lease_expires_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds),
                    updated_at=func.now(),
                )
                .returning(CONV.c.lease_fence, CONV.c.ownership_epoch, CONV.c.lease_expires_at)
            ).one_or_none()
            if row is None:
                raise c.OwnershipRejected(c.RejectReason.UNCLASSIFIED, snap)
            return c.Lease(
                conversation_id=conversation_id, tenant_id=tenant_id, namespace=ns, owner_id=owner_id,
                fence=int(row.lease_fence), epoch=int(row.ownership_epoch), expires_at=row.lease_expires_at,
                db_now=snap.db_now, takeover=takeover,
            )

    def renew(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, lease_seconds: int,
    ) -> c.Lease:
        """Extend an unexpired lease held with exactly this token."""
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        token = c.validate_token(token)
        lease_seconds = c.validate_lease_seconds(lease_seconds)
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            self._require(snap, token)
            row = conn.execute(
                self._guarded_update(conversation_id, tenant_id, ns, token)
                .values(
                    lease_expires_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds),
                    updated_at=func.now(),
                )
                .returning(CONV.c.lease_expires_at)
            ).one_or_none()
            if row is None:
                raise c.OwnershipRejected(c.RejectReason.UNCLASSIFIED, snap)
            return c.Lease(
                conversation_id=conversation_id, tenant_id=tenant_id, namespace=ns, owner_id=token.owner_id,
                fence=token.fence, epoch=token.epoch, expires_at=row.lease_expires_at, db_now=snap.db_now,
                takeover=False,
            )

    def release(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken,
    ) -> c.ConversationSnapshot:
        """Give up an unexpired lease. The fence stays where it is."""
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        token = c.validate_token(token)
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            self._require(snap, token)
            result = conn.execute(
                self._guarded_update(conversation_id, tenant_id, ns, token)
                .values(lease_owner=None, lease_expires_at=None, updated_at=func.now())
            )
            if result.rowcount != 1:
                raise c.OwnershipRejected(c.RejectReason.UNCLASSIFIED, snap)
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
                        updated_at=func.now())
            )
            return self._load(conn, tenant_id, ns, conversation_id=conversation_id)

    # ── State ────────────────────────────────────────────────────────────────

    def commit_state(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken,
        expected_revision: int, payload: Mapping[str, Any],
    ) -> c.StateCommit:
        """Compare-and-set the state: revision ``expected`` becomes ``expected + 1``."""
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        conversation_id = c.validate_counter(conversation_id, field="conversation_id")
        token = c.validate_token(token)
        expected_revision = c.validate_counter(expected_revision, field="expected_revision")
        body = c.validate_payload(payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
        with self._engine.begin() as conn:
            snap = self._lock(conn, tenant_id, ns, conversation_id)
            self._require(snap, token, expected_revision=expected_revision)
            row = self._apply_state(conn, conversation_id, tenant_id, ns, token, expected_revision, body)
            if row is None:
                raise c.OwnershipRejected(c.RejectReason.UNCLASSIFIED, snap)
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
        """Record the single immutable terminal of a turn, atomically with an
        optional state transition, under a valid ownership token.

        Processing outcome, transport outcome and customer reach are recorded
        as three separate facts; ``transport_outcome='unknown'`` is stored as
        unknown and grants no replay. ``_fault_before_commit`` is a test-only
        fault-injection point that runs after every statement and before the
        commit; production callers never pass it.
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
                turn = conn.execute(
                    select(TURN).where(TURN.c.id == turn_id, TURN.c.tenant_id == tenant_id, TURN.c.namespace == ns)
                ).one_or_none()
                if turn is None:
                    raise c.TurnNotFound(f"turn not found in tenant {tenant_id}/{ns}")
                conversation_id = int(turn._mapping["conversation_id"])
                snap = self._lock(conn, tenant_id, ns, conversation_id)
                self._require(snap, token, expected_revision=expected_revision)
                existing = conn.execute(select(TERM).where(TERM.c.turn_id == turn_id)).one_or_none()
                if existing is not None:
                    raise c.TerminalAlreadyRecorded(_terminal(existing))
                if state_body is not None:
                    applied = self._apply_state(conn, conversation_id, tenant_id, ns, token,
                                                expected_revision, state_body)
                    if applied is None:
                        raise c.OwnershipRejected(c.RejectReason.UNCLASSIFIED, snap)
                row = conn.execute(
                    insert(TERM).values(
                        turn_id=turn_id, tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id,
                        processing_outcome=processing, transport_outcome=transport, customer_reach=reach,
                        recorded_fence=token.fence, recorded_epoch=token.epoch, recorded_by=token.owner_id,
                        details=body,
                    ).returning(TERM)
                ).one()
                if _fault_before_commit is not None:
                    _fault_before_commit()
                return _terminal(row)
        except IntegrityError:
            # Two completion attempts raced past the existence check: the
            # primary key kept exactly one, this transaction rolled back whole.
            existing = self.get_terminal(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
            if existing is None:
                raise
            raise c.TerminalAlreadyRecorded(existing) from None

    # ── Internals ────────────────────────────────────────────────────────────

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
        return _snapshot(row, _db_now(conn))

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

    @staticmethod
    def _guarded_update(conversation_id: int, tenant_id: int, ns: str, token: c.OwnershipToken):
        return update(CONV).where(
            CONV.c.id == conversation_id, CONV.c.tenant_id == tenant_id, CONV.c.namespace == ns,
            CONV.c.lease_owner == token.owner_id, CONV.c.lease_fence == token.fence,
            CONV.c.ownership_epoch == token.epoch, CONV.c.lease_expires_at > func.now(),
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
                state_committed_fence=token.fence, state_committed_epoch=token.epoch, updated_at=func.now(),
            )
            .returning(CONV.c.state_revision, CONV.c.updated_at)
        ).one_or_none()


__all__ = ["CommerceRuntimeRepository"]
