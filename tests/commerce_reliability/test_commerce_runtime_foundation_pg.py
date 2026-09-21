"""Dormant commerce runtime foundation, proven on real PostgreSQL.

Every test runs against a disposable database created for this module and
migrated with the repository's own Alembic chain to revision ``0108``;
concurrency cases use independent spawned processes with their own
connections. Requires ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and
``NAHLA_RELIABILITY_PG_ADMIN_DSN``; without them the module skips and that
skip is reported, never counted as a pass.

These tests prove the *foundation* only. They do not touch, measure or
retire the UC-01 / UC-02 baseline allowances of the reliability gate.
"""
from __future__ import annotations

import dataclasses
import multiprocessing as mp
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from core.commerce_runtime import contracts as c
from core.commerce_runtime import models as m
from core.commerce_runtime.repositories import CommerceRuntimeRepository
from tests.commerce_reliability import commerce_runtime_workers as w

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "0107"      # integration-bootstrap chain head before this slice
THIS_REVISION = "0108"
OTHER_HEAD = "0092"         # pre-existing A1-Validate branch head, untouched
# The application head. 0109 (ledgers) carries two siblings: 0111 (handover),
# which is the application head, and 0110 (address provenance). Neither is an
# ancestor of the other.
APPLICATION_HEAD = "0111"
TABLES = (m.CONVERSATIONS_TABLE, m.TURNS_TABLE, m.TERMINALS_TABLE)
CHANNEL = "wa:connection-1"
LIVE = c.Namespace.LIVE
SHADOW = c.Namespace.SHADOW


# ── Disposable database helpers ──────────────────────────────────────────────


def _create_database(admin_dsn: str) -> Tuple[str, str]:
    name = "nahla_runtime_" + uuid.uuid4().hex[:10]
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f"CREATE DATABASE \"{name}\" ENCODING 'UTF8' TEMPLATE template0"))
    admin.dispose()
    return name, admin_dsn.rsplit("/", 1)[0] + "/" + name


def _drop_database(admin_dsn: str, name: str) -> None:
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _alembic_config(dsn: str):
    from alembic.config import Config  # noqa: PLC0415

    cfg = Config(str(REPO_ROOT / "database" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def _alembic(dsn: str, revision: str, *, downgrade: bool = False) -> None:
    """Run the repository's own migration chain against ``dsn``."""
    from alembic import command  # noqa: PLC0415

    previous_cwd, previous_url = os.getcwd(), os.environ.get("DATABASE_URL")
    os.chdir(REPO_ROOT / "database")
    os.environ["DATABASE_URL"] = dsn
    try:
        (command.downgrade if downgrade else command.upgrade)(_alembic_config(dsn), revision)
    finally:
        os.chdir(previous_cwd)
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _script_heads() -> set:
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    return set(ScriptDirectory.from_config(_alembic_config("postgresql://unused")).get_heads())


def _current_revisions(engine) -> set:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))}


def _trigger_count(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = :n AND NOT tgisinternal"),
            {"n": m.TERMINAL_IMMUTABLE_TRIGGER},
        ).scalar())


def _function_present(engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(
            text("SELECT 1 FROM pg_proc WHERE proname = :n"), {"n": m.TERMINAL_IMMUTABLE_FUNCTION},
        ).scalar())


def _seed_tenant(engine, label: str) -> int:
    with engine.begin() as conn:
        return int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر تجريبي عام {label} {uuid.uuid4().hex[:8]}"},
        ).scalar_one())


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
    out["trigger_count"] = _trigger_count(engine)
    out["function"] = _function_present(engine)
    return out


# ── Process helpers ──────────────────────────────────────────────────────────


