"""The two controls the closure review reproduced, run against the real thing.

Both are about the same claim: that a handover which reports *settled* really
has nothing outstanding, and that getting there never costs the customer a
second answer. Each is written in two arms — the shape the review reproduced,
and the same sequence on the corrected implementation — so the difference is
the code and not the test.

**Control A — a previously selected invocation admitting after a zero-count
settlement check.** An invocation selects the runtime, the operator drains and
settles while that invocation is still between selection and admission, and the
invocation then writes a turn the settlement snapshot recorded as absent. The
correction reads the barrier on the admission transaction's own connection,
under a tenant-scoped advisory lock, so the database orders the two and there is
no third ordering to fall through.

**Control B — a new legacy response while an abandoned runtime send
subsequently completes.** A send is outstanding with no receipt when the next
inbound arrives. Releasing it to another owner answers a conversation whose
first answer may still be on its way. The correction withholds it from every
owner, records it for disposition, and keeps the handover blocked until the
send's fate is established rather than assumed.

Real PostgreSQL, the repository's own migration chain, the real admission path,
the real ledger and the real operator job. The Anthropic HTTP call and the
WhatsApp transport are scripted and named as such; nothing here sends anything.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
import uuid
from unittest.mock import patch
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import contracts as c
from core.commerce_runtime import conversation_link as cl
from core.commerce_runtime import delivery_dispatch as dd
from core.commerce_runtime import handover
from core.commerce_runtime import pilot_guard as pgd
from core.commerce_runtime import recovery
from core.commerce_runtime import runtime_entry as entry
from core.commerce_runtime.ledgers import LedgerRepository
from scripts.operators import commerce_runtime_pilot_handover as job
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
)
from tests.commerce_reliability.test_commerce_runtime_pilot_pg import (
    MODEL,
    ScriptedAnthropic,
    Transport,
    accepted,
    reply,
    step,
    timed_out,
)

from tests.commerce_reliability.runtime_support import stop_record_for  # noqa: E402

REVISION = "0111"
PHONE = "+966500000042"
# Meta phone-number ids are globally unique; the route drops an id that
# resolves to more than one connection as ambiguous. Every merchant in this
# module therefore gets a phone id of its own, built on this prefix.
PHONE_ID_PREFIX = "1555"
QUESTION = "عندكم قميص قطني أزرق؟"


@dataclasses.dataclass
class Store:
    """One merchant with everything an inbound turn needs, and nothing else."""

    engine: Any
    session_factory: Any
    ledgers: LedgerRepository
    tenant_id: int
    connection_id: int
    phone_id: str
    customer_id: int
    conversation_id: int

    # ── the things a case drives ──────────────────────────────────────────

    def session(self) -> Any:
        return self.session_factory()

    def state(self) -> Any:
        return recovery.handover_state(tenant_ids=[self.tenant_id], engine=self.engine)[0]

    def turns(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(
                text("SELECT count(*) FROM commerce_runtime_turns WHERE tenant_id = :t"),
                {"t": self.tenant_id}).scalar() or 0)

    def run_turn(self, *, transport: Transport, barrier: Optional[Any] = None,
                 provider_message_id: Optional[str] = None) -> entry.TurnReport:
        return entry.run_commerce_runtime_turn(
            engine=self.engine, session_factory=self.session_factory,
            tenant_id=self.tenant_id, conversation_id=self.conversation_id,
            connection_ref=f"wa:{self.connection_id}", connection_id=str(self.connection_id),
            customer_id=self.customer_id, normalized_customer_phone=PHONE,
            provider_message_id=provider_message_id or ("wamid." + uuid.uuid4().hex),
            inbound_text=QUESTION, inbound_metadata={"source": "handover-control"},
            transport=transport, instructions="EXISTING-INSTRUCTIONS", model=MODEL,
            admission_barrier=barrier,
            budget=ac.LoopBudget(max_steps=3, max_tool_calls=4, tool_timeout_seconds=10.0,
                                 provider_timeout_seconds=15.0, deadline_seconds=45.0),
            anthropic_provider=ScriptedAnthropic([step([reply("تفضل", call_id="r1")])]),
        )

    def production_barrier(self) -> Any:
        """The closure the pilot seam installs, verbatim in behaviour."""
        return lambda conn: handover.admits_new_work_on(conn, tenant_id=self.tenant_id)

    def reserve_reply(self, body: str) -> Any:
        """Admit a turn and reserve one delivery intent, leaving the turn open."""
        from core.commerce_runtime import agent_scripted as sp
        from core.commerce_runtime.agent_loop import AgentLoop
        from tests.commerce_reliability.agent_fixture_catalog import build_registry

        admitted = self.ledgers.foundation.admit_turn(
            tenant_id=self.tenant_id, namespace=entry.NAMESPACE,
            conversation_ref=self.conversation_ref,
            channel_connection_ref=f"wa:{self.connection_id}",
            provider_message_id="wamid." + uuid.uuid4().hex, payload={"text": QUESTION})
        with self.owned(turn_id=admitted.turn_id, owner="reserver") as token:
            outcome = AgentLoop(self.ledgers, build_registry()).run_turn(
                tenant_id=self.tenant_id, namespace=entry.NAMESPACE,
                conversation_id=admitted.conversation_id, turn_id=admitted.turn_id,
                token=token, provider=sp.ScriptedReasoningProvider([sp.reply(body)]))
        assert outcome.status == ac.LoopStatus.PENDING_DELIVERY.value
        return admitted.turn_id, outcome.delivery_sequence_id

    @property
    def conversation_ref(self) -> str:
        return cl.conversation_ref_for(channel=entry.CHANNEL,
                                       app_conversation_id=self.conversation_id)

    @property
    def runtime_conversation_id(self) -> int:
        return self.ledgers.foundation.get_conversation(
            tenant_id=self.tenant_id, namespace=entry.NAMESPACE,
            conversation_ref=self.conversation_ref).conversation_id

    def owned(self, *, turn_id: int, owner: str = "control") -> Any:
        import contextlib

        @contextlib.contextmanager
        def _held() -> Any:
            lease = self.ledgers.foundation.claim(
                tenant_id=self.tenant_id, namespace=entry.NAMESPACE,
                conversation_id=self.runtime_conversation_id, owner_id=owner,
                lease_seconds=60, turn_id=turn_id)
            token = c.OwnershipToken(owner_id=owner, fence=lease.fence, epoch=lease.epoch,
                                     tenant_id=self.tenant_id, namespace=entry.NAMESPACE,
                                     conversation_id=self.runtime_conversation_id)
            try:
                yield token
            finally:
                try:
                    self.ledgers.foundation.release(
                        tenant_id=self.tenant_id, namespace=entry.NAMESPACE,
                        conversation_id=self.runtime_conversation_id, token=token)
                except c.CommerceRuntimeError:
                    pass

        return _held()


# What retiring a worker has to be able to show. Silence is deliberately not in
# it: a quiet worker is one nobody has heard from, which is the case the fleet
# table exists to keep apart from a stopped one.
STOP_EVIDENCE = {
    "deployment": "railway:nahla-backend@deploy-1234",
    "stop_verified_by": "railway deployment status=REMOVED, replicas=0",
    "observed_at": "2099-01-01T00:00:00+00:00",
}
# The platform's own record, structured as the stop-record job writes it: this
# deployment, its incarnation, an inactive state, zero active replicas, observed
# at the moment the retirement claims.
STOP_EVIDENCE["stop_record"] = stop_record_for(
    STOP_EVIDENCE["deployment"], observed_at=STOP_EVIDENCE["observed_at"])
# The deployment inventory every settlement and release is reconciled against.
EXPECTED_WORKERS = "worker-a"


@pytest.fixture(scope="module")
def database(pg_admin_dsn: str) -> Any:
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True, pool_size=10, max_overflow=10)
    try:
        _alembic(dsn, REVISION)
        yield engine
    finally:
        entry.reset_schema_probe()
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


@pytest.fixture()
def store(database: Any) -> Any:
    """A merchant of this case's own, so every count belongs to this case."""
    engine = database
    phone_id = PHONE_ID_PREFIX + str(uuid.uuid4().int)[:11]
    with engine.begin() as conn:
        tenant_id = int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر تجريبي عام {uuid.uuid4().hex[:8]}"}).scalar_one())
        connection_id = int(conn.execute(
            text("INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
                 "VALUES (:t, :p, 'connected') RETURNING id"),
            {"t": tenant_id, "p": phone_id}).scalar_one())
        customer_id = int(conn.execute(
            text("INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
                 "VALUES (:t, :p, :p, :n) RETURNING id"),
            {"t": tenant_id, "p": PHONE, "n": "أحمد سالم"}).scalar_one())
        conversation_id = int(conn.execute(
            text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                 "VALUES (:t, :c, :e, 'active') RETURNING id"),
            {"t": tenant_id, "c": customer_id, "e": PHONE}).scalar_one())
    entry.reset_schema_probe()
    handover._last_heartbeat.clear()
    yield Store(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False),
                ledgers=LedgerRepository(engine), tenant_id=tenant_id,
                connection_id=connection_id, phone_id=phone_id, customer_id=customer_id,
                conversation_id=conversation_id)
    entry.reset_schema_probe()


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    """The pilot, configured for this merchant only, and the operator job bound
    to this database rather than to the process default."""
    monkeypatch.setenv(pgd.ENV_ENABLED, "true")
    monkeypatch.setenv(pgd.ENV_TENANT_ALLOWLIST, str(store.tenant_id))
    monkeypatch.setenv(pgd.ENV_RECIPIENT_ALLOWLIST, PHONE)
    monkeypatch.setenv(pgd.ENV_MODEL, MODEL)
    monkeypatch.delenv(pgd.ENV_DRAINING, raising=False)
    monkeypatch.setenv(job.ENV_EXPECTED_WORKERS, EXPECTED_WORKERS)
    monkeypatch.setattr(job, "session", store.session)
    monkeypatch.setattr(job, "observe", lambda tenants: recovery.handover_state(
        tenant_ids=list(tenants), engine=store.engine))
    monkeypatch.setattr(job, "observe_on", lambda conn, tenants: recovery.handover_state_on(
        conn, tenant_ids=list(tenants)))
    handover._last_heartbeat.clear()


# ── Operator steps, run for real ─────────────────────────────────────────────


def drain_and_converge(store: Store) -> None:
    """The procedure's first two steps: close the barrier, let a worker report."""
    db = store.session()
    try:
        handover.open_drain(db, tenant_id=store.tenant_id)
        observed = handover.read_barrier(db, tenant_id=store.tenant_id)
        handover.note_worker(db, tenant_id=store.tenant_id,
                             observed_generation=observed.generation,
                             observed_state=observed.state, name="worker-a", force=True)
    finally:
        db.close()


def settle(store: Store) -> int:
    return job.main(["settle"])


def evidence(store: Store) -> Dict[str, Any]:
    db = store.session()
    try:
        return dict(handover.read_barrier(db, tenant_id=store.tenant_id).evidence)
    finally:
        db.close()


def barrier_of(store: Store) -> Any:
    db = store.session()
    try:
        return handover.read_barrier(db, tenant_id=store.tenant_id)
    finally:
        db.close()


def pending(store: Store) -> Any:
    db = store.session()
    try:
        return handover.pending_inbound(db, tenant_id=store.tenant_id)
    finally:
        db.close()


def defer(store: Store, *, identity: str,
          reason: str = handover.REASON_DRAIN_BUFFERED) -> Any:
    db = store.session()
    try:
        return handover.record_inbound(
            db, tenant_id=store.tenant_id, phone_number_id=store.phone_id,
            channel_connection_ref=f"wa:{store.connection_id}", recipient=PHONE,
            provider_message_id=identity, payload={"text": QUESTION}, reason=reason,
            barrier_generation=handover.read_barrier(
                db, tenant_id=store.tenant_id).generation)
    finally:
        db.close()


# ═════════════════════════════════════════════════════════════════════════════
# Control A — a previously selected invocation admitting after a zero-count
#             settlement check
# ═════════════════════════════════════════════════════════════════════════════


