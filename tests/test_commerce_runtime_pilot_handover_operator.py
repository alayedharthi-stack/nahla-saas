"""The handover procedure, executed: barrier, fleet, disposition, evidence.

Every step runs against the runtime's **own** relations in an isolated SQLite
database, so the barrier, the worker rows and the deferred inbounds these cases
read and write are the ones the runtime reads — not doubles of them. Only the
*work counts* are supplied, because they come from the nine ledger relations,
which are proved on real PostgreSQL in
``tests/commerce_reliability/test_commerce_runtime_pilot_pg.py``; the locked
interleavings are proved there too, in
``test_commerce_runtime_pilot_handover_controls_pg.py``.

What is held here is that the procedure is executable, that each refusal names
a concrete reason, and that none of its verdicts rests on somebody asserting
something: convergence comes from what a worker observed, retirement from an
operator saying so on the record, and disposition from named entries with
evidence attached.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import JSON, create_engine, event  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.commerce_runtime import handover  # noqa: E402
from core.commerce_runtime import handover_models as hm  # noqa: E402
from core.commerce_runtime import pilot_guard as pg  # noqa: E402
from core.commerce_runtime import recovery  # noqa: E402
from core.commerce_runtime.handover_models import create_handover_tables  # noqa: E402
from core.commerce_runtime.models import RuntimeBase  # noqa: E402
from database.models import Base, Tenant  # noqa: E402
from scripts.operators import commerce_runtime_pilot_handover as job  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
@event.listens_for(RuntimeBase.metadata, "before_create")
def _remap_jsonb(target: Any, connection: Any, **kw: Any) -> None:
    """SQLite has no JSONB and cannot parse a ``::jsonb`` cast in a default.

    The columns are JSON here and JSONB on PostgreSQL, where these tables are
    actually proved; the values the code writes are identical either way.
    """
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
            default = getattr(col.server_default, "arg", None)
            if default is not None and "::" in str(default):
                col.server_default = None


@pytest.fixture()
def db() -> Any:
    # StaticPool: one in-memory database shared by every connection, so the
    # transaction a transition opens for itself sees the same rows instead of a
    # fresh empty database.
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    create_handover_tables(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.tenant_id = tenant.id                        # type: ignore[attr-defined]
    yield session
    session.close()
    engine.dispose()


def state(tenant_id: int, *, open_turns: int = 0, reserved: int = 0, unresolved: int = 0,
          unknown: int = 0, deferred: int = 0) -> recovery.HandoverState:
    return recovery.HandoverState(tenant_id=tenant_id, open_turns=open_turns,
                                  reserved_undispatched=reserved,
                                  unresolved_attempts=unresolved, unknown_outcomes=unknown,
                                  deferred_pending=deferred)


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch, db: Any) -> Dict[str, Any]:
    """The job bound to this database, with the ledger counts supplied.

    ``deferred_pending`` is **not** supplied: it is counted from the real rows,
    on the same session the transition uses, because that count is exactly what
    a stale inspection got wrong.
    """
    supplied: Dict[str, Any] = {"counts": {}}

    def _counts(tenant_id: int) -> Dict[str, int]:
        return dict(supplied["counts"])

    def _observe(tenants: Any) -> tuple:
        return tuple(state(t, deferred=handover.pending_count(db, tenant_id=t),
                           **_counts(t)) for t in tenants)

    def _observe_on(session: Any, tenants: Any) -> tuple:
        return tuple(state(t, deferred=handover.pending_count_on(session, tenant_id=t),
                           **_counts(t)) for t in tenants)

    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(db.tenant_id))
    monkeypatch.setattr(job, "session", lambda: db)
    monkeypatch.setattr(job, "observe", _observe)
    monkeypatch.setattr(job, "observe_on", _observe_on)
    handover._last_heartbeat.clear()
    return supplied


def run(args: List[str]) -> int:
    return job.main(args)


def barrier(db: Any) -> handover.Barrier:
    return handover.read_barrier(db, tenant_id=db.tenant_id)


# What retiring a worker has to be able to show. A name and a sentence are not
# evidence a process stopped; these three are what an on-call engineer can
# re-check afterwards.
STOP_EVIDENCE = {
    "deployment": "railway:nahla-backend@deploy-1234",
    "stop_verified_by": "railway deployment status=REMOVED, replicas=0",
    "observed_at": "2099-01-01T00:00:00+00:00",
}


def report(db: Any, *, generation: Optional[int] = None, name: str = "worker-a",
           observed_state: Optional[str] = None) -> None:
    """One worker says what it read. The reading is the argument, not a re-read."""
    current = barrier(db)
    handover.note_worker(
        db, tenant_id=db.tenant_id,
        observed_generation=current.generation if generation is None else generation,
        observed_state=observed_state or current.state, name=name, force=True)


def defer(db: Any, *, identity: str, reason: str = handover.REASON_DRAIN_BUFFERED,
          text: str = "سؤال أثناء التسليم") -> Any:
    return handover.record_inbound(
        db, tenant_id=db.tenant_id, phone_number_id="PID", channel_connection_ref="wa:PID",
        recipient="+966500000123", provider_message_id=identity, payload={"text": text},
        reason=reason, barrier_generation=barrier(db).generation)


def drain_and_converge(db: Any) -> None:
    assert run(["drain"]) == job.EXIT_OK
    report(db)


# ── The barrier is shared, durable and the runtime's own ─────────────────────


def test_a_fresh_tenant_admits_work_and_has_no_row(configured, db):
    current = barrier(db)
    assert current.state == handover.STATE_OPEN and current.admits_new_work
    assert current.exists is False


def test_a_drain_is_visible_to_a_reader_that_never_saw_the_command(configured, db):
    assert run(["drain"]) == job.EXIT_OK
    assert barrier(db).draining is True
    assert handover.barrier_admits_new_work(db, tenant_id=db.tenant_id) is False


def test_each_transition_moves_the_generation_on(configured, db):
    assert run(["drain"]) == job.EXIT_OK
    first = barrier(db).generation
    report(db)
    assert run(["settle"]) == job.EXIT_OK
    assert run(["reopen"]) == job.EXIT_OK
    assert barrier(db).generation > first


def test_a_barrier_that_cannot_be_read_refuses_new_work(configured, db):
    class _Unreadable:
        def query(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("the database is unavailable")

    assert handover.barrier_admits_new_work(_Unreadable(), tenant_id=db.tenant_id) is False


def test_an_unrelated_settings_write_cannot_touch_the_barrier(configured, db):
    """H2: the runtime's state is not in a document anybody else writes.

    The widget-settings update below is the real one — a whole-document
    read-modify-write over ``tenant_settings.metadata``, interleaved between the
    drain and a deferred inbound. Both survive, because they no longer share a
    row: the settings change is in ``tenant_settings`` and the handover is in
    the runtime's own relations.
    """
    from database.models import TenantSettings

    # Settings as they stood before the handover, read by an unrelated writer.
    db.add(TenantSettings(tenant_id=db.tenant_id,
                          extra_metadata={"widget": {"theme": "light"}}))
    db.commit()
    stale = dict(db.query(TenantSettings)
                 .filter(TenantSettings.tenant_id == db.tenant_id).first().extra_metadata)

    assert run(["drain"]) == job.EXIT_OK
    assert defer(db, identity="wamid.during.settings") is not None

    # …and now that writer commits its whole document, built from the copy it
    # took before the drain. This is the interleaving that used to reopen the
    # barrier and erase the buffer.
    row = (db.query(TenantSettings)
           .filter(TenantSettings.tenant_id == db.tenant_id).first())
    row.extra_metadata = dict(stale, widget={"theme": "dark"})
    db.commit()

    assert row.extra_metadata["widget"] == {"theme": "dark"}      # the settings change kept
    assert barrier(db).draining is True                           # and the drain kept
    assert [e.provider_message_id for e in handover.pending_inbound(db, tenant_id=db.tenant_id)] \
        == ["wamid.during.settings"]


# ── Convergence is observed, and silence is not retirement (H3) ──────────────


def test_a_fleet_nobody_has_reported_is_not_converged(configured, db):
    assert run(["drain"]) == job.EXIT_OK
    result = handover.convergence(barrier(db), handover.fleet(db, tenant_id=db.tenant_id))
    assert result["converged"] is False
    assert job.convergence_blockers(result) == ["no_worker_has_reported_this_generation"]


def test_a_worker_reporting_this_generation_converges(configured, db):
    drain_and_converge(db)
    result = handover.convergence(barrier(db), handover.fleet(db, tenant_id=db.tenant_id))
    assert result["converged"] is True and result["on_generation"] == ["worker-a"]


def test_a_worker_still_on_the_previous_generation_is_behind(configured, db):
    report(db)                                           # observed OPEN, generation 0
    assert run(["drain"]) == job.EXIT_OK
    report(db, name="worker-b")                          # observed the drain
    result = handover.convergence(barrier(db), handover.fleet(db, tenant_id=db.tenant_id))
    assert result["converged"] is False
    assert result["behind"] == ["worker-a"] or result["stale"] == ["worker-a"]


def test_an_observation_taken_before_the_drain_is_not_stamped_with_a_later_generation(
        configured, db):
    """H3, exactly: what the worker *read* is what the row says.

    The worker reads an OPEN barrier, the operator drains, and only then does
    the heartbeat reach the database. Re-deriving the generation at write time
    would record this worker as converged on a drain it has never seen.
    """
    observed = barrier(db)
    assert observed.state == handover.STATE_OPEN
    assert run(["drain"]) == job.EXIT_OK                 # the drain lands in between
    handover.note_worker(db, tenant_id=db.tenant_id,
                         observed_generation=observed.generation,
                         observed_state=observed.state, name="worker-late", force=True)

    row = handover.fleet(db, tenant_id=db.tenant_id)[0]
    assert row.observed_generation == observed.generation      # what it read
    assert row.observed_state == handover.STATE_OPEN
    result = handover.convergence(barrier(db), handover.fleet(db, tenant_id=db.tenant_id))
    assert result["converged"] is False and result["behind"] == ["worker-late"]
    assert run(["settle"]) == job.EXIT_BLOCKED


def test_a_worker_that_stops_reporting_is_stale_and_blocks(configured, db):
    """Disappearing from the recent-report window is not evidence it stopped."""
    report(db, name="worker-gone")
    assert run(["drain"]) == job.EXIT_OK
    report(db, name="worker-here")
    # Push the quiet worker's last report outside the window.
    with handover._own_session(db) as session:
        row = (session.query(hm.HandoverWorker)
               .filter(hm.HandoverWorker.worker_id == "worker-gone").first())
        row.seen_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=handover.WORKER_LIVE_SECONDS + 60)
        session.commit()

    result = handover.convergence(barrier(db), handover.fleet(db, tenant_id=db.tenant_id))
    assert result["stale"] == ["worker-gone"] and result["converged"] is False
    assert job.convergence_blockers(result) == ["workers_stale_retire_or_wait:worker-gone"]
    assert run(["settle"]) == job.EXIT_BLOCKED


def test_retiring_a_worker_is_an_operator_statement_with_a_reason(configured, db):
    report(db, name="worker-gone")
    assert run(["drain"]) == job.EXIT_OK
    report(db, name="worker-here")
    with handover._own_session(db) as session:
        row = (session.query(hm.HandoverWorker)
               .filter(hm.HandoverWorker.worker_id == "worker-gone").first())
        row.seen_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        session.commit()

    assert run(["retire", "--worker", "worker-gone"]) == job.EXIT_USAGE   # no by, no reason
    # A name and a sentence are not evidence a process stopped.
    assert run(["retire", "--worker", "worker-gone", "--by", "owner",
                "--reason", "terminated in deploy 1234"]) == job.EXIT_USAGE
    assert run(["retire", "--worker", "worker-gone", "--by", "owner",
                "--reason", "terminated in deploy 1234",
                "--evidence", json.dumps(STOP_EVIDENCE)]) == job.EXIT_OK

    result = handover.convergence(barrier(db), handover.fleet(db, tenant_id=db.tenant_id))
    assert result["retired"] == ["worker-gone"] and result["converged"] is True
    assert run(["settle"]) == job.EXIT_OK
    # And the settlement evidence carries who said so, and why.
    retired = barrier(db).evidence["retired_workers"]
    assert retired == [{"worker_id": "worker-gone", "by": "owner",
                        "reason": "terminated in deploy 1234"}]


def test_a_retired_worker_that_reports_again_is_back_in_the_fleet(configured, db):
    report(db, name="worker-a")
    assert handover.retire_worker(db, tenant_id=db.tenant_id, name="worker-a",
                                  by="owner", reason="believed stopped",
                                  evidence=STOP_EVIDENCE) is True
    report(db, name="worker-a")
    assert handover.fleet(db, tenant_id=db.tenant_id)[0].retired is False


def test_retiring_a_worker_nobody_has_heard_of_is_refused(configured, db):
    assert run(["retire", "--worker", "ghost", "--by", "owner", "--reason", "x",
                "--evidence", json.dumps(STOP_EVIDENCE)]) == job.EXIT_USAGE


def test_a_worker_that_reported_after_the_stop_was_observed_cannot_be_retired(
        configured, db):
    """It is demonstrably running, whatever the operator believes."""
    report(db, name="worker-live")
    stale_evidence = dict(STOP_EVIDENCE,
                          observed_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(handover.RetirementRefused) as refused:
        handover.retire_worker(db, tenant_id=db.tenant_id, name="worker-live",
                               by="owner", reason="believed stopped",
                               evidence=stale_evidence)
    assert "it is running" in str(refused.value)
    assert handover.fleet(db, tenant_id=db.tenant_id)[0].retired is False


def test_the_retirement_evidence_is_stored_with_the_row(configured, db):
    report(db, name="worker-gone")
    with handover._own_session(db) as session:
        row = (session.query(hm.HandoverWorker)
               .filter(hm.HandoverWorker.worker_id == "worker-gone").first())
        row.seen_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        session.commit()
    assert handover.retire_worker(db, tenant_id=db.tenant_id, name="worker-gone",
                                  by="owner", reason="scaled to zero",
                                  evidence=STOP_EVIDENCE) is True
    worker = handover.fleet(db, tenant_id=db.tenant_id)[0]
    assert worker.retirement_evidence["deployment"] == STOP_EVIDENCE["deployment"]
    assert worker.retirement_evidence["stop_verified_by"] == STOP_EVIDENCE["stop_verified_by"]
    assert worker.retirement_evidence["recorded_at"]


def test_the_expected_deployment_inventory_names_a_replica_that_never_reported(
        configured, db, monkeypatch):
    """Convergence can only see processes that wrote a row.

    A replica that never reported is invisible to it — and is exactly the one
    that would still be admitting — so the operator states what the deployment
    contains and the reconciliation names the gap.
    """
    report(db, name="worker-a")
    monkeypatch.setenv(job.ENV_EXPECTED_WORKERS, "worker-a,worker-b")
    entry = job.inspect(db, [db.tenant_id])[0]
    assert entry["fleet_inventory"]["missing_from_fleet"] == ["worker-b"]
    assert entry["fleet_inventory"]["reconciled"] is False
    assert "workers_expected_but_never_reported:worker-b" in job.blockers_for(entry)


# ── Settlement validates and transitions together (H1) ───────────────────────


def test_the_procedure_settles_when_everything_holds(configured, db):
    drain_and_converge(db)
    assert run(["settle"]) == job.EXIT_OK
    assert barrier(db).state == handover.STATE_SETTLED


def test_settling_is_refused_while_the_barrier_is_not_draining(configured, db):
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert barrier(db).state == handover.STATE_OPEN


@pytest.mark.parametrize("counts,expected", [
    ({"open_turns": 1}, "open_turns=1"),
    ({"reserved": 2}, "reserved_undispatched=2"),
    ({"unresolved": 1}, "unresolved_attempts=1"),
    ({"unknown": 3}, "unknown_outcomes=3"),
])
def test_any_outstanding_work_blocks_settlement_by_name(configured, db, counts, expected):
    configured["counts"] = counts
    drain_and_converge(db)
    assert run(["settle"]) == job.EXIT_BLOCKED
    entry = job.inspect(db, [db.tenant_id])[0]
    assert expected in job.blockers_for(entry)
    assert barrier(db).state == handover.STATE_DRAINING


def test_an_unknown_outcome_never_ages_out_of_the_blockers(configured, db):
    configured["counts"] = {"unknown": 1}
    drain_and_converge(db)
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert run(["settle"]) == job.EXIT_BLOCKED           # and again, later
    configured["counts"] = {}                            # established by the operator
    assert run(["settle"]) == job.EXIT_OK


def test_work_that_appears_between_inspection_and_settlement_is_not_settled_over(
        configured, db, monkeypatch):
    """H1's first reproduction, driven at the seam the operator job uses.

    A deferred inbound commits after ``status`` has been taken and before the
    transition. The transition recounts on its own session, so it refuses; the
    barrier is still draining and no evidence was written.
    """
    drain_and_converge(db)
    original = job.inspect
    arrived: List[str] = []

    def _inspect_then_arrive(session: Any, tenants: Any) -> Any:
        entries = original(session, tenants)
        if not arrived:                                   # exactly once, after inspection
            arrived.append("wamid.late")
            assert defer(db, identity="wamid.late") is not None
        return entries

    monkeypatch.setattr(job, "inspect", _inspect_then_arrive)
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert barrier(db).state == handover.STATE_DRAINING
    assert barrier(db).evidence == {}                     # nothing was recorded as settled


def test_settlement_refuses_when_the_generation_moved_under_it(configured, db):
    drain_and_converge(db)
    current = barrier(db)
    # Somebody re-drained: the generation the caller decided on is gone.
    handover.open_drain(db, tenant_id=db.tenant_id)
    outcome = handover.settle(db, tenant_id=db.tenant_id,
                              expected_generation=current.generation,
                              validate=lambda _s, _b: ([], {}))
    assert outcome.settled is False
    assert any(reason.startswith("generation_moved") for reason in outcome.blockers)
    assert barrier(db).state == handover.STATE_DRAINING


def test_the_evidence_describes_the_state_that_was_actually_settled(configured, db):
    drain_and_converge(db)
    generation = barrier(db).generation
    assert run(["settle"]) == job.EXIT_OK
    evidence = barrier(db).evidence
    assert evidence["settled_generation"] == generation
    assert evidence["work"]["settled"] is True
    assert evidence["convergence"]["converged"] is True
    assert evidence["convergence"]["on_generation"] == ["worker-a"]


# ── Reopening is checked and applied together (H1) ───────────────────────────


def test_reopening_before_settlement_is_refused(configured, db):
    assert run(["drain"]) == job.EXIT_OK
    assert run(["reopen"]) == job.EXIT_USAGE
    assert barrier(db).draining is True


def test_reopening_after_settlement_admits_work_again_and_keeps_the_evidence(configured, db):
    drain_and_converge(db)
    assert run(["settle"]) == job.EXIT_OK
    settled = dict(barrier(db).evidence)
    assert run(["reopen"]) == job.EXIT_OK
    current = barrier(db)
    assert current.admits_new_work is True
    assert current.evidence == settled                   # kept, not overwritten


def test_a_drain_starting_between_the_precheck_and_the_mutation_is_not_reopened_over(
        configured, db, monkeypatch):
    """H1's second reproduction: the reopen is checked where it is applied."""
    drain_and_converge(db)
    assert run(["settle"]) == job.EXIT_OK
    settled_generation = barrier(db).generation

    real_reopen = handover.reopen
    started: List[str] = []

    def _drain_then_reopen(session: Any, **kwargs: Any) -> Any:
        if not started:
            started.append("x")
            handover.open_drain(db, tenant_id=db.tenant_id)   # a fresh drain lands here
        return real_reopen(session, **kwargs)

    monkeypatch.setattr(handover, "reopen", _drain_then_reopen)
    assert run(["reopen"]) == job.EXIT_BLOCKED
    current = barrier(db)
    assert current.draining is True                       # the new drain survived
    assert current.generation > settled_generation


