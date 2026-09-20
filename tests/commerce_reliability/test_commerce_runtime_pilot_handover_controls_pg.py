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
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

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

REVISION = "0110"
PHONE = "+966500000042"
PHONE_ID = "1555000424"
QUESTION = "عندكم قميص قطني أزرق؟"


@dataclasses.dataclass
class Store:
    """One merchant with everything an inbound turn needs, and nothing else."""

    engine: Any
    session_factory: Any
    ledgers: LedgerRepository
    tenant_id: int
    connection_id: int
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
    with engine.begin() as conn:
        tenant_id = int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر تجريبي عام {uuid.uuid4().hex[:8]}"}).scalar_one())
        connection_id = int(conn.execute(
            text("INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
                 "VALUES (:t, :p, 'connected') RETURNING id"),
            {"t": tenant_id, "p": PHONE_ID}).scalar_one())
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
                connection_id=connection_id, customer_id=customer_id,
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
            db, tenant_id=store.tenant_id, phone_number_id=PHONE_ID,
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
                phone_number_id=PHONE_ID, inbound_text="وين ردّي؟")
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
            db, tenant_id=store.tenant_id, phone_id=PHONE_ID, to=PHONE,
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
            db, tenant_id=store.tenant_id, phone_id=PHONE_ID, to=PHONE,
            text="سؤال أثناء التسليم", wa_msg_id="wamid.buffered.one")
        assert claim is not None and claim.basis == "drain_buffered"
        db.commit()
    finally:
        db.close()

    assert settle(store) == job.EXIT_BLOCKED
    entry = pending(store)[0]
    assert job.main(["dispose", "--entry", str(entry.id), "--disposition", "replayed",
                     "--evidence", '{"replayed_at": "2026-09-20T00:00:00Z"}',
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
            {"t": other, "p": PHONE_ID + "9"})

    drain_and_converge(store)
    monkeypatch.setenv(pgd.ENV_TENANT_ALLOWLIST, f"{store.tenant_id},{other}")
    db = store.session()
    try:
        assert handover.read_barrier(db, tenant_id=other).admits_new_work is True
        claim = seam.commerce_runtime_claims_inbound(
            db, tenant_id=other, phone_id=PHONE_ID + "9", to=PHONE,
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
                                     validate=lambda _s, _b: ([], {}))
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
    assert job.main(["dispose", "--entry", str(seen.id), "--disposition", "replayed",
                     "--evidence", '{"by_hand": true}', "--by", "owner"]) == job.EXIT_OK
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
    drain_and_converge(store)
    defer(store, identity="wamid.finished", reason=handover.REASON_ACCEPTED)
    db = store.session()
    try:
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
            db, tenant_id=store.tenant_id, phone_id=PHONE_ID, to=PHONE,
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

    assert job.main(["retire", "--worker", "worker-gone", "--by", "owner",
                     "--reason", "terminated in deploy 1234"]) == job.EXIT_OK
    assert settle(store) == job.EXIT_OK
    assert evidence(store)["retired_workers"] == [
        {"worker_id": "worker-gone", "by": "owner", "reason": "terminated in deploy 1234"}]