def test_control_a_the_reviewed_shape_admits_after_settlement_reported_zero(configured, store):
    """The defect, reproduced: an invocation that checked the barrier early.

    This arm asks the barrier the way a check *outside* the admission
    transaction asks it — read it, then admit. The read is honest and the answer
    is correct at the moment it is given; it is simply not ordered against the
    drain. The operator settles on a zero count, and the turn is written
    afterwards.
    """
    selected = store.session()
    try:
        assert handover.barrier_admits_new_work(selected, tenant_id=store.tenant_id) is True
    finally:
        selected.close()

    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK                     # nothing recorded outstanding
    assert evidence(store)["work"]["open_turns"] == 0

    # …and the invocation that had already selected the runtime now admits.
    report = store.run_turn(transport=Transport([accepted("wamid.LATE")]), barrier=None)
    assert report.reason == entry.HANDLED
    assert store.turns() == 1
    # Work that the settlement snapshot recorded as absent exists in the tenant
    # the operator has just been told is safe to switch off.
    assert store.state().as_log_fields()["open_turns"] == 0  # this one completed…
    with store.engine.connect() as conn:
        admitted_after = int(conn.execute(
            text("SELECT count(*) FROM commerce_runtime_turns t "
                 "JOIN commerce_runtime_conversations cv ON cv.id = t.conversation_id "
                 "WHERE cv.tenant_id = :t"), {"t": store.tenant_id}).scalar() or 0)
    assert admitted_after == 1                               # …but it ran after SETTLED


def test_control_a_the_corrected_admission_refuses_and_writes_nothing(configured, store):
    """The same sequence, with the barrier read on the admission connection."""
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK

    transport = Transport([])                                # any send would raise
    report = store.run_turn(transport=transport, barrier=store.production_barrier())

    assert report.reason == entry.HANDOVER_BARRIER
    assert report.turn_id is None and not report.reply_text
    assert transport.sent == []
    assert store.turns() == 0                                # nothing was written
    assert store.state().settled is True


def test_control_a_a_drain_landing_during_admission_is_ordered_not_raced(configured, store):
    """The window itself, driven deterministically in both directions.

    The admitting transaction takes the tenant's advisory lock shared; a drain
    takes it exclusively. So the database decides which happened first, and the
    test only chooses which one to start. Here the admission gets there first:
    the drain must wait, and the turn it waits for is therefore visible to
    everything the drain does afterwards.
    """
    inside = threading.Event()
    release = threading.Event()
    drained = threading.Event()

    def barrier(conn: Any) -> bool:
        allowed = handover.admits_new_work_on(conn, tenant_id=store.tenant_id)
        inside.set()                     # the shared lock is held from here
        release.wait(20)
        return allowed

    outcome: Dict[str, Any] = {}

    def admit() -> None:
        outcome["report"] = store.run_turn(
            transport=Transport([accepted("wamid.FIRST")]), barrier=barrier)

    def drain() -> None:
        db = store.session()
        try:
            handover.open_drain(db, tenant_id=store.tenant_id)
            drained.set()
        finally:
            db.close()

    admitter = threading.Thread(target=admit)
    admitter.start()
    assert inside.wait(20), "the admission never reached the barrier"
    drainer = threading.Thread(target=drain)
    drainer.start()
    try:
        # The drain cannot commit while the admission holds the shared lock.
        assert not drained.wait(1.5)
    finally:
        release.set()
        admitter.join(30)
        drainer.join(30)

    assert drained.is_set()
    assert outcome["report"].reason == entry.HANDLED         # admitted, before the drain
    # And the drain — and every count taken after it — sees that turn.
    with store.engine.connect() as conn:
        assert int(conn.execute(
            text("SELECT count(*) FROM commerce_runtime_turns WHERE tenant_id = :t"),
            {"t": store.tenant_id}).scalar() or 0) == 1


def test_control_a_a_drain_that_committed_first_refuses_the_admission(configured, store):
    """The other ordering, and the one the review's shape got wrong."""
    drain_and_converge(store)
    report = store.run_turn(transport=Transport([]), barrier=store.production_barrier())
    assert report.reason == entry.HANDOVER_BARRIER
    assert store.turns() == 0


def test_control_a_the_barrier_never_blocks_finishing_a_turn_already_admitted(configured, store):
    """A drain stops new work. Refusing a re-entry would abandon the old work."""
    provider_message_id = "wamid." + uuid.uuid4().hex
    first = store.run_turn(transport=Transport([accepted("wamid.ONE")]),
                           barrier=store.production_barrier(),
                           provider_message_id=provider_message_id)
    assert first.reason == entry.HANDLED

    drain_and_converge(store)
    again = store.run_turn(transport=Transport([]), barrier=store.production_barrier(),
                           provider_message_id=provider_message_id)
    # Re-entry reaches the turn it already owns rather than the barrier, and the
    # ledger — not the barrier — decides that nothing more is to be sent.
    assert again.reason != entry.HANDOVER_BARRIER
    assert store.turns() == 1


def test_control_a_an_unreadable_barrier_refuses_rather_than_admits(configured, store):
    """Fail closed: not knowing is not permission to start new work."""
    def barrier(_conn: Any) -> bool:
        raise RuntimeError("the barrier could not be read")

    def guarded(conn: Any) -> bool:
        try:
            return barrier(conn)
        except Exception:                                    # noqa: BLE001
            return handover.admits_new_work_on(object(), tenant_id=store.tenant_id)

    report = store.run_turn(transport=Transport([]), barrier=guarded)
    assert report.reason == entry.HANDOVER_BARRIER
    assert store.turns() == 0


# ═════════════════════════════════════════════════════════════════════════════
# Control B — a new legacy response while an abandoned runtime send
#             subsequently completes
# ═════════════════════════════════════════════════════════════════════════════


def abandoned_send(store: Store) -> Dict[str, Any]:
    """A send on the wire that nobody is waiting for any more.

    The dispatching invocation is gone; the attempt is recorded and no receipt
    exists, which is the state a handover has to reason about honestly.
    """
    turn_id, sequence_id = store.reserve_reply("ردّ معلّق")
    started = threading.Event()
    finish = threading.Event()
    result: Dict[str, Any] = {}

    def slow(payload: Any) -> Any:
        started.set()
        finish.wait(30)
        return accepted("wamid.LATE")

    def dispatch() -> None:
        with store.owned(turn_id=turn_id, owner="abandoned") as token:
            result["outcome"] = dd.dispatch_reserved_delivery(
                ledgers=store.ledgers, tenant_id=store.tenant_id, namespace=entry.NAMESPACE,
                conversation_id=store.runtime_conversation_id, token=token,
                sequence_id=sequence_id, transport=slow, recorded_by="abandoned")

    worker = threading.Thread(target=dispatch)
    worker.start()
    assert started.wait(20), "the send never reached the transport"
    return {"turn_id": turn_id, "sequence_id": sequence_id, "worker": worker,
            "finish": finish, "result": result}


def test_control_b_the_reviewed_shape_still_says_the_legacy_path_owns_it(configured, store,
                                                                         monkeypatch):
    """The refusal the review followed, shown at the layer that produced it.

    With draining set, the route guard refuses and ``legacy_owns_turn`` is true.
    That answer, taken at face value, is the second answer: it invites another
    owner into a conversation whose first reply is still on the wire.
    """
    outstanding = abandoned_send(store)
    try:
        assert store.state().unresolved_attempts == 1        # a send with no receipt
        monkeypatch.setenv(pgd.ENV_DRAINING, "true")
        db = store.session()
        try:
            decision = pgd.evaluate_pilot_route(
                db, tenant_id=store.tenant_id, customer_phone=PHONE,
                phone_number_id=store.phone_id, inbound_text="وين ردّي؟")
        finally:
            db.close()
        assert decision.reason == pgd.PILOT_DRAINING
        assert decision.legacy_owns_turn is True             # the reviewed shape
    finally:
        outstanding["finish"].set()
        outstanding["worker"].join(30)


def test_control_b_the_corrected_route_withholds_it_from_every_owner(configured, store,
                                                                     monkeypatch):
    """The same inbound, on the corrected routing: claimed, buffered, unanswered.

    And the send it was protecting completes afterwards, which is the whole
    reason the conversation could not be released: there was an answer coming.
    """
    from services import commerce_runtime_pilot as seam

    outstanding = abandoned_send(store)
    monkeypatch.setenv(pgd.ENV_DRAINING, "true")
    db = store.session()
    try:
        claim = seam.commerce_runtime_claims_inbound(
            db, tenant_id=store.tenant_id, phone_id=store.phone_id, to=PHONE,
            text="وين ردّي؟", wa_msg_id="wamid.during.handover")
        assert claim is not None and claim.basis == "drain_buffered"
        assert claim.applies_to(tenant_id=store.tenant_id, recipient=PHONE,
                                provider_message_id="wamid.during.handover")
        deferred = handover.pending_inbound(db, tenant_id=store.tenant_id)
        assert [e.provider_message_id for e in deferred] == ["wamid.during.handover"]
        assert deferred[0].payload == {"text": "وين ردّي؟"}

        # Settlement is blocked, and says so by name, while the send is out.
        assert settle(store) == job.EXIT_BLOCKED

        outstanding["finish"].set()
        outstanding["worker"].join(30)
        assert outstanding["result"]["outcome"].status == dd.SENT_ACCEPTED
        assert outstanding["result"]["outcome"].provider_message_id == "wamid.LATE"
    finally:
        outstanding["finish"].set()
        outstanding["worker"].join(30)
        db.close()

    # Exactly one answer reached the customer: the one the runtime had already
    # put on the wire before the handover began.
    assert store.state().unresolved_attempts == 0


def test_control_b_the_handover_stays_blocked_until_the_send_is_established(configured, store):
    """Neither elapsed time nor a cancelled wait resolves an outstanding send."""
    outstanding = abandoned_send(store)
    try:
        drain_and_converge(store)
        assert settle(store) == job.EXIT_BLOCKED
        assert evidence(store) == {}                         # nothing was recorded as settled
        # A second attempt, later, with the send still out: the same refusal.
        time.sleep(0.2)
        assert settle(store) == job.EXIT_BLOCKED
    finally:
        outstanding["finish"].set()
        outstanding["worker"].join(30)

    # Once the transport's own answer is recorded the turn can be completed and
    # the tenant settles — on evidence, not on the wait having ended.
    with store.owned(turn_id=outstanding["turn_id"]) as token:
        dd.complete_turn(ledgers=store.ledgers, tenant_id=store.tenant_id,
                         namespace=entry.NAMESPACE, turn_id=outstanding["turn_id"],
                         token=token,
                         processing_outcome=c.ProcessingOutcome.COMPLETED.value)
    assert store.state().settled is True
    assert settle(store) == job.EXIT_OK


def test_control_b_a_buffered_inbound_is_disposed_before_anything_settles(configured, store,
                                                                          monkeypatch):
    """Buffering is only honest if nothing is acknowledged and quietly dropped."""
    from services import commerce_runtime_pilot as seam

    drain_and_converge(store)
    db = store.session()
    try:
        claim = seam.commerce_runtime_claims_inbound(
            db, tenant_id=store.tenant_id, phone_id=store.phone_id, to=PHONE,
            text="سؤال أثناء التسليم", wa_msg_id="wamid.buffered.one")
        assert claim is not None and claim.basis == "drain_buffered"
        db.commit()
    finally:
        db.close()

    assert settle(store) == job.EXIT_BLOCKED
    entry = pending(store)[0]
    # A free-text note is not proof of a replay. ``not_required`` is the one
    # disposition that claims no delivery, and it carries its authorisation.
    assert job.main(["dispose", "--entry", str(entry.id), "--disposition", "replayed",
                     "--evidence", '{"replayed_at": "2026-09-20T00:00:00Z"}',
                     "--by", "owner"]) == job.EXIT_BLOCKED
    assert job.main(["dispose", "--entry", str(entry.id), "--disposition", "not_required",
                     "--evidence", '{"authorized_by": "owner", "why": "answered by hand from the operator console"}',
                     "--by", "owner"]) == job.EXIT_OK
    assert settle(store) == job.EXIT_OK

    recorded = evidence(store)
    assert recorded["work"]["deferred_pending"] == 0
    assert recorded["convergence"]["converged"] is True


def test_control_b_the_evidence_is_written_before_ingress_reopens(configured, store):
    """What the decision rested on has to outlive the state it describes."""
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    settled = evidence(store)
    assert settled["work"]["settled"] is True

    assert job.main(["reopen"]) == job.EXIT_OK
    db = store.session()
    try:
        barrier = handover.read_barrier(db, tenant_id=store.tenant_id)
    finally:
        db.close()
    assert barrier.state == handover.STATE_OPEN
    assert barrier.evidence == settled                       # kept, not overwritten
    assert barrier.admits_new_work is True