# ── Disposition is per entry, checked and evidenced (H4) ─────────────────────


def test_a_deferred_inbound_keeps_what_a_replay_needs(configured, db):
    assert run(["drain"]) == job.EXIT_OK
    record = defer(db, identity="wamid.keep", text="وين طلبي؟")
    assert record is not None
    assert (record.tenant_id, record.phone_number_id, record.channel_connection_ref) == \
        (db.tenant_id, "PID", "wa:PID")
    assert record.recipient == "+966500000123"
    assert record.provider_message_id == "wamid.keep"
    assert record.payload == {"text": "وين طلبي؟"}
    assert record.reason == handover.REASON_DRAIN_BUFFERED
    assert record.barrier_generation == barrier(db).generation


def test_the_same_inbound_is_deferred_once(configured, db):
    assert run(["drain"]) == job.EXIT_OK
    first = defer(db, identity="wamid.twice")
    second = defer(db, identity="wamid.twice")
    assert first is not None and second is not None and first.id == second.id
    assert len(handover.pending_inbound(db, tenant_id=db.tenant_id)) == 1


def test_pending_deferred_work_blocks_settlement_until_it_is_disposed(configured, db):
    drain_and_converge(db)
    entry = defer(db, identity="wamid.pending")
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert "deferred_pending=1" in job.blockers_for(job.inspect(db, [db.tenant_id])[0])

    # A free-text note is not proof of a replay: the disposition that claims one
    # has to name the identity that carried it, and that identity has to resolve
    # to a runtime turn with a terminal.
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "replayed",
                "--evidence", json.dumps({"replayed_at": "2026-09-20T00:00:00Z"}),
                "--by", "owner"]) == job.EXIT_BLOCKED
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner", "why": "the customer asked again and was answered"}),
                "--by", "owner"]) == job.EXIT_OK
    assert run(["settle"]) == job.EXIT_OK