def _run_workers(targets: Sequence[Tuple[Callable, tuple]], timeout: float = 120.0) -> List[Dict[str, Any]]:
    """Run workers in separate spawned processes; fail loudly on hangs."""
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(targets))
    out = ctx.Queue()
    procs = [ctx.Process(target=fn, args=(*args, barrier, out)) for fn, args in targets]
    for proc in procs:
        proc.start()
    results: List[Dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while len(results) < len(targets):
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"workers hung; results so far: {results}"
        results.append(out.get(timeout=remaining))
    for proc in procs:
        proc.join(timeout=30)
    assert all(proc.exitcode == 0 for proc in procs), [proc.exitcode for proc in procs]
    return results


def _run_crash_worker(fn: Callable, args: tuple, timeout: float = 120.0) -> Tuple[int, List[Dict[str, Any]]]:
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    proc = ctx.Process(target=fn, args=(*args, out))
    proc.start()
    messages: List[Dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while proc.is_alive() or not out.empty():
        try:
            messages.append(out.get(timeout=0.5))
        except Exception:  # noqa: BLE001 — queue.Empty while the worker is still running
            if not proc.is_alive():
                break
        assert time.monotonic() < deadline, "crash worker hung"
    proc.join(timeout=30)
    return int(proc.exitcode), messages


def _statuses(results: Sequence[Dict[str, Any]]) -> List[str]:
    return sorted(r["status"] for r in results)


def _wait_past(engine, moment) -> None:
    deadline = time.monotonic() + 15
    while True:
        with engine.connect() as conn:
            if conn.execute(text("SELECT clock_timestamp() > :t"), {"t": moment}).scalar():
                return
        assert time.monotonic() < deadline, "database clock never passed the lease expiry"
        time.sleep(0.2)


def _ref() -> str:
    return "conv:" + uuid.uuid4().hex[:12]


def _pmid() -> str:
    return "wamid." + uuid.uuid4().hex


# ── Fixtures ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Foundation:
    name: str
    dsn: str
    engine: Any
    repo: CommerceRuntimeRepository
    tenant_a: int
    tenant_b: int

    def admit(self, ref: str, *, tenant: Optional[int] = None, namespace: Any = LIVE,
              pmid: Optional[str] = None, payload: Optional[dict] = None) -> c.AdmittedTurn:
        return self.repo.admit_turn(
            tenant_id=tenant or self.tenant_a, namespace=namespace, conversation_ref=ref,
            channel_connection_ref=CHANNEL, provider_message_id=pmid or _pmid(), payload=payload or {"kind": "text"},
        )

    def snapshot(self, conversation_id: int, *, tenant: Optional[int] = None, namespace: Any = LIVE):
        return self.repo.get_conversation(tenant_id=tenant or self.tenant_a, namespace=namespace,
                                          conversation_id=conversation_id)

    def claim(self, conversation_id: int, owner: str, *, seconds: int = 60, tenant: Optional[int] = None,
              namespace: Any = LIVE, turn_id: Optional[int] = None) -> c.Lease:
        return self.repo.claim(tenant_id=tenant or self.tenant_a, namespace=namespace,
                               conversation_id=conversation_id, owner_id=owner, lease_seconds=seconds,
                               turn_id=turn_id)

    def finalize(self, turn: c.AdmittedTurn, token: c.OwnershipToken, *, namespace: Any = LIVE,
                 tenant: Optional[int] = None, state: Optional[c.StateTransition] = None) -> c.TerminalRecord:
        return self.repo.record_terminal(
            tenant_id=tenant or self.tenant_a, namespace=namespace, turn_id=turn.turn_id, token=token,
            processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable",
            state_transition=state,
        )


@pytest.fixture(scope="module")
def foundation(pg_admin_dsn: str):
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, THIS_REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        handle = Foundation(
            name=name, dsn=dsn, engine=engine, repo=CommerceRuntimeRepository(engine),
            tenant_a=_seed_tenant(engine, "A"), tenant_b=_seed_tenant(engine, "B"),
        )
        yield handle
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


# ── Migration ────────────────────────────────────────────────────────────────


def test_migration_applies_cleanly_on_the_current_head_and_is_reversible(pg_admin_dsn: str) -> None:
    # 0111 extends 0107 → 0108 → 0109 and leaves the A1-Validate head alone. The
    # address revision 0110 is a sibling of 0111 (both revise 0109), so the
    # repository carries {0092, 0111} until that branch merges and
    # {0092, 0110, 0111} afterwards — either is the expected topology.
    from scripts.operators.bootstrap_migration_contract import repository_heads_expected  # noqa: PLC0415

    heads = _script_heads()
    assert {OTHER_HEAD, APPLICATION_HEAD} <= heads and repository_heads_expected(heads), \
        f"the chain must extend 0107 and leave 0092 untouched, got {sorted(heads)}"
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, PREVIOUS_HEAD)
        engine = create_engine(dsn, pool_pre_ping=True)
        assert _current_revisions(engine) == {PREVIOUS_HEAD}
        assert not set(TABLES) & set(inspect(engine).get_table_names())

        _alembic(dsn, THIS_REVISION)
        engine.dispose()
        engine = create_engine(dsn, pool_pre_ping=True)
        assert _current_revisions(engine) == {THIS_REVISION}
        assert set(TABLES) <= set(inspect(engine).get_table_names())
        assert _trigger_count(engine) == 1 and _function_present(engine)
        dump = _schema_dump(engine)
        assert "uq_commerce_runtime_turns_admission" in dump[m.TURNS_TABLE]["uniques"]
        assert "uq_commerce_runtime_turns_order" in dump[m.TURNS_TABLE]["uniques"]
        assert dump[m.TERMINALS_TABLE]["pk"] == ("turn_id",)
        assert "fk_commerce_runtime_turns_conversation_scope" in dump[m.TURNS_TABLE]["fks"]

        _alembic(dsn, PREVIOUS_HEAD, downgrade=True)
        engine.dispose()
        engine = create_engine(dsn, pool_pre_ping=True)
        assert _current_revisions(engine) == {PREVIOUS_HEAD}
        assert not set(TABLES) & set(inspect(engine).get_table_names())
        assert _trigger_count(engine) == 0 and not _function_present(engine)

        # State B: tables created by the package helper first, then the revision
        # reconciles additively (no duplicate trigger, no error).
        m.create_runtime_tables(engine)
        assert _trigger_count(engine) == 1
        _alembic(dsn, THIS_REVISION)
        engine.dispose()
        engine = create_engine(dsn, pool_pre_ping=True)
        assert _current_revisions(engine) == {THIS_REVISION}
        assert _trigger_count(engine) == 1
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def test_package_metadata_and_migration_declare_the_same_schema(pg_admin_dsn: str) -> None:
    migrated_name, migrated_dsn = _create_database(pg_admin_dsn)
    declared_name, declared_dsn = _create_database(pg_admin_dsn)
    engines = []
    try:
        _alembic(migrated_dsn, THIS_REVISION)
        _alembic(declared_dsn, PREVIOUS_HEAD)
        migrated = create_engine(migrated_dsn, pool_pre_ping=True)
        declared = create_engine(declared_dsn, pool_pre_ping=True)
        engines = [migrated, declared]
        m.create_runtime_tables(declared)
        assert _schema_dump(migrated) == _schema_dump(declared)
    finally:
        for engine in engines:
            engine.dispose()
        _drop_database(pg_admin_dsn, migrated_name)
        _drop_database(pg_admin_dsn, declared_name)


# ── Admission ────────────────────────────────────────────────────────────────


def test_duplicate_admission_resolves_to_one_durable_turn(foundation: Foundation) -> None:
    f, ref, pmid = foundation, _ref(), _pmid()
    first = f.admit(ref, pmid=pmid, payload={"text": "مرحبا"})
    again = f.admit(ref, pmid=pmid, payload={"text": "مرحبا"})
    assert (first.duplicate, first.sequence) == (False, 1)
    assert (again.duplicate, again.turn_id, again.sequence) == (True, first.turn_id, 1)

    # Four processes admit the same new message at once.
    pmid2 = _pmid()
    args = dict(tenant_id=f.tenant_a, namespace="live", conversation_ref=ref, channel_connection_ref=CHANNEL,
                provider_message_id=pmid2, payload={"text": "same message"})
    results = _run_workers([(w.admit_worker, (f.dsn, f"dup-{i}", args)) for i in range(4)])
    assert _statuses(results) == ["ok"] * 4, results
    turn_ids = {r["result"]["turn_id"] for r in results}
    assert len(turn_ids) == 1
    assert sorted(r["result"]["duplicate"] for r in results) == [False, True, True, True]
    assert {r["result"]["sequence"] for r in results} == {2}

    turns = f.repo.list_turns(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=first.conversation_id)
    assert [t.sequence for t in turns] == [1, 2]
    assert f.snapshot(first.conversation_id).next_sequence == 3


def test_concurrent_admission_preserves_unique_conversation_order(foundation: Foundation) -> None:
    f, ref_x, ref_y = foundation, _ref(), _ref()
    targets = []
    for i in range(6):
        targets.append((w.admit_worker, (f.dsn, f"x-{i}", dict(
            tenant_id=f.tenant_a, namespace="live", conversation_ref=ref_x, channel_connection_ref=CHANNEL,
            provider_message_id=_pmid(), payload={"i": i}))))
    for i in range(3):
        targets.append((w.admit_worker, (f.dsn, f"y-{i}", dict(
            tenant_id=f.tenant_a, namespace="live", conversation_ref=ref_y, channel_connection_ref=CHANNEL,
            provider_message_id=_pmid(), payload={"i": i}))))
    results = _run_workers(targets)
    assert _statuses(results) == ["ok"] * 9, results
    x = [r["result"] for r in results if r["label"].startswith("x-")]
    y = [r["result"] for r in results if r["label"].startswith("y-")]
    assert sorted(t["sequence"] for t in x) == [1, 2, 3, 4, 5, 6]
    assert sorted(t["sequence"] for t in y) == [1, 2, 3]
    assert len({t["conversation_id"] for t in x}) == 1 and len({t["conversation_id"] for t in y}) == 1
    assert len({t["turn_id"] for t in x + y}) == 9
    conv_x = f.repo.get_conversation(tenant_id=f.tenant_a, namespace=LIVE, conversation_ref=ref_x)
    stored = f.repo.list_turns(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=conv_x.conversation_id)
    assert [t.sequence for t in stored] == [1, 2, 3, 4, 5, 6]
    assert conv_x.next_sequence == 7


def test_inbound_identity_bound_to_another_conversation_is_an_explicit_conflict(foundation: Foundation) -> None:
    f, pmid, ref_1, ref_2 = foundation, _pmid(), _ref(), _ref()
    first = f.admit(ref_1, pmid=pmid)
    with pytest.raises(c.AdmissionConflict):
        f.admit(ref_2, pmid=pmid)
    # The rejected admission wrote nothing: not even the second conversation row.
    with pytest.raises(c.ConversationNotFound):
        f.repo.get_conversation(tenant_id=f.tenant_a, namespace=LIVE, conversation_ref=ref_2)
    assert f.snapshot(first.conversation_id).next_sequence == 2
    assert [t.turn_id for t in f.repo.list_turns(tenant_id=f.tenant_a, namespace=LIVE,
                                                  conversation_id=first.conversation_id)] == [first.turn_id]


def test_bounded_payloads_are_rejected_before_any_write(foundation: Foundation) -> None:
    f, ref = foundation, _ref()
    with pytest.raises(c.ValidationError):
        f.admit(ref, payload={"blob": "x" * c.MAX_PAYLOAD_BYTES})
    with pytest.raises(c.ConversationNotFound):
        f.repo.get_conversation(tenant_id=f.tenant_a, namespace=LIVE, conversation_ref=ref)
    turn = f.admit(ref)
    lease = f.claim(turn.conversation_id, "worker-a")
    with pytest.raises(c.ValidationError):
        f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                            token=lease.token, expected_revision=0, payload={"blob": "x" * c.MAX_PAYLOAD_BYTES})
    assert f.snapshot(turn.conversation_id).state_revision == 0


