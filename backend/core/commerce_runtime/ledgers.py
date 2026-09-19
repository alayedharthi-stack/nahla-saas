"""Short-transaction repository of the dormant effect and delivery ledgers.

Transaction behaviour (bounded, stated exactly)
===============================================
Every public write operation runs **one write transaction**, opened and
committed or rolled back before the operation returns; reads run one
read-only transaction. ``finalize_turn`` may open one further read-only
transaction after a terminal primary-key race, exactly like the foundation's
``record_terminal``. No operation calls a model, a provider or the network,
so no transaction is ever held across an external call: a dispatch is
*reserved* in one transaction, performed by the caller outside any
transaction, and its outcome is *recorded* in another.

Authority and serialisation
===========================
Every write locks the conversation row first (the foundation's ``_lock``),
so the ledgers of one conversation change one operation at a time. Intent
and dispatch reservations require a valid scoped ownership token, re-judged
on the database clock inside the transaction, and the originating turn must
be the conversation's eligible turn; dispatch re-checks authorization at
dispatch time, so authority at intent time alone never dispatches anything.
Recording an outcome for an **existing** attempt is deliberately different:
it is scope-bound (tenant, namespace, conversation, attempt) and needs no
lease, so late evidence from a worker whose ownership ended still attaches
to its own attempt, while that worker can create no new effect or attempt.

No blind redispatch
===================
An attempt whose completion is not established, or whose outcome is
unknown, blocks every further dispatch of its effect for every owner,
including a new owner after lease expiry or takeover. A confirmed effect is
reused, never re-executed; a definitive rejection closes the effect. For a
delivery sequence, only a proven rejection of a rich attempt permits one
text recovery attempt; an accepted or unknown send permits nothing.

PostgreSQL only (row locks, ``clock_timestamp()``, ``ON CONFLICT``, JSONB).
"""
from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine, Row
from sqlalchemy.exc import IntegrityError

from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime.ledger_models import (
    RuntimeDeliveryAttempt,
    RuntimeDeliveryReceipt,
    RuntimeDeliverySequence,
    RuntimeEffect,
    RuntimeEffectAttempt,
    RuntimeEffectResult,
)
from core.commerce_runtime.repositories import CommerceRuntimeRepository

EFF = RuntimeEffect.__table__
EATT = RuntimeEffectAttempt.__table__
ERES = RuntimeEffectResult.__table__
DSEQ = RuntimeDeliverySequence.__table__
DATT = RuntimeDeliveryAttempt.__table__
DRCP = RuntimeDeliveryReceipt.__table__


# ── Row → record ─────────────────────────────────────────────────────────────


def _effect(row: Row) -> lc.EffectRecord:
    m = row._mapping
    return lc.EffectRecord(
        effect_id=int(m["id"]), tenant_id=int(m["tenant_id"]), namespace=str(m["namespace"]),
        conversation_id=int(m["conversation_id"]), turn_id=int(m["turn_id"]), action_type=str(m["action_type"]),
        idempotency_key=str(m["idempotency_key"]), payload=dict(m["payload"] or {}), payload_hash=str(m["payload_hash"]),
        status=str(m["status"]), attempt_count=int(m["attempt_count"]), reserved_by=str(m["reserved_by"]),
        reserved_fence=int(m["reserved_fence"]), reserved_epoch=int(m["reserved_epoch"]),
        confirmed_result=(dict(m["confirmed_result"]) if m["confirmed_result"] is not None else None),
        created_at=m["created_at"], updated_at=m["updated_at"],
    )


def _effect_attempt(row: Row) -> lc.EffectAttemptRecord:
    m = row._mapping
    return lc.EffectAttemptRecord(
        attempt_id=int(m["id"]), effect_id=int(m["effect_id"]), attempt_no=int(m["attempt_no"]),
        dispatch_key=str(m["dispatch_key"]), reserved_by=str(m["reserved_by"]), reserved_fence=int(m["reserved_fence"]),
        reserved_epoch=int(m["reserved_epoch"]), reserved_at=m["reserved_at"],
    )


def _effect_result(row: Row) -> lc.EffectResultRecord:
    m = row._mapping
    return lc.EffectResultRecord(
        result_id=int(m["id"]), attempt_id=int(m["attempt_id"]), effect_id=int(m["effect_id"]),
        result_no=int(m["result_no"]), outcome=str(m["outcome"]), evidence=dict(m["evidence"] or {}),
        recorded_by=str(m["recorded_by"]), recorded_at=m["recorded_at"],
    )


def _sequence(row: Row) -> lc.DeliverySequenceRecord:
    m = row._mapping
    return lc.DeliverySequenceRecord(
        sequence_id=int(m["id"]), tenant_id=int(m["tenant_id"]), namespace=str(m["namespace"]),
        conversation_id=int(m["conversation_id"]), turn_id=int(m["turn_id"]), intent_kind=str(m["intent_kind"]),
        intent_payload=dict(m["intent_payload"] or {}), intent_hash=str(m["intent_hash"]),
        attempt_count=int(m["attempt_count"]), outcome=str(m["outcome"]), reserved_by=str(m["reserved_by"]),
        reserved_fence=int(m["reserved_fence"]), reserved_epoch=int(m["reserved_epoch"]),
        created_at=m["created_at"], updated_at=m["updated_at"],
    )