def test_a_disposition_needs_named_entries_a_kind_and_an_operator(configured, db):
    drain_and_converge(db)
    entry = defer(db, identity="wamid.needs")
    assert run(["dispose", "--by", "owner", "--disposition", "replayed"]) == job.EXIT_USAGE
    assert run(["dispose", "--entry", str(entry.id), "--by", "owner"]) == job.EXIT_USAGE
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "replayed"]) \
        == job.EXIT_USAGE
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "made_up",
                "--by", "owner"]) == job.EXIT_USAGE
    assert handover.pending_inbound(db, tenant_id=db.tenant_id)[0].id == entry.id


def test_evidence_travels_with_the_entry_it_describes(configured, db):
    drain_and_converge(db)
    entry = defer(db, identity="wamid.evidenced")
    # ``answered`` claims the customer was answered: naming a support ticket is
    # not that claim's evidence, and it is refused.
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "answered",
                "--evidence", json.dumps({"answered_by": "support", "ticket": "T-91"}),
                "--by", "owner"]) == job.EXIT_BLOCKED
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner",
                                          "why": "duplicate of a message already answered",
                                          "ticket": "T-91"}),
                "--by", "owner"]) == job.EXIT_OK
    with handover._own_session(db) as session:
        row = session.query(hm.DeferredInbound).filter(
            hm.DeferredInbound.id == entry.id).first()
    assert row.state == hm.DEFERRED_DISPOSED and row.disposition == "not_required"
    assert row.disposition_evidence["ticket"] == "T-91"
    assert row.disposition_evidence["verified"]["authorized_by"] == "owner"
    assert row.disposition_evidence["verified"]["why"]
    assert row.disposed_by == "owner"