def test_control_b_reopening_before_settlement_is_refused(configured, store):
    drain_and_converge(store)
    assert job.main(["reopen"]) == job.EXIT_USAGE
    db = store.session()
    try:
        assert handover.read_barrier(db, tenant_id=store.tenant_id).draining is True
    finally:
        db.close()


def test_control_b_a_drain_leaves_another_merchant_completely_alone(configured, store, database,
                                                                    monkeypatch):
    """Platform-wide: one merchant's handover is not another merchant's outage."""
    from services import commerce_runtime_pilot as seam

    with database.begin() as conn:
        other = int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر عطور {uuid.uuid4().hex[:8]}"}).scalar_one())
        conn.execute(
            text("INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
                 "VALUES (:t, :p, 'connected')"),
            {"t": other, "p": store.phone_id + "9"})

    drain_and_converge(store)
    monkeypatch.setenv(pgd.ENV_TENANT_ALLOWLIST, f"{store.tenant_id},{other}")
    db = store.session()
    try:
        assert handover.read_barrier(db, tenant_id=other).admits_new_work is True
        claim = seam.commerce_runtime_claims_inbound(
            db, tenant_id=other, phone_id=store.phone_id + "9", to=PHONE,
            text="عطر ورد 100ml موجود؟", wa_msg_id="wamid.other.tenant")
        assert claim is not None and claim.basis == "configured"
        assert handover.pending_inbound(db, tenant_id=other) == ()
    finally:
        db.close()


def test_control_b_settlement_reports_every_blocker_it_found_by_name(configured, store):
    """A refusal an operator cannot act on is a refusal that gets overridden."""
    outstanding = abandoned_send(store)
    try:
        entries = job.inspect(store.session(), [store.tenant_id])
        reasons = job.blockers_for(entries[0])
    finally:
        outstanding["finish"].set()
        outstanding["worker"].join(30)
    assert f"barrier_is_{handover.STATE_OPEN}_not_draining" in reasons
    assert "no_worker_has_reported_this_generation" in reasons
    assert "open_turns=1" in reasons
    assert "unresolved_attempts=1" in reasons


def test_control_b_a_send_recorded_unknown_never_ages_out_of_the_blockers(configured, store):
    """An unknown outcome is not evidence of non-delivery, however long it sits."""
    from core.commerce_runtime import ledger_contracts as lc

    turn_id, sequence_id = store.reserve_reply("مصير غير معروف")
    with store.owned(turn_id=turn_id) as token:
        outcome = dd.dispatch_reserved_delivery(
            ledgers=store.ledgers, tenant_id=store.tenant_id, namespace=entry.NAMESPACE,
            conversation_id=store.runtime_conversation_id, token=token,
            sequence_id=sequence_id, transport=Transport([timed_out()]),
            recorded_by="control")
        assert outcome.status == dd.SENT_UNKNOWN
        dd.complete_turn(ledgers=store.ledgers, tenant_id=store.tenant_id,
                         namespace=entry.NAMESPACE, turn_id=turn_id, token=token,
                         processing_outcome=outcome.processing_outcome)

    drain_and_converge(store)
    assert settle(store) == job.EXIT_BLOCKED
    entries = job.inspect(store.session(), [store.tenant_id])
    assert "unknown_outcomes=1" in job.blockers_for(entries[0])

    # Established by the operator against the provider, and only then resolved.
    store.ledgers.record_delivery_receipt(
        tenant_id=store.tenant_id, namespace=entry.NAMESPACE,
        conversation_id=store.runtime_conversation_id, attempt_id=outcome.attempt_id,
        kind=lc.ReceiptKind.ACCEPTED, provider_message_id="wamid.ESTABLISHED",
        evidence={"established_by": "operator"}, recorded_by="operator")
    assert settle(store) == job.EXIT_OK


__all__: List[str] = []


# ═════════════════════════════════════════════════════════════════════════════
# H1 — the transition is validated where it is written
# ═════════════════════════════════════════════════════════════════════════════


def test_h1_a_deferred_entry_committing_between_inspection_and_settlement_blocks(
        configured, store, monkeypatch):
    """The reviewed shape settled over it and recorded evidence that was stale.

    Here the inspection happens, a deferred inbound commits, and only then does
    the transition run. It recounts on its own locked session, refuses, and
    writes nothing — so the barrier is still draining and the tenant still owes
    that customer an answer.
    """
    drain_and_converge(store)
    original = job.inspect
    arrived: List[str] = []

    def _inspect_then_arrive(session: Any, tenants: Any) -> Any:
        entries = original(session, tenants)
        if not arrived:
            arrived.append("wamid.between")
            assert defer(store, identity="wamid.between") is not None
        return entries

    monkeypatch.setattr(job, "inspect", _inspect_then_arrive)
    assert settle(store) == job.EXIT_BLOCKED
    assert barrier_of(store).draining is True
    assert evidence(store) == {}
    assert [e.provider_message_id for e in pending(store)] == ["wamid.between"]


def test_h1_a_settlement_whose_generation_moved_writes_nothing(configured, store):
    """The generation the operator decided on is re-checked under the lock."""
    drain_and_converge(store)
    decided = barrier_of(store).generation
    db = store.session()
    try:
        handover.open_drain(db, tenant_id=store.tenant_id)      # somebody re-drained
        outcome = handover.settle(db, tenant_id=store.tenant_id,
                                  expected_generation=decided,
                                  validate=lambda _s, _b: ([], {"note": "would have settled"}))
    finally:
        db.close()
    assert outcome.settled is False
    assert any(reason.startswith("generation_moved") for reason in outcome.blockers)
    assert barrier_of(store).draining is True and evidence(store) == {}


def test_h1_the_evidence_is_the_state_that_was_settled(configured, store):
    drain_and_converge(store)
    generation = barrier_of(store).generation
    assert settle(store) == job.EXIT_OK
    recorded = evidence(store)
    assert recorded["settled_generation"] == generation
    assert recorded["work"]["settled"] is True and recorded["work"]["deferred_pending"] == 0
    assert recorded["convergence"]["converged"] is True
    assert recorded["convergence"]["on_generation"] == ["worker-a"]


def test_h1_a_drain_between_the_reopen_precheck_and_the_mutation_survives(
        configured, store, monkeypatch):
    """The second reproduction: reopening is decided where it is applied."""
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    settled_generation = barrier_of(store).generation

    real_reopen = handover.reopen
    started: List[str] = []

    def _drain_then_reopen(session: Any, **kwargs: Any) -> Any:
        if not started:
            started.append("x")
            db = store.session()
            try:
                handover.open_drain(db, tenant_id=store.tenant_id)
            finally:
                db.close()
        return real_reopen(session, **kwargs)

    monkeypatch.setattr(handover, "reopen", _drain_then_reopen)
    assert job.main(["reopen"]) == job.EXIT_BLOCKED
    current = barrier_of(store)
    assert current.draining is True                       # the fresh drain was not reopened over
    assert current.generation > settled_generation


def test_h1_two_settlements_racing_produce_exactly_one_transition(configured, store):
    """The advisory lock orders them; the loser sees the generation has moved."""
    drain_and_converge(store)
    decided = barrier_of(store).generation
    outcomes: List[Any] = []
    lock = threading.Lock()

    def attempt() -> None:
        db = store.session()
        try:
            result = handover.settle(db, tenant_id=store.tenant_id,
                                     expected_generation=decided,
                                     validate=lambda _s, _b: ([], {}),
                                     expected_workers=[EXPECTED_WORKERS])
        finally:
            db.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(o.settled for o in outcomes) == [False, True]
    assert barrier_of(store).state == handover.STATE_SETTLED


# ═════════════════════════════════════════════════════════════════════════════
# H4 / H5 — identity, evidence and durable acceptance, on the real relations
# ═════════════════════════════════════════════════════════════════════════════


def test_h4_disposition_is_per_entry_and_leaves_a_later_arrival_alone(configured, store):
    drain_and_converge(store)
    seen = defer(store, identity="wamid.inspected")
    later = defer(store, identity="wamid.after.inspection")
    assert job.main(["dispose", "--entry", str(seen.id), "--disposition", "not_required",
                     "--evidence", '{"authorized_by": "owner", "why": "answered by hand from the operator console"}',
                     "--by", "owner"]) == job.EXIT_OK
    assert [e.id for e in pending(store)] == [later.id]
    assert settle(store) == job.EXIT_BLOCKED


def test_h5_an_unresolved_accepted_record_blocks_settlement(configured, store):
    """A message the provider was told we had, and nobody has answered."""
    drain_and_converge(store)
    record = defer(store, identity="wamid.accepted", reason=handover.REASON_ACCEPTED)
    assert record is not None
    assert store.state().deferred_pending == 1
    assert settle(store) == job.EXIT_BLOCKED
    assert "deferred_pending=1" in job.blockers_for(job.inspect(store.session(),
                                                                [store.tenant_id])[0])


def test_h5_a_record_the_runtime_finished_stops_counting(configured, store):
    """A record stops counting when the runtime's own turn for it reached a
    terminal that is an accepted reply — and only then: with no turn, the same
    call refuses and the record keeps counting."""
    drain_and_converge(store)
    defer(store, identity="wamid.finished", reason=handover.REASON_ACCEPTED)
    db = store.session()
    try:
        assert handover.resolve_inbound(
            db, tenant_id=store.tenant_id,
            channel_connection_ref=f"wa:{store.connection_id}",
            provider_message_id="wamid.finished") is False        # nothing answered it yet
        assert store.state().deferred_pending == 1
        report = store.run_turn(transport=Transport([accepted("wamid.out.h5")]),
                                provider_message_id="wamid.finished")
        assert report.reason == entry.HANDLED
        assert handover.resolve_inbound(
            db, tenant_id=store.tenant_id,
            channel_connection_ref=f"wa:{store.connection_id}",
            provider_message_id="wamid.finished") is True
    finally:
        db.close()
    assert store.state().deferred_pending == 0
    assert settle(store) == job.EXIT_OK


def test_h5_an_inbound_arriving_in_the_settled_window_is_recorded_not_lost(configured, store,
                                                                           monkeypatch):
    """Between settlement and reopening, new work is still somebody's problem."""
    from services import commerce_runtime_pilot as seam

    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    assert barrier_of(store).settled is True

    db = store.session()
    try:
        claim = seam.commerce_runtime_claims_inbound(
            db, tenant_id=store.tenant_id, phone_id=store.phone_id, to=PHONE,
            text="سؤال بعد التسوية", wa_msg_id="wamid.settled.window")
        db.commit()
    finally:
        db.close()
    assert claim is not None and claim.basis == "drain_buffered"
    entry = pending(store)[0]
    assert entry.provider_message_id == "wamid.settled.window"
    assert entry.reason == handover.REASON_SETTLED_WINDOW
    # It is not lost, and it is not answered: the operator disposes of it or the
    # reopened runtime replays it from this record.
    assert entry.payload == {"text": "سؤال بعد التسوية"}


def test_h3_a_worker_that_observed_the_open_barrier_is_behind_after_a_drain(configured, store):
    """The heartbeat reaches the database after the drain, and still says OPEN."""
    db = store.session()
    try:
        observed = handover.read_barrier(db, tenant_id=store.tenant_id)
        handover.open_drain(db, tenant_id=store.tenant_id)
        handover.note_worker(db, tenant_id=store.tenant_id,
                             observed_generation=observed.generation,
                             observed_state=observed.state, name="worker-late", force=True)
        workers = handover.fleet(db, tenant_id=store.tenant_id)
        result = handover.convergence(handover.read_barrier(db, tenant_id=store.tenant_id),
                                      workers)
    finally:
        db.close()
    assert workers[0].observed_state == handover.STATE_OPEN
    assert result["converged"] is False and result["behind"] == ["worker-late"]
    assert settle(store) == job.EXIT_BLOCKED