def _delivery_attempt(row: Row) -> lc.DeliveryAttemptRecord:
    m = row._mapping
    return lc.DeliveryAttemptRecord(
        attempt_id=int(m["id"]), sequence_id=int(m["sequence_id"]), attempt_no=int(m["attempt_no"]),
        kind=str(m["kind"]), dispatch_key=str(m["dispatch_key"]), payload=dict(m["payload"] or {}),
        reserved_by=str(m["reserved_by"]), reserved_fence=int(m["reserved_fence"]),
        reserved_epoch=int(m["reserved_epoch"]), reserved_at=m["reserved_at"],
    )


def _receipt(row: Row) -> lc.DeliveryReceiptRecord:
    m = row._mapping
    return lc.DeliveryReceiptRecord(
        receipt_id=int(m["id"]), attempt_id=int(m["attempt_id"]), sequence_id=int(m["sequence_id"]),
        receipt_no=int(m["receipt_no"]), kind=str(m["kind"]), provider_message_id=m["provider_message_id"],
        evidence=dict(m["evidence"] or {}), recorded_by=str(m["recorded_by"]), recorded_at=m["recorded_at"],
    )


def _dispatch_key() -> str:
    return uuid.uuid4().hex


class LedgerRepository:
    """Tenant-scoped, namespace-scoped, conversation-scoped ledger operations."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._foundation = CommerceRuntimeRepository(engine)

    @property
    def foundation(self) -> CommerceRuntimeRepository:
        return self._foundation

    # ── Reads (one read transaction each) ────────────────────────────────────

    def get_effect(self, *, tenant_id: int, namespace: Any, conversation_id: int, effect_id: int) -> lc.EffectRecord:
        tenant_id, ns, conversation_id = self._scope(tenant_id, namespace, conversation_id)
        effect_id = c.validate_counter(effect_id, field="effect_id")
        with self._engine.begin() as conn:
            return _effect(self._effect_row(conn, tenant_id, ns, conversation_id, effect_id))

    def find_effect(self, *, tenant_id: int, namespace: Any, idempotency_key: str) -> Optional[lc.EffectRecord]:
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        key = lc.validate_idempotency_key(idempotency_key)
        with self._engine.begin() as conn:
            row = conn.execute(
                select(EFF).where(EFF.c.tenant_id == tenant_id, EFF.c.namespace == ns, EFF.c.idempotency_key == key)
            ).one_or_none()
        return _effect(row) if row is not None else None

    def list_effect_attempts(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, effect_id: int,
    ) -> List[lc.EffectAttemptRecord]:
        tenant_id, ns, conversation_id = self._scope(tenant_id, namespace, conversation_id)
        effect_id = c.validate_counter(effect_id, field="effect_id")
        with self._engine.begin() as conn:
            self._effect_row(conn, tenant_id, ns, conversation_id, effect_id)
            rows = conn.execute(
                select(EATT).where(EATT.c.effect_id == effect_id).order_by(EATT.c.attempt_no)
            ).all()
        return [_effect_attempt(r) for r in rows]

    def list_effect_results(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, effect_id: int,
    ) -> List[lc.EffectResultRecord]:
        tenant_id, ns, conversation_id = self._scope(tenant_id, namespace, conversation_id)
        effect_id = c.validate_counter(effect_id, field="effect_id")
        with self._engine.begin() as conn:
            self._effect_row(conn, tenant_id, ns, conversation_id, effect_id)
            rows = conn.execute(
                select(ERES).where(ERES.c.effect_id == effect_id).order_by(ERES.c.attempt_id, ERES.c.result_no)
            ).all()
        return [_effect_result(r) for r in rows]

    def get_delivery_sequence(
        self, *, tenant_id: int, namespace: Any, turn_id: int,
    ) -> Optional[lc.DeliverySequenceRecord]:
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        turn_id = c.validate_counter(turn_id, field="turn_id")
        with self._engine.begin() as conn:
            row = conn.execute(
                select(DSEQ).where(DSEQ.c.tenant_id == tenant_id, DSEQ.c.namespace == ns, DSEQ.c.turn_id == turn_id)
            ).one_or_none()
        return _sequence(row) if row is not None else None

    def list_delivery_attempts(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, sequence_id: int,
    ) -> List[lc.DeliveryAttemptRecord]:
        tenant_id, ns, conversation_id = self._scope(tenant_id, namespace, conversation_id)
        sequence_id = c.validate_counter(sequence_id, field="sequence_id")
        with self._engine.begin() as conn:
            self._sequence_row(conn, tenant_id, ns, conversation_id, sequence_id)
            rows = conn.execute(
                select(DATT).where(DATT.c.sequence_id == sequence_id).order_by(DATT.c.attempt_no)
            ).all()
        return [_delivery_attempt(r) for r in rows]

    def list_delivery_receipts(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, sequence_id: int,
    ) -> List[lc.DeliveryReceiptRecord]:
        tenant_id, ns, conversation_id = self._scope(tenant_id, namespace, conversation_id)
        sequence_id = c.validate_counter(sequence_id, field="sequence_id")
        with self._engine.begin() as conn:
            self._sequence_row(conn, tenant_id, ns, conversation_id, sequence_id)
            rows = conn.execute(
                select(DRCP).where(DRCP.c.sequence_id == sequence_id).order_by(DRCP.c.attempt_id, DRCP.c.receipt_no)
            ).all()
        return [_receipt(r) for r in rows]

    def turn_ledger_summary(self, *, tenant_id: int, namespace: Any, turn_id: int) -> lc.TurnLedgerSummary:
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        turn_id = c.validate_counter(turn_id, field="turn_id")
        with self._engine.begin() as conn:
            return self._summary_in(conn, tenant_id, ns, turn_id)

    # ── Intents (token-bearing: valid lease, eligible turn) ──────────────────

    def reserve_effect(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, turn_id: int,
        intent: lc.EffectIntent,
    ) -> lc.EffectReservation:
        """Reserve one business action before any attempt to execute it.

        The action is bound to tenant, namespace, conversation, originating
        turn, action type and business idempotency key. Reserving the same
        action and payload again returns the existing record (``created``
        False), whatever its status; the same key with a different action,
        payload or conversation is an explicit ``EffectConflict``.
        """
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        turn_id = c.validate_counter(turn_id, field="turn_id")
        intent = lc.validate_effect_intent(intent)
        with self._engine.begin() as conn:
            snap = self._foundation._lock(conn, tenant_id, ns, conversation_id)
            self._foundation._require(snap, token)
            self._require_eligible(snap, turn_id)
            self._touch_under_guard(conn, snap, token)
            return self._reserve_effect_in(conn, snap, token, turn_id, intent)

    def reserve_delivery(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, turn_id: int,
        intent: lc.DeliveryIntent,
    ) -> lc.DeliverySequenceRecord:
        """Reserve the single logical delivery sequence of the eligible turn."""
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        turn_id = c.validate_counter(turn_id, field="turn_id")
        intent = lc.validate_delivery_intent(intent)
        with self._engine.begin() as conn:
            snap = self._foundation._lock(conn, tenant_id, ns, conversation_id)
            self._foundation._require(snap, token)
            self._require_eligible(snap, turn_id)
            self._touch_under_guard(conn, snap, token)
            return self._reserve_delivery_in(conn, snap, token, turn_id, intent)

    def commit_turn_decision(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, turn_id: int,
        state_transition: Optional[c.StateTransition] = None,
        effect_intents: Sequence[lc.EffectIntent] = (), delivery_intent: Optional[lc.DeliveryIntent] = None,
        _fault_before_commit: Optional[Callable[[], None]] = None,
    ) -> lc.TurnDecision:
        """Persist a validated state transition together with the effect and
        delivery intents it decided, in one transaction, for the eligible turn.

        Either everything is committed or nothing is: a crash before commit
        leaves no state change, no effect and no delivery sequence. The
        terminal is *not* part of this operation; ``finalize_turn`` records it
        once every dispatched attempt has an established outcome.
        ``_fault_before_commit`` is a test-only fault-injection point.
        """
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        turn_id = c.validate_counter(turn_id, field="turn_id")
        intents = [lc.validate_effect_intent(i) for i in effect_intents]
        keys = [i.idempotency_key for i in intents]
        if len(set(keys)) != len(keys):
            raise c.ValidationError("a decision cannot reserve the same idempotency key twice")
        delivery = lc.validate_delivery_intent(delivery_intent) if delivery_intent is not None else None
        expected_revision: Optional[int] = None
        state_body: Optional[Dict[str, Any]] = None
        if state_transition is not None:
            if not isinstance(state_transition, c.StateTransition):
                raise c.ValidationError("state_transition must be a StateTransition")
            expected_revision = c.validate_counter(state_transition.expected_revision, field="expected_revision")
            state_body = c.validate_payload(state_transition.payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
        with self._engine.begin() as conn:
            snap = self._foundation._lock(conn, tenant_id, ns, conversation_id)
            self._foundation._require(snap, token, expected_revision=expected_revision)
            self._require_eligible(snap, turn_id)
            state_commit: Optional[c.StateCommit] = None
            if state_body is not None:
                applied = self._foundation._apply_state(conn, conversation_id, tenant_id, ns, token,
                                                        expected_revision, state_body)
                if applied is None:
                    self._foundation._reject_after_failed_guard(conn, tenant_id, ns, conversation_id, token,
                                                                expected_revision=expected_revision)
                state_commit = c.StateCommit(
                    conversation_id=conversation_id, revision=int(applied.state_revision), fence=token.fence,
                    epoch=token.epoch, committed_at=applied.updated_at,
                )
            else:
                self._touch_under_guard(conn, snap, token)
            effects = tuple(self._reserve_effect_in(conn, snap, token, turn_id, intent) for intent in intents)
            sequence = self._reserve_delivery_in(conn, snap, token, turn_id, delivery) if delivery is not None else None
            if _fault_before_commit is not None:
                _fault_before_commit()
            return lc.TurnDecision(
                conversation_id=conversation_id, turn_id=turn_id, state=state_commit, effects=effects, delivery=sequence,
            )

    # ── Dispatch reservations (token-bearing: authorization re-checked) ──────

    def reserve_effect_dispatch(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, effect_id: int,
    ) -> lc.EffectAttemptRecord:
        """Reserve the one durable dispatch attempt of a reserved effect.

        Requires a currently valid lease and the effect's originating turn
        to be the eligible turn. Refused, with an exact reason, while an
        attempt's completion is not established, when the outcome is
        unknown, when the effect is confirmed (reuse the result) or rejected.
        The caller performs the external call *after* this transaction
        committed and records its outcome with ``record_effect_result``.
        """
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        effect_id = c.validate_counter(effect_id, field="effect_id")
        with self._engine.begin() as conn:
            snap = self._foundation._lock(conn, tenant_id, ns, conversation_id)
            self._foundation._require(snap, token)
            effect = _effect(self._effect_row(conn, tenant_id, ns, conversation_id, effect_id, for_update=True))
            self._require_eligible(snap, effect.turn_id)
            status = lc.EffectStatus(effect.status)
            if status is lc.EffectStatus.DISPATCHING:
                raise lc.DispatchBlocked(lc.DispatchBlock.ATTEMPT_PENDING, effect,
                                         open_attempt=self._latest_effect_attempt(conn, effect_id))
            if status is lc.EffectStatus.UNKNOWN:
                raise lc.DispatchBlocked(lc.DispatchBlock.OUTCOME_UNKNOWN, effect,
                                         open_attempt=self._latest_effect_attempt(conn, effect_id))
            if status is lc.EffectStatus.CONFIRMED:
                raise lc.DispatchBlocked(lc.DispatchBlock.ALREADY_CONFIRMED, effect)
            if status is lc.EffectStatus.REJECTED:
                raise lc.DispatchBlocked(lc.DispatchBlock.REJECTED_FINAL, effect)
            if effect.attempt_count >= lc.MAX_EFFECT_ATTEMPTS:
                raise lc.DispatchBlocked(lc.DispatchBlock.ATTEMPTS_EXHAUSTED, effect)
            self._touch_under_guard(conn, snap, token)
            attempt_no = effect.attempt_count + 1
            row = conn.execute(
                insert(EATT).values(
                    effect_id=effect_id, tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id,
                    attempt_no=attempt_no, dispatch_key=_dispatch_key(), reserved_by=token.owner_id,
                    reserved_fence=token.fence, reserved_epoch=token.epoch,
                ).returning(EATT)
            ).one()
            conn.execute(
                update(EFF).where(EFF.c.id == effect_id)
                .values(status=lc.EffectStatus.DISPATCHING.value, attempt_count=attempt_no,
                        updated_at=func.clock_timestamp())
            )
            return _effect_attempt(row)

    def reserve_delivery_dispatch(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, sequence_id: int,
    ) -> lc.DeliveryAttemptRecord:
        """Reserve the first dispatch attempt of a delivery sequence (its intent)."""
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        sequence_id = c.validate_counter(sequence_id, field="sequence_id")
        with self._engine.begin() as conn:
            snap = self._foundation._lock(conn, tenant_id, ns, conversation_id)
            self._foundation._require(snap, token)
            seq = _sequence(self._sequence_row(conn, tenant_id, ns, conversation_id, sequence_id, for_update=True))
            self._require_eligible(snap, seq.turn_id)
            if seq.attempt_count > 0:
                outcome = lc.DeliveryOutcome(seq.outcome)
                reason = {
                    lc.DeliveryOutcome.PENDING: lc.DispatchBlock.ATTEMPT_PENDING,
                    lc.DeliveryOutcome.ACCEPTED: lc.DispatchBlock.ALREADY_CONFIRMED,
                    lc.DeliveryOutcome.UNKNOWN: lc.DispatchBlock.OUTCOME_UNKNOWN,
                    lc.DeliveryOutcome.REJECTED: lc.DispatchBlock.RECOVERY_ONLY,
                }[outcome]
                raise lc.DeliveryDispatchBlocked(reason, seq)
            self._touch_under_guard(conn, snap, token)
            return self._insert_delivery_attempt(conn, seq, token, attempt_no=1, kind=seq.intent_kind,
                                                 payload=seq.intent_payload)

    def reserve_delivery_recovery(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, token: c.OwnershipToken, sequence_id: int,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> lc.DeliveryAttemptRecord:
        """Reserve the one bounded rich-to-text recovery attempt.

        Permitted only after a *proven* definitive rejection of the first,
        rich attempt. An accepted or unknown send permits nothing; a pending
        attempt permits nothing; a text attempt has no recovery.
        """
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        sequence_id = c.validate_counter(sequence_id, field="sequence_id")
        body = c.validate_payload(payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
        with self._engine.begin() as conn:
            snap = self._foundation._lock(conn, tenant_id, ns, conversation_id)
            self._foundation._require(snap, token)
            seq = _sequence(self._sequence_row(conn, tenant_id, ns, conversation_id, sequence_id, for_update=True))
            self._require_eligible(snap, seq.turn_id)
            latest = self._latest_delivery_attempt(conn, sequence_id)
            if latest is None:
                raise lc.RecoveryNotPermitted(lc.RecoveryRefusal.NO_ATTEMPT, seq)
            outcome = lc.DeliveryOutcome(seq.outcome)
            if outcome is lc.DeliveryOutcome.PENDING:
                raise lc.RecoveryNotPermitted(lc.RecoveryRefusal.ATTEMPT_PENDING, seq)
            if outcome is lc.DeliveryOutcome.ACCEPTED:
                raise lc.RecoveryNotPermitted(lc.RecoveryRefusal.OUTCOME_ACCEPTED, seq)
            if outcome is lc.DeliveryOutcome.UNKNOWN:
                raise lc.RecoveryNotPermitted(lc.RecoveryRefusal.OUTCOME_UNKNOWN, seq)
            if seq.attempt_count >= lc.MAX_DELIVERY_ATTEMPTS:
                raise lc.RecoveryNotPermitted(lc.RecoveryRefusal.ATTEMPTS_EXHAUSTED, seq)
            if latest.kind != lc.DeliveryKind.RICH.value:
                raise lc.RecoveryNotPermitted(lc.RecoveryRefusal.NOT_RICH_TO_TEXT, seq)
            self._touch_under_guard(conn, snap, token)
            next_no = seq.attempt_count + 1
            return self._insert_delivery_attempt(conn, seq, token, attempt_no=next_no,
                                                 kind=lc.DeliveryKind.TEXT.value, payload=body)

    # ── Outcome evidence (scope-bound; no lease required) ────────────────────

    def record_effect_result(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, attempt_id: int, outcome: Any,
        evidence: Optional[Mapping[str, Any]] = None, recorded_by: str,
    ) -> lc.EffectResultRecord:
        """Append the outcome of an existing attempt and move the effect accordingly.

        The first result of an attempt establishes ``confirmed``, ``rejected``
        or ``unknown``; an ``unknown`` result may later be resolved once by
        ``confirmed`` or ``rejected`` evidence for the same attempt. A
        confirmed or rejected result is final: an identical repeat returns
        the existing row, anything else is an ``IllegalTransition``. No lease
        is required, so a worker whose ownership ended still records the
        outcome of its own attempt; it gains no authority to create effects.
        """
        tenant_id, ns, conversation_id = self._scope(tenant_id, namespace, conversation_id)
        attempt_id = c.validate_counter(attempt_id, field="attempt_id")
        outcome_value = c.validate_enum(outcome, lc.EffectOutcome, field="outcome")
        body = lc.validate_evidence(evidence)
        recorded_by = c.validate_owner_id(recorded_by)
        with self._engine.begin() as conn:
            self._foundation._lock(conn, tenant_id, ns, conversation_id)
            attempt_row = conn.execute(
                select(EATT).where(EATT.c.id == attempt_id, EATT.c.tenant_id == tenant_id, EATT.c.namespace == ns,
                                   EATT.c.conversation_id == conversation_id)
            ).one_or_none()
            if attempt_row is None:
                raise lc.AttemptNotFound(f"effect attempt not found in tenant {tenant_id}/{ns}")
            attempt = _effect_attempt(attempt_row)
            effect = _effect(self._effect_row(conn, tenant_id, ns, conversation_id, attempt.effect_id, for_update=True))
            results = [_effect_result(r) for r in conn.execute(
                select(ERES).where(ERES.c.attempt_id == attempt_id).order_by(ERES.c.result_no)
            ).all()]
            if results:
                last = results[-1]
                if last.outcome == outcome_value and last.evidence == body:
                    return last
                if last.outcome != lc.EffectOutcome.UNKNOWN.value or outcome_value == lc.EffectOutcome.UNKNOWN.value:
                    raise lc.IllegalTransition("the attempt's outcome is already established",
                                               current=last.outcome, attempted=outcome_value)
            elif effect.status != lc.EffectStatus.DISPATCHING.value:
                raise lc.IllegalTransition("the attempt is not the effect's open dispatch",
                                           current=effect.status, attempted=outcome_value)
            if attempt.attempt_no != effect.attempt_count:
                raise lc.IllegalTransition("only the latest attempt can receive a result",
                                           current=effect.status, attempted=outcome_value)
            if not lc.effect_transition_allowed(effect.status, outcome_value):
                raise lc.IllegalTransition("illegal effect transition", current=effect.status, attempted=outcome_value)
            row = conn.execute(
                insert(ERES).values(
                    attempt_id=attempt_id, effect_id=attempt.effect_id, result_no=len(results) + 1,
                    outcome=outcome_value, evidence=body, recorded_by=recorded_by,
                ).returning(ERES)
            ).one()
            values: Dict[str, Any] = {"status": outcome_value, "updated_at": func.clock_timestamp()}
            if outcome_value == lc.EffectOutcome.CONFIRMED.value:
                values["confirmed_result"] = body
            conn.execute(update(EFF).where(EFF.c.id == attempt.effect_id).values(**values))
            return _effect_result(row)

    def record_delivery_receipt(
        self, *, tenant_id: int, namespace: Any, conversation_id: int, attempt_id: int, kind: Any,
        provider_message_id: Optional[str] = None, evidence: Optional[Mapping[str, Any]] = None, recorded_by: str,
    ) -> lc.DeliveryReceiptRecord:
        """Append transport or reach evidence to an existing delivery attempt.

        ``accepted`` (provider message id required), ``rejected`` and
        ``unknown`` establish the attempt's transport outcome; ``unknown``
        may later be resolved once by ``accepted`` or ``rejected``. Reach
        evidence (``delivered``, ``read``, ``failed``) is accepted only for an
        attempt whose send was accepted and, when it names a provider message
        id, only for that id. Acceptance is never treated as reach.
        """
        tenant_id, ns, conversation_id = self._scope(tenant_id, namespace, conversation_id)
        attempt_id = c.validate_counter(attempt_id, field="attempt_id")
        kind_value = c.validate_enum(kind, lc.ReceiptKind, field="kind")
        pmid = None
        if provider_message_id is not None:
            pmid = c.validate_ref(provider_message_id, field="provider_message_id",
                                  max_length=c.MAX_PROVIDER_MESSAGE_ID_LENGTH)
        if kind_value == lc.ReceiptKind.ACCEPTED.value and pmid is None:
            raise c.ValidationError("an accepted receipt requires the provider message id")
        body = lc.validate_evidence(evidence)
        recorded_by = c.validate_owner_id(recorded_by)
        with self._engine.begin() as conn:
            self._foundation._lock(conn, tenant_id, ns, conversation_id)
            attempt_row = conn.execute(
                select(DATT).where(DATT.c.id == attempt_id, DATT.c.tenant_id == tenant_id, DATT.c.namespace == ns,
                                   DATT.c.conversation_id == conversation_id)
            ).one_or_none()
            if attempt_row is None:
                raise lc.AttemptNotFound(f"delivery attempt not found in tenant {tenant_id}/{ns}")
            attempt = _delivery_attempt(attempt_row)
            seq = _sequence(self._sequence_row(conn, tenant_id, ns, conversation_id, attempt.sequence_id,
                                               for_update=True))
            receipts = [_receipt(r) for r in conn.execute(
                select(DRCP).where(DRCP.c.attempt_id == attempt_id).order_by(DRCP.c.receipt_no)
            ).all()]
            outcome_kinds = {k.value for k in lc.OUTCOME_RECEIPTS}
            outcome_receipts = [r for r in receipts if r.kind in outcome_kinds]
            current = outcome_receipts[-1].kind if outcome_receipts else lc.DeliveryOutcome.PENDING.value
            if kind_value in outcome_kinds:
                stored_pmid = pmid
                duplicate = next((r for r in outcome_receipts if r.kind == kind_value
                                  and r.provider_message_id == stored_pmid and r.evidence == body), None)
                if duplicate is not None:
                    return duplicate
                if current == lc.DeliveryOutcome.PENDING.value:
                    pass
                elif current == lc.ReceiptKind.UNKNOWN.value and kind_value != lc.ReceiptKind.UNKNOWN.value:
                    pass   # late evidence resolves an unknown send once
                else:
                    raise lc.IllegalTransition("the attempt's transport outcome is already established",
                                               current=current, attempted=kind_value)
            else:
                if current != lc.ReceiptKind.ACCEPTED.value:
                    raise lc.IllegalTransition("reach evidence requires an accepted send",
                                               current=current, attempted=kind_value)
                stored_pmid = next(r.provider_message_id for r in reversed(outcome_receipts)
                                   if r.kind == lc.ReceiptKind.ACCEPTED.value)
                if pmid is not None and pmid != stored_pmid:
                    raise lc.IllegalTransition("the provider message id is not the accepted send's",
                                               current=current, attempted=kind_value)
                same_kind = [r for r in receipts if r.kind == kind_value]
                if same_kind and same_kind[-1].evidence == body:
                    return same_kind[-1]
                if same_kind:
                    raise lc.IllegalTransition("this reach receipt is already recorded with other evidence",
                                               current=current, attempted=kind_value)
            row = conn.execute(
                insert(DRCP).values(
                    attempt_id=attempt_id, sequence_id=attempt.sequence_id, receipt_no=len(receipts) + 1,
                    kind=kind_value, provider_message_id=stored_pmid, evidence=body, recorded_by=recorded_by,
                ).returning(DRCP)
            ).one()
            if kind_value in {k.value for k in lc.OUTCOME_RECEIPTS} and attempt.attempt_no == seq.attempt_count:
                conn.execute(
                    update(DSEQ).where(DSEQ.c.id == seq.sequence_id)
                    .values(outcome=kind_value, updated_at=func.clock_timestamp())
                )
            return _receipt(row)

    # ── Completion (token-bearing; transport and reach derived under the lock) ─

    def finalize_turn(
        self, *, tenant_id: int, namespace: Any, turn_id: int, token: c.OwnershipToken, processing_outcome: Any,
        details: Optional[Mapping[str, Any]] = None, state_transition: Optional[c.StateTransition] = None,
        _fault_before_commit: Optional[Callable[[], None]] = None,
    ) -> c.TerminalRecord:
        """Record the eligible turn's single immutable terminal with the
        transport outcome and customer reach derived from the ledgers, in the
        same transaction, atomically with an optional state transition.

        Refused (``CompletionBlocked``) while an effect or delivery intent of
        the turn is reserved but not dispatched, or while an attempt has no
        established outcome; the supported order is prepare → reserve dispatch
        → record outcome → finalize. A recorded ``unknown`` does not block:
        it is retained in the terminal's summary as unknown, never as success.
        Evidence recorded later updates the ledgers only; the terminal never
        changes.
        """
        tenant_id = c.validate_tenant_id(tenant_id)
        ns = c.validate_namespace(namespace).value
        turn_id = c.validate_counter(turn_id, field="turn_id")
        token = c.validate_token(token)
        processing = c.validate_enum(processing_outcome, c.ProcessingOutcome, field="processing_outcome")
        body = c.validate_payload(details, field="details", max_bytes=c.MAX_DETAILS_BYTES)
        if "ledger" in body:
            raise c.ValidationError("details.ledger is reserved for the derived ledger summary")
        expected_revision: Optional[int] = None
        state_body: Optional[Dict[str, Any]] = None
        if state_transition is not None:
            if not isinstance(state_transition, c.StateTransition):
                raise c.ValidationError("state_transition must be a StateTransition")
            expected_revision = c.validate_counter(state_transition.expected_revision, field="expected_revision")
            state_body = c.validate_payload(state_transition.payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)

        def resolve(conn: Connection, snap: c.ConversationSnapshot) -> Tuple[str, str, Mapping[str, Any]]:
            # The foundation's terminal path has already refused reserved-but-undispatched
            # intents and attempts without an established outcome under this lock.
            summary = self._summary_in(conn, tenant_id, ns, turn_id)
            merged = dict(body)
            merged["ledger"] = dataclasses.asdict(summary)
            return summary.transport_outcome, summary.customer_reach, merged

        try:
            with self._engine.begin() as conn:
                return self._foundation._record_terminal_in(
                    conn, tenant_id=tenant_id, ns=ns, turn_id=turn_id, token=token, processing=processing,
                    transport=None, reach=None, details=body, expected_revision=expected_revision,
                    state_body=state_body, fault_before_commit=_fault_before_commit, resolve=resolve,
                )
        except IntegrityError:
            existing = self._foundation.get_terminal(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
            if existing is None:
                raise
            raise c.TerminalAlreadyRecorded(existing) from None

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _scope(tenant_id: Any, namespace: Any, conversation_id: Any) -> Tuple[int, str, int]:
        return (c.validate_tenant_id(tenant_id), c.validate_namespace(namespace).value,
                c.validate_counter(conversation_id, field="conversation_id"))

    @staticmethod
    def _require_eligible(snap: c.ConversationSnapshot, turn_id: int) -> None:
        if snap.eligible_turn_id != turn_id:
            raise c.OwnershipRejected(c.RejectReason.TURN_NOT_ELIGIBLE, snap)

    def _touch_under_guard(self, conn: Connection, snap: c.ConversationSnapshot, token: c.OwnershipToken) -> None:
        """Re-judge the lease on the database clock inside the transaction."""
        touched = conn.execute(
            self._foundation._guarded_update(snap.conversation_id, snap.tenant_id, snap.namespace, token)
            .values(updated_at=func.clock_timestamp())
        )
        if touched.rowcount != 1:
            self._foundation._reject_after_failed_guard(conn, snap.tenant_id, snap.namespace,
                                                        snap.conversation_id, token)

    @staticmethod
    def _effect_row(conn: Connection, tenant_id: int, ns: str, conversation_id: int, effect_id: int,
                    *, for_update: bool = False) -> Row:
        stmt = select(EFF).where(EFF.c.id == effect_id, EFF.c.tenant_id == tenant_id, EFF.c.namespace == ns,
                                 EFF.c.conversation_id == conversation_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).one_or_none()
        if row is None:
            raise lc.EffectNotFound(f"effect not found in tenant {tenant_id}/{ns}")
        return row

    @staticmethod
    def _sequence_row(conn: Connection, tenant_id: int, ns: str, conversation_id: int, sequence_id: int,
                      *, for_update: bool = False) -> Row:
        stmt = select(DSEQ).where(DSEQ.c.id == sequence_id, DSEQ.c.tenant_id == tenant_id, DSEQ.c.namespace == ns,
                                  DSEQ.c.conversation_id == conversation_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).one_or_none()
        if row is None:
            raise lc.SequenceNotFound(f"delivery sequence not found in tenant {tenant_id}/{ns}")
        return row

    @staticmethod
    def _latest_effect_attempt(conn: Connection, effect_id: int) -> Optional[lc.EffectAttemptRecord]:
        row = conn.execute(
            select(EATT).where(EATT.c.effect_id == effect_id).order_by(EATT.c.attempt_no.desc()).limit(1)
        ).one_or_none()
        return _effect_attempt(row) if row is not None else None

    @staticmethod
    def _latest_delivery_attempt(conn: Connection, sequence_id: int) -> Optional[lc.DeliveryAttemptRecord]:
        row = conn.execute(
            select(DATT).where(DATT.c.sequence_id == sequence_id).order_by(DATT.c.attempt_no.desc()).limit(1)
        ).one_or_none()
        return _delivery_attempt(row) if row is not None else None

    def _reserve_effect_in(
        self, conn: Connection, snap: c.ConversationSnapshot, token: c.OwnershipToken, turn_id: int,
        intent: lc.EffectIntent,
    ) -> lc.EffectReservation:
        digest = lc.payload_hash(intent.payload)
        inserted = conn.execute(
            pg_insert(EFF).values(
                tenant_id=snap.tenant_id, namespace=snap.namespace, conversation_id=snap.conversation_id,
                turn_id=turn_id, action_type=intent.action_type, idempotency_key=intent.idempotency_key,
                payload=dict(intent.payload), payload_hash=digest, status=lc.EffectStatus.RESERVED.value,
                attempt_count=0, reserved_by=token.owner_id, reserved_fence=token.fence, reserved_epoch=token.epoch,
            ).on_conflict_do_nothing(constraint="uq_commerce_runtime_effects_key").returning(EFF.c.id)
        ).scalar_one_or_none()
        row = conn.execute(
            select(EFF).where(EFF.c.tenant_id == snap.tenant_id, EFF.c.namespace == snap.namespace,
                              EFF.c.idempotency_key == intent.idempotency_key)
        ).one()
        record = _effect(row)
        if inserted is not None:
            return lc.EffectReservation(effect=record, created=True)
        if record.conversation_id != snap.conversation_id:
            raise lc.EffectConflict("conversation", record)
        if record.action_type != intent.action_type:
            raise lc.EffectConflict("action_type", record)
        if record.payload_hash != digest:
            raise lc.EffectConflict("payload", record)
        return lc.EffectReservation(effect=record, created=False)

    def _reserve_delivery_in(
        self, conn: Connection, snap: c.ConversationSnapshot, token: c.OwnershipToken, turn_id: int,
        intent: lc.DeliveryIntent,
    ) -> lc.DeliverySequenceRecord:
        digest = lc.payload_hash(intent.payload)
        inserted = conn.execute(
            pg_insert(DSEQ).values(
                tenant_id=snap.tenant_id, namespace=snap.namespace, conversation_id=snap.conversation_id,
                turn_id=turn_id, intent_kind=intent.kind, intent_payload=dict(intent.payload), intent_hash=digest,
                attempt_count=0, outcome=lc.DeliveryOutcome.PENDING.value, reserved_by=token.owner_id,
                reserved_fence=token.fence, reserved_epoch=token.epoch,
            ).on_conflict_do_nothing(constraint="uq_commerce_runtime_delivery_sequences_turn").returning(DSEQ.c.id)
        ).scalar_one_or_none()
        row = conn.execute(select(DSEQ).where(DSEQ.c.turn_id == turn_id)).one()
        record = _sequence(row)
        if inserted is not None:
            return record
        if record.intent_kind != intent.kind:
            raise lc.DeliveryConflict("kind", record)
        if record.intent_hash != digest:
            raise lc.DeliveryConflict("payload", record)
        return record

    @staticmethod
    def _insert_delivery_attempt(
        conn: Connection, seq: lc.DeliverySequenceRecord, token: c.OwnershipToken, *, attempt_no: int, kind: str,
        payload: Mapping[str, Any],
    ) -> lc.DeliveryAttemptRecord:
        row = conn.execute(
            insert(DATT).values(
                sequence_id=seq.sequence_id, tenant_id=seq.tenant_id, namespace=seq.namespace,
                conversation_id=seq.conversation_id, attempt_no=attempt_no, kind=kind, dispatch_key=_dispatch_key(),
                payload=dict(payload), reserved_by=token.owner_id, reserved_fence=token.fence,
                reserved_epoch=token.epoch,
            ).returning(DATT)
        ).one()
        conn.execute(
            update(DSEQ).where(DSEQ.c.id == seq.sequence_id)
            .values(attempt_count=attempt_no, outcome=lc.DeliveryOutcome.PENDING.value,
                    updated_at=func.clock_timestamp())
        )
        return _delivery_attempt(row)

    def _summary_in(self, conn: Connection, tenant_id: int, ns: str, turn_id: int) -> lc.TurnLedgerSummary:
        effects = [_effect(r) for r in conn.execute(
            select(EFF).where(EFF.c.tenant_id == tenant_id, EFF.c.namespace == ns, EFF.c.turn_id == turn_id)
        ).all()]
        by_status: Dict[str, int] = {status.value: 0 for status in lc.EffectStatus}
        for effect in effects:
            by_status[effect.status] += 1
        seq_row = conn.execute(
            select(DSEQ).where(DSEQ.c.tenant_id == tenant_id, DSEQ.c.namespace == ns, DSEQ.c.turn_id == turn_id)
        ).one_or_none()
        if seq_row is None:
            return lc.TurnLedgerSummary(
                turn_id=turn_id, effects_by_status=by_status,
                pending_effect_attempts=by_status[lc.EffectStatus.DISPATCHING.value],
                delivery_outcome=c.TransportOutcome.NOT_ATTEMPTED.value, delivery_attempt_count=0,
                delivery_pending=False, transport_outcome=c.TransportOutcome.NOT_ATTEMPTED.value,
                customer_reach=c.CustomerReach.NOT_APPLICABLE.value,
            )
        seq = _sequence(seq_row)
        if seq.attempt_count == 0:
            # Reserved, never dispatched: nothing reached the customer and nothing is pending externally.
            return lc.TurnLedgerSummary(
                turn_id=turn_id, effects_by_status=by_status,
                pending_effect_attempts=by_status[lc.EffectStatus.DISPATCHING.value],
                delivery_outcome=lc.DeliveryOutcome.PENDING.value, delivery_attempt_count=0, delivery_pending=False,
                transport_outcome=c.TransportOutcome.NOT_ATTEMPTED.value,
                customer_reach=c.CustomerReach.NOT_REACHED.value,
            )
        latest = self._latest_delivery_attempt(conn, seq.sequence_id)
        assert latest is not None
        reach_kinds = [str(r[0]) for r in conn.execute(
            select(DRCP.c.kind).where(DRCP.c.attempt_id == latest.attempt_id)
        ).all() if str(r[0]) in {k.value for k in lc.REACH_RECEIPTS}]
        pending = seq.outcome == lc.DeliveryOutcome.PENDING.value
        return lc.TurnLedgerSummary(
            turn_id=turn_id, effects_by_status=by_status,
            pending_effect_attempts=by_status[lc.EffectStatus.DISPATCHING.value],
            delivery_outcome=seq.outcome, delivery_attempt_count=seq.attempt_count, delivery_pending=pending,
            transport_outcome=lc.transport_outcome_for(seq.outcome),
            customer_reach=lc.customer_reach_for(seq.outcome, reach_kinds),
        )


__all__ = ["LedgerRepository"]
