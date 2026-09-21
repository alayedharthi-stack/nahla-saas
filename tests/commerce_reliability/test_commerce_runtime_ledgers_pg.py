"""Dormant effect and delivery ledgers, proven on real PostgreSQL.

Every test runs against a disposable database created for this module and
migrated with the repository's own Alembic chain to revision ``0109``;
concurrency and crash cases use independent spawned processes with their
own connections. Every external outcome is scripted: no model, commerce,
payment or messaging provider is called. Requires
``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips and that skip is reported, never counted.

These tests prove the *ledgers* only. They register no upstream
idempotency guarantee, no reconciliation source and no exactly-once
external execution: local uniqueness proves one reserved dispatch, never
one provider execution.
"""
from __future__ import annotations

import contextlib
import dataclasses
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import ledger_models as lm
from core.commerce_runtime.ledgers import LedgerRepository
from core.commerce_runtime.repositories import CommerceRuntimeRepository
from tests.commerce_reliability import commerce_runtime_ledger_workers as w
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    CHANNEL,
    LIVE,
    OTHER_HEAD,
    SHADOW,
    _alembic,
    _create_database,
    _current_revisions,
    _drop_database,
    _pmid,
    _ref,
    _run_crash_worker,
    _run_workers,
    _script_heads,
    _seed_tenant,
    _wait_past,
)

FOUNDATION_REVISION = "0108"
THIS_REVISION = "0109"
TABLES = tuple(t.name for t in lm.LEDGER_TABLES)
WORKER_A, WORKER_B = "worker-a", "worker-b"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ledger_trigger_relations(engine) -> List[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = :n AND NOT t.tgisinternal ORDER BY c.relname"), {"n": lm.LEDGER_IMMUTABLE_TRIGGER},
        ).all()
    return [str(r[0]) for r in rows]


def _ledger_function_present(engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(
            text("SELECT 1 FROM pg_proc WHERE proname = :n"), {"n": lm.LEDGER_IMMUTABLE_FUNCTION},
        ).scalar())