def test_h3_retirement_is_recorded_and_carried_into_the_evidence(configured, store):
    db = store.session()
    try:
        handover.note_worker(db, tenant_id=store.tenant_id, observed_generation=0,
                             observed_state=handover.STATE_OPEN, name="worker-gone",
                             force=True)
    finally:
        db.close()
    drain_and_converge(store)
    assert settle(store) == job.EXIT_BLOCKED              # worker-gone is behind

    # A name and a sentence are not evidence a process stopped.
    assert job.main(["retire", "--worker", "worker-gone", "--by", "owner",
                     "--reason", "terminated in deploy 1234"]) == job.EXIT_USAGE
    assert job.main(["retire", "--worker", "worker-gone", "--by", "owner",
                     "--reason", "terminated in deploy 1234",
                     "--evidence", json.dumps(STOP_EVIDENCE)]) == job.EXIT_OK
    assert settle(store) == job.EXIT_OK
    retired = evidence(store)["retired_workers"]
    assert [(r["worker_id"], r["by"], r["reason"]) for r in retired] == [
        ("worker-gone", "owner", "terminated in deploy 1234")]
    # ...and what the operator showed, retained with its digest.
    assert retired[0]["evidence"]["deployment"] == STOP_EVIDENCE["deployment"]
    assert retired[0]["evidence"]["stop_record_sha256"]
    assert retired[0]["evidence"]["stop_record"] == STOP_EVIDENCE["stop_record"]


# ═════════════════════════════════════════════════════════════════════════════
# A disposition that claims the customer was handled is checked against the
# records that would show it
# ═════════════════════════════════════════════════════════════════════════════


def test_a_replayed_disposition_is_verified_against_a_real_terminal(configured, store):
    """``replayed`` names the identity that carried the replay, and that
    identity has to resolve to a runtime turn with a terminal for this tenant
    and this connection. A plausible-looking id that does not is refused."""
    drain_and_converge(store)
    identity = "wamid.needs.proof." + uuid.uuid4().hex
    entry_row = defer(store, identity=identity)

    assert job.main(["dispose", "--entry", str(entry_row.id), "--disposition", "replayed",
                     "--evidence", json.dumps({"replayed_as_provider_message_id": identity}),
                     "--by", "owner"]) == job.EXIT_BLOCKED          # nothing ran yet

    # A turn for *another* inbound that really ran and really reached a
    # terminal proves nothing about this one: a replay is of this entry's own
    # identity, and naming a different message is refused by name.
    other_id = "wamid.other." + uuid.uuid4().hex
    assert store.run_turn(transport=Transport([accepted("wamid.out.0")]),
                          provider_message_id=other_id).reason == entry.HANDLED
    disposed = handover.dispose_inbound(
        store.session(), tenant_id=store.tenant_id, entry_ids=[entry_row.id],
        disposition="replayed", evidence={"replayed_as_provider_message_id": other_id},
        by="owner")
    assert disposed.refused == {entry_row.id: "replayed_identity_is_not_this_inbound"}

    # The replay of this entry, under this entry's identity, reaching a terminal.
    report = store.run_turn(transport=Transport([accepted("wamid.out.1")]),
                            provider_message_id=identity)
    assert report.reason == entry.HANDLED
    found = recovery.admitted_turn_for(tenant_id=store.tenant_id,
                                       phone_number_id=str(store.connection_id),
                                       provider_message_id=identity,
                                       engine=store.engine)
    assert found is not None and found.finished is True

    assert job.main(["dispose", "--entry", str(entry_row.id), "--disposition", "replayed",
                     "--evidence", json.dumps({"replayed_as_provider_message_id": identity}),
                     "--by", "owner"]) == job.EXIT_OK
    db = store.session()
    try:
        row = handover.accepted_inbound(
            db, tenant_id=store.tenant_id,
            channel_connection_ref=f"wa:{store.connection_id}",
            provider_message_id=identity)
    finally:
        db.close()
    assert row is not None and row.disposition == "replayed"
    assert row.disposition_evidence["verified"]["turn_id"] == found.turn_id
    assert row.disposition_evidence["verified"]["verified_against"] == \
        "commerce_runtime_turn_terminals"


def test_a_turn_without_a_terminal_cannot_evidence_a_replay(configured, store):
    """An admitted turn is not a handled one."""
    drain_and_converge(store)
    entry_row = defer(store, identity="wamid.open.turn")
    turn_id, _sequence = store.reserve_reply("نص محجوز")
    assert turn_id
    db = store.session()
    try:
        open_identity = db.execute(text(
            "SELECT provider_message_id FROM commerce_runtime_turns WHERE id = :t"),
            {"t": turn_id}).scalar()
    finally:
        db.close()
    assert job.main(["dispose", "--entry", str(entry_row.id), "--disposition", "replayed",
                     "--evidence", json.dumps(
                         {"replayed_as_provider_message_id": open_identity}),
                     "--by", "owner"]) == job.EXIT_BLOCKED


# ═════════════════════════════════════════════════════════════════════════════
# The release verdict is taken now, not read from a settlement
# ═════════════════════════════════════════════════════════════════════════════


def test_an_inbound_in_the_settled_window_invalidates_the_release(configured, store):
    """The settlement was honest about the instant it was taken.

    Switching the pilot off on the strength of it ten minutes later abandons
    whatever arrived since — and something does arrive: an inbound in the
    settled window is recorded rather than lost. The release verdict is
    therefore derived at the moment it matters, so the arrival blocks it.
    """
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK

    late = defer(store, identity="wamid.settled.window",
                 reason=handover.REASON_SETTLED_WINDOW)
    assert late is not None

    db = store.session()
    try:
        state = handover.release_state(db, tenant_id=store.tenant_id)
    finally:
        db.close()
    assert state.released is False
    assert state.arrived_after_settlement == 1
    # And the transition itself refuses, inside its own transaction, and writes
    # nothing: the barrier is still settled, not released.
    assert job.main(["release"]) == job.EXIT_BLOCKED
    assert barrier_of(store).state == handover.STATE_SETTLED
    assert any(b.startswith("deferred_pending=") for b in state.blockers)
    assert job.main(["release"]) == job.EXIT_BLOCKED


def test_reopening_over_work_nobody_met_is_refused(configured, store):
    """Reopening buries a pending entry under the traffic it lets back in."""
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    late = defer(store, identity="wamid.before.reopen",
                 reason=handover.REASON_SETTLED_WINDOW)
    assert late is not None

    assert job.main(["reopen"]) == job.EXIT_BLOCKED
    assert barrier_of(store).state == handover.STATE_SETTLED

    assert job.main(["dispose", "--entry", str(late.id), "--disposition", "not_required",
                     "--evidence", json.dumps(
                         {"authorized_by": "owner", "why": "answered by hand"}),
                     "--by", "owner"]) == job.EXIT_OK
    assert job.main(["reopen"]) == job.EXIT_OK
    assert barrier_of(store).state == handover.STATE_OPEN


# ═════════════════════════════════════════════════════════════════════════════
# Recovery: an accepted inbound nobody finished, worked by the operator path
# ═════════════════════════════════════════════════════════════════════════════


def test_recovery_closes_an_entry_whose_turn_already_reached_a_terminal(configured, store):
    """Completed work is never repeated; the obligation is closed against it."""
    from services import commerce_runtime_recovery as runner

    identity = "wamid.finished." + uuid.uuid4().hex
    report = store.run_turn(transport=Transport([accepted("wamid.out.2")]),
                            provider_message_id=identity)
    assert report.reason == entry.HANDLED
    entry_row = defer(store, identity=identity, reason=handover.REASON_ACCEPTED)
    assert entry_row is not None

    db = store.session()
    try:
        outcome = runner.recover_tenant(db, tenant_id=store.tenant_id, dry_run=False)
    finally:
        db.close()
    assert outcome.counted() == {runner.RESOLVED_ALREADY_FINISHED: 1}
    assert pending(store) == ()


def test_recovery_refuses_to_replay_into_a_closed_barrier(configured, store):
    """A settled barrier takes nothing back; the run says to drain first.

    Draining is *not* closed to recovery — accepted work is what a drain
    exists to finish (proved end to end below). Settled and released are.
    """
    from services import commerce_runtime_recovery as runner

    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    entry_row = defer(store, identity="wamid.after.settlement",
                      reason=handover.REASON_SETTLED_WINDOW)
    assert entry_row is not None

    db = store.session()
    try:
        outcome = runner.recover_tenant(db, tenant_id=store.tenant_id, dry_run=False)
    finally:
        db.close()
    assert outcome.counted() == {runner.SKIPPED_BARRIER_CLOSED: 1}
    assert [e.id for e in pending(store)] == [entry_row.id]


def test_recovery_leaves_an_unknown_delivery_alone(configured, store):
    """An ``unknown`` outcome is not "did not arrive".

    The case is a worker that sent, could not establish what the provider did
    with it, and stopped before recording a terminal. The turn is therefore
    unfinished *and* carries a send that may already have reached the customer.
    Replaying it risks a second delivery, so the entry is left for an operator
    with the evidence in front of them rather than handed back.
    """
    from services import commerce_runtime_recovery as runner

    # A turn that sent, could not establish what the provider did with it, and
    # was never completed. Terminals are immutable by trigger, so this is built
    # the way it really happens rather than by deleting one afterwards.
    turn_id, sequence_id = store.reserve_reply("مصير غير معروف")
    with store.owned(turn_id=turn_id) as token:
        outcome = dd.dispatch_reserved_delivery(
            ledgers=store.ledgers, tenant_id=store.tenant_id, namespace=entry.NAMESPACE,
            conversation_id=store.runtime_conversation_id, token=token,
            sequence_id=sequence_id, transport=Transport([timed_out()]),
            recorded_by="recovery-control")
    assert outcome.status == dd.SENT_UNKNOWN
    assert store.state().unknown_outcomes == 1

    db = store.session()
    try:
        identity = db.execute(text(
            "SELECT provider_message_id FROM commerce_runtime_turns WHERE id = :t"),
            {"t": turn_id}).scalar()
    finally:
        db.close()
    unfinished = recovery.admitted_turn_for(
        tenant_id=store.tenant_id, phone_number_id=str(store.connection_id),
        provider_message_id=identity, engine=store.engine)
    assert unfinished is not None and unfinished.finished is False

    entry_row = defer(store, identity=identity, reason=handover.REASON_ACCEPTED)
    assert entry_row is not None

    db = store.session()
    try:
        assert runner._unknown_delivery(store.tenant_id, unfinished.turn_id,
                                        store.engine) is True
        outcome = runner.recover_tenant(db, tenant_id=store.tenant_id, dry_run=False)
    finally:
        db.close()
    assert outcome.counted() == {runner.SKIPPED_UNKNOWN_DELIVERY: 1}
    assert [e.id for e in pending(store)] == [entry_row.id]


def test_a_dry_run_decides_everything_except_the_replay(configured, store):
    from services import commerce_runtime_recovery as runner

    entry_row = defer(store, identity="wamid.dry", reason=handover.REASON_ACCEPTED)
    assert entry_row is not None
    replays: List[Any] = []

    async def _never(_body: Any) -> None:
        replays.append(_body)

    db = store.session()
    try:
        with patch.object(runner, "_replay", _never):
            outcome = runner.recover_tenant(db, tenant_id=store.tenant_id, dry_run=True)
    finally:
        db.close()
    assert outcome.counted() == {runner.REPLAYED: 1}
    assert replays == []                                  # nothing was handed back
    assert [e.id for e in pending(store)] == [entry_row.id]


def test_two_runners_do_not_replay_the_same_entry(configured, store):
    """The per-entry advisory lock: the second runner skips rather than races."""
    from services import commerce_runtime_recovery as runner

    entry_row = defer(store, identity="wamid.contended", reason=handover.REASON_ACCEPTED)
    assert entry_row is not None
    held = threading.Event()
    release = threading.Event()
    outcomes: Dict[str, Any] = {}

    def _first() -> None:
        db = store.session()
        try:
            db.execute(text("SELECT pg_advisory_xact_lock(:ns, :e)"),
                       {"ns": runner.ENTRY_LOCK_NAMESPACE, "e": entry_row.id})
            held.set()
            release.wait(timeout=10)
            db.rollback()
        finally:
            db.close()

    worker = threading.Thread(target=_first)
    worker.start()
    assert held.wait(timeout=10)
    db = store.session()
    try:
        with patch.object(runner, "_replay", lambda _b: None):
            outcomes["second"] = runner.recover_tenant(
                db, tenant_id=store.tenant_id, dry_run=False)
    finally:
        db.close()
        release.set()
        worker.join(timeout=10)
    assert outcomes["second"].counted() == {runner.SKIPPED_IN_FLIGHT: 1}
    assert [e.id for e in pending(store)] == [entry_row.id]