# ── Ownership ────────────────────────────────────────────────────────────────


def test_only_one_worker_owns_a_conversation_at_a_time(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    args = dict(tenant_id=f.tenant_a, namespace="live", conversation_id=turn.conversation_id, lease_seconds=60)
    results = _run_workers([
        (w.claim_worker, (f.dsn, f"claim-{i}", {**args, "owner_id": f"worker-{i}"})) for i in range(5)
    ])
    assert _statuses(results) == ["ok"] + ["rejected"] * 4, results
    winner = next(r for r in results if r["status"] == "ok")["result"]
    assert (winner["fence"], winner["epoch"], winner["takeover"]) == (1, 0, False)
    assert {r["reason"] for r in results if r["status"] == "rejected"} == {"lease_held"}
    with pytest.raises(c.OwnershipRejected) as late:
        f.claim(turn.conversation_id, "late-worker")
    assert late.value.reason is c.RejectReason.LEASE_HELD
    assert late.value.snapshot.lease_owner == winner["owner_id"]
    assert f.snapshot(turn.conversation_id).lease_fence == 1


def test_different_conversations_progress_independently(foundation: Foundation) -> None:
    f = foundation
    turn_1, turn_2 = f.admit(_ref()), f.admit(_ref())
    lease_a = f.claim(turn_1.conversation_id, "worker-a")
    lease_b = f.claim(turn_2.conversation_id, "worker-b")
    commit_a = f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn_1.conversation_id, turn_id=turn_1.turn_id,
                                   token=lease_a.token, expected_revision=0, payload={"owner": "a"})
    commit_b = f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn_2.conversation_id, turn_id=turn_2.turn_id,
                                   token=lease_b.token, expected_revision=0, payload={"owner": "b"})
    assert (commit_a.revision, commit_b.revision) == (1, 1)
    # A token is bound to its own conversation: the binding refuses before any ownership rule.
    with pytest.raises(c.ScopeMismatch):
        f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn_2.conversation_id, turn_id=turn_2.turn_id,
                            token=lease_a.token, expected_revision=1, payload={"owner": "a"})
    for turn, lease in ((turn_1, lease_a), (turn_2, lease_b)):
        record = f.repo.record_terminal(
            tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome=c.ProcessingOutcome.COMPLETED, transport_outcome=c.TransportOutcome.NOT_ATTEMPTED,
            customer_reach=c.CustomerReach.NOT_APPLICABLE,
        )
        assert (record.recorded_by, record.recorded_fence) == (lease.owner_id, lease.fence)
    assert f.snapshot(turn_1.conversation_id).state_payload == {"owner": "a"}
    assert f.snapshot(turn_2.conversation_id).state_payload == {"owner": "b"}


