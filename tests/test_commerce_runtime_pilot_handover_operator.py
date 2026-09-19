"""The handover procedure, executed: barrier, convergence, disposition, evidence.

Every step runs against a real ``tenant_settings`` row in an isolated SQLite
database, so the barrier these cases read and write is the one the runtime reads
— not a double of it. Only the runtime *work counts* are supplied, because they
come from the nine runtime relations, which are proved on real PostgreSQL in
``tests/commerce_reliability/test_commerce_runtime_pilot_pg.py``.

What is held here is the thing the second closure review asked for: that the
procedure is executable and that each of its refusals names a concrete reason,
rather than a verdict resting on someone asserting the fleet was restarted.
"""
from __future__ import annotations

import datetime as dt
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
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.commerce_runtime import handover  # noqa: E402
from core.commerce_runtime import pilot_guard as pg  # noqa: E402
from core.commerce_runtime import recovery  # noqa: E402
from database.models import Base, Tenant  # noqa: E402
from scripts.operators import commerce_runtime_pilot_handover as job  # noqa: E402

TENANT = 4242


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target: Any, connection: Any, **kw: Any) -> None:
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest.fixture()
def db() -> Any:
    # StaticPool: one in-memory database shared by every connection, so a
    # second session (the handover barrier opens its own) sees the same rows
    # instead of a fresh empty database.
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.tenant_id = tenant.id                        # type: ignore[attr-defined]
    yield session
    session.close()
    engine.dispose()


def state(tenant_id: int, *, open_turns: int = 0, reserved: int = 0,
          unresolved: int = 0, unknown: int = 0) -> recovery.HandoverState:
    return recovery.HandoverState(tenant_id=tenant_id, open_turns=open_turns,
                                  reserved_undispatched=reserved,
                                  unresolved_attempts=unresolved, unknown_outcomes=unknown)


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(db.tenant_id))
    monkeypatch.setattr(job, "session", lambda: db)
    monkeypatch.setattr(job, "observe", lambda tenants: tuple(state(t) for t in tenants))
    handover._last_heartbeat.clear()


def run(args: List[str]) -> int:
    return job.main(args)


def converged(db: Any, *, generation: Optional[int] = None) -> None:
    """One worker reports the current generation, as a live replica would."""
    barrier = handover.read_barrier(db, tenant_id=db.tenant_id)
    handover.note_worker(db, tenant_id=db.tenant_id, state=barrier.state, force=True)
    if generation is not None:
        assert handover.read_barrier(db, tenant_id=db.tenant_id).generation == generation


# ── The barrier is shared state, not a per-process flag ─────────────────────


def test_a_tenant_with_no_barrier_yet_admits_work(db):
    barrier = handover.read_barrier(db, tenant_id=db.tenant_id)
    assert barrier.state == handover.STATE_OPEN and barrier.admits_new_work is True


def test_draining_closes_the_barrier_for_every_reader(db):
    handover.open_drain(db, tenant_id=db.tenant_id)
    assert handover.barrier_admits_new_work(db, tenant_id=db.tenant_id) is False
    # A second reader — another replica — sees the same thing, because it is the
    # same row rather than the same environment variable.
    assert handover.read_barrier(db, tenant_id=db.tenant_id).draining is True


def test_each_drain_bumps_the_generation_so_a_stale_worker_is_visible(db):
    first = handover.open_drain(db, tenant_id=db.tenant_id).generation
    second = handover.open_drain(db, tenant_id=db.tenant_id).generation
    assert second == first + 1


def test_a_barrier_that_cannot_be_read_refuses_new_work(db, monkeypatch):
    def explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(handover, "read_barrier", explode)
    assert handover.barrier_admits_new_work(db, tenant_id=db.tenant_id) is False


# ── Convergence is observed, not asserted ───────────────────────────────────


def test_a_fleet_nobody_has_heard_from_is_not_converged(configured, db):
    handover.open_drain(db, tenant_id=db.tenant_id)
    convergence = handover.read_barrier(db, tenant_id=db.tenant_id).convergence()
    assert convergence["converged"] is False
    assert convergence["on_generation"] == []


def test_a_worker_reporting_the_current_generation_converges_the_fleet(configured, db):
    handover.open_drain(db, tenant_id=db.tenant_id)
    converged(db)
    convergence = handover.read_barrier(db, tenant_id=db.tenant_id).convergence()
    assert convergence["converged"] is True and convergence["behind"] == []


def test_a_worker_still_on_the_previous_generation_blocks_convergence(configured, db):
    converged(db)                                        # reports generation 0
    handover.open_drain(db, tenant_id=db.tenant_id)      # now generation 1
    convergence = handover.read_barrier(db, tenant_id=db.tenant_id).convergence()
    assert convergence["converged"] is False
    assert convergence["unknown_disposition"]            # alive, not heard from since