# ═════════════════════════════════════════════════════════════════════════════
# The composed lifecycle, through the supported commands, on real PostgreSQL:
# authenticated acceptance → acknowledgement → interruption → recovery →
# verified outcome → settlement → release → refusal of what arrives after.
# ═════════════════════════════════════════════════════════════════════════════
#
# What is real: the Meta route's signature evaluator, the acceptance record,
# the dispatcher (``_handle_whatsapp_body`` → ``_dispatch_message`` → the
# ownership claim), the pilot seam, the runtime admission under the recovery
# grant, ownership, the agent loop, the delivery ledger, the terminal, the
# deferred record's resolution, and the operator job. What is doubled, and
# named as such: the Anthropic HTTP call and the WhatsApp transport (scripted),
# the legacy merchant handler's own gates (a thin stub that hands the turn to
# the real seam with the store's conversation), the background spawn (never
# run — that *is* the interruption), and the process default engine (pointed
# at this case's database).

import contextlib
import functools

from services import commerce_runtime_acceptance as acceptance_service
from tests.commerce_reliability.runtime_support import (
    stop_record_for,
    META_TEST_APP_SECRET as APP_SECRET,
    meta_webhook_request as meta_request,
)

SENDER = PHONE.lstrip("+")


def inbound_body(identity: str, *, phone_id: str, text: str = QUESTION) -> Dict[str, Any]:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_id, "display_phone_number": ""},
            "messages": [{"id": identity, "from": SENDER, "type": "text",
                          "timestamp": "1700000000", "text": {"body": text}}],
        },
    }]}]}


@contextlib.contextmanager
def bound_to(store: Store, monkeypatch: pytest.MonkeyPatch, *, spawned: List[Any],
             handled: List[Any], runtime_calls: List[Any]):
    """Everything the route and the dispatcher would read from the process
    default, pointed at this case's database; the two doubles, scripted."""
    import database.session as db_session
    import routers.whatsapp_webhook as webhook
    import services.commerce_runtime_pilot as seam
    from unittest.mock import AsyncMock

    monkeypatch.setattr(db_session, "engine", store.engine)
    monkeypatch.setattr(db_session, "SessionLocal", store.session_factory)
    monkeypatch.setattr(acceptance_service, "record_before_acknowledging",
                        functools.partial(acceptance_service.record_before_acknowledging,
                                          session_factory=store.session_factory))
    monkeypatch.setattr(acceptance_service, "durable_status",
                        functools.partial(acceptance_service.durable_status,
                                          session_factory=store.session_factory))

    def _spawn(coro: Any, name: str = "") -> None:
        spawned.append(name)
        coro.close()                       # acknowledged; the process dies before this runs

    async def _handler(**kwargs: Any) -> None:
        from database.models import Conversation

        db = kwargs["db"]
        convo = db.get(Conversation, store.conversation_id)
        handled.append(kwargs)
        await seam.maybe_handle_with_commerce_runtime(
            db=db, tenant_id=kwargs["tenant_id"], phone_id=kwargs["phone_id"],
            to=kwargs["to"], text=kwargs["text"], convo=convo,
            wa_msg_id=kwargs.get("wa_msg_id"), inbound_metadata=kwargs.get("inbound_metadata"),
            trace=None, legacy_already_answered=False, ai_gate_skipped=False)

    real_run = entry.run_commerce_runtime_turn

    def _run(**kwargs: Any) -> Any:
        kwargs["anthropic_provider"] = ScriptedAnthropic([step([reply("تفضل", call_id="r1")])])
        kwargs["transport"] = Transport([accepted("wamid.out." + uuid.uuid4().hex[:8])])
        runtime_calls.append(kwargs)
        return real_run(**kwargs)

    with (
        patch("core.runtime_perf.spawn_background", _spawn),
        patch.object(webhook, "META_APP_SECRET", APP_SECRET),
        patch.object(webhook, "_record_signature_audit", lambda *a, **k: None),
        patch.object(webhook, "get_db", lambda: iter([store.session()])),
        patch.object(webhook, "_is_platform_tenant", lambda db, tenant_id: False),
        patch.object(webhook, "_handle_merchant_message", _handler),
        patch.object(webhook, "_post_wa", new=AsyncMock(return_value={"messages": []})),
        patch.object(entry, "run_commerce_runtime_turn", _run),
    ):
        yield


def accept_through_the_route(store: Store, identity: str, *, signature: str = "valid") -> Any:
    """The real Meta route, signed for real. Returns the HTTP response."""
    import asyncio

    import routers.whatsapp_webhook as webhook

    return asyncio.run(webhook.whatsapp_incoming(
        meta_request(inbound_body(identity, phone_id=store.phone_id), signature=signature)))


def accepted_record(store: Store, identity: str) -> Any:
    db = store.session()
    try:
        return handover.accepted_inbound(db, tenant_id=store.tenant_id,
                                         channel_connection_ref=f"wa:{store.phone_id}",
                                         provider_message_id=identity)
    finally:
        db.close()


def terminal_for(store: Store, identity: str) -> Any:
    return recovery.admitted_turn_for(tenant_id=store.tenant_id, phone_number_id=store.phone_id,
                                      provider_message_id=identity, engine=store.engine)


def test_the_composed_lifecycle_accept_interrupt_recover_settle_release(configured, store,
                                                                         monkeypatch):
    """One customer message, from the wire to the switch, through the commands."""
    spawned: List[Any] = []
    handled: List[Any] = []
    runtime_calls: List[Any] = []
    identity = "wamid.lifecycle." + uuid.uuid4().hex

    with bound_to(store, monkeypatch, spawned=spawned, handled=handled,
                  runtime_calls=runtime_calls):
        # 1. Authenticated acceptance, acknowledged before anything runs.
        response = accept_through_the_route(store, identity)
        assert response.status_code == 200
        assert spawned == ["webhook_meta"]                 # scheduled, never executed
        record = accepted_record(store, identity)
        assert record is not None and record.pending
        assert record.payload["raw"]["id"] == identity      # replayable, not just a note

        # 2. Interruption: the process died before background execution.
        assert terminal_for(store, identity) is None
        assert handled == []

        # 3. The operator drains — accepted work is what the drain has to meet.
        drain_and_converge(store)
        assert settle(store) == job.EXIT_BLOCKED             # deferred_pending=1

        # 4. Recovery, through the supported command, while draining.
        assert job.main(["recover"]) == job.EXIT_OK          # the plan
        assert accepted_record(store, identity).pending      # a plan replays nothing
        assert job.main(["recover", "--apply"]) == job.EXIT_OK

        # 5. The outcome is verified against the authoritative records.
        assert len(handled) == 1
        assert handled[0]["commerce_runtime_claim"].basis == "recovery_admitted"
        assert len(runtime_calls) == 1
        finished = terminal_for(store, identity)
        assert finished is not None and finished.finished is True
        resolved = accepted_record(store, identity)
        assert resolved is not None and resolved.state == "resolved"
        assert resolved.disposition_evidence["terminal_for_turn_id"] == finished.turn_id
        assert store.state().open_turns == 0 and store.state().deferred_pending == 0

        # 6. Settlement, then the release transition.
        assert settle(store) == job.EXIT_OK
        assert job.main(["release"]) == job.EXIT_OK
        assert barrier_of(store).state == handover.STATE_RELEASED

        # 7. A message arriving after the release is refused, not accepted and
        #    abandoned: retryable, nothing recorded, nothing spawned.
        late = "wamid.after.release." + uuid.uuid4().hex
        response = accept_through_the_route(store, late)
        assert response.status_code == 503
        assert json.loads(bytes(response.body))["reason"] == "pilot_released"
        assert accepted_record(store, late) is None
        assert spawned == ["webhook_meta"]
        assert job.main(["release"]) == job.EXIT_OK          # idempotent

        # 8. And once the switch is off, the same message is the legacy path's.
        monkeypatch.delenv(pgd.ENV_ENABLED)
        response = accept_through_the_route(store, late)
        assert response.status_code == 200
        assert spawned == ["webhook_meta", "webhook_meta"]
        assert accepted_record(store, late) is None


def test_a_recovery_replay_runs_through_both_dedup_boundaries_only_once(configured, store,
                                                                         monkeypatch):
    """A second ``recover --apply`` after the first finished the turn repeats
    nothing: the record is resolved, the turn has a terminal, and the runner
    closes rather than replays."""
    spawned: List[Any] = []
    handled: List[Any] = []
    runtime_calls: List[Any] = []
    identity = "wamid.once." + uuid.uuid4().hex
    with bound_to(store, monkeypatch, spawned=spawned, handled=handled,
                  runtime_calls=runtime_calls):
        assert accept_through_the_route(store, identity).status_code == 200
        drain_and_converge(store)
        assert job.main(["recover", "--apply"]) == job.EXIT_OK
        assert len(runtime_calls) == 1
        assert job.main(["recover", "--apply"]) == job.EXIT_OK
        assert len(runtime_calls) == 1                       # nothing repeated
        assert accepted_record(store, identity).state == "resolved"


def test_a_pending_arrival_after_settlement_is_met_by_drain_recover_settle(configured, store,
                                                                            monkeypatch):
    """The trap the review named: recovery needed an open barrier, reopening
    needed nothing pending. The way out is drain → recover → settle → release."""
    spawned: List[Any] = []
    handled: List[Any] = []
    runtime_calls: List[Any] = []
    identity = "wamid.settled.arrival." + uuid.uuid4().hex
    with bound_to(store, monkeypatch, spawned=spawned, handled=handled,
                  runtime_calls=runtime_calls):
        drain_and_converge(store)
        assert settle(store) == job.EXIT_OK
        # The arrival in the settled window: accepted, recorded, answered by nobody.
        assert accept_through_the_route(store, identity).status_code == 200
        assert accepted_record(store, identity).pending
        assert job.main(["release"]) == job.EXIT_BLOCKED        # arrived_after_settlement
        assert job.main(["recover", "--apply"]) == job.EXIT_OK  # ...but nothing replays
        assert accepted_record(store, identity).pending         # settled: run 'drain' first
        assert runtime_calls == []

        assert job.main(["drain"]) == job.EXIT_OK
        db = store.session()
        try:
            observed = handover.read_barrier(db, tenant_id=store.tenant_id)
            handover.note_worker(db, tenant_id=store.tenant_id,
                                 observed_generation=observed.generation,
                                 observed_state=observed.state, name="worker-a", force=True)
        finally:
            db.close()
        assert job.main(["recover", "--apply"]) == job.EXIT_OK
        assert len(runtime_calls) == 1
        assert accepted_record(store, identity).state == "resolved"
        assert settle(store) == job.EXIT_OK
        assert job.main(["release"]) == job.EXIT_OK
        assert job.main(["reopen"]) == job.EXIT_OK
        assert barrier_of(store).admits_new_work is True


def test_an_unauthenticated_request_takes_no_pilot_obligation(configured, store, monkeypatch):
    """Audit mode for the legacy path does not extend to the pilot: with the
    signature missing or wrong, nothing pilot-scoped is recorded or spawned,
    and the request is answered retryable."""
    spawned: List[Any] = []
    with bound_to(store, monkeypatch, spawned=spawned, handled=[], runtime_calls=[]):
        for signature in ("missing", "invalid"):
            identity = f"wamid.{signature}." + uuid.uuid4().hex
            response = accept_through_the_route(store, identity, signature=signature)
            assert response.status_code == 503
            assert json.loads(bytes(response.body))["reason"] == "pilot_scope_unauthenticated"
            assert accepted_record(store, identity) is None
        assert spawned == []
        identity = "wamid.valid." + uuid.uuid4().hex
        assert accept_through_the_route(store, identity).status_code == 200
        assert accepted_record(store, identity) is not None
        assert spawned == ["webhook_meta"]