def test_a_later_message_can_supersede_an_earlier_one_only_if_it_exists(configured, db):
    drain_and_converge(db)
    first = defer(db, identity="wamid.first")
    assert run(["dispose", "--entry", str(first.id), "--disposition", "superseded",
                "--evidence", json.dumps({"superseded_by_provider_message_id": "wamid.ghost"}),
                "--by", "owner"]) == job.EXIT_BLOCKED
    later = defer(db, identity="wamid.later")
    assert run(["dispose", "--entry", str(first.id), "--disposition", "superseded",
                "--evidence", json.dumps({"superseded_by_provider_message_id": "wamid.later"}),
                "--by", "owner"]) == job.EXIT_OK
    with handover._own_session(db) as session:
        row = session.query(hm.DeferredInbound).filter(
            hm.DeferredInbound.id == first.id).first()
    assert row.disposition_evidence["verified"]["superseded_by_entry_id"] == later.id


def test_an_entry_that_arrives_after_the_operator_looked_is_not_disposed_of(configured, db):
    """H4, exactly: the operator disposes of what they inspected, and no more."""
    drain_and_converge(db)
    seen = defer(db, identity="wamid.seen")
    # The operator reads 'status', then a second message arrives.
    later = defer(db, identity="wamid.arrived.later")

    assert run(["dispose", "--entry", str(seen.id), "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner", "why": "the customer asked again and was answered"}),
                "--by", "owner"]) == job.EXIT_OK
    pending = handover.pending_inbound(db, tenant_id=db.tenant_id)
    assert [e.id for e in pending] == [later.id]          # untouched, still owed an answer
    assert run(["settle"]) == job.EXIT_BLOCKED