def test_a_worker_gone_long_enough_to_be_gone_does_not_block(configured, db):
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=handover.WORKER_LIVE_SECONDS + 60)
    barrier = handover.Barrier(
        tenant_id=db.tenant_id, state=handover.STATE_DRAINING, generation=2,
        opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10),
        workers=(handover.WorkerReport("live:1", 2, "draining",
                                       dt.datetime.now(dt.timezone.utc)),
                 handover.WorkerReport("departed:2", 1, "on", stale)))
    convergence = barrier.convergence()
    assert convergence["converged"] is True
    assert convergence["on_generation"] == ["live:1"]


# ── The procedure ───────────────────────────────────────────────────────────


def test_status_names_every_blocker_rather_than_answering_no(configured, db, capsys):
    assert run(["status"]) == job.EXIT_OK
    out = capsys.readouterr().out
    assert "barrier_is_open_not_draining" in out
    assert f"RESULT={job.RESULT_REPORTED}" in out


def test_drain_then_converge_then_settle_is_the_whole_procedure(configured, db, capsys):
    assert run(["drain"]) == job.EXIT_OK
    assert f"RESULT={job.RESULT_DRAINING}" in capsys.readouterr().out

    # Not settled yet: nothing has reported the new generation.
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert "no_worker_has_reported_this_generation" in capsys.readouterr().out

    converged(db)
    assert run(["settle"]) == job.EXIT_OK
    out = capsys.readouterr().out
    assert f"RESULT={job.RESULT_SETTLED}" in out
    assert "COMMERCE_RUNTIME_PILOT_ENABLED=false" in out

    barrier = handover.read_barrier(db, tenant_id=db.tenant_id)
    assert barrier.state == handover.STATE_SETTLED
    # The evidence was written before anything reopened.
    assert barrier.evidence["convergence"]["converged"] is True
    assert barrier.evidence["work"]["settled"] is True


@pytest.mark.parametrize("counts, blocker", [
    ({"open_turns": 1}, "open_turns=1"),
    ({"reserved": 2}, "reserved_undispatched=2"),
    ({"unresolved": 1}, "unresolved_attempts=1"),
    ({"unknown": 3}, "unknown_outcomes=3"),
])
def test_any_outstanding_work_blocks_settlement_by_name(configured, db, monkeypatch, capsys,
                                                        counts, blocker):
    monkeypatch.setattr(job, "observe",
                        lambda tenants: tuple(state(t, **counts) for t in tenants))
    run(["drain"])
    converged(db)
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert blocker in capsys.readouterr().out


def test_an_unknown_outcome_never_ages_out_of_the_blockers(configured, db, monkeypatch, capsys):
    """Elapsed time and a cancelled wait are not evidence of non-delivery."""
    monkeypatch.setattr(job, "observe",
                        lambda tenants: tuple(state(t, unknown=1) for t in tenants))
    run(["drain"])
    converged(db)
    for _ in range(3):                                   # however often it is asked
        assert run(["settle"]) == job.EXIT_BLOCKED
    assert "unknown_outcomes=1" in capsys.readouterr().out


def test_settling_is_refused_while_the_barrier_is_not_draining(configured, db, capsys):
    converged(db)
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert "barrier_is_open_not_draining" in capsys.readouterr().out