def test_recovery_admission_follows_the_barrier_on_the_admitting_connection(configured, store):
    """Open and draining admit accepted work under a grant; settled and released
    refuse it — read on the admitting transaction's own connection."""
    def admits(record: Any) -> bool:
        with store.engine.connect() as conn:
            with conn.begin():
                return handover.admits_recovery_on(
                    conn, tenant_id=store.tenant_id, entry_id=record.id,
                    channel_connection_ref=record.channel_connection_ref,
                    provider_message_id=record.provider_message_id)

    def closed(record: Any) -> None:
        assert dispose(store, record.id, "not_required",
                       {"authorized_by": "owner", "why": "test"}).disposed == (record.id,)

    first = defer(store, identity="wamid.grant.open", reason=handover.REASON_ACCEPTED)
    assert admits(first) is True                             # open (no barrier row yet)
    drain_and_converge(store)
    assert admits(first) is True                             # draining
    closed(first)                                            # settlement needs nothing pending
    assert settle(store) == job.EXIT_OK
    second = defer(store, identity="wamid.grant.settled", reason=handover.REASON_SETTLED_WINDOW)
    assert admits(second) is False                           # settled: the entry is pending, the barrier refuses
    closed(second)
    # An arrival after settlement blocks release until the counts are re-taken:
    # drain, settle again, then release — the documented way out.
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    assert job.main(["release"]) == job.EXIT_OK
    assert admits(second) is False                           # released (and nothing can be recorded now)
    assert job.main(["reopen"]) == job.EXIT_OK
    third = defer(store, identity="wamid.grant.reopened", reason=handover.REASON_ACCEPTED)
    assert admits(third) is True


# ═════════════════════════════════════════════════════════════════════════════
# Release is a transition acceptance reads
# ═════════════════════════════════════════════════════════════════════════════


def test_after_release_the_record_write_itself_refuses(configured, store):
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    assert job.main(["release"]) == job.EXIT_OK
    with pytest.raises(handover.BarrierReleased):
        defer(store, identity="wamid.too.late", reason=handover.REASON_SETTLED_WINDOW)
    assert pending(store) == ()
    outcome = acceptance_service.record_before_acknowledging(
        inbound_body("wamid.too.late.2", phone_id=store.phone_id),
        session_factory=store.session_factory,
        authenticated=True)
    assert outcome.ok is False and outcome.reason == acceptance_service.REFUSED_RELEASED


def test_a_release_racing_an_acceptance_sees_it_or_refuses_it(configured, store):
    """Two orderings, no third: the insert commits first and the release counts
    it as pending, or the release commits first and the insert is refused."""
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    outcomes: Dict[str, Any] = {}
    started = threading.Event()

    def accept() -> None:
        started.wait(5)
        try:
            outcomes["record"] = defer(store, identity="wamid.race",
                                       reason=handover.REASON_SETTLED_WINDOW)
        except handover.BarrierReleased:
            outcomes["record"] = "refused"

    def release() -> None:
        started.set()
        outcomes["release"] = job.main(["release"])

    threads = [threading.Thread(target=accept), threading.Thread(target=release)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    if outcomes["release"] == job.EXIT_OK:
        assert outcomes["record"] == "refused" and pending(store) == ()
    else:
        assert outcomes["record"] != "refused" and len(pending(store)) == 1
        assert barrier_of(store).state == handover.STATE_SETTLED


# ═════════════════════════════════════════════════════════════════════════════
# Disposition evidence is bound to the entry it accounts for
# ═════════════════════════════════════════════════════════════════════════════

OTHER_PHONE = "+966500000043"


def second_customer(store: Store) -> Tuple[int, int]:
    with store.engine.begin() as conn:
        customer_id = int(conn.execute(
            text("INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
                 "VALUES (:t, :p, :p, :n) RETURNING id"),
            {"t": store.tenant_id, "p": OTHER_PHONE, "n": "نورة عبدالله"}).scalar_one())
        conversation_id = int(conn.execute(
            text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                 "VALUES (:t, :c, :e, 'active') RETURNING id"),
            {"t": store.tenant_id, "c": customer_id, "e": OTHER_PHONE}).scalar_one())
    return customer_id, conversation_id


def run_turn_for(store: Store, *, conversation_id: int, customer_id: int, phone: str,
                 provider_message_id: str) -> entry.TurnReport:
    return entry.run_commerce_runtime_turn(
        engine=store.engine, session_factory=store.session_factory,
        tenant_id=store.tenant_id, conversation_id=conversation_id,
        connection_ref=f"wa:{store.connection_id}", connection_id=str(store.connection_id),
        customer_id=customer_id, normalized_customer_phone=phone,
        provider_message_id=provider_message_id, inbound_text=QUESTION,
        inbound_metadata={"source": "disposition-binding"},
        transport=Transport([accepted("wamid.out." + uuid.uuid4().hex[:8])]),
        instructions="EXISTING-INSTRUCTIONS", model=MODEL,
        budget=ac.LoopBudget(max_steps=3, max_tool_calls=4, tool_timeout_seconds=10.0,
                             provider_timeout_seconds=15.0, deadline_seconds=45.0),
        anthropic_provider=ScriptedAnthropic([step([reply("تفضل", call_id="r1")])]),
    )


def dispose(store: Store, entry_id: int, kind: str, evidence: Dict[str, Any]) -> Any:
    db = store.session()
    try:
        return handover.dispose_inbound(db, tenant_id=store.tenant_id, entry_ids=[entry_id],
                                        disposition=kind, evidence=evidence, by="owner")
    finally:
        db.close()


def test_an_answered_disposition_must_name_this_customers_own_conversation(configured, store):
    """Another conversation's terminal proves nothing about this entry."""
    drain_and_converge(store)
    entry_row = defer(store, identity="wamid.owed." + uuid.uuid4().hex)

    other_customer, other_conversation = second_customer(store)
    theirs = "wamid.theirs." + uuid.uuid4().hex
    assert run_turn_for(store, conversation_id=other_conversation, customer_id=other_customer,
                        phone=OTHER_PHONE, provider_message_id=theirs).reason == entry.HANDLED
    refused = dispose(store, entry_row.id, "answered",
                      {"answered_by_provider_message_id": theirs})
    assert refused.refused == {entry_row.id: "turn_belongs_to_another_customer"}

    ours = "wamid.ours." + uuid.uuid4().hex
    assert store.run_turn(transport=Transport([accepted("wamid.out.a")]),
                          provider_message_id=ours).reason == entry.HANDLED
    done = dispose(store, entry_row.id, "answered", {"answered_by_provider_message_id": ours})
    assert done.disposed == (entry_row.id,)
    db = store.session()
    try:
        row = handover.accepted_inbound(db, tenant_id=store.tenant_id,
                                        channel_connection_ref=f"wa:{store.connection_id}",
                                        provider_message_id=entry_row.provider_message_id)
    finally:
        db.close()
    assert row.disposition == "answered"
    assert row.disposition_evidence["verified"]["app_conversation_id"] == store.conversation_id


def test_an_answer_recorded_before_the_message_arrived_did_not_answer_it(configured, store):
    drain_and_converge(store)
    earlier = "wamid.earlier." + uuid.uuid4().hex
    assert store.run_turn(transport=Transport([accepted("wamid.out.e")]),
                          provider_message_id=earlier).reason == entry.HANDLED
    time.sleep(0.05)
    entry_row = defer(store, identity="wamid.later.arrival." + uuid.uuid4().hex)
    refused = dispose(store, entry_row.id, "answered",
                      {"answered_by_provider_message_id": earlier})
    assert refused.refused == {entry_row.id: "answering_terminal_predates_this_inbound"}


def test_supersession_needs_the_same_connection_and_customer_and_a_later_arrival(configured,
                                                                               store):
    drain_and_converge(store)
    first = defer(store, identity="wamid.first." + uuid.uuid4().hex)
    time.sleep(0.01)
    second = defer(store, identity="wamid.second." + uuid.uuid4().hex)

    # Later supersedes earlier.
    done = dispose(store, first.id, "superseded",
                   {"superseded_by_provider_message_id": second.provider_message_id})
    assert done.disposed == (first.id,)
    # Earlier does not supersede later.
    refused = dispose(store, second.id, "superseded",
                      {"superseded_by_provider_message_id": first.provider_message_id})
    assert refused.refused == {second.id: "superseding_inbound_is_not_later"}
    # Another connection's later message is not this conversation's.
    db = store.session()
    try:
        elsewhere = handover.record_inbound(
            db, tenant_id=store.tenant_id, phone_number_id="OTHER_PID",
            channel_connection_ref="wa:OTHER_PID", recipient=PHONE,
            provider_message_id="wamid.elsewhere." + uuid.uuid4().hex,
            payload={"text": QUESTION}, reason=handover.REASON_DRAIN_BUFFERED,
            barrier_generation=1)
    finally:
        db.close()
    refused = dispose(store, second.id, "superseded",
                      {"superseded_by_provider_message_id": elsewhere.provider_message_id})
    assert refused.refused == {second.id: "superseding_inbound_not_found"}


# ═════════════════════════════════════════════════════════════════════════════
# The fleet is accounted for inside the transition, and retirement rests on a
# retained record
# ═════════════════════════════════════════════════════════════════════════════


def test_settlement_refuses_without_a_stated_inventory(configured, store, monkeypatch):
    drain_and_converge(store)
    monkeypatch.delenv(job.ENV_EXPECTED_WORKERS)
    assert settle(store) == job.EXIT_BLOCKED
    assert barrier_of(store).state == handover.STATE_DRAINING
    db = store.session()
    try:
        # The authoritative call, with no inventory, refuses on its own — no
        # CLI precheck is involved.
        outcome = handover.settle(db, tenant_id=store.tenant_id, expected_workers=None)
    finally:
        db.close()
    assert outcome.settled is False
    assert handover.BLOCKER_INVENTORY_UNSTATED in outcome.blockers


def test_settlement_names_a_replica_in_the_inventory_that_never_reported(configured, store,
                                                                         monkeypatch):
    drain_and_converge(store)
    monkeypatch.setenv(job.ENV_EXPECTED_WORKERS, "worker-a,worker-b")
    assert settle(store) == job.EXIT_BLOCKED
    db = store.session()
    try:
        outcome = handover.settle(db, tenant_id=store.tenant_id,
                                  expected_workers=["worker-a", "worker-b"])
    finally:
        db.close()
    assert "workers_expected_but_never_reported:worker-b" in outcome.blockers
    assert outcome.evidence["fleet_inventory"]["missing_from_fleet"] == ["worker-b"]


def test_retirement_needs_a_stop_record_that_names_the_deployment(configured, store):
    db = store.session()
    try:
        handover.note_worker(db, tenant_id=store.tenant_id, observed_generation=0,
                             observed_state=handover.STATE_OPEN, name="worker-gone",
                             force=True)
        sentence_only = {k: v for k, v in STOP_EVIDENCE.items() if k != "stop_record"}
        with pytest.raises(handover.RetirementRefused) as caught:
            handover.retire_worker(db, tenant_id=store.tenant_id, name="worker-gone",
                                   by="owner", reason="gone", evidence=sentence_only)
        assert "stop_record" in str(caught.value)
        elsewhere = dict(STOP_EVIDENCE, stop_record=stop_record_for(
            "some-other-deploy", observed_at=STOP_EVIDENCE["observed_at"]))
        with pytest.raises(handover.RetirementRefused) as caught:
            handover.retire_worker(db, tenant_id=store.tenant_id, name="worker-gone",
                                   by="owner", reason="gone", evidence=elsewhere)
        assert "stop_record_does_not_name_the_deployment" in str(caught.value)
        assert handover.retire_worker(db, tenant_id=store.tenant_id, name="worker-gone",
                                      by="owner", reason="gone", evidence=STOP_EVIDENCE)
        worker = next(w for w in handover.fleet(db, tenant_id=store.tenant_id)
                      if w.worker_id == "worker-gone")
    finally:
        db.close()
    import hashlib

    assert worker.retirement_evidence["stop_record"] == STOP_EVIDENCE["stop_record"]
    assert worker.retirement_evidence["stop_record_sha256"] == hashlib.sha256(
        STOP_EVIDENCE["stop_record"].encode()).hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# Closure round: the grant re-checked at admission, a terminal is not an
# answer, both lock orders of release-versus-acceptance, and a stop record
# that has to say what it says.
# ═════════════════════════════════════════════════════════════════════════════

from sqlalchemy import text as sa_text  # noqa: E402

from core.commerce_runtime import contracts as rc  # noqa: E402
from core.commerce_runtime import handover_models as hm  # noqa: E402
from tests.commerce_reliability.test_commerce_runtime_pilot_pg import rejected  # noqa: E402

CONNECTION_REF_OF = "wa:{}"