def test_an_entry_created_after_the_inspection_is_refused_even_when_named(
        configured, db):
    """``--as-of`` is the moment the operator read 'status'.

    Passing a later entry's id then refuses it by name instead of disposing of
    something nobody looked at.
    """
    drain_and_converge(db)
    cutoff = dt.datetime.now(dt.timezone.utc)
    later = defer(db, identity="wamid.after.cutoff")
    with handover._own_session(db) as session:
        row = session.query(hm.DeferredInbound).filter(
            hm.DeferredInbound.id == later.id).first()
        row.created_at = cutoff + dt.timedelta(seconds=30)
        session.commit()
    assert run(["dispose", "--entry", str(later.id), "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner", "why": "the customer asked again and was answered"}),
                "--by", "owner", "--as-of", cutoff.isoformat()]) == job.EXIT_BLOCKED
    assert handover.pending_inbound(db, tenant_id=db.tenant_id)[0].id == later.id


def test_disposing_an_entry_twice_is_refused_by_name(configured, db):
    drain_and_converge(db)
    entry = defer(db, identity="wamid.once")
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner", "why": "the customer asked again and was answered"}),
                "--by", "owner"]) == job.EXIT_OK
    assert run(["dispose", "--entry", str(entry.id), "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner", "why": "the customer asked again and was answered"}),
                "--by", "owner"]) == job.EXIT_BLOCKED