def _schema_dump(engine) -> Dict[str, Any]:
    insp = inspect(engine)
    out: Dict[str, Any] = {}
    for table in TABLES:
        out[table] = {
            "columns": {col["name"]: (str(col["type"]), bool(col["nullable"]), col["default"] is not None)
                        for col in insp.get_columns(table)},
            "pk": tuple(insp.get_pk_constraint(table)["constrained_columns"]),
            "uniques": {u["name"]: tuple(u["column_names"]) for u in insp.get_unique_constraints(table)},
            "fks": {f["name"]: (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                    for f in insp.get_foreign_keys(table)},
            "checks": {k["name"] for k in insp.get_check_constraints(table)},
            "indexes": {i["name"]: (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)},
        }
    out["trigger_relations"] = _ledger_trigger_relations(engine)
    out["function"] = _ledger_function_present(engine)
    return out


def _key() -> str:
    return "order_cancel:t:" + uuid.uuid4().hex[:10]


class ScriptedTransport:
    """WhatsApp-shaped transport whose every outcome is scripted; nothing leaves the process."""

    def __init__(self, *outcomes: Tuple[Any, ...]) -> None:
        self._outcomes = list(outcomes)
        self.calls: List[str] = []

    def send(self, attempt: lc.DeliveryAttemptRecord) -> lc.SendResponse:
        self.calls.append(attempt.dispatch_key)
        kind, *rest = self._outcomes.pop(0)
        if kind == "accepted":
            return lc.SendResponse(200, {"messages": [{"id": rest[0]}]})
        if kind == "no_id":
            return lc.SendResponse(200, {"messages": []})
        if kind == "timeout":
            return lc.SendResponse(None, {}, timed_out=True)
        if kind == "rejected":
            return lc.SendResponse(400, {"error": {"code": 131026}})
        if kind == "server_error":
            return lc.SendResponse(500, {})
        raise AssertionError(kind)


@dataclasses.dataclass
class Ledgers:
    name: str
    dsn: str
    engine: Any
    repo: LedgerRepository
    tenant_a: int
    tenant_b: int

    # foundation shortcuts
    def admit(self, ref: Optional[str] = None, *, tenant: Optional[int] = None, namespace: Any = LIVE) -> c.AdmittedTurn:
        return self.repo.foundation.admit_turn(
            tenant_id=tenant or self.tenant_a, namespace=namespace, conversation_ref=ref or _ref(),
            channel_connection_ref=CHANNEL, provider_message_id=_pmid(), payload={"kind": "text"},
        )

    def claim(self, conversation_id: int, owner: str, *, seconds: int = 60, tenant: Optional[int] = None,
              namespace: Any = LIVE) -> c.Lease:
        return self.repo.foundation.claim(tenant_id=tenant or self.tenant_a, namespace=namespace,
                                          conversation_id=conversation_id, owner_id=owner, lease_seconds=seconds)

    def start(self, owner: str = WORKER_A, *, seconds: int = 60, tenant: Optional[int] = None,
              namespace: Any = LIVE) -> Tuple[c.AdmittedTurn, c.Lease]:
        turn = self.admit(tenant=tenant, namespace=namespace)
        return turn, self.claim(turn.conversation_id, owner, seconds=seconds, tenant=tenant, namespace=namespace)

    def snapshot(self, conversation_id: int, *, tenant: Optional[int] = None, namespace: Any = LIVE):
        return self.repo.foundation.get_conversation(tenant_id=tenant or self.tenant_a, namespace=namespace,
                                                     conversation_id=conversation_id)

    # ledger shortcuts (tenant A, live, unless told otherwise)
    def intent(self, key: Optional[str] = None, *, action: str = "order_cancel",
               payload: Optional[dict] = None) -> lc.EffectIntent:
        return lc.EffectIntent(action_type=action, idempotency_key=key or _key(),
                               payload=payload if payload is not None else {"order": "SO-1"})

    def reserve(self, turn: c.AdmittedTurn, lease: c.Lease, intent: Optional[lc.EffectIntent] = None, *,
                tenant: Optional[int] = None, namespace: Any = LIVE) -> lc.EffectReservation:
        return self.repo.reserve_effect(tenant_id=tenant or self.tenant_a, namespace=namespace,
                                        conversation_id=turn.conversation_id, token=lease.token,
                                        turn_id=turn.turn_id, intent=intent or self.intent())

    def dispatch(self, turn: c.AdmittedTurn, lease: c.Lease, effect_id: int, *, tenant: Optional[int] = None,
                 namespace: Any = LIVE) -> lc.EffectAttemptRecord:
        return self.repo.reserve_effect_dispatch(tenant_id=tenant or self.tenant_a, namespace=namespace,
                                                 conversation_id=turn.conversation_id, token=lease.token,
                                                 effect_id=effect_id)

    def result(self, turn: c.AdmittedTurn, attempt_id: int, outcome: str, evidence: Optional[dict] = None, *,
               by: str = WORKER_A, tenant: Optional[int] = None, namespace: Any = LIVE) -> lc.EffectResultRecord:
        return self.repo.record_effect_result(tenant_id=tenant or self.tenant_a, namespace=namespace,
                                              conversation_id=turn.conversation_id, attempt_id=attempt_id,
                                              outcome=outcome, evidence=evidence, recorded_by=by)

    def effect(self, turn: c.AdmittedTurn, effect_id: int, *, tenant: Optional[int] = None,
               namespace: Any = LIVE) -> lc.EffectRecord:
        return self.repo.get_effect(tenant_id=tenant or self.tenant_a, namespace=namespace,
                                    conversation_id=turn.conversation_id, effect_id=effect_id)

    def attempts(self, turn: c.AdmittedTurn, effect_id: int) -> List[lc.EffectAttemptRecord]:
        return self.repo.list_effect_attempts(tenant_id=self.tenant_a, namespace=LIVE,
                                              conversation_id=turn.conversation_id, effect_id=effect_id)

    def results(self, turn: c.AdmittedTurn, effect_id: int) -> List[lc.EffectResultRecord]:
        return self.repo.list_effect_results(tenant_id=self.tenant_a, namespace=LIVE,
                                             conversation_id=turn.conversation_id, effect_id=effect_id)

    def delivery(self, turn: c.AdmittedTurn, lease: c.Lease, *, kind: str = "rich",
                 payload: Optional[dict] = None) -> lc.DeliverySequenceRecord:
        return self.repo.reserve_delivery(tenant_id=self.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                          token=lease.token, turn_id=turn.turn_id,
                                          intent=lc.DeliveryIntent(kind=kind, payload=payload or {"body": "rich card"}))

    def send(self, turn: c.AdmittedTurn, lease: c.Lease, seq: lc.DeliverySequenceRecord,
             transport: ScriptedTransport, *, by: str = WORKER_A) -> Tuple[lc.DeliveryAttemptRecord, lc.DeliveryReceiptRecord]:
        attempt = self.repo.reserve_delivery_dispatch(tenant_id=self.tenant_a, namespace=LIVE,
                                                      conversation_id=turn.conversation_id, token=lease.token,
                                                      sequence_id=seq.sequence_id)
        return attempt, self._deliver(turn, attempt, transport, by)

    def recover(self, turn: c.AdmittedTurn, lease: c.Lease, seq: lc.DeliverySequenceRecord,
                transport: ScriptedTransport, *, by: str = WORKER_A) -> Tuple[lc.DeliveryAttemptRecord, lc.DeliveryReceiptRecord]:
        attempt = self.repo.reserve_delivery_recovery(tenant_id=self.tenant_a, namespace=LIVE,
                                                      conversation_id=turn.conversation_id, token=lease.token,
                                                      sequence_id=seq.sequence_id, payload={"body": "plain text"})
        return attempt, self._deliver(turn, attempt, transport, by)

    def _deliver(self, turn: c.AdmittedTurn, attempt: lc.DeliveryAttemptRecord, transport: ScriptedTransport,
                 by: str) -> lc.DeliveryReceiptRecord:
        # The external call happens here, outside any repository transaction.
        kind, pmid = lc.classify_send_response(transport.send(attempt))
        return self.receipt(turn, attempt.attempt_id, kind.value, pmid=pmid, by=by)

    def receipt(self, turn: c.AdmittedTurn, attempt_id: int, kind: str, *, pmid: Optional[str] = None,
                evidence: Optional[dict] = None, by: str = WORKER_A) -> lc.DeliveryReceiptRecord:
        return self.repo.record_delivery_receipt(tenant_id=self.tenant_a, namespace=LIVE,
                                                 conversation_id=turn.conversation_id, attempt_id=attempt_id,
                                                 kind=kind, provider_message_id=pmid, evidence=evidence, recorded_by=by)

    def sequence(self, turn: c.AdmittedTurn) -> Optional[lc.DeliverySequenceRecord]:
        return self.repo.get_delivery_sequence(tenant_id=self.tenant_a, namespace=LIVE, turn_id=turn.turn_id)

    def receipts(self, turn: c.AdmittedTurn, seq: lc.DeliverySequenceRecord) -> List[lc.DeliveryReceiptRecord]:
        return self.repo.list_delivery_receipts(tenant_id=self.tenant_a, namespace=LIVE,
                                                conversation_id=turn.conversation_id, sequence_id=seq.sequence_id)

    def summary(self, turn: c.AdmittedTurn) -> lc.TurnLedgerSummary:
        return self.repo.turn_ledger_summary(tenant_id=self.tenant_a, namespace=LIVE, turn_id=turn.turn_id)

    def finalize(self, turn: c.AdmittedTurn, lease: c.Lease, *, processing: str = "completed",
                 details: Optional[dict] = None, state: Optional[c.StateTransition] = None) -> c.TerminalRecord:
        return self.repo.finalize_turn(tenant_id=self.tenant_a, namespace=LIVE, turn_id=turn.turn_id,
                                       token=lease.token, processing_outcome=processing, details=details,
                                       state_transition=state)

    def terminal(self, turn: c.AdmittedTurn) -> Optional[c.TerminalRecord]:
        return self.repo.foundation.get_terminal(tenant_id=self.tenant_a, namespace=LIVE, turn_id=turn.turn_id)

    def open_transactions(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state IN ('idle in transaction', 'idle in transaction (aborted)')")).scalar())


@pytest.fixture(scope="module")
def ledgers(pg_admin_dsn: str):
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, THIS_REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        handle = Ledgers(name=name, dsn=dsn, engine=engine, repo=LedgerRepository(engine),
                         tenant_a=_seed_tenant(engine, "A"), tenant_b=_seed_tenant(engine, "B"))
        yield handle
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


# ── Migration and metadata ───────────────────────────────────────────────────


def test_migration_0109_applies_on_0108_and_is_reversible(pg_admin_dsn: str) -> None:
    # 0109 is no longer a head: two siblings extend it — 0111 (handover) and
    # 0110 (address provenance), whose branch has now merged. It must still
    # sit on the integration branch and leave the A1-Validate head alone, so
    # the heads are {0092, 0110, 0111} here and {0092, 0111} in a checkout
    # from before the address branch merged.
    from scripts.operators.bootstrap_migration_contract import repository_heads_expected  # noqa: PLC0415

    heads = _script_heads()
    assert OTHER_HEAD in heads and repository_heads_expected(heads), \
        f"0109 must stay on the integration branch and leave 0092 untouched, got {sorted(heads)}"
    name, dsn = _create_database(pg_admin_dsn)
    try:
        _alembic(dsn, FOUNDATION_REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        assert _current_revisions(engine) == {FOUNDATION_REVISION}
        assert not any(inspect(engine).has_table(t) for t in TABLES)
        _alembic(dsn, THIS_REVISION)
        assert _current_revisions(engine) == {THIS_REVISION}
        assert all(inspect(engine).has_table(t) for t in TABLES)
        assert _ledger_trigger_relations(engine) == sorted(lm.APPEND_ONLY_TABLES)
        assert _ledger_function_present(engine)
        _alembic(dsn, FOUNDATION_REVISION, downgrade=True)
        assert _current_revisions(engine) == {FOUNDATION_REVISION}
        assert not any(inspect(engine).has_table(t) for t in TABLES)
        assert _ledger_trigger_relations(engine) == [] and not _ledger_function_present(engine)
        # The foundation tables and their own immutability trigger survive the downgrade.
        assert inspect(engine).has_table("commerce_runtime_turn_terminals")
        _alembic(dsn, THIS_REVISION)
        assert _current_revisions(engine) == {THIS_REVISION}
        engine.dispose()
    finally:
        _drop_database(pg_admin_dsn, name)


def test_ledger_metadata_and_migration_declare_the_same_schema(pg_admin_dsn: str) -> None:
    migrated_name, migrated_dsn = _create_database(pg_admin_dsn)
    declared_name, declared_dsn = _create_database(pg_admin_dsn)
    try:
        _alembic(migrated_dsn, THIS_REVISION)
        _alembic(declared_dsn, FOUNDATION_REVISION)
        migrated, declared = create_engine(migrated_dsn), create_engine(declared_dsn)
        lm.create_ledger_tables(declared)
        assert _schema_dump(declared) == _schema_dump(migrated)
        migrated.dispose()
        declared.dispose()
    finally:
        _drop_database(pg_admin_dsn, migrated_name)
        _drop_database(pg_admin_dsn, declared_name)


# ── Business-action identity ─────────────────────────────────────────────────


def test_concurrent_duplicate_reservations_produce_one_logical_action(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    intent = L.intent(payload={"order": "SO-77"})
    args = dict(tenant_id=L.tenant_a, namespace="live", conversation_id=turn.conversation_id,
                token=dataclasses.asdict(lease.token), turn_id=turn.turn_id, intent=dataclasses.asdict(intent))
    results = _run_workers([(w.reserve_effect_worker, (L.dsn, f"r{i}", args)) for i in range(3)])
    assert [r["status"] for r in results] == ["ok"] * 3, results
    ids = {r["result"]["effect"]["effect_id"] for r in results}
    assert len(ids) == 1
    assert sorted(r["result"]["created"] for r in results) == [False, False, True]
    with L.engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {lm.EFFECTS_TABLE} WHERE idempotency_key = :k"),
                            {"k": intent.idempotency_key}).scalar() == 1
    effect = L.effect(turn, ids.pop())
    assert (effect.status, effect.attempt_count, effect.turn_id) == ("reserved", 0, turn.turn_id)
    assert (effect.reserved_by, effect.reserved_fence, effect.reserved_epoch) == (WORKER_A, lease.fence, lease.epoch)


def test_same_key_with_a_different_action_payload_or_conversation_is_rejected_explicitly(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    intent = L.intent(payload={"order": "SO-1"})
    first = L.reserve(turn, lease, intent)
    assert first.created
    with pytest.raises(lc.EffectConflict) as payload_conflict:
        L.reserve(turn, lease, lc.EffectIntent("order_cancel", intent.idempotency_key, {"order": "SO-2"}))
    assert payload_conflict.value.reason == "payload" and payload_conflict.value.existing.effect_id == first.effect.effect_id
    with pytest.raises(lc.EffectConflict) as action_conflict:
        L.reserve(turn, lease, lc.EffectIntent("order_update", intent.idempotency_key, {"order": "SO-1"}))
    assert action_conflict.value.reason == "action_type"
    other_turn, other_lease = L.start(WORKER_B)
    with pytest.raises(lc.EffectConflict) as conversation_conflict:
        L.reserve(other_turn, other_lease, intent)
    assert conversation_conflict.value.reason == "conversation"
    # A provider tool-call id is not a business key: the same action under a fresh key is a second effect.
    second = L.reserve(turn, lease, lc.EffectIntent("order_cancel", "toolcall_" + uuid.uuid4().hex, {"order": "SO-1"}))
    assert second.created and second.effect.effect_id != first.effect.effect_id
    again = L.reserve(turn, lease, intent)
    assert not again.created and again.effect == first.effect
    assert L.repo.find_effect(tenant_id=L.tenant_a, namespace=LIVE, idempotency_key=intent.idempotency_key) == first.effect


def test_reservations_and_lookups_are_isolated_by_tenant_namespace_conversation_and_turn(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    intent = L.intent()
    mine = L.reserve(turn, lease, intent)
    # Same key in another tenant and in the shadow namespace: distinct effects.
    turn_b, lease_b = L.start(tenant=L.tenant_b)
    theirs = L.reserve(turn_b, lease_b, intent, tenant=L.tenant_b)
    shadow_turn, shadow_lease = L.start(namespace=SHADOW)
    shadow = L.reserve(shadow_turn, shadow_lease, intent, namespace=SHADOW)
    assert theirs.created and shadow.created
    assert len({mine.effect.effect_id, theirs.effect.effect_id, shadow.effect.effect_id}) == 3
    # A token issued for one conversation cannot reserve for another, before any database access.
    other_turn, _ = L.start()
    with pytest.raises(c.ScopeMismatch):
        L.repo.reserve_effect(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=other_turn.conversation_id,
                              token=lease.token, turn_id=other_turn.turn_id, intent=L.intent())
    # Foreign scopes look identical to absent rows.
    with pytest.raises(lc.EffectNotFound):
        L.effect(turn, mine.effect.effect_id, tenant=L.tenant_b)
    with pytest.raises(lc.EffectNotFound):
        L.effect(turn, mine.effect.effect_id, namespace=SHADOW)
    with pytest.raises(lc.EffectNotFound):
        L.repo.get_effect(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=other_turn.conversation_id,
                          effect_id=mine.effect.effect_id)
    with pytest.raises(lc.EffectNotFound):
        L.repo.reserve_effect_dispatch(tenant_id=L.tenant_b, namespace=LIVE, conversation_id=turn_b.conversation_id,
                                       token=lease_b.token, effect_id=mine.effect.effect_id)
    # The originating turn must be the eligible turn: a newer turn cannot reserve while an older one is unresolved.
    newer = L.admit(ref=L.snapshot(turn.conversation_id).conversation_ref)
    assert newer.sequence == turn.sequence + 1
    with pytest.raises(c.OwnershipRejected) as not_eligible:
        L.repo.reserve_effect(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                              token=lease.token, turn_id=newer.turn_id, intent=L.intent())
    assert not_eligible.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    assert L.effect(turn, mine.effect.effect_id).turn_id == turn.turn_id


# ── Ownership and dispatch boundaries ────────────────────────────────────────


def test_stale_lease_fence_and_epoch_are_rejected_for_intent_and_dispatch(ledgers: Ledgers) -> None:
    L = ledgers
    turn, short = L.start(seconds=1)
    _wait_past(L.engine, short.expires_at)
    with pytest.raises(c.OwnershipRejected) as expired:
        L.reserve(turn, short)
    assert expired.value.reason is c.RejectReason.EXPIRED_LEASE
    taker = L.claim(turn.conversation_id, WORKER_B)
    assert taker.takeover and taker.fence > short.fence and taker.epoch == short.epoch + 1
    reserved = L.reserve(turn, taker)
    seq = L.delivery(turn, taker)
    for call in (
        lambda: L.reserve(turn, short),
        lambda: L.dispatch(turn, short, reserved.effect.effect_id),
        lambda: L.delivery(turn, short),
        lambda: L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE,
                                                 conversation_id=turn.conversation_id, token=short.token,
                                                 sequence_id=seq.sequence_id),
        lambda: L.repo.commit_turn_decision(tenant_id=L.tenant_a, namespace=LIVE,
                                            conversation_id=turn.conversation_id, token=short.token,
                                            turn_id=turn.turn_id, effect_intents=[L.intent()]),
    ):
        with pytest.raises(c.OwnershipRejected) as superseded:
            call()
        assert superseded.value.reason is c.RejectReason.SUPERSEDED_FENCE
    L.repo.foundation.invalidate_ownership(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id)
    with pytest.raises(c.OwnershipRejected) as obsolete:
        L.dispatch(turn, taker, reserved.effect.effect_id)
    assert obsolete.value.reason is c.RejectReason.OBSOLETE_EPOCH
    assert L.effect(turn, reserved.effect.effect_id).status == "reserved"
    assert L.attempts(turn, reserved.effect.effect_id) == []
    assert L.sequence(turn).attempt_count == 0


def test_ownership_change_between_intent_and_dispatch_requires_fresh_authorization(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease_a = L.start(seconds=2)
    reserved = L.reserve(turn, lease_a)
    seq = L.delivery(turn, lease_a)
    _wait_past(L.engine, lease_a.expires_at)
    lease_b = L.claim(turn.conversation_id, WORKER_B)
    assert lease_b.takeover
    # Authority at intent time is not authority to dispatch.
    with pytest.raises(c.OwnershipRejected) as stale:
        L.dispatch(turn, lease_a, reserved.effect.effect_id)
    assert stale.value.reason is c.RejectReason.SUPERSEDED_FENCE
    with pytest.raises(c.OwnershipRejected):
        L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                         token=lease_a.token, sequence_id=seq.sequence_id)
    assert L.attempts(turn, reserved.effect.effect_id) == []
    # The current owner re-checks its own authorization at dispatch time and proceeds.
    attempt = L.dispatch(turn, lease_b, reserved.effect.effect_id)
    assert (attempt.attempt_no, attempt.reserved_by, attempt.reserved_fence, attempt.reserved_epoch) == (
        1, WORKER_B, lease_b.fence, lease_b.epoch)
    effect = L.effect(turn, reserved.effect.effect_id)
    assert (effect.status, effect.attempt_count, effect.reserved_by) == ("dispatching", 1, WORKER_A)


def test_concurrent_dispatch_reservations_create_exactly_one_attempt(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    reserved = L.reserve(turn, lease)
    seq = L.delivery(turn, lease)
    token = dataclasses.asdict(lease.token)
    effect_args = dict(tenant_id=L.tenant_a, namespace="live", conversation_id=turn.conversation_id, token=token,
                       effect_id=reserved.effect.effect_id)
    results = _run_workers([(w.reserve_effect_dispatch_worker, (L.dsn, f"d{i}", effect_args)) for i in range(3)])
    assert sorted(r["status"] for r in results) == ["dispatch_blocked:attempt_pending"] * 2 + ["ok"], results
    assert len(L.attempts(turn, reserved.effect.effect_id)) == 1
    delivery_args = dict(tenant_id=L.tenant_a, namespace="live", conversation_id=turn.conversation_id, token=token,
                         sequence_id=seq.sequence_id)
    results = _run_workers([(w.reserve_delivery_dispatch_worker, (L.dsn, f"s{i}", delivery_args)) for i in range(3)])
    assert sorted(r["status"] for r in results) == ["delivery_dispatch_blocked:attempt_pending"] * 2 + ["ok"], results
    assert L.sequence(turn).attempt_count == 1


# ── Honest outcomes and recovery ─────────────────────────────────────────────


def test_confirmed_mutation_is_reused_without_a_second_execution(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    intent = L.intent(payload={"order": "SO-9"})
    reserved = L.reserve(turn, lease, intent)
    attempt = L.dispatch(turn, lease, reserved.effect.effect_id)
    executed = ["provider call performed once, outside any transaction"]
    result = L.result(turn, attempt.attempt_id, "confirmed", {"order_ref": "SO-9", "provider_ref": "cancel-1"})
    assert (result.result_no, result.outcome) == (1, "confirmed")
    effect = L.effect(turn, reserved.effect.effect_id)
    assert effect.status == "confirmed" and effect.confirmed_result == {"order_ref": "SO-9", "provider_ref": "cancel-1"}
    # A reasoning retry reserves the same action again and gets the confirmed result back.
    again = L.reserve(turn, lease, intent)
    assert not again.created and again.effect.status == "confirmed" and again.effect.confirmed_result == effect.confirmed_result
    with pytest.raises(lc.DispatchBlocked) as blocked:
        L.dispatch(turn, lease, reserved.effect.effect_id)
    assert blocked.value.reason is lc.DispatchBlock.ALREADY_CONFIRMED and blocked.value.effect.confirmed_result == effect.confirmed_result
    # A provider failover after confirmation is the same effect, not a new execution.
    L.finalize(turn, lease)
    later = L.admit(ref=L.snapshot(turn.conversation_id).conversation_ref)
    later_reservation = L.repo.reserve_effect(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                              token=lease.token, turn_id=later.turn_id, intent=intent)
    assert not later_reservation.created and later_reservation.effect.turn_id == turn.turn_id
    assert len(executed) == 1 and len(L.attempts(turn, reserved.effect.effect_id)) == 1
    assert len(L.results(turn, reserved.effect.effect_id)) == 1


def test_unknown_outcome_blocks_replay_and_provider_failover(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease_a = L.start(seconds=2)
    reserved = L.reserve(turn, lease_a)
    attempt = L.dispatch(turn, lease_a, reserved.effect.effect_id)
    unknown = L.result(turn, attempt.attempt_id, "unknown", {"timeout_seconds": 30, "submitted": True})
    assert unknown.outcome == "unknown" and L.effect(turn, reserved.effect.effect_id).status == "unknown"
    with pytest.raises(lc.DispatchBlocked) as blocked:
        L.dispatch(turn, lease_a, reserved.effect.effect_id)
    assert blocked.value.reason is lc.DispatchBlock.OUTCOME_UNKNOWN
    assert blocked.value.open_attempt is not None and blocked.value.open_attempt.attempt_id == attempt.attempt_id
    # Ownership takeover changes nothing: the new owner may not redispatch, with this or another provider.
    _wait_past(L.engine, lease_a.expires_at)
    lease_b = L.claim(turn.conversation_id, WORKER_B)
    with pytest.raises(lc.DispatchBlocked) as still_blocked:
        L.dispatch(turn, lease_b, reserved.effect.effect_id)
    assert still_blocked.value.reason is lc.DispatchBlock.OUTCOME_UNKNOWN
    assert len(L.attempts(turn, reserved.effect.effect_id)) == 1
    # Late evidence for the same attempt resolves the uncertainty once; nothing is re-executed.
    resolved = L.result(turn, attempt.attempt_id, "confirmed", {"provider_ref": "cancel-late"}, by=WORKER_B)
    assert (resolved.result_no, resolved.outcome) == (2, "confirmed")
    assert L.effect(turn, reserved.effect.effect_id).status == "confirmed"
    with pytest.raises(lc.DispatchBlocked) as confirmed:
        L.dispatch(turn, lease_b, reserved.effect.effect_id)
    assert confirmed.value.reason is lc.DispatchBlock.ALREADY_CONFIRMED
    with pytest.raises(lc.IllegalTransition):
        L.result(turn, attempt.attempt_id, "rejected", {"contradiction": True}, by=WORKER_B)


def test_crash_before_commit_leaves_no_partial_intent_state_or_terminal(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    keys = [_key(), _key()]
    decision = dict(
        tenant_id=L.tenant_a, namespace="live", conversation_id=turn.conversation_id,
        token=dataclasses.asdict(lease.token), turn_id=turn.turn_id,
        state_transition={"expected_revision": 0, "payload": {"decided": True}},
        effect_intents=[{"action_type": "order_cancel", "idempotency_key": keys[0], "payload": {"order": "SO-1"}},
                        {"action_type": "coupon_apply", "idempotency_key": keys[1], "payload": {"code": "WELCOME"}}],
        delivery_intent={"kind": "rich", "payload": {"body": "card"}},
    )
    exit_code, messages = _run_crash_worker(w.crash_decision_worker, (L.dsn, "crash", decision))
    assert exit_code == 9 and [msg["status"] for msg in messages] == ["dying_before_commit"], (exit_code, messages)
    snap = L.snapshot(turn.conversation_id)
    assert snap.state_revision == 0 and snap.state_payload == {}
    assert all(L.repo.find_effect(tenant_id=L.tenant_a, namespace=LIVE, idempotency_key=k) is None for k in keys)
    assert L.sequence(turn) is None and L.terminal(turn) is None
    assert L.open_transactions() == 0
    # The same decision, committed normally, is whole.
    committed = L.repo.commit_turn_decision(
        tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
        turn_id=turn.turn_id, state_transition=c.StateTransition(expected_revision=0, payload={"decided": True}),
        effect_intents=[lc.EffectIntent("order_cancel", keys[0], {"order": "SO-1"}),
                        lc.EffectIntent("coupon_apply", keys[1], {"code": "WELCOME"})],
        delivery_intent=lc.DeliveryIntent("rich", {"body": "card"}))
    assert committed.state is not None and committed.state.revision == 1
    assert [r.created for r in committed.effects] == [True, True] and committed.delivery is not None
    # A crash inside the terminal recording leaves no terminal either.
    for reservation in committed.effects:
        attempt = L.dispatch(turn, lease, reservation.effect.effect_id)
        L.result(turn, attempt.attempt_id, "confirmed", {"ok": True})
    _, receipt = L.send(turn, lease, committed.delivery, ScriptedTransport(("accepted", "wamid.crash")))
    assert receipt.kind == "accepted"
    exit_code, messages = _run_crash_worker(w.crash_finalize_worker, (L.dsn, "crash-finalize", dict(
        tenant_id=L.tenant_a, namespace="live", turn_id=turn.turn_id, token=dataclasses.asdict(lease.token),
        processing_outcome="completed")))
    assert exit_code == 9 and [msg["status"] for msg in messages] == ["dying_before_commit"], (exit_code, messages)
    assert L.terminal(turn) is None and L.snapshot(turn.conversation_id).eligible_turn_id == turn.turn_id
    record = L.finalize(turn, lease)
    assert (record.transport_outcome, record.customer_reach) == ("accepted", "unknown")


def test_crash_after_durable_dispatch_reservation_blocks_blind_redispatch(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease_a = L.start(seconds=2)
    reserved = L.reserve(turn, lease_a)
    seq = L.delivery(turn, lease_a)
    effect_attempt = L.dispatch(turn, lease_a, reserved.effect.effect_id)
    delivery_attempt = L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE,
                                                        conversation_id=turn.conversation_id, token=lease_a.token,
                                                        sequence_id=seq.sequence_id)
    # Worker A dies here: the external calls may or may not have happened. Its lease lapses.
    _wait_past(L.engine, lease_a.expires_at)
    lease_b = L.claim(turn.conversation_id, WORKER_B)
    assert lease_b.takeover
    with pytest.raises(lc.DispatchBlocked) as blocked:
        L.dispatch(turn, lease_b, reserved.effect.effect_id)
    assert blocked.value.reason is lc.DispatchBlock.ATTEMPT_PENDING
    assert blocked.value.open_attempt.attempt_id == effect_attempt.attempt_id
    with pytest.raises(lc.DeliveryDispatchBlocked) as delivery_blocked:
        L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                         token=lease_b.token, sequence_id=seq.sequence_id)
    assert delivery_blocked.value.reason is lc.DispatchBlock.ATTEMPT_PENDING
    with pytest.raises(lc.RecoveryNotPermitted) as recovery:
        L.repo.reserve_delivery_recovery(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                         token=lease_b.token, sequence_id=seq.sequence_id, payload={"body": "text"})
    assert recovery.value.reason is lc.RecoveryRefusal.ATTEMPT_PENDING
    with pytest.raises(lc.CompletionBlocked):
        L.finalize(turn, lease_b)
    # Absence of a local success record is not proof that nothing happened: B records uncertainty.
    L.result(turn, effect_attempt.attempt_id, "unknown", {"crash": "worker-a", "receipt": None}, by=WORKER_B)
    L.receipt(turn, delivery_attempt.attempt_id, "unknown", evidence={"crash": "worker-a"}, by=WORKER_B)
    with pytest.raises(lc.DispatchBlocked) as still:
        L.dispatch(turn, lease_b, reserved.effect.effect_id)
    assert still.value.reason is lc.DispatchBlock.OUTCOME_UNKNOWN
    with pytest.raises(lc.DeliveryDispatchBlocked) as still_delivery:
        L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                         token=lease_b.token, sequence_id=seq.sequence_id)
    assert still_delivery.value.reason is lc.DispatchBlock.OUTCOME_UNKNOWN
    assert len(L.attempts(turn, reserved.effect.effect_id)) == 1 and L.sequence(turn).attempt_count == 1
    record = L.finalize(turn, lease_b, processing="failed")
    assert (record.transport_outcome, record.customer_reach) == ("unknown", "unknown")
    assert record.details["ledger"]["effects_by_status"]["unknown"] == 1
    # Completion did not turn the unknown into success, and it did not authorise a redispatch either.
    with pytest.raises(c.OwnershipRejected) as closed:
        L.dispatch(turn, lease_b, reserved.effect.effect_id)
    assert closed.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    assert L.effect(turn, reserved.effect.effect_id).status == "unknown"
    assert len(L.attempts(turn, reserved.effect.effect_id)) == 1


def test_late_evidence_attaches_only_to_its_existing_attempt(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease_a = L.start(seconds=2)
    reserved = L.reserve(turn, lease_a)
    attempt = L.dispatch(turn, lease_a, reserved.effect.effect_id)
    _wait_past(L.engine, lease_a.expires_at)
    lease_b = L.claim(turn.conversation_id, WORKER_B)
    # A's ownership ended, but the provider answered A: the evidence attaches to A's attempt.
    late = L.result(turn, attempt.attempt_id, "confirmed", {"provider_ref": "late-1"}, by=WORKER_A)
    assert late.attempt_id == attempt.attempt_id and late.recorded_by == WORKER_A
    effect = L.effect(turn, reserved.effect.effect_id)
    assert effect.status == "confirmed" and effect.confirmed_result == {"provider_ref": "late-1"}
    # ...without granting A authority to create anything new.
    with pytest.raises(c.OwnershipRejected) as rejected:
        L.reserve(turn, lease_a)
    assert rejected.value.reason is c.RejectReason.SUPERSEDED_FENCE
    with pytest.raises(c.OwnershipRejected):
        L.delivery(turn, lease_a)
    with pytest.raises(lc.AttemptNotFound):
        L.result(turn, attempt.attempt_id + 100000, "confirmed", {}, by=WORKER_A)
    other_turn, _ = L.start()
    with pytest.raises(lc.AttemptNotFound):
        L.repo.record_effect_result(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=other_turn.conversation_id,
                                    attempt_id=attempt.attempt_id, outcome="rejected", evidence={}, recorded_by=WORKER_B)
    with pytest.raises(c.ConversationNotFound):                       # a foreign tenant sees no conversation at all
        L.result(turn, attempt.attempt_id, "rejected", {}, by=WORKER_B, tenant=L.tenant_b)
    assert [r.outcome for r in L.results(turn, reserved.effect.effect_id)] == ["confirmed"]
    # The evidence rows themselves are append-only at the database level.
    with L.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(f"UPDATE {lm.EFFECT_RESULTS_TABLE} SET outcome = 'rejected' WHERE id = :id"),
                         {"id": late.result_id})
    with L.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(f"DELETE FROM {lm.EFFECT_ATTEMPTS_TABLE} WHERE id = :id"), {"id": attempt.attempt_id})
    assert lease_b.fence > lease_a.fence


def test_definitive_rejection_permits_one_bounded_recovery_attempt(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    seq = L.delivery(turn, lease, kind="rich")
    transport = ScriptedTransport(("rejected",), ("accepted", "wamid.recovered"))
    first, first_receipt = L.send(turn, lease, seq, transport)
    assert (first.attempt_no, first.kind, first_receipt.kind) == (1, "rich", "rejected")
    assert L.sequence(turn).outcome == "rejected"
    with pytest.raises(lc.DeliveryDispatchBlocked) as blocked:
        L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                         token=lease.token, sequence_id=seq.sequence_id)
    assert blocked.value.reason is lc.DispatchBlock.RECOVERY_ONLY
    second, second_receipt = L.recover(turn, lease, seq, transport)
    assert (second.attempt_no, second.kind, second.payload) == (2, "text", {"body": "plain text"})
    assert (second_receipt.kind, second_receipt.provider_message_id) == ("accepted", "wamid.recovered")
    assert L.sequence(turn).outcome == "accepted" and L.sequence(turn).attempt_count == 2
    assert transport.calls == [first.dispatch_key, second.dispatch_key]
    with pytest.raises(lc.RecoveryNotPermitted) as accepted:
        L.recover(turn, lease, seq, ScriptedTransport())
    assert accepted.value.reason is lc.RecoveryRefusal.OUTCOME_ACCEPTED
    assert L.summary(turn).transport_outcome == "accepted"
    # A text first attempt has no recovery; two rejections exhaust the sequence.
    text_turn, text_lease = L.start()
    text_seq = L.delivery(text_turn, text_lease, kind="text", payload={"body": "hi"})
    L.send(text_turn, text_lease, text_seq, ScriptedTransport(("rejected",)))
    with pytest.raises(lc.RecoveryNotPermitted) as not_rich:
        L.recover(text_turn, text_lease, text_seq, ScriptedTransport())
    assert not_rich.value.reason is lc.RecoveryRefusal.NOT_RICH_TO_TEXT
    twice_turn, twice_lease = L.start()
    twice_seq = L.delivery(twice_turn, twice_lease, kind="rich")
    twice_transport = ScriptedTransport(("rejected",), ("rejected",))
    L.send(twice_turn, twice_lease, twice_seq, twice_transport)
    L.recover(twice_turn, twice_lease, twice_seq, twice_transport)
    with pytest.raises(lc.RecoveryNotPermitted) as exhausted:
        L.recover(twice_turn, twice_lease, twice_seq, ScriptedTransport())
    assert exhausted.value.reason is lc.RecoveryRefusal.ATTEMPTS_EXHAUSTED
    assert L.summary(twice_turn).transport_outcome == "rejected_definitive"
    assert L.summary(twice_turn).customer_reach == "not_reached"


@pytest.mark.parametrize("script", [("no_id",), ("timeout",), ("server_error",)])
def test_timeout_or_missing_provider_message_id_prevents_fallback(ledgers: Ledgers, script) -> None:
    L = ledgers
    turn, lease = L.start()
    seq = L.delivery(turn, lease, kind="rich")
    attempt, receipt = L.send(turn, lease, seq, ScriptedTransport(script))
    assert (receipt.kind, receipt.provider_message_id) == ("unknown", None)
    assert L.sequence(turn).outcome == "unknown"
    with pytest.raises(lc.RecoveryNotPermitted) as recovery:
        L.recover(turn, lease, seq, ScriptedTransport(("accepted", "wamid.never")))
    assert recovery.value.reason is lc.RecoveryRefusal.OUTCOME_UNKNOWN
    with pytest.raises(lc.DeliveryDispatchBlocked) as blocked:
        L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                         token=lease.token, sequence_id=seq.sequence_id)
    assert blocked.value.reason is lc.DispatchBlock.OUTCOME_UNKNOWN
    assert L.sequence(turn).attempt_count == 1
    assert L.summary(turn).transport_outcome == "unknown" and L.summary(turn).customer_reach == "unknown"
    # Late evidence for the same attempt establishes acceptance; still no fallback, still no second send.
    resolved = L.receipt(turn, attempt.attempt_id, "accepted", pmid="wamid.late", evidence={"status_webhook": True})
    assert resolved.receipt_no == 2 and L.sequence(turn).outcome == "accepted"
    with pytest.raises(lc.RecoveryNotPermitted) as after:
        L.recover(turn, lease, seq, ScriptedTransport())
    assert after.value.reason is lc.RecoveryRefusal.OUTCOME_ACCEPTED
    assert L.sequence(turn).attempt_count == 1


def test_acceptance_delivery_and_read_receipts_remain_distinct(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    seq = L.delivery(turn, lease)
    attempt = L.repo.reserve_delivery_dispatch(tenant_id=L.tenant_a, namespace=LIVE,
                                               conversation_id=turn.conversation_id, token=lease.token,
                                               sequence_id=seq.sequence_id)
    with pytest.raises(lc.IllegalTransition):
        L.receipt(turn, attempt.attempt_id, "delivered", pmid="wamid.x")      # reach before acceptance
    accepted = L.receipt(turn, attempt.attempt_id, "accepted", pmid="wamid.x")
    assert L.summary(turn).customer_reach == "unknown"                          # acceptance is not reach
    record = L.finalize(turn, lease)
    assert (record.transport_outcome, record.customer_reach) == ("accepted", "unknown")
    with pytest.raises(lc.IllegalTransition):
        L.receipt(turn, attempt.attempt_id, "delivered", pmid="wamid.other")   # someone else's message id
    delivered = L.receipt(turn, attempt.attempt_id, "delivered", pmid="wamid.x", evidence={"ts": 1})
    read = L.receipt(turn, attempt.attempt_id, "read", evidence={"ts": 2})
    assert [r.kind for r in L.receipts(turn, seq)] == ["accepted", "delivered", "read"]
    assert {accepted.provider_message_id, delivered.provider_message_id, read.provider_message_id} == {"wamid.x"}
    assert L.summary(turn).customer_reach == "reached" and L.summary(turn).transport_outcome == "accepted"
    # The terminal recorded processing completion as it was; later receipts live in the ledger only.
    assert L.terminal(turn) == record and record.customer_reach == "unknown"
    assert L.receipt(turn, attempt.attempt_id, "read", evidence={"ts": 2}) == read   # identical repeat: same row
    with pytest.raises(lc.IllegalTransition):
        L.receipt(turn, attempt.attempt_id, "read", evidence={"ts": 3})


def test_illegal_or_contradictory_transitions_fail_explicitly(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    reserved = L.reserve(turn, lease)
    attempt = L.dispatch(turn, lease, reserved.effect.effect_id)
    confirmed = L.result(turn, attempt.attempt_id, "confirmed", {"ref": "c1"})
    assert L.result(turn, attempt.attempt_id, "confirmed", {"ref": "c1"}) == confirmed   # idempotent repeat
    for outcome, evidence in (("rejected", {"ref": "c1"}), ("unknown", {"ref": "c1"}), ("confirmed", {"ref": "c2"})):
        with pytest.raises(lc.IllegalTransition) as illegal:
            L.result(turn, attempt.attempt_id, outcome, evidence)
        assert (illegal.value.current, illegal.value.attempted) == ("confirmed", outcome)
    assert L.effect(turn, reserved.effect.effect_id).confirmed_result == {"ref": "c1"}
    untouched = L.reserve(turn, lease)
    assert L.attempts(turn, untouched.effect.effect_id) == []          # a reserved effect has no attempt to answer
    with pytest.raises(lc.AttemptNotFound):
        L.result(turn, attempt.attempt_id + 100000, "confirmed", {})
    seq = L.delivery(turn, lease)
    sent, receipt = L.send(turn, lease, seq, ScriptedTransport(("accepted", "wamid.ok")))
    assert receipt.kind == "accepted"
    with pytest.raises(lc.IllegalTransition):
        L.receipt(turn, sent.attempt_id, "rejected")
    with pytest.raises(lc.IllegalTransition):
        L.receipt(turn, sent.attempt_id, "accepted", pmid="wamid.other")
    with pytest.raises(c.ValidationError):
        L.receipt(turn, sent.attempt_id, "accepted")                       # acceptance without an id
    with pytest.raises(c.ValidationError):
        L.finalize(turn, lease, details={"ledger": {"forged": True}})
    # Database-level guards hold independently of the repository.
    with L.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="ck_commerce_runtime_effects_status"):
            conn.execute(text(f"UPDATE {lm.EFFECTS_TABLE} SET status = 'replayed' WHERE id = :id"),
                         {"id": untouched.effect.effect_id})
    with L.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="ck_commerce_runtime_effects_confirmed_pair"):
            conn.execute(text(f"UPDATE {lm.EFFECTS_TABLE} SET confirmed_result = NULL WHERE id = :id"),
                         {"id": reserved.effect.effect_id})
    with L.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(f"UPDATE {lm.DELIVERY_RECEIPTS_TABLE} SET kind = 'read' WHERE id = :id"),
                         {"id": receipt.receipt_id})


def test_commit_turn_decision_is_atomic_and_finalize_derives_transport_and_reach(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    intents = [lc.EffectIntent("order_update", _key(), {"order": "SO-5", "status": "confirmed"}),
               lc.EffectIntent("payment_link_create", _key(), {"order": "SO-5"})]
    delivery = lc.DeliveryIntent("rich", {"body": "order confirmed card"})
    decision = L.repo.commit_turn_decision(
        tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
        turn_id=turn.turn_id, state_transition=c.StateTransition(expected_revision=0, payload={"stage": "confirm"}),
        effect_intents=intents, delivery_intent=delivery)
    assert decision.state.revision == 1 and [r.created for r in decision.effects] == [True, True]
    assert decision.delivery.turn_id == turn.turn_id and decision.delivery.intent_kind == "rich"
    assert L.snapshot(turn.conversation_id).state_payload == {"stage": "confirm"}
    # A stale revision refuses the whole decision; nothing new is written.
    with pytest.raises(c.StateConflict):
        L.repo.commit_turn_decision(
            tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
            turn_id=turn.turn_id, state_transition=c.StateTransition(expected_revision=0, payload={"stage": "x"}),
            effect_intents=[lc.EffectIntent("coupon_apply", _key(), {})], delivery_intent=delivery)
    assert L.snapshot(turn.conversation_id).state_revision == 1
    # A repeated decision on the current revision returns the same intents and the same sequence.
    repeat = L.repo.commit_turn_decision(
        tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
        turn_id=turn.turn_id, state_transition=c.StateTransition(expected_revision=1, payload={"stage": "confirm"}),
        effect_intents=intents, delivery_intent=delivery)
    assert repeat.state.revision == 2 and [r.created for r in repeat.effects] == [False, False]
    assert repeat.delivery.sequence_id == decision.delivery.sequence_id
    # A different delivery intent for the same turn is a conflict, and the transaction rolls back whole.
    with pytest.raises(lc.DeliveryConflict) as conflict:
        L.repo.commit_turn_decision(
            tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
            turn_id=turn.turn_id, state_transition=c.StateTransition(expected_revision=2, payload={"stage": "z"}),
            delivery_intent=lc.DeliveryIntent("text", {"body": "different"}))
    assert conflict.value.reason == "kind" and L.snapshot(turn.conversation_id).state_revision == 2
    with pytest.raises(c.ValidationError):
        L.repo.commit_turn_decision(
            tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
            turn_id=turn.turn_id, effect_intents=[intents[0], intents[0]])
    # Completion is refused while an attempt is open or an intent is undispatched, then derives
    # transport and reach from the ledgers once every intent went through the supported lifecycle.
    attempt = L.dispatch(turn, lease, decision.effects[0].effect.effect_id)
    with pytest.raises(lc.CompletionBlocked) as pending:
        L.finalize(turn, lease)
    assert pending.value.reason == "actionable_work_remains"
    L.result(turn, attempt.attempt_id, "confirmed", {"ok": True})
    with pytest.raises(lc.CompletionBlocked) as undispatched:
        L.finalize(turn, lease)
    assert set(undispatched.value.blockers) == {"1 effect intent(s) reserved but not dispatched",
                                                "delivery intent reserved but not dispatched"}
    second = L.dispatch(turn, lease, decision.effects[1].effect.effect_id)
    L.result(turn, second.attempt_id, "confirmed", {"ok": True})
    with pytest.raises(lc.CompletionBlocked) as delivery_undispatched:
        L.finalize(turn, lease)
    assert delivery_undispatched.value.blockers == ("delivery intent reserved but not dispatched",)
    _, receipt = L.send(turn, lease, decision.delivery, ScriptedTransport(("accepted", "wamid.final")))
    L.receipt(turn, receipt.attempt_id, "delivered", pmid="wamid.final")
    record = L.finalize(turn, lease, details={"note": "done"},
                        state=c.StateTransition(expected_revision=2, payload={"stage": "closed"}))
    assert (record.processing_outcome, record.transport_outcome, record.customer_reach) == ("completed", "accepted", "reached")
    assert record.details["note"] == "done"
    assert record.details["ledger"]["effects_by_status"] == {"reserved": 0, "dispatching": 0, "confirmed": 2,
                                                             "rejected": 0, "unknown": 0}
    assert record.details["ledger"]["delivery_outcome"] == "accepted"
    assert L.snapshot(turn.conversation_id).state_revision == 3
    with pytest.raises(c.TerminalAlreadyRecorded):
        L.finalize(turn, lease)
    with L.engine.begin() as conn:
        with pytest.raises(DBAPIError, match="immutable"):
            conn.execute(text("UPDATE commerce_runtime_turn_terminals SET customer_reach = 'unknown' WHERE turn_id = :t"),
                         {"t": turn.turn_id})


def test_handoff_request_is_not_human_ownership_transfer(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    request = L.reserve(turn, lease, lc.EffectIntent("handoff_request", _key(), {"reason": "asked", "needs_human": True}))
    attempt = L.dispatch(turn, lease, request.effect.effect_id)
    L.result(turn, attempt.attempt_id, "confirmed", {"request_id": "hr-1", "needs_human": True})
    effect = L.effect(turn, request.effect.effect_id)
    assert effect.status == "confirmed" and lc.human_transfer_established(effect) is False
    snap = L.snapshot(turn.conversation_id)
    assert (snap.lease_owner, snap.lease_fence, snap.ownership_epoch) == (WORKER_A, lease.fence, lease.epoch)
    # Only explicit transfer evidence in a confirmed result establishes a human owner.
    proven = L.reserve(turn, lease, lc.EffectIntent("handoff_request", _key(), {"reason": "escalate"}))
    proven_attempt = L.dispatch(turn, lease, proven.effect.effect_id)
    L.result(turn, proven_attempt.attempt_id, "confirmed",
             {"transfer": {"human_owner_ref": "agent:42", "accepted_at": "2026-09-19T12:00:00Z"}})
    assert lc.human_transfer_established(L.effect(turn, proven.effect.effect_id)) is True


# ── L1: completion boundary (both terminal entry points) ─────────────────────


def test_reserved_intents_block_premature_completion_on_both_entry_points(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    intent = L.intent(payload={"order": "SO-31"})
    decision = L.repo.commit_turn_decision(
        tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
        turn_id=turn.turn_id, state_transition=c.StateTransition(expected_revision=0, payload={"decided": True}),
        effect_intents=[intent], delivery_intent=lc.DeliveryIntent("rich", {"body": "card"}))
    assert decision.state.revision == 1
    before = L.snapshot(turn.conversation_id)
    # Ledger-derived path: refused while the intents were never dispatched; nothing is written.
    with pytest.raises(lc.CompletionBlocked) as blocked:
        L.finalize(turn, lease, state=c.StateTransition(expected_revision=1, payload={"stage": "premature"}))
    assert blocked.value.reason == "actionable_work_remains"
    assert set(blocked.value.blockers) == {"1 effect intent(s) reserved but not dispatched",
                                           "delivery intent reserved but not dispatched"}
    # Foundation path: a ledger-bearing turn cannot be completed with caller-supplied outcomes at all.
    with pytest.raises(c.CompletionBlocked) as bypass:
        L.repo.foundation.record_terminal(
            tenant_id=L.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="accepted", customer_reach="reached",
            state_transition=c.StateTransition(expected_revision=1, payload={"stage": "bypass"}))
    assert bypass.value.reason == "ledger_bearing_turn"
    after = L.snapshot(turn.conversation_id)
    assert (after.state_revision, after.state_payload, after.eligible_turn_id) == (1, {"decided": True}, turn.turn_id)
    assert dataclasses.replace(after, db_now=before.db_now) == before
    assert L.terminal(turn) is None and L.open_transactions() == 0
    # The retained work proceeds through the supported lifecycle: dispatch, outcome, then completion.
    attempt = L.dispatch(turn, lease, decision.effects[0].effect.effect_id)
    L.result(turn, attempt.attempt_id, "confirmed", {"provider_ref": "cancel-31"})
    with pytest.raises(lc.CompletionBlocked) as still:
        L.finalize(turn, lease)
    assert still.value.blockers == ("delivery intent reserved but not dispatched",)
    sent, receipt = L.send(turn, lease, decision.delivery, ScriptedTransport(("accepted", "wamid.31")))
    assert receipt.kind == "accepted"
    record = L.finalize(turn, lease, state=c.StateTransition(expected_revision=1, payload={"stage": "closed"}))
    assert (record.transport_outcome, record.customer_reach) == ("accepted", "unknown")
    assert record.details["ledger"]["effects_by_status"]["confirmed"] == 1
    assert L.snapshot(turn.conversation_id).state_revision == 2
    # A later turn reusing the business key gets the confirmed effect back, never a stranded one.
    later = L.admit(ref=L.snapshot(turn.conversation_id).conversation_ref)
    reuse = L.repo.reserve_effect(tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                                  token=lease.token, turn_id=later.turn_id, intent=intent)
    assert not reuse.created and reuse.effect.status == "confirmed"


def test_dispatching_attempts_cannot_be_completed_through_the_foundation_entry_point(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    reserved = L.reserve(turn, lease)
    attempt = L.dispatch(turn, lease, reserved.effect.effect_id)
    with pytest.raises(c.CompletionBlocked) as bypass:
        L.repo.foundation.record_terminal(
            tenant_id=L.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable")
    assert bypass.value.reason == "ledger_bearing_turn"
    with pytest.raises(lc.CompletionBlocked) as pending:
        L.finalize(turn, lease)
    assert pending.value.reason == "actionable_work_remains"
    assert pending.value.blockers == ("1 effect attempt(s) without an established outcome",)
    assert L.terminal(turn) is None and L.snapshot(turn.conversation_id).eligible_turn_id == turn.turn_id
    # A recorded unknown is an established (uncertain) outcome: completion may proceed through the
    # ledger-derived path only, the unknown stays distinct from success, and nothing redispatches.
    L.result(turn, attempt.attempt_id, "unknown", {"timeout_seconds": 30})
    with pytest.raises(c.CompletionBlocked) as still_bypass:
        L.repo.foundation.record_terminal(
            tenant_id=L.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="accepted", customer_reach="reached")
    assert still_bypass.value.reason == "ledger_bearing_turn"
    record = L.finalize(turn, lease, processing="failed")
    assert (record.processing_outcome, record.transport_outcome, record.customer_reach) == (
        "failed", "not_attempted", "not_applicable")
    assert record.details["ledger"]["effects_by_status"]["unknown"] == 1
    assert L.effect(turn, reserved.effect.effect_id).status == "unknown"
    with pytest.raises(c.OwnershipRejected) as closed:
        L.dispatch(turn, lease, reserved.effect.effect_id)
    assert closed.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    assert len(L.attempts(turn, reserved.effect.effect_id)) == 1


def test_foundation_terminal_remains_available_for_turns_without_ledger_records(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    assert L.summary(turn).delivery_outcome == "not_attempted"
    record = L.repo.foundation.record_terminal(
        tenant_id=L.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
        processing_outcome="completed", transport_outcome="accepted", customer_reach="reached",
        details={"legacy_path": True})
    assert (record.transport_outcome, record.customer_reach) == ("accepted", "reached")
    other, other_lease = L.start()
    ledger_record = L.finalize(other, other_lease)
    assert (ledger_record.transport_outcome, ledger_record.customer_reach) == ("not_attempted", "not_applicable")


# ── L2: distinct business identities ─────────────────────────────────────────


def test_distinct_business_identities_stay_distinct_with_equal_payloads(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    payload = {"order": "SO-1"}
    salt = uuid.uuid4().hex[:6]
    components = [("a:b" + salt, "c"), ("a" + salt, "b:c"), ("ab" + salt,), ("a" + salt, "b"), ("a" + salt, "b", "c")]
    keys = [lc.derive_business_key("order_cancel", *parts) for parts in components]
    assert len(set(keys)) == len(keys)
    reservations = [L.reserve(turn, lease, lc.EffectIntent("order_cancel", key, payload)) for key in keys]
    assert all(r.created for r in reservations)
    assert len({r.effect.effect_id for r in reservations}) == len(keys)
    # Identical components reproduce the identical key and the identical effect on a retry.
    again = L.reserve(turn, lease, lc.EffectIntent("order_cancel", lc.derive_business_key("order_cancel", "a:b" + salt, "c"), payload))
    assert not again.created and again.effect.effect_id == reservations[0].effect.effect_id


def test_every_ledger_operation_closes_its_transaction_before_returning(ledgers: Ledgers) -> None:
    L = ledgers
    turn, lease = L.start()
    decision = L.repo.commit_turn_decision(
        tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
        turn_id=turn.turn_id, effect_intents=[L.intent()], delivery_intent=lc.DeliveryIntent("rich", {"b": 1}))
    assert L.open_transactions() == 0
    attempt = L.dispatch(turn, lease, decision.effects[0].effect.effect_id)
    assert L.open_transactions() == 0
    L.result(turn, attempt.attempt_id, "rejected", {"code": 422})
    assert L.open_transactions() == 0
    with pytest.raises(lc.DispatchBlocked):
        L.dispatch(turn, lease, decision.effects[0].effect.effect_id)
    assert L.open_transactions() == 0
    _, receipt = L.send(turn, lease, decision.delivery, ScriptedTransport(("rejected",)))
    L.recover(turn, lease, decision.delivery, ScriptedTransport(("accepted", "wamid.tx")))
    assert L.open_transactions() == 0
    L.summary(turn)
    L.finalize(turn, lease)
    assert L.open_transactions() == 0 and receipt.kind == "rejected"
    assert L.engine.pool.checkedout() == 0


# ── Schema-state completion guard: standalone 0108 / partial / complete ─────


@contextlib.contextmanager
def _relation_unavailable(engine, relation: str):
    """Hide one ledger relation behind another name so that its expected name
    resolves to nothing, then restore it. Rows, constraints, indexes and
    triggers survive the rename, so retained work is still there afterwards."""
    hidden = f"{relation}__unavailable"
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE public.{relation} RENAME TO {hidden}"))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE public.{hidden} RENAME TO {relation}"))


def _relation_present(engine, relation: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass(:r) IS NOT NULL"), {"r": f"public.{relation}"}).scalar())


def _assert_completion_refused_without_writes(L: Ledgers, turn: c.AdmittedTurn, lease: c.Lease, before,
                                              *, missing: Tuple[str, ...]) -> None:
    """Both terminal entry points refuse with the exact missing relations; the
    state transition they carried is not applied and no terminal exists."""
    transition = c.StateTransition(expected_revision=before.state_revision, payload={"stage": "partial"})
    with pytest.raises(c.LedgerSchemaIncomplete) as derived:
        L.finalize(turn, lease, state=transition)
    assert derived.value.missing == missing
    assert set(derived.value.present) == set(TABLES) - set(missing)
    with pytest.raises(c.LedgerSchemaIncomplete) as foundation:
        L.repo.foundation.record_terminal(
            tenant_id=L.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable",
            state_transition=transition)
    assert foundation.value.missing == missing
    after = L.snapshot(turn.conversation_id)
    assert dataclasses.replace(after, db_now=before.db_now) == before
    assert after.eligible_turn_id == turn.turn_id
    assert L.terminal(turn) is None and L.open_transactions() == 0


def test_partial_ledger_schema_fails_closed_while_effect_obligations_remain(ledgers: Ledgers) -> None:
    """Delivery parent relation unavailable while a reserved and a dispatching effect remain."""
    L = ledgers
    turn, lease = L.start()
    decision = L.repo.commit_turn_decision(
        tenant_id=L.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token,
        turn_id=turn.turn_id, state_transition=c.StateTransition(expected_revision=0, payload={"decided": True}),
        effect_intents=[L.intent(payload={"order": "SO-41"}), L.intent(payload={"order": "SO-42"})])
    first, second = (e.effect.effect_id for e in decision.effects)
    open_attempt = L.dispatch(turn, lease, first)
    assert (L.effect(turn, first).status, L.effect(turn, second).status) == ("dispatching", "reserved")
    before = L.snapshot(turn.conversation_id)
    with _relation_unavailable(L.engine, lm.DELIVERY_SEQUENCES_TABLE):
        assert not _relation_present(L.engine, lm.DELIVERY_SEQUENCES_TABLE)
        _assert_completion_refused_without_writes(L, turn, lease, before, missing=(lm.DELIVERY_SEQUENCES_TABLE,))
    # Restored: the complete schema applies the ledger-aware rules to the retained obligations ...
    assert _relation_present(L.engine, lm.DELIVERY_SEQUENCES_TABLE)
    with pytest.raises(lc.CompletionBlocked) as blocked:
        L.finalize(turn, lease)
    assert set(blocked.value.blockers) == {"1 effect intent(s) reserved but not dispatched",
                                           "1 effect attempt(s) without an established outcome"}
    # ... and the work completes through the supported lifecycle: outcome, dispatch, outcome, finalize.
    L.result(turn, open_attempt.attempt_id, "confirmed", {"provider_ref": "cancel-41"})
    L.result(turn, L.dispatch(turn, lease, second).attempt_id, "confirmed", {"provider_ref": "cancel-42"})
    record = L.finalize(turn, lease, state=c.StateTransition(expected_revision=1, payload={"stage": "closed"}))
    assert record.details["ledger"]["effects_by_status"] == {"reserved": 0, "dispatching": 0, "confirmed": 2,
                                                             "rejected": 0, "unknown": 0}
    assert (record.transport_outcome, record.customer_reach) == ("not_attempted", "not_applicable")
    assert L.snapshot(turn.conversation_id).state_revision == 2 and L.open_transactions() == 0


def test_partial_ledger_schema_fails_closed_while_delivery_obligations_remain(ledgers: Ledgers) -> None:
    """Effects parent relation unavailable while the reserved delivery sequence remains."""
    L = ledgers
    turn, lease = L.start()
    seq = L.delivery(turn, lease)
    assert (seq.attempt_count, seq.outcome) == (0, "pending")
    before = L.snapshot(turn.conversation_id)
    with _relation_unavailable(L.engine, lm.EFFECTS_TABLE):
        assert not _relation_present(L.engine, lm.EFFECTS_TABLE)
        _assert_completion_refused_without_writes(L, turn, lease, before, missing=(lm.EFFECTS_TABLE,))
    # A missing child relation is partial as well: complete means every ledger relation.
    with _relation_unavailable(L.engine, lm.DELIVERY_RECEIPTS_TABLE):
        _assert_completion_refused_without_writes(L, turn, lease, before, missing=(lm.DELIVERY_RECEIPTS_TABLE,))
    assert all(_relation_present(L.engine, t) for t in TABLES)
    with pytest.raises(lc.CompletionBlocked) as blocked:
        L.finalize(turn, lease)
    assert blocked.value.blockers == ("delivery intent reserved but not dispatched",)
    _, receipt = L.send(turn, lease, seq, ScriptedTransport(("accepted", "wamid.partial-1")))
    assert receipt.kind == "accepted"
    record = L.finalize(turn, lease)
    assert (record.transport_outcome, record.customer_reach) == ("accepted", "unknown")
    assert record.details["ledger"]["delivery_outcome"] == "accepted" and L.open_transactions() == 0


def test_standalone_0108_schema_keeps_the_foundation_terminal_available(pg_admin_dsn: str) -> None:
    """Positive control: a database with no ledger relation at all is the standalone
    foundation schema, not a partial one; the foundation terminal path is unchanged."""
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, FOUNDATION_REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        assert _current_revisions(engine) == {FOUNDATION_REVISION}
        assert not any(_relation_present(engine, t) for t in TABLES)
        foundation = CommerceRuntimeRepository(engine)
        tenant = _seed_tenant(engine, "S")
        turn = foundation.admit_turn(tenant_id=tenant, namespace=LIVE, conversation_ref=_ref(),
                                     channel_connection_ref=CHANNEL, provider_message_id=_pmid(),
                                     payload={"kind": "text"})
        lease = foundation.claim(tenant_id=tenant, namespace=LIVE, conversation_id=turn.conversation_id,
                                 owner_id=WORKER_A, lease_seconds=60)
        record = foundation.record_terminal(
            tenant_id=tenant, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="accepted", customer_reach="reached",
            state_transition=c.StateTransition(expected_revision=0, payload={"stage": "closed"}))
        assert (record.transport_outcome, record.customer_reach) == ("accepted", "reached")
        snapshot = foundation.get_conversation(tenant_id=tenant, namespace=LIVE, conversation_id=turn.conversation_id)
        assert (snapshot.state_revision, snapshot.eligible_turn_id) == (1, None)
        assert foundation.get_terminal(tenant_id=tenant, namespace=LIVE, turn_id=turn.turn_id) == record
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


# ── Reservation / completion race on independent connections ────────────────


def _wait_for_lock_waiters(engine, expected: int) -> None:
    """Return once exactly ``expected`` other backends of this database wait on
    a lock. Synchronisation is on observed backend state, never on elapsed
    time; the deadline only turns a hang into a failure."""
    deadline = time.monotonic() + 30
    with engine.connect() as conn:
        while True:
            waiting = int(conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'")).scalar())
            if waiting == expected:
                return
            assert time.monotonic() < deadline, f"expected {expected} lock waiter(s), observed {waiting}"
            time.sleep(0.01)


def _race_reservation_against_completion(L: Ledgers, turn: c.AdmittedTurn, lease: c.Lease, *,
                                         first: str) -> Dict[str, Any]:
    """Queue a reservation and a completion, on independent connections, behind
    a held conversation row lock in the given arrival order. Each contender is
    observed waiting before the next one starts, and PostgreSQL grants the row
    lock to the waiters in arrival order once the holder commits."""
    outcomes: Dict[str, Any] = {}

    def reservation() -> None:
        try:
            outcomes["reservation"] = L.reserve(turn, lease, L.intent(payload={"order": "SO-race"}))
        except c.CommerceRuntimeError as exc:
            outcomes["reservation"] = exc

    def completion() -> None:
        try:
            outcomes["completion"] = L.finalize(turn, lease)
        except c.CommerceRuntimeError as exc:
            outcomes["completion"] = exc

    contenders = {"reservation": reservation, "completion": completion}
    order = [first, "completion" if first == "reservation" else "reservation"]
    holder = L.engine.connect()
    threads: List[threading.Thread] = []
    try:
        holder_tx = holder.begin()
        holder.execute(text("SELECT id FROM commerce_runtime_conversations WHERE id = :id FOR UPDATE"),
                       {"id": turn.conversation_id})
        for arrived, name in enumerate(order, start=1):
            thread = threading.Thread(target=contenders[name], name=name, daemon=True)
            thread.start()
            threads.append(thread)
            _wait_for_lock_waiters(L.engine, expected=arrived)
        holder_tx.commit()
    finally:
        holder.close()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), f"{thread.name} did not finish"
    assert set(outcomes) == {"reservation", "completion"}, outcomes
    return outcomes


def test_reservation_and_completion_race_in_both_lock_orders(ledgers: Ledgers) -> None:
    L = ledgers
    # Reservation first in the lock queue: completion then sees the new obligation and is refused.
    turn, lease = L.start()
    outcomes = _race_reservation_against_completion(L, turn, lease, first="reservation")
    reservation, completion = outcomes["reservation"], outcomes["completion"]
    assert isinstance(reservation, lc.EffectReservation) and reservation.created, reservation
    assert isinstance(completion, lc.CompletionBlocked), completion
    assert completion.reason == "actionable_work_remains"
    assert completion.blockers == ("1 effect intent(s) reserved but not dispatched",)
    assert L.terminal(turn) is None and L.snapshot(turn.conversation_id).eligible_turn_id == turn.turn_id
    assert L.effect(turn, reservation.effect.effect_id).status == "reserved"
    L.result(turn, L.dispatch(turn, lease, reservation.effect.effect_id).attempt_id, "confirmed",
             {"provider_ref": "race-1"})
    assert L.finalize(turn, lease).details["ledger"]["effects_by_status"]["confirmed"] == 1
    # Completion first in the lock queue: the waiting reservation finds a completed turn and inserts nothing.
    turn, lease = L.start()
    outcomes = _race_reservation_against_completion(L, turn, lease, first="completion")
    reservation, completion = outcomes["reservation"], outcomes["completion"]
    assert isinstance(completion, c.TerminalRecord), completion
    assert (completion.transport_outcome, completion.customer_reach) == ("not_attempted", "not_applicable")
    assert isinstance(reservation, c.OwnershipRejected), reservation
    assert reservation.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    summary = L.summary(turn)
    assert sum(summary.effects_by_status.values()) == 0 and summary.delivery_outcome == "not_attempted"
    assert L.terminal(turn) == completion and L.snapshot(turn.conversation_id).eligible_turn_id is None
    assert L.open_transactions() == 0