def pending_entry(store: Store, identity: str, *, reason: str = handover.REASON_ACCEPTED) -> Any:
    """A pending obligation on this merchant's own connection reference — the
    same reference the runtime admits turns under, so a turn and its record
    bind to each other exactly as the seam records them."""
    db = store.session()
    try:
        record = handover.record_inbound(
            db, tenant_id=store.tenant_id, phone_number_id=store.phone_id,
            channel_connection_ref=CONNECTION_REF_OF.format(store.connection_id), recipient=PHONE,
            provider_message_id=identity,
            payload={"text": QUESTION, "type": "text", "raw": {"id": identity, "type": "text",
                                                               "text": {"body": QUESTION}}},
            reason=reason, barrier_generation=0)
        assert record is not None and record.pending
        return record
    finally:
        db.close()


def entry_state(store: Store, entry_id: int) -> Tuple[str, Optional[str], Dict[str, Any]]:
    with store.engine.connect() as conn:
        row = conn.execute(sa_text(
            f"SELECT state, disposition, disposition_evidence FROM {hm.DEFERRED_TABLE} "
            f"WHERE id = :id"), {"id": int(entry_id)}).mappings().one()
    return str(row["state"]), row["disposition"], dict(row["disposition_evidence"] or {})


def run_turn_with(store: Store, *, provider_message_id: str, transport: Any = None,
                  anthropic: Any = None) -> entry.TurnReport:
    """The real runtime for this merchant's customer, with the two doubles chosen."""
    return entry.run_commerce_runtime_turn(
        engine=store.engine, session_factory=store.session_factory,
        tenant_id=store.tenant_id, conversation_id=store.conversation_id,
        connection_ref=CONNECTION_REF_OF.format(store.connection_id),
        connection_id=str(store.connection_id),
        customer_id=store.customer_id, normalized_customer_phone=PHONE,
        provider_message_id=provider_message_id, inbound_text=QUESTION,
        inbound_metadata={"source": "closure-round"},
        transport=transport if transport is not None
        else Transport([accepted("wamid.out." + uuid.uuid4().hex[:8])]),
        instructions="EXISTING-INSTRUCTIONS", model=MODEL,
        budget=ac.LoopBudget(max_steps=3, max_tool_calls=4, tool_timeout_seconds=10.0,
                             provider_timeout_seconds=15.0, deadline_seconds=45.0),
        anthropic_provider=anthropic if anthropic is not None
        else ScriptedAnthropic([step([reply("تفضل", call_id="r1")])]),
    )


def admitted_for(store: Store, identity: str) -> Any:
    """The runtime turn admitted for ``identity`` under this merchant's own
    connection reference (the one ``pending_entry`` and ``run_turn_with`` use)."""
    return recovery.admitted_turn_for(tenant_id=store.tenant_id,
                                      phone_number_id=str(store.connection_id),
                                      provider_message_id=identity, engine=store.engine)


def terminal_outcomes(store: Store, identity: str) -> Tuple[str, str, str]:
    found = admitted_for(store, identity)
    assert found is not None and found.finished
    with store.engine.connect() as conn:
        row = conn.execute(sa_text(
            "SELECT processing_outcome, transport_outcome, customer_reach "
            "FROM commerce_runtime_turn_terminals WHERE turn_id = :id"),
            {"id": int(found.turn_id)}).one()
    return tuple(str(v) for v in row)


def wait_for(thread: threading.Thread, *, seconds: float) -> bool:
    thread.join(seconds)
    return not thread.is_alive()


# ── D: the grant is checked again inside the admitting transaction ───────────


def test_a_disposition_that_committed_first_withdraws_the_grant_at_admission(configured, store):
    """Reproduction of the closure finding, on real PostgreSQL: a grant captured
    for a pending entry while draining; the operator disposes of the entry;
    the admission check for that grant, run on the admitting connection, is
    false — pending count is zero **and** the callback says no."""
    record = pending_entry(store, "wamid.grant.withdrawn")
    drain_and_converge(store)
    claim = dict(entry_id=record.id, channel_connection_ref=record.channel_connection_ref,
                 provider_message_id=record.provider_message_id)

    with store.engine.begin() as conn:
        assert handover.admits_recovery_on(conn, tenant_id=store.tenant_id, **claim) is True

    closed = dispose(store, record.id, "not_required",
                     {"authorized_by": "owner", "why": "withdrawn while draining"})
    assert closed.disposed == (record.id,), closed.refused
    assert handover.pending_count(store.session(), tenant_id=store.tenant_id) == 0

    with store.engine.begin() as conn:
        assert handover.admits_recovery_on(conn, tenant_id=store.tenant_id, **claim) is False


def test_an_admission_holding_the_entry_makes_the_disposition_wait_and_then_refuse(configured, store):
    """Order one, forced: the admission transaction has passed the grant check
    (the entry row is locked, the shared tenant lock held) and inserts its
    turn; a disposition started meanwhile blocks on the tenant lock — observed,
    not assumed — and once the admission commits it finds an admitted,
    unfinished turn and refuses by name."""
    record = pending_entry(store, "wamid.grant.admitted.first")
    drain_and_converge(store)
    repository = LedgerRepository(store.engine).foundation
    passed = threading.Event()
    proceed = threading.Event()

    def guard(conn: Any) -> bool:
        ok = handover.admits_recovery_on(
            conn, tenant_id=store.tenant_id, entry_id=record.id,
            channel_connection_ref=record.channel_connection_ref,
            provider_message_id=record.provider_message_id)
        passed.set()
        assert proceed.wait(20), "the test never released the admission"
        return ok

    admitted: Dict[str, Any] = {}

    def admit() -> None:
        admitted["turn"] = repository.admit_turn(
            tenant_id=store.tenant_id, namespace=rc.Namespace.LIVE,
            conversation_ref=f"whatsapp:{store.conversation_id}",
            channel_connection_ref=record.channel_connection_ref,
            provider_message_id=record.provider_message_id, admission_guard=guard)

    admission = threading.Thread(target=admit, daemon=True)
    admission.start()
    assert passed.wait(20)

    outcome: Dict[str, Any] = {}
    disposition = threading.Thread(
        target=lambda: outcome.update(result=dispose(
            store, record.id, "not_required", {"authorized_by": "owner", "why": "late"})),
        daemon=True)
    disposition.start()
    assert not wait_for(disposition, seconds=1.0)          # blocked behind the admission
    proceed.set()
    assert wait_for(admission, seconds=20) and admitted["turn"].duplicate is False
    assert wait_for(disposition, seconds=20)
    refused = outcome["result"].refused
    assert refused == {record.id: f"turn_admitted_and_unfinished:{admitted['turn'].turn_id}"}
    assert entry_state(store, record.id)[0] == "pending"


def test_a_disposition_holding_the_entry_makes_the_admission_wait_and_then_refuse(configured, store):
    """Order two, forced: the disposition transaction holds the tenant lock
    and the entry row; the admission check started meanwhile blocks — observed
    — and once the disposition commits it reads a disposed entry and refuses."""
    record = pending_entry(store, "wamid.grant.disposed.first")
    drain_and_converge(store)
    db = store.session()
    verdict: Dict[str, Any] = {}

    def check() -> None:
        with store.engine.begin() as conn:
            verdict["admits"] = handover.admits_recovery_on(
                conn, tenant_id=store.tenant_id, entry_id=record.id,
                channel_connection_ref=record.channel_connection_ref,
                provider_message_id=record.provider_message_id)

    try:
        with handover._locked(db, store.tenant_id) as session:      # noqa: SLF001
            row = (session.query(hm.DeferredInbound)
                   .filter(hm.DeferredInbound.id == record.id).with_for_update().one())
            checker = threading.Thread(target=check, daemon=True)
            checker.start()
            assert not wait_for(checker, seconds=1.0)               # blocked behind the lock
            row.state = hm.DEFERRED_DISPOSED
            row.disposition = "not_required"
            row.disposed_by = "owner"
            row.disposed_at = handover._now()                        # noqa: SLF001
            row.disposition_evidence = {"authorized_by": "owner", "why": "first"}
            session.commit()
        assert wait_for(checker, seconds=20)
    finally:
        db.close()
    assert verdict["admits"] is False


def test_a_live_grant_for_a_pending_entry_still_admits_while_draining(configured, store):
    """The control: nothing disposed, barrier draining, the same check says yes
    on the admitting connection and the turn is admitted."""
    record = pending_entry(store, "wamid.grant.live")
    drain_and_converge(store)
    repository = LedgerRepository(store.engine).foundation
    admitted = repository.admit_turn(
        tenant_id=store.tenant_id, namespace=rc.Namespace.LIVE,
        conversation_ref=f"whatsapp:{store.conversation_id}",
        channel_connection_ref=record.channel_connection_ref,
        provider_message_id=record.provider_message_id,
        admission_guard=lambda conn: handover.admits_recovery_on(
            conn, tenant_id=store.tenant_id, entry_id=record.id,
            channel_connection_ref=record.channel_connection_ref,
            provider_message_id=record.provider_message_id))
    assert admitted.duplicate is False
    assert admitted_for(store, "wamid.grant.live") is not None


# ── E: a terminal is not an answer ───────────────────────────────────────────


def test_a_failed_terminal_bound_to_the_entry_does_not_establish_answered(configured, store):
    """Reproduction of the closure finding, on the real runtime: the model call
    fails, the runtime records a terminal with ``processing=failed``,
    ``transport=not_attempted`` and a customer reach that is not ``reached`` —
    correctly bound to this entry and later than it. That terminal establishes
    nothing:
    ``answered`` and ``replayed`` are refused with the outcomes it recorded,
    normal resolution refuses, recovery reports it, and the entry stays pending
    until the operator closes it under the honest name."""
    from services import commerce_runtime_recovery as runner

    record = pending_entry(store, "wamid.failed.terminal")
    report = run_turn_with(store, provider_message_id="wamid.failed.terminal",
                           anthropic=ScriptedAnthropic([]))
    assert report.reason != "ok"
    processing, transport, reach = terminal_outcomes(store, "wamid.failed.terminal")
    # The runtime's own vocabulary for a model failure with no send attempted:
    # the customer was neither reached nor not reached — no reply existed.
    assert (processing, transport, reach) == ("failed", "not_attempted", "not_applicable")

    for kind, key in (("answered", "answered_by_provider_message_id"),
                      ("replayed", "replayed_as_provider_message_id")):
        refused = dispose(store, record.id, kind, {key: "wamid.failed.terminal"})
        assert refused.disposed == ()
        assert refused.refused[record.id] == ("terminal_is_not_an_accepted_reply:"
                                             "processing=failed,transport=not_attempted,"
                                             "customer_reach=not_applicable")

    db = store.session()
    try:
        assert handover.resolve_inbound(
            db, tenant_id=store.tenant_id, channel_connection_ref=record.channel_connection_ref,
            provider_message_id="wamid.failed.terminal") is False
        ok, why, _ = handover.verify_handling(db, tenant_id=store.tenant_id, entry_id=record.id)
        assert ok is False and why.startswith("terminal_is_not_an_accepted_reply:")
        assert handover.pending_count(db, tenant_id=store.tenant_id) == 1
        for dry_run in (True, False):
            outcome = runner.recover_tenant(db, tenant_id=store.tenant_id, dry_run=dry_run)
            assert [o.outcome for o in outcome.outcomes] == [runner.SKIPPED_FINISHED_UNANSWERED]
            assert "processing=failed" in outcome.outcomes[0].detail
        assert handover.pending_count(db, tenant_id=store.tenant_id) == 1
    finally:
        db.close()

    closed = dispose(store, record.id, "unanswered",
                     {"authorized_by": "owner", "why": "the model call failed; closing knowingly"})
    assert closed.disposed == (record.id,), closed.refused
    state, disposition, evidence = entry_state(store, record.id)
    assert (state, disposition) == ("disposed", "unanswered")
    assert evidence["verified"]["runtime_terminal"].startswith("terminal_is_not_an_accepted_reply:")
    assert evidence["verified"]["runtime_turn_id"] == admitted_for(store, "wamid.failed.terminal").turn_id