def test_a_database_that_cannot_be_read_is_never_settled(configured, db, monkeypatch, capsys):
    monkeypatch.setattr(job, "observe",
                        lambda tenants: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert run(["settle"]) == job.EXIT_FAILED
    assert f"RESULT={job.RESULT_FAILED}" in capsys.readouterr().out


# ── Buffered work is never acknowledged and dropped ─────────────────────────


def test_buffered_work_blocks_settlement_until_it_is_disposed(configured, db, capsys):
    run(["drain"])
    converged(db)
    assert handover.buffer_inbound(db, tenant_id=db.tenant_id,
                                   provider_message_id="wamid.buffered",
                                   recipient="+966500000001",
                                   reason="handover_draining") is True
    assert run(["settle"]) == job.EXIT_BLOCKED
    assert "buffered_awaiting_disposition=1" in capsys.readouterr().out

    assert run(["dispose", "--note", "replayed by hand", "--by", "owner"]) == job.EXIT_OK
    assert run(["settle"]) == job.EXIT_OK
    barrier = handover.read_barrier(db, tenant_id=db.tenant_id)
    entry = barrier.buffered[0]
    assert entry["disposition"] == "replayed by hand" and entry["disposed_by"] == "owner"
    # The record survives settlement: what was buffered is part of the evidence.
    assert barrier.evidence["buffered_total"] == 1


def test_disposing_without_saying_what_was_done_is_refused(configured, db, capsys):
    run(["drain"])
    assert run(["dispose", "--by", "owner"]) == job.EXIT_USAGE
    assert "disposition_note_required" in capsys.readouterr().out


def test_the_same_inbound_is_buffered_once(configured, db):
    run(["drain"])
    first = handover.buffer_inbound(db, tenant_id=db.tenant_id, provider_message_id="wamid.x",
                                    recipient="+966500000001", reason="handover_draining")
    second = handover.buffer_inbound(db, tenant_id=db.tenant_id, provider_message_id="wamid.x",
                                     recipient="+966500000001", reason="handover_draining")
    assert (first, second) == (True, False)
    assert len(handover.read_barrier(db, tenant_id=db.tenant_id).buffered) == 1


def test_a_full_buffer_refuses_rather_than_discarding_the_oldest(configured, db, monkeypatch):
    monkeypatch.setattr(handover, "MAX_BUFFERED_ENTRIES", 2)
    run(["drain"])
    for n in range(3):
        handover.buffer_inbound(db, tenant_id=db.tenant_id, provider_message_id=f"wamid.{n}",
                                recipient="+966500000001", reason="handover_draining")
    buffered = handover.read_barrier(db, tenant_id=db.tenant_id).buffered
    assert [entry["provider_message_id"] for entry in buffered] == ["wamid.0", "wamid.1"]


# ── Reopening keeps the audit trail ─────────────────────────────────────────


def test_reopening_before_settlement_is_refused(configured, db, capsys):
    run(["drain"])
    assert run(["reopen"]) == job.EXIT_USAGE
    assert "not_settled" in capsys.readouterr().out


def test_reopening_after_settlement_admits_work_again_and_keeps_the_evidence(configured, db):
    run(["drain"])
    converged(db)
    handover.buffer_inbound(db, tenant_id=db.tenant_id, provider_message_id="wamid.kept",
                            recipient="+966500000001", reason="handover_draining")
    run(["dispose", "--note", "answered by hand", "--by", "owner"])
    assert run(["settle"]) == job.EXIT_OK
    assert run(["reopen"]) == job.EXIT_OK

    barrier = handover.read_barrier(db, tenant_id=db.tenant_id)
    assert barrier.state == handover.STATE_OPEN and barrier.admits_new_work is True
    assert barrier.evidence and barrier.buffered                      # the trail survives
    assert barrier.workers == ()                                      # a fresh generation


# ── Scope ───────────────────────────────────────────────────────────────────


def test_without_an_allowlist_the_job_refuses(monkeypatch, db, capsys):
    monkeypatch.delenv(pg.ENV_TENANT_ALLOWLIST, raising=False)
    monkeypatch.setattr(job, "session", lambda: db)
    assert run(["status"]) == job.EXIT_USAGE
    assert "no_tenant_allowlist" in capsys.readouterr().out


def test_more_tenants_than_it_inspects_is_refused_rather_than_truncated(monkeypatch, db,
                                                                        capsys):
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST,
                       ",".join(str(n) for n in range(1, recovery.MAX_TENANTS_CONSIDERED + 2)))
    monkeypatch.setattr(job, "session", lambda: db)
    asked: List[Any] = []
    monkeypatch.setattr(job, "observe", lambda tenants: asked.append(tenants) or ())
    assert run(["status"]) == job.EXIT_USAGE
    assert "tenant_allowlist_too_large" in capsys.readouterr().out
    assert asked == []


def test_the_tenants_acted_on_are_the_pilot_s_own_allowlist():
    assert job.configured_tenants({pg.ENV_TENANT_ALLOWLIST: "7, 4242, abc, -1, 0, 7"}) == [7, 4242]


@pytest.mark.parametrize("env, expected", [
    ({}, "off"),
    ({pg.ENV_ENABLED: "true"}, "on"),
    ({pg.ENV_ENABLED: "true", pg.ENV_DRAINING: "true"}, "draining"),
    ({pg.ENV_DRAINING: "true"}, "off"),
])
def test_the_process_flags_are_reported_separately_from_the_barrier(env: Dict[str, str],
                                                                    expected: str):
    assert job.mode(env) == expected


def test_every_line_the_job_prints_is_grep_able_under_one_prefix(configured, db, capsys):
    run(["status"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines and all(line.startswith(job.LOG_PREFIX) for line in lines)


def test_settled_means_every_count_is_zero():
    assert state(1).settled is True
    for counts in ({"open_turns": 1}, {"reserved": 1}, {"unresolved": 1}, {"unknown": 1}):
        assert state(1, **counts).settled is False