def test_lease_expiry_permits_recovery_with_a_higher_fence_and_fences_out_the_old_worker(
    foundation: Foundation,
) -> None:
    f = foundation
    turn = f.admit(_ref())
    old = f.claim(turn.conversation_id, "worker-a", seconds=1)
    assert (old.fence, old.epoch) == (1, 0)
    _wait_past(f.engine, old.expires_at)

    def attempts(token: c.OwnershipToken) -> Dict[str, c.RejectReason]:
        seen: Dict[str, c.RejectReason] = {}
        with pytest.raises(c.OwnershipRejected) as e1:
            f.repo.renew(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                         token=token, lease_seconds=60)
        seen["renew"] = e1.value.reason
        with pytest.raises(c.OwnershipRejected) as e2:
            f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                                token=token, expected_revision=0, payload={"from": token.owner_id})
        seen["commit"] = e2.value.reason
        with pytest.raises(c.OwnershipRejected) as e3:
            f.repo.record_terminal(
                tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=token,
                processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable",
            )
        seen["terminal"] = e3.value.reason
        with pytest.raises(c.OwnershipRejected) as e4:
            f.repo.release(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=token)
        seen["release"] = e4.value.reason
        return seen

    # Expired but not yet superseded: every guarded operation names the expiry.
    assert set(attempts(old.token).values()) == {c.RejectReason.EXPIRED_LEASE}
    assert f.snapshot(turn.conversation_id).state_revision == 0

    recovered = f.claim(turn.conversation_id, "worker-b")
    assert (recovered.fence, recovered.epoch, recovered.takeover) == (2, 1, True)

    # Superseded: the old fence is refused everywhere, and nothing it tried landed.
    assert set(attempts(old.token).values()) == {c.RejectReason.SUPERSEDED_FENCE}
    snap = f.snapshot(turn.conversation_id)
    assert (snap.state_revision, snap.lease_owner, snap.lease_fence, snap.ownership_epoch) == (0, "worker-b", 2, 1)
    assert f.repo.get_terminal(tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id) is None

    renewed = f.repo.renew(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                           token=recovered.token, lease_seconds=60)
    assert (renewed.fence, renewed.epoch) == (2, 1) and renewed.expires_at > recovered.expires_at
    commit = f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                                 token=recovered.token, expected_revision=0, payload={"from": "worker-b"})
    assert commit.revision == 1


def test_stale_revisions_cannot_overwrite_newer_state(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    lease = f.claim(turn.conversation_id, "worker-a")
    first = f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                                token=lease.token, expected_revision=0, payload={"step": 1})
    assert first.revision == 1
    with pytest.raises(c.StateConflict) as stale:
        f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                            token=lease.token, expected_revision=0, payload={"step": "stale"})
    assert stale.value.reason is c.RejectReason.STALE_REVISION
    assert stale.value.snapshot.state_revision == 1
    assert f.snapshot(turn.conversation_id).state_payload == {"step": 1}

    token = dataclasses.asdict(lease.token)
    results = _run_workers([
        (w.commit_state_worker, (f.dsn, f"cas-{i}", dict(
            tenant_id=f.tenant_a, namespace="live", conversation_id=turn.conversation_id, token=token,
            turn_id=turn.turn_id, expected_revision=1, payload={"step": 2, "writer": i}))) for i in range(3)
    ])
    assert _statuses(results) == ["ok", "rejected", "rejected"], results
    assert {r["reason"] for r in results if r["status"] == "rejected"} == {"stale_revision"}
    winner = next(r for r in results if r["status"] == "ok")
    snap = f.snapshot(turn.conversation_id)
    assert snap.state_revision == 2
    assert snap.state_payload == {"step": 2, "writer": int(winner["label"].split("-")[1])}