def test_a_definitively_rejected_send_is_not_an_answer_either(configured, store):
    record = pending_entry(store, "wamid.rejected.send")
    run_turn_with(store, provider_message_id="wamid.rejected.send",
                  transport=Transport([rejected()]))
    processing, transport, reach = terminal_outcomes(store, "wamid.rejected.send")
    assert transport == "rejected_definitive" and reach == "not_reached"
    refused = dispose(store, record.id, "answered",
                      {"answered_by_provider_message_id": "wamid.rejected.send"})
    assert refused.refused[record.id].startswith("terminal_is_not_an_accepted_reply:")
    assert handover.resolve_inbound(
        store.session(), tenant_id=store.tenant_id,
        channel_connection_ref=record.channel_connection_ref,
        provider_message_id="wamid.rejected.send") is False
    closed = dispose(store, record.id, "unanswered",
                     {"authorized_by": "owner", "why": "the provider rejected the send"})
    assert closed.disposed == (record.id,), closed.refused


def test_an_accepted_reply_resolves_the_entry_and_records_delivery_separately(configured, store):
    """The positive control: a completed turn whose reply the provider accepted
    resolves its own entry, and what is stored says exactly that — accepted by
    the provider, delivery **not** confirmed — rather than 'answered'."""
    record = pending_entry(store, "wamid.accepted.reply")
    run_turn_with(store, provider_message_id="wamid.accepted.reply")
    processing, transport, reach = terminal_outcomes(store, "wamid.accepted.reply")
    assert (processing, transport) == ("completed", "accepted")
    db = store.session()
    try:
        ok, why, verified = handover.verify_handling(db, tenant_id=store.tenant_id,
                                                     entry_id=record.id)
        assert (ok, why) == (True, "")
        assert verified["reply_accepted_by_provider"] is True
        assert verified["customer_delivery_confirmed"] is (reach == "reached")
        assert verified["customer_reach"] == reach
        assert handover.resolve_inbound(
            db, tenant_id=store.tenant_id, channel_connection_ref=record.channel_connection_ref,
            provider_message_id="wamid.accepted.reply",
            evidence={"closed_by": "test"}) is True
    finally:
        db.close()
    state, _disposition, evidence = entry_state(store, record.id)
    assert state == "resolved"
    assert evidence["closed_by"] == "test"
    assert evidence["verified"]["turn_id"] == admitted_for(store, "wamid.accepted.reply").turn_id
    assert evidence["verified"]["transport_outcome"] == "accepted"


def test_no_response_dispositions_are_refused_when_the_runtime_answered(configured, store):
    """``not_required`` and ``unanswered`` are statements that nothing answered
    this inbound. When the runtime's own turn did, both are refused; the
    truthful disposition is ``answered``, and it is accepted."""
    record = pending_entry(store, "wamid.answered.already")
    run_turn_with(store, provider_message_id="wamid.answered.already")
    for kind in ("not_required", "unanswered"):
        refused = dispose(store, record.id, kind, {"authorized_by": "owner", "why": "x"})
        assert refused.refused == {record.id: "the_runtime_answered_this_inbound"}
    closed = dispose(store, record.id, "answered",
                     {"answered_by_provider_message_id": "wamid.answered.already"})
    assert closed.disposed == (record.id,), closed.refused


def test_an_unknown_send_outcome_cannot_be_called_unanswered(configured, store):
    """A send whose outcome nobody established may have arrived. It is not an
    answer (transport is not ``accepted``) and it is not 'unanswered' either;
    the entry stays pending, exactly as an unknown outcome blocks settlement."""
    record = pending_entry(store, "wamid.unknown.send")
    run_turn_with(store, provider_message_id="wamid.unknown.send",
                  transport=Transport([timed_out()]))
    _processing, transport, _reach = terminal_outcomes(store, "wamid.unknown.send")
    assert transport == "unknown"
    for kind in ("unanswered", "not_required"):
        refused = dispose(store, record.id, kind, {"authorized_by": "owner", "why": "x"})
        assert refused.refused == {record.id: "delivery_outcome_unknown"}
    answered = dispose(store, record.id, "answered",
                       {"answered_by_provider_message_id": "wamid.unknown.send"})
    assert answered.refused[record.id].startswith("terminal_is_not_an_accepted_reply:")
    assert entry_state(store, record.id)[0] == "pending"


def test_resolution_refuses_an_entry_with_no_turn_at_all(configured, store):
    record = pending_entry(store, "wamid.never.admitted")
    db = store.session()
    try:
        assert handover.resolve_inbound(
            db, tenant_id=store.tenant_id, channel_connection_ref=record.channel_connection_ref,
            provider_message_id="wamid.never.admitted", evidence={"closed_by": "nobody"}) is False
        assert handover.verify_handling(db, tenant_id=store.tenant_id, entry_id=record.id)[:2] \
            == (False, "no_runtime_turn_for_that_identity")
    finally:
        db.close()
    assert entry_state(store, record.id)[0] == "pending"


# ── F: release and acceptance, both lock orders, forced ─────────────────────


def _barrier_state(store: Store) -> str:
    db = store.session()
    try:
        return handover.read_barrier(db, tenant_id=store.tenant_id).state
    finally:
        db.close()


def _settled(store: Store) -> None:
    drain_and_converge(store)
    assert settle(store) == job.EXIT_OK
    assert _barrier_state(store) == handover.STATE_SETTLED


def test_a_release_waits_for_an_acceptance_holding_the_shared_lock_and_then_sees_it(
        configured, store, monkeypatch):
    """Order one: an acceptance transaction holds the shared tenant lock with
    its row inserted and not yet committed; ``release`` started meanwhile
    blocks on the exclusive lock — observed — and, once the acceptance commits,
    refuses because the row is there. The barrier stays settled."""
    _settled(store)
    monkeypatch.setenv(job.ENV_EXPECTED_WORKERS, EXPECTED_WORKERS)
    outcome: Dict[str, Any] = {}

    def release() -> None:
        db = store.session()
        try:
            outcome["result"] = handover.release(db, tenant_id=store.tenant_id,
                                                 expected_workers=[EXPECTED_WORKERS])
        finally:
            db.close()

    with store.engine.connect() as conn:
        with conn.begin():
            handover._take_advisory_lock(conn, tenant_id=store.tenant_id, exclusive=False)  # noqa: SLF001
            conn.execute(sa_text(
                f"INSERT INTO {hm.DEFERRED_TABLE} (tenant_id, namespace, channel_connection_ref, "
                f"phone_number_id, recipient, provider_message_id, payload, reason, state) "
                f"VALUES (:t, 'live', :ref, :pid, :to, :pmid, '{{}}'::jsonb, 'accepted', 'pending')"),
                {"t": store.tenant_id, "ref": CONNECTION_REF_OF.format(store.connection_id),
                 "pid": store.phone_id, "to": PHONE, "pmid": "wamid.race.accept.first"})
            releasing = threading.Thread(target=release, daemon=True)
            releasing.start()
            assert not wait_for(releasing, seconds=1.0)     # blocked behind the shared lock
        # committed: the acceptance is durable
    assert wait_for(releasing, seconds=20)
    result = outcome["result"]
    assert result.released is False
    assert "deferred_pending=1" in result.blockers
    assert _barrier_state(store) == handover.STATE_SETTLED


def test_an_acceptance_waits_for_a_release_holding_the_exclusive_lock_and_then_refuses(
        configured, store):
    """Order two: a release transaction holds the exclusive tenant lock with
    the state written and not yet committed; ``record_inbound`` started
    meanwhile blocks on the shared lock — observed — and, once the release
    commits, raises ``BarrierReleased`` and writes nothing."""
    _settled(store)
    outcome: Dict[str, Any] = {}

    def accept() -> None:
        db = store.session()
        try:
            handover.record_inbound(
                db, tenant_id=store.tenant_id, phone_number_id=store.phone_id,
                channel_connection_ref=CONNECTION_REF_OF.format(store.connection_id),
                recipient=PHONE, provider_message_id="wamid.race.release.first",
                payload={"text": QUESTION}, reason=handover.REASON_ACCEPTED,
                barrier_generation=1)
            outcome["result"] = "recorded"
        except handover.BarrierReleased as refused:
            outcome["result"] = refused
        finally:
            db.close()

    with store.engine.connect() as conn:
        with conn.begin():
            handover._take_advisory_lock(conn, tenant_id=store.tenant_id, exclusive=True)  # noqa: SLF001
            conn.execute(sa_text(
                f"UPDATE {hm.BARRIER_TABLE} SET state = 'released', released_at = now() "
                f"WHERE tenant_id = :t AND namespace = 'live'"), {"t": store.tenant_id})
            accepting = threading.Thread(target=accept, daemon=True)
            accepting.start()
            assert not wait_for(accepting, seconds=1.0)     # blocked behind the exclusive lock
    assert wait_for(accepting, seconds=20)
    assert isinstance(outcome["result"], handover.BarrierReleased)
    assert handover.pending_count(store.session(), tenant_id=store.tenant_id) == 0
    assert _barrier_state(store) == handover.STATE_RELEASED


# ── G: the stop record has to say what it says ──────────────────────────────


def _reporting(store: Store, name: str) -> None:
    db = store.session()
    try:
        handover.note_worker(db, tenant_id=store.tenant_id, observed_generation=0,
                             observed_state=handover.STATE_OPEN, name=name, force=True)
    finally:
        db.close()


def _retire(store: Store, name: str, evidence: Dict[str, Any]) -> Any:
    db = store.session()
    try:
        return handover.retire_worker(db, tenant_id=store.tenant_id, name=name, by="owner",
                                      reason="gone", evidence=evidence)
    finally:
        db.close()


@pytest.mark.parametrize("record, needle", [
    (dict(state="running"), "stop_record_state_is_not_inactive:running"),
    (dict(state="SUCCESS"), "stop_record_state_is_not_inactive:success"),
    (dict(state="sleeping"), "stop_record_state_is_not_inactive:sleeping"),
    (dict(active_replicas=1), "stop_record_reports_active_replicas:1"),
    (dict(observed_at="2098-12-31T00:00:00+00:00"), "stop_record_observation_differs_from_observed_at"),
])
def test_a_stop_record_that_contradicts_the_stop_is_refused(configured, store, record, needle):
    """Reproduction of the closure finding: a record naming the deployment with
    ``status=RUNNING`` and ``replicas=1`` used to retire the worker on the
    strength of its digest. The record is now read, and a record that says the
    deployment is running — or that replicas are active, or that observes a
    different moment — refuses the retirement by name."""
    _reporting(store, "worker-contradicted")
    fields = dict(observed_at=STOP_EVIDENCE["observed_at"])
    fields.update(record)
    contradictory = dict(STOP_EVIDENCE, stop_record=stop_record_for(
        STOP_EVIDENCE["deployment"], **fields))
    with pytest.raises(handover.RetirementRefused) as caught:
        _retire(store, "worker-contradicted", contradictory)
    assert needle in str(caught.value)
    db = store.session()
    try:
        worker = next(w for w in handover.fleet(db, tenant_id=store.tenant_id)
                      if w.worker_id == "worker-contradicted")
    finally:
        db.close()
    assert worker.retired is False


def test_a_free_text_stop_record_is_refused_whatever_it_says(configured, store):
    _reporting(store, "worker-prose")
    prose = dict(STOP_EVIDENCE, stop_record="railway:nahla-backend@deploy-1234 status=REMOVED replicas=0")
    with pytest.raises(handover.RetirementRefused, match="stop_record_not_structured"):
        _retire(store, "worker-prose", prose)


def test_a_worker_that_named_its_deployment_is_retired_only_against_it(configured, store):
    """A worker that reported as ``<deployment>/<replica>@host:pid`` carries
    its deployment on its row. Evidence about another deployment retires
    nothing; evidence about its own does."""
    named = "deploy-9f1/0@host-a:41"
    _reporting(store, named)
    other = dict(STOP_EVIDENCE, deployment="deploy-000", stop_record=stop_record_for(
        "deploy-000", observed_at=STOP_EVIDENCE["observed_at"]))
    with pytest.raises(handover.RetirementRefused, match="deployment_does_not_match_the_worker_s_own:deploy-9f1"):
        _retire(store, named, other)
    own = dict(STOP_EVIDENCE, deployment="deploy-9f1", stop_record=stop_record_for(
        "deploy-9f1", observed_at=STOP_EVIDENCE["observed_at"], incarnation="deploy-9f1/0"))
    assert _retire(store, named, own) is True
    db = store.session()
    try:
        worker = next(w for w in handover.fleet(db, tenant_id=store.tenant_id)
                      if w.worker_id == named)
    finally:
        db.close()
    assert worker.retired is True
    assert worker.retirement_evidence["stop_record_state"] == "removed"
    assert worker.retirement_evidence["stop_record_incarnation"] == "deploy-9f1/0"