def test_disposing_an_entry_that_does_not_exist_is_refused(configured, db):
    drain_and_converge(db)
    assert run(["dispose", "--entry", "99999", "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner", "why": "the customer asked again and was answered"}),
                "--by", "owner"]) == job.EXIT_BLOCKED


def test_a_runtime_that_finishes_its_own_turn_resolves_the_record(configured, db):
    """A resolved record stops counting; it is never resolved by time passing."""
    assert run(["drain"]) == job.EXIT_OK
    defer(db, identity="wamid.finished")
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1
    assert handover.resolve_inbound(db, tenant_id=db.tenant_id,
                                    channel_connection_ref="wa:PID",
                                    provider_message_id="wamid.finished") is True
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0
    assert handover.resolve_inbound(db, tenant_id=db.tenant_id,
                                    channel_connection_ref="wa:PID",
                                    provider_message_id="wamid.finished") is False


def test_disposed_history_does_not_consume_the_pending_capacity(configured, db,
                                                                monkeypatch):
    """Retention: what has been accounted for stops occupying the limit."""
    monkeypatch.setattr(handover, "MAX_PENDING_DEFERRED", 2)
    assert run(["drain"]) == job.EXIT_OK
    first = defer(db, identity="wamid.a")
    second = defer(db, identity="wamid.b")
    assert defer(db, identity="wamid.c") is None          # full: refused, not dropped
    assert run(["dispose", "--entry", str(first.id), "--entry", str(second.id),
                "--disposition", "not_required",
                "--evidence", json.dumps({"authorized_by": "owner",
                                          "why": "the customer asked again and was answered"}),
                "--by", "owner"]) == job.EXIT_OK
    assert defer(db, identity="wamid.c") is not None      # room again, history kept
    with handover._own_session(db) as session:
        assert session.query(hm.DeferredInbound).count() == 3