def test_ownership_epoch_change_invalidates_older_work(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    lease = f.claim(turn.conversation_id, "worker-a")
    assert (lease.fence, lease.epoch) == (1, 0)
    invalidated = f.repo.invalidate_ownership(tenant_id=f.tenant_a, namespace=LIVE,
                                              conversation_id=turn.conversation_id)
    assert (invalidated.ownership_epoch, invalidated.lease_owner, invalidated.lease_fence) == (1, None, 1)
    for op in (
        lambda: f.repo.renew(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                             token=lease.token, lease_seconds=60),
        lambda: f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                                    token=lease.token, expected_revision=0, payload={"x": 1}),
        lambda: f.repo.record_terminal(
            tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable"),
    ):
        with pytest.raises(c.OwnershipRejected) as rejected:
            op()
        assert rejected.value.reason is c.RejectReason.OBSOLETE_EPOCH
    assert f.snapshot(turn.conversation_id).state_revision == 0
    assert f.repo.get_terminal(tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id) is None
    # A cleared lease is claimed without a further epoch bump; the fence still advances.
    fresh = f.claim(turn.conversation_id, "worker-b")
    assert (fresh.fence, fresh.epoch, fresh.takeover) == (2, 1, False)


def test_fencing_identities_are_never_reset_or_reused(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    first = f.claim(turn.conversation_id, "worker-a")
    f.repo.release(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=first.token)
    after_release = f.snapshot(turn.conversation_id)
    assert (after_release.lease_owner, after_release.lease_fence, after_release.ownership_epoch) == (None, 1, 0)
    second = f.claim(turn.conversation_id, "worker-a", seconds=1)   # same worker, new fence
    assert (second.fence, second.epoch, second.takeover) == (2, 0, False)
    _wait_past(f.engine, second.expires_at)
    third = f.claim(turn.conversation_id, "worker-c")               # takeover of an expired lease
    assert (third.fence, third.epoch, third.takeover) == (3, 1, True)
    f.repo.invalidate_ownership(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id)
    fourth = f.claim(turn.conversation_id, "worker-d")
    assert (fourth.fence, fourth.epoch) == (4, 2)
    with pytest.raises(c.OwnershipRejected) as reused:
        f.repo.renew(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                     token=first.token, lease_seconds=60)
    assert reused.value.reason is c.RejectReason.SUPERSEDED_FENCE


# ── Tenant and namespace isolation ───────────────────────────────────────────


def test_cross_tenant_access_is_rejected(foundation: Foundation) -> None:
    f, ref, pmid = foundation, _ref(), _pmid()
    turn = f.admit(ref, pmid=pmid)
    lease = f.claim(turn.conversation_id, "worker-a")
    before = f.snapshot(turn.conversation_id)
    foreign = f.tenant_b
    with pytest.raises(c.ConversationNotFound):
        f.repo.get_conversation(tenant_id=foreign, namespace=LIVE, conversation_ref=ref)
    with pytest.raises(c.ConversationNotFound):
        f.repo.get_conversation(tenant_id=foreign, namespace=LIVE, conversation_id=turn.conversation_id)
    with pytest.raises(c.ConversationNotFound):
        f.claim(turn.conversation_id, "intruder", tenant=foreign)
    # Token-bearing operations refuse the foreign scope before reading anything.
    with pytest.raises(c.ScopeMismatch):
        f.repo.renew(tenant_id=foreign, namespace=LIVE, conversation_id=turn.conversation_id,
                     token=lease.token, lease_seconds=60)
    with pytest.raises(c.ScopeMismatch):
        f.repo.commit_state(tenant_id=foreign, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                            token=lease.token, expected_revision=0, payload={"stolen": True})
    with pytest.raises(c.ScopeMismatch):
        f.repo.release(tenant_id=foreign, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token)
    with pytest.raises(c.ConversationNotFound):
        f.repo.invalidate_ownership(tenant_id=foreign, namespace=LIVE, conversation_id=turn.conversation_id)
    with pytest.raises(c.TurnNotFound):
        f.repo.record_terminal(
            tenant_id=foreign, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable")
    assert f.repo.list_turns(tenant_id=foreign, namespace=LIVE, conversation_id=turn.conversation_id) == []
    assert f.repo.get_terminal(tenant_id=foreign, namespace=LIVE, turn_id=turn.turn_id) is None
    after = f.snapshot(turn.conversation_id)
    assert dataclasses.replace(after, db_now=before.db_now) == before

    # The same channel + provider message id under another tenant is a different admission.
    other = f.admit(ref, tenant=foreign, pmid=pmid)
    assert other.conversation_id != turn.conversation_id and other.sequence == 1 and not other.duplicate
    assert f.snapshot(turn.conversation_id).next_sequence == 2


def test_shadow_and_live_namespaces_are_independent(foundation: Foundation) -> None:
    f, ref = foundation, _ref()
    live_turn = f.admit(ref, namespace=LIVE)
    shadow_turn = f.admit(ref, namespace=SHADOW)
    assert live_turn.conversation_id != shadow_turn.conversation_id
    assert (live_turn.sequence, shadow_turn.sequence) == (1, 1)
    live_lease = f.claim(live_turn.conversation_id, "worker-live")
    shadow_lease = f.claim(shadow_turn.conversation_id, "worker-shadow", namespace=SHADOW)
    assert (live_lease.fence, shadow_lease.fence) == (1, 1)
    with pytest.raises(c.ConversationNotFound):
        f.repo.get_conversation(tenant_id=f.tenant_a, namespace=SHADOW, conversation_id=live_turn.conversation_id)
    with pytest.raises(c.TurnNotFound):
        f.repo.record_terminal(
            tenant_id=f.tenant_a, namespace=LIVE, turn_id=shadow_turn.turn_id, token=live_lease.token,
            processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable")
    with pytest.raises(c.ScopeMismatch):
        f.repo.commit_state(tenant_id=f.tenant_a, namespace=SHADOW, conversation_id=shadow_turn.conversation_id, turn_id=shadow_turn.turn_id,
                            token=live_lease.token, expected_revision=0, payload={"x": 1})
    shadow_record = f.repo.record_terminal(
        tenant_id=f.tenant_a, namespace=SHADOW, turn_id=shadow_turn.turn_id, token=shadow_lease.token,
        processing_outcome="completed", transport_outcome="not_attempted", customer_reach="not_applicable",
        state_transition=c.StateTransition(expected_revision=0, payload={"shadow": True}))
    assert shadow_record.namespace == "shadow"
    assert f.snapshot(live_turn.conversation_id).state_revision == 0
    assert f.snapshot(shadow_turn.conversation_id, namespace=SHADOW).state_payload == {"shadow": True}
    assert f.repo.get_terminal(tenant_id=f.tenant_a, namespace=LIVE, turn_id=live_turn.turn_id) is None


# ── Terminals ────────────────────────────────────────────────────────────────


def test_competing_completion_attempts_cannot_create_two_terminals(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    lease = f.claim(turn.conversation_id, "worker-a")
    token = dataclasses.asdict(lease.token)
    base = dict(tenant_id=f.tenant_a, namespace="live", turn_id=turn.turn_id, token=token,
                processing_outcome="completed", transport_outcome="accepted", customer_reach="reached")
    results = _run_workers([(w.terminal_worker, (f.dsn, f"fin-{i}", {**base, "details": {"i": i}}))
                            for i in range(4)])
    assert _statuses(results) == ["ok", "terminal_exists", "terminal_exists", "terminal_exists"], results
    with f.engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {m.TERMINALS_TABLE} WHERE turn_id = :t"),
                            {"t": turn.turn_id}).scalar() == 1

    # With a state transition: exactly one commit and one terminal, no partial application.
    second = f.admit(_ref())
    lease_2 = f.claim(second.conversation_id, "worker-b")
    base_2 = dict(tenant_id=f.tenant_a, namespace="live", turn_id=second.turn_id,
                  token=dataclasses.asdict(lease_2.token), processing_outcome="completed",
                  transport_outcome="accepted", customer_reach="reached")
    results_2 = _run_workers([(w.terminal_worker, (f.dsn, f"fin2-{i}", {
        **base_2, "state_transition": {"expected_revision": 0, "payload": {"writer": i}}})) for i in range(4)])
    assert [r["status"] for r in results_2].count("ok") == 1, results_2
    assert all(r["status"] in ("ok", "rejected", "terminal_exists") for r in results_2), results_2
    winner = next(r for r in results_2 if r["status"] == "ok")
    snap = f.snapshot(second.conversation_id)
    assert snap.state_revision == 1
    assert snap.state_payload == {"writer": int(winner["label"].split("-")[1])}
    with f.engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {m.TERMINALS_TABLE} WHERE conversation_id = :cid"),
                            {"cid": second.conversation_id}).scalar() == 1

    # Immutable: neither UPDATE nor DELETE can touch a terminal row.
    for statement in (
        f"UPDATE {m.TERMINALS_TABLE} SET transport_outcome = 'unknown' WHERE turn_id = :t",
        f"DELETE FROM {m.TERMINALS_TABLE} WHERE turn_id = :t",
    ):
        with pytest.raises(DBAPIError) as blocked:
            with f.engine.begin() as conn:
                conn.execute(text(statement), {"t": turn.turn_id})
        assert "immutable" in str(blocked.value)
    record = f.repo.get_terminal(tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id)
    assert record is not None and record.transport_outcome == "accepted"


def test_crash_before_commit_leaves_no_partial_transition(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    lease = f.claim(turn.conversation_id, "worker-a")
    before = f.snapshot(turn.conversation_id)
    exit_code, messages = _run_crash_worker(w.crash_before_commit_worker, (f.dsn, "crash", dict(
        tenant_id=f.tenant_a, namespace="live", turn_id=turn.turn_id, token=dataclasses.asdict(lease.token),
        processing_outcome="completed", transport_outcome="accepted", customer_reach="reached",
        state_transition={"expected_revision": 0, "payload": {"partial": True}})))
    assert exit_code == 9, (exit_code, messages)
    assert [msg["status"] for msg in messages] == ["dying_before_commit"], messages
    after = f.snapshot(turn.conversation_id)
    assert dataclasses.replace(after, db_now=before.db_now) == before
    assert after.state_revision == 0 and after.state_payload == {}
    assert f.repo.get_terminal(tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id) is None
    with f.engine.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state = 'idle in transaction'")).scalar() == 0
    # The still-valid owner can complete the turn afterwards from a consistent state.
    record = f.repo.record_terminal(
        tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
        processing_outcome="completed", transport_outcome="accepted", customer_reach="reached",
        state_transition=c.StateTransition(expected_revision=0, payload={"partial": False}))
    assert record.recorded_fence == lease.fence
    assert f.snapshot(turn.conversation_id).state_payload == {"partial": False}


def test_unknown_transport_outcome_is_recorded_as_unknown_and_grants_no_replay(foundation: Foundation) -> None:
    f, ref, pmid = foundation, _ref(), _pmid()
    turn = f.admit(ref, pmid=pmid)
    lease = f.claim(turn.conversation_id, "worker-a")
    record = f.repo.record_terminal(
        tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
        processing_outcome=c.ProcessingOutcome.FAILED, transport_outcome=c.TransportOutcome.UNKNOWN,
        customer_reach=c.CustomerReach.UNKNOWN, details={"provider_status": None, "timeout": True})
    assert (record.processing_outcome, record.transport_outcome, record.customer_reach) == ("failed", "unknown", "unknown")
    # The same inbound message admitted again is the same turn; the turn keeps its single terminal.
    again = f.admit(ref, pmid=pmid)
    assert again.duplicate and again.turn_id == turn.turn_id
    with pytest.raises(c.TerminalAlreadyRecorded) as second:
        f.repo.record_terminal(
            tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
            processing_outcome="completed", transport_outcome="accepted", customer_reach="reached")
    assert second.value.existing.transport_outcome == "unknown"
    assert not any(name in dir(f.repo) for name in ("replay", "resend", "retry", "redeliver"))


def test_every_operation_closes_its_transaction_before_returning(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    lease = f.claim(turn.conversation_id, "worker-a")
    f.repo.renew(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                 token=lease.token, lease_seconds=60)
    f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                        token=lease.token, expected_revision=0, payload={"n": 1})
    f.repo.record_terminal(
        tenant_id=f.tenant_a, namespace=LIVE, turn_id=turn.turn_id, token=lease.token,
        processing_outcome="completed", transport_outcome="accepted", customer_reach="reached")
    f.repo.release(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id, token=lease.token)
    with f.engine.connect() as conn:
        open_transactions = conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state IN ('idle in transaction', 'idle in transaction (aborted)')")).scalar()
    assert open_transactions == 0


# ── B1: the lease clock after a lock wait ────────────────────────────────────


def _hold_row_lock(dsn: str, conversation_id: int, locked: threading.Event, release: threading.Event) -> None:
    """Independent connection: lock the conversation row until told to release."""
    engine = create_engine(dsn, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            tx = conn.begin()
            conn.execute(text(f"SELECT id FROM {m.CONVERSATIONS_TABLE} WHERE id = :id FOR UPDATE"),
                         {"id": conversation_id})
            locked.set()
            release.wait(timeout=60)
            tx.rollback()
    finally:
        engine.dispose()


def _call_on_own_connection(dsn: str, fn: Callable[[CommerceRuntimeRepository], Any]) -> "queue.Queue":
    out: "queue.Queue" = queue.Queue()

    def run() -> None:
        engine = create_engine(dsn, pool_pre_ping=True)
        try:
            out.put(("ok", fn(CommerceRuntimeRepository(engine))))
        except Exception as exc:  # noqa: BLE001 — surfaced to the test, never hidden
            out.put(("err", exc))
        finally:
            engine.dispose()

    threading.Thread(target=run, daemon=True).start()
    return out


def _wait_for_lock_waiter(engine) -> None:
    deadline = time.monotonic() + 15
    while True:
        with engine.connect() as conn:
            waiting = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'")).scalar()
        if waiting:
            return
        assert time.monotonic() < deadline, "no connection is waiting on a row lock"
        time.sleep(0.05)


def _blocked_across_expiry(f: Foundation, conversation_id: int, expires_at, fn) -> Tuple[str, Any]:
    """Run ``fn`` on its own connection while another connection holds the row
    lock until the database clock passes ``expires_at``; return its outcome."""
    locked, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_row_lock, args=(f.dsn, conversation_id, locked, release), daemon=True)
    holder.start()
    assert locked.wait(timeout=30)
    try:
        out = _call_on_own_connection(f.dsn, fn)
        _wait_for_lock_waiter(f.engine)
        _wait_past(f.engine, expires_at)
    finally:
        release.set()
    holder.join(timeout=30)
    return out.get(timeout=60)


@pytest.mark.parametrize("operation", ["renew", "release", "commit", "finalize"])
def test_lock_wait_across_expiry_rejects_the_expired_owner(foundation: Foundation, operation: str) -> None:
    f = foundation
    turn = f.admit(_ref())
    lease = f.claim(turn.conversation_id, "worker-a", seconds=2)
    tid, cid, token = f.tenant_a, turn.conversation_id, lease.token
    calls = {
        "renew": lambda r: r.renew(tenant_id=tid, namespace=LIVE, conversation_id=cid, token=token, lease_seconds=60),
        "release": lambda r: r.release(tenant_id=tid, namespace=LIVE, conversation_id=cid, token=token),
        "commit": lambda r: r.commit_state(tenant_id=tid, namespace=LIVE, conversation_id=cid, token=token,
                                           turn_id=turn.turn_id, expected_revision=0, payload={"late": True}),
        "finalize": lambda r: r.record_terminal(
            tenant_id=tid, namespace=LIVE, turn_id=turn.turn_id, token=token, processing_outcome="completed",
            transport_outcome="accepted", customer_reach="reached",
            state_transition=c.StateTransition(expected_revision=0, payload={"late": True})),
    }
    status, outcome = _blocked_across_expiry(f, cid, lease.expires_at, calls[operation])
    assert status == "err", f"{operation} was accepted after the lease expired during a lock wait: {outcome!r}"
    assert isinstance(outcome, c.OwnershipRejected) and outcome.reason is c.RejectReason.EXPIRED_LEASE, outcome
    after = f.snapshot(cid)
    assert (after.state_revision, after.state_payload, after.lease_owner, after.lease_fence) == (0, {}, "worker-a", 1)
    assert f.repo.get_terminal(tenant_id=tid, namespace=LIVE, turn_id=turn.turn_id) is None


def test_lock_wait_across_expiry_lets_a_new_claimant_take_over(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    old = f.claim(turn.conversation_id, "worker-a", seconds=2)
    status, outcome = _blocked_across_expiry(
        f, turn.conversation_id, old.expires_at,
        lambda r: r.claim(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                          owner_id="worker-b", lease_seconds=30),
    )
    assert status == "ok", f"claim after waiting across the expiry was refused: {outcome!r}"
    assert (outcome.owner_id, outcome.fence, outcome.epoch, outcome.takeover) == ("worker-b", 2, 1, True)
    assert outcome.expires_at > outcome.db_now
    with f.engine.connect() as conn:
        assert conn.execute(text("SELECT clock_timestamp() < :t"), {"t": outcome.expires_at}).scalar()
    renewed = f.repo.renew(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                           token=outcome.token, lease_seconds=60)
    assert renewed.fence == 2


def test_claim_expiry_is_measured_after_the_lock_wait(foundation: Foundation) -> None:
    f = foundation
    turn = f.admit(_ref())
    with f.engine.connect() as conn:
        far_future = conn.execute(text("SELECT clock_timestamp() + interval '2 seconds'")).scalar()
    status, outcome = _blocked_across_expiry(
        f, turn.conversation_id, far_future,   # hold the lock for two seconds, longer than the lease requested
        lambda r: r.claim(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                          owner_id="worker-a", lease_seconds=1),
    )
    assert status == "ok", outcome
    assert outcome.expires_at > outcome.db_now, "lease already expired when the claim returned"
    with f.engine.connect() as conn:
        assert conn.execute(text("SELECT clock_timestamp() < :t"), {"t": outcome.expires_at}).scalar()
    renewed = f.repo.renew(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=turn.conversation_id,
                           token=outcome.token, lease_seconds=60)
    assert renewed.expires_at > outcome.expires_at


# ── B2: scope-bound ownership tokens ─────────────────────────────────────────


def test_same_owner_fence_and_epoch_cannot_cross_conversation_namespace_or_tenant(foundation: Foundation) -> None:
    f, owner, ref = foundation, "worker-same", _ref()
    source = f.admit(ref)
    other_conversation = f.admit(_ref())
    other_namespace = f.admit(ref, namespace=SHADOW)
    other_tenant = f.admit(ref, tenant=f.tenant_b)
    source_lease = f.claim(source.conversation_id, owner)
    targets = [
        ("conversation", f.tenant_a, LIVE, other_conversation, f.claim(other_conversation.conversation_id, owner)),
        ("namespace", f.tenant_a, SHADOW, other_namespace, f.claim(other_namespace.conversation_id, owner, namespace=SHADOW)),
        ("tenant", f.tenant_b, LIVE, other_tenant, f.claim(other_tenant.conversation_id, owner, tenant=f.tenant_b)),
    ]
    # Identical owner, fence and epoch everywhere: only the scope binding can tell the tokens apart.
    for _, _, _, _, lease in targets:
        assert (lease.owner_id, lease.fence, lease.epoch) == (source_lease.owner_id, source_lease.fence, source_lease.epoch)
    foreign = source_lease.token
    for label, tenant, namespace, turn, _ in targets:
        before = f.snapshot(turn.conversation_id, tenant=tenant, namespace=namespace)
        attempts = {
            "renew": lambda: f.repo.renew(tenant_id=tenant, namespace=namespace, conversation_id=turn.conversation_id,
                                          token=foreign, lease_seconds=60),
            "release": lambda: f.repo.release(tenant_id=tenant, namespace=namespace,
                                              conversation_id=turn.conversation_id, token=foreign),
            "commit": lambda: f.repo.commit_state(tenant_id=tenant, namespace=namespace,
                                                  conversation_id=turn.conversation_id, token=foreign,
                                                  turn_id=turn.turn_id, expected_revision=0, payload={"stolen": label}),
            "finalize": lambda: f.finalize(turn, foreign, namespace=namespace, tenant=tenant,
                                           state=c.StateTransition(expected_revision=0, payload={"stolen": label})),
        }
        for name, attempt in attempts.items():
            with pytest.raises(c.ScopeMismatch, match="does not match target scope"):
                attempt()
        after = f.snapshot(turn.conversation_id, tenant=tenant, namespace=namespace)
        assert dataclasses.replace(after, db_now=before.db_now) == before, (label, name)
        assert f.repo.get_terminal(tenant_id=tenant, namespace=namespace, turn_id=turn.turn_id) is None
    # Positive control: each token still works on the scope it was issued for.
    for label, tenant, namespace, turn, lease in targets:
        commit = f.repo.commit_state(tenant_id=tenant, namespace=namespace, conversation_id=turn.conversation_id,
                                     token=lease.token, turn_id=turn.turn_id, expected_revision=0, payload={"own": label})
        assert commit.revision == 1
    assert f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=source.conversation_id,
                               token=foreign, turn_id=source.turn_id, expected_revision=0, payload={"own": "source"}).revision == 1


def test_terminal_scope_is_derived_from_the_turn_not_the_caller(foundation: Foundation) -> None:
    f, owner = foundation, "worker-same"
    one, two = f.admit(_ref()), f.admit(_ref())
    lease_one, lease_two = f.claim(one.conversation_id, owner), f.claim(two.conversation_id, owner)
    assert (lease_one.fence, lease_one.epoch) == (lease_two.fence, lease_two.epoch)
    # The token of conversation one presented for a turn that belongs to conversation two.
    with pytest.raises(c.ScopeMismatch):
        f.finalize(two, lease_one.token)
    assert f.repo.get_terminal(tenant_id=f.tenant_a, namespace=LIVE, turn_id=two.turn_id) is None
    assert f.finalize(two, lease_two.token).turn_id == two.turn_id


# ── Ordered processing at the repository boundary ────────────────────────────


def test_processing_is_bound_to_the_oldest_unresolved_turn(foundation: Foundation) -> None:
    f, ref = foundation, _ref()
    first, second, third = f.admit(ref), f.admit(ref), f.admit(ref)
    assert [t.sequence for t in (first, second, third)] == [1, 2, 3]
    lease = f.claim(first.conversation_id, "worker-a")
    assert (lease.eligible_turn_id, lease.eligible_sequence) == (first.turn_id, 1)

    # Finalising or committing for sequence 2 while sequence 1 is unresolved is refused, and nothing changes.
    with pytest.raises(c.OwnershipRejected) as early:
        f.finalize(second, lease.token, state=c.StateTransition(expected_revision=0, payload={"processed_sequence": 2}))
    assert early.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    assert early.value.snapshot.eligible_turn_id == first.turn_id
    with pytest.raises(c.OwnershipRejected) as early_commit:
        f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=first.conversation_id,
                            token=lease.token, turn_id=second.turn_id, expected_revision=0, payload={"processed_sequence": 2})
    assert early_commit.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    snap = f.snapshot(first.conversation_id)
    assert (snap.state_revision, snap.state_payload, snap.eligible_sequence) == (0, {}, 1)
    assert f.repo.get_terminal(tenant_id=f.tenant_a, namespace=LIVE, turn_id=second.turn_id) is None

    # In order: commit and finalise 1, then 2, then 3; eligibility advances after each terminal.
    assert f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=first.conversation_id,
                               token=lease.token, turn_id=first.turn_id, expected_revision=0,
                               payload={"processed_sequence": 1}).revision == 1
    f.finalize(first, lease.token)
    assert f.snapshot(first.conversation_id).eligible_turn_id == second.turn_id
    with pytest.raises(c.OwnershipRejected) as skip:
        f.finalize(third, lease.token)
    assert skip.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    f.finalize(second, lease.token, state=c.StateTransition(expected_revision=1, payload={"processed_sequence": 2}))
    assert f.snapshot(first.conversation_id).state_payload == {"processed_sequence": 2}
    f.finalize(third, lease.token)
    after = f.snapshot(first.conversation_id)
    assert (after.eligible_turn_id, after.eligible_sequence) == (None, None)
    # With every turn resolved there is nothing to commit for.
    with pytest.raises(c.OwnershipRejected) as nothing:
        f.repo.commit_state(tenant_id=f.tenant_a, namespace=LIVE, conversation_id=first.conversation_id,
                            token=lease.token, turn_id=third.turn_id, expected_revision=2, payload={"x": 1})
    assert nothing.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    # A claim naming a turn that is not the eligible one is refused; naming the eligible one is not.
    ref_2 = _ref()
    a, b = f.admit(ref_2), f.admit(ref_2)
    with pytest.raises(c.OwnershipRejected) as wrong_turn:
        f.claim(a.conversation_id, "worker-b", turn_id=b.turn_id)
    assert wrong_turn.value.reason is c.RejectReason.TURN_NOT_ELIGIBLE
    assert f.snapshot(a.conversation_id).lease_owner is None
    named = f.claim(a.conversation_id, "worker-b", turn_id=a.turn_id)
    assert (named.eligible_turn_id, named.eligible_sequence) == (a.turn_id, 1)