# ── The job's own boundaries ─────────────────────────────────────────────────


def test_a_full_buffer_refuses_rather_than_discarding_the_oldest(configured, db, monkeypatch):
    monkeypatch.setattr(handover, "MAX_PENDING_DEFERRED", 1)
    assert run(["drain"]) == job.EXIT_OK
    assert defer(db, identity="wamid.first") is not None
    assert defer(db, identity="wamid.second") is None
    assert [e.provider_message_id for e in handover.pending_inbound(db, tenant_id=db.tenant_id)] \
        == ["wamid.first"]


def test_no_tenant_allowlist_is_a_precondition_failure(configured, db, monkeypatch):
    monkeypatch.delenv(pg.ENV_TENANT_ALLOWLIST, raising=False)
    assert run(["status"]) == job.EXIT_USAGE


def test_more_tenants_than_it_inspects_is_refused_rather_than_truncated(configured, db,
                                                                        monkeypatch):
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST,
                       ",".join(str(i) for i in range(1, recovery.MAX_TENANTS_CONSIDERED + 2)))
    assert run(["status"]) == job.EXIT_USAGE


def test_a_database_that_cannot_be_read_is_never_settled(configured, db, monkeypatch):
    monkeypatch.setattr(job, "session", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert run(["settle"]) == job.EXIT_FAILED


def test_status_reports_and_decides_nothing(configured, db):
    assert run(["drain"]) == job.EXIT_OK
    defer(db, identity="wamid.listed")
    assert run(["status"]) == job.EXIT_OK
    assert barrier(db).draining is True                   # status changed nothing


def test_every_line_carries_the_operator_prefix(configured, db, capsys):
    run(["status"])
    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert printed and all(line.startswith(job.LOG_PREFIX) for line in printed)
