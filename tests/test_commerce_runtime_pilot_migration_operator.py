"""The pilot schema job's contract and its refusals (no database, no Alembic).

The job exists to be run once, deliberately, by an operator against a database
the application itself may never advance. What is proved here is that it cannot
be run by accident, cannot run against the wrong database, cannot repair a
half-present schema, and cannot report success it did not achieve.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from scripts.operators import commerce_runtime_pilot_migration as job
from scripts.operators import commerce_runtime_pilot_migration_contract as k


def observation(*, revisions=("0107",), present=()) -> Dict[str, Any]:
    return {
        "alembic_version": tuple(sorted(revisions)),
        "present": tuple(present),
        "missing": tuple(name for name in k.RUNTIME_RELATIONS if name not in present),
    }


# ── The contract ─────────────────────────────────────────────────────────────


def test_the_job_targets_a_pinned_revision_and_never_head():
    assert k.TARGET_REVISION == "0109"
    argv = k.build_upgrade_argv(python_executable="python")
    assert argv == ["python", "-m", "alembic", "upgrade", "0109"]
    assert "head" not in argv


def test_the_nine_relations_are_the_whole_change():
    assert len(k.RUNTIME_RELATIONS) == 9
    assert set(k.RUNTIME_RELATIONS) == set(k.FOUNDATION_RELATIONS) | set(k.LEDGER_RELATIONS)
    assert all(name.startswith("commerce_runtime_") for name in k.RUNTIME_RELATIONS)


def test_the_declared_relations_are_the_ones_the_runtime_itself_requires():
    from core.commerce_runtime import models as m
    from core.commerce_runtime.repositories import LEDGER_RELATIONS as runtime_ledger

    assert set(k.LEDGER_RELATIONS) == set(runtime_ledger)
    assert set(k.FOUNDATION_RELATIONS) == {m.CONVERSATIONS_TABLE, m.TURNS_TABLE, m.TERMINALS_TABLE}


def test_the_target_is_beyond_normal_bootstrap_and_on_the_application_chain():
    """What this job's target must satisfy, stated as its own properties.

    It used to be asserted as equality with ``APPLICATION_ALEMBIC_HEAD``.
    That coupled a deliberately BOUNDED component migration to every later
    application migration: the moment an unrelated revision extended the
    chain, this job "failed" although nothing about it had changed and its
    nine-relation scope was still exactly right. Equality was never the
    property worth holding — these are.
    """
    import os
    from pathlib import Path

    from alembic.script import ScriptDirectory

    from scripts.operators.bootstrap_migration_contract import (
        APPLICATION_ALEMBIC_HEAD,
        INTEGRATION_BOOTSTRAP_TARGET,
        REPOSITORY_ALEMBIC_HEADS,
    )

    repo = Path(__file__).resolve().parents[1]
    prev = os.getcwd()
    try:
        os.chdir(repo / "database")
        script = ScriptDirectory(str(repo / "database" / "migrations"))
    finally:
        os.chdir(prev)

    # 1. The target is a real revision, and the job names it literally.
    target = script.get_revision(k.TARGET_REVISION)
    assert target is not None
    assert k.build_upgrade_argv(python_executable="python")[-1] == k.TARGET_REVISION

    # 2. It is deliberately beyond what normal bootstrap applies, which is
    #    why an operator has to run this job at all.
    assert INTEGRATION_BOOTSTRAP_TARGET != k.TARGET_REVISION
    bootstrap_chain = set()
    node = script.get_revision(INTEGRATION_BOOTSTRAP_TARGET)
    while node is not None:
        bootstrap_chain.add(node.revision)
        down = node.down_revision
        node = script.get_revision(down) if isinstance(down, str) else None
    assert k.TARGET_REVISION not in bootstrap_chain

    # 3. It is ON the application chain: the application head reaches it by
    #    ancestry, so this job never leaves a pilot database on a branch
    #    the application does not know.
    ancestry = set()
    node = script.get_revision(APPLICATION_ALEMBIC_HEAD)
    while node is not None:
        ancestry.add(node.revision)
        down = node.down_revision
        node = script.get_revision(down) if isinstance(down, str) else None
    assert k.TARGET_REVISION in ancestry
    assert k.FOUNDATION_REVISION in ancestry

    # 4. It does not traverse the parallel A1 head. That branch is not this
    #    job's business and must not be dragged in.
    parallel = sorted(REPOSITORY_ALEMBIC_HEADS - {APPLICATION_ALEMBIC_HEAD})
    assert parallel, "the repository is expected to keep a parallel head"
    for other_head in parallel:
        assert other_head not in ancestry


def test_revisions_after_the_target_are_outside_this_job():
    """Later application revisions exist, and this job does not apply them.

    A revision beyond the target is someone else's change. The contract
    stays the nine commerce-runtime relations; the job's command stops at
    its own target and an already-applied pilot database is recognised at
    that target, not at whatever the application head has become.
    """
    import os
    from pathlib import Path

    from alembic.script import ScriptDirectory

    from scripts.operators.bootstrap_migration_contract import APPLICATION_ALEMBIC_HEAD

    repo = Path(__file__).resolve().parents[1]
    prev = os.getcwd()
    try:
        os.chdir(repo / "database")
        script = ScriptDirectory(str(repo / "database" / "migrations"))
    finally:
        os.chdir(prev)

    beyond = []
    node = script.get_revision(APPLICATION_ALEMBIC_HEAD)
    while node is not None and node.revision != k.TARGET_REVISION:
        beyond.append(node.revision)
        down = node.down_revision
        node = script.get_revision(down) if isinstance(down, str) else None
    assert node is not None, "the target must be an ancestor of the application head"

    for revision in beyond:
        assert revision not in k.build_upgrade_argv(python_executable="python")
        assert not any(revision in frozen for frozen in k.ACCEPTED_START_REVISIONS)
        assert k.already_applied(frozenset({revision})) is False
    # The pilot database is "done" at the target, whatever came after it.
    assert k.already_applied(frozenset({k.TARGET_REVISION})) is True
    assert len(k.RUNTIME_RELATIONS) == 9

    # And the reason the target may stop where it does: the revisions
    # beyond it touch none of the nine relations this job is contracted to
    # create, so not applying them leaves nothing of this job's undone.
    versions = repo / "database" / "migrations" / "versions"
    for revision in beyond:
        sources = [
            path.read_text(encoding="utf-8")
            for path in versions.glob(f"{revision}_*.py")
        ]
        assert sources, f"revision {revision} has no migration file"
        for source in sources:
            for relation in k.RUNTIME_RELATIONS:
                assert relation not in source, (revision, relation)

    # The target itself is where the ledger relations arrive.
    target_sources = "".join(
        path.read_text(encoding="utf-8")
        for path in versions.glob(f"{k.TARGET_REVISION}_*.py")
    )
    assert target_sources
    for relation in k.LEDGER_RELATIONS:
        assert relation in target_sources


@pytest.mark.parametrize("revisions, accepted", [
    (frozenset({"0107"}), True),
    (frozenset({"0088", "0107"}), True),
    (frozenset({"0108"}), True),
    (frozenset({"0093"}), False),
    (frozenset({"0105"}), False),
    (frozenset({"0092", "0107"}), False),
    (frozenset(), False),
])
def test_only_known_starting_revisions_are_accepted(revisions, accepted):
    assert k.start_state_accepted(revisions) is accepted


def test_an_already_applied_database_is_recognised_rather_than_migrated_again():
    assert k.already_applied(frozenset({"0109"})) is True
    assert k.already_applied(frozenset({"0088", "0109"})) is True
    assert k.already_applied(frozenset({"0108"})) is False


@pytest.mark.parametrize("value, expected", [
    (None, k.DEFAULT_TIMEOUT_SEC),
    ("", k.DEFAULT_TIMEOUT_SEC),
    ("nonsense", k.DEFAULT_TIMEOUT_SEC),
    ("1", k.MIN_TIMEOUT_SEC),
    ("999999", k.MAX_TIMEOUT_SEC),
    ("600", 600),
])
def test_the_timeout_is_clamped_into_its_declared_range(value, expected):
    assert k.clamp_timeout(value) == expected


# ── Refusals ─────────────────────────────────────────────────────────────────


def test_the_job_does_not_run_without_its_explicit_confirmation(monkeypatch, capsys):
    monkeypatch.delenv(k.CONFIRMATION_ENV, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.internal:5432/nahla")
    assert job.main([]) == k.EXIT_USAGE
    out = capsys.readouterr().out
    assert f"RESULT={k.RESULT_FAILED_PRECONDITION}" in out
    assert k.CONFIRMATION_TOKEN in out


def test_a_wrong_confirmation_token_is_not_a_confirmation(monkeypatch):
    monkeypatch.setenv(k.CONFIRMATION_ENV, "yes")
    assert job.confirmed() is False
    monkeypatch.setenv(k.CONFIRMATION_ENV, k.CONFIRMATION_TOKEN)
    assert job.confirmed() is True


@pytest.mark.parametrize("url, reason", [
    ("", "DATABASE_URL_unresolved"),
    ("   ", "DATABASE_URL_unresolved"),
    ("postgresql://u:p@localhost:5432/nahla", "DATABASE_URL_is_local"),
    ("postgresql://u:p@127.0.0.1:5433/nahla", "DATABASE_URL_is_local"),
])
def test_a_local_or_missing_database_url_is_never_the_pilot_database(url, reason):
    resolved, refusal = job.database_url({"DATABASE_URL": url})
    assert resolved is None and refusal == reason


def test_a_remote_database_url_is_accepted_as_given():
    resolved, refusal = job.database_url({"DATABASE_URL": "postgresql://u:p@db.railway:5432/n"})
    assert refusal is None and resolved.endswith("/n")


def test_the_job_refuses_a_local_database_even_when_confirmed(monkeypatch, capsys):
    monkeypatch.setenv(k.CONFIRMATION_ENV, k.CONFIRMATION_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/nahla")
    assert job.main([]) == k.EXIT_USAGE
    assert "DATABASE_URL_is_local" in capsys.readouterr().out


# ── Shape classification and outcomes ────────────────────────────────────────


@pytest.mark.parametrize("present, shape", [
    ((), "fresh"),
    (k.RUNTIME_RELATIONS, "complete"),
    (k.FOUNDATION_RELATIONS, "partial"),
    (k.RUNTIME_RELATIONS[:1], "partial"),
])
def test_the_runtime_schema_shape_is_classified_exactly(present, shape):
    assert job.classify(observation(present=present)) == shape


def _prepare(monkeypatch, before, after=None, rc=0):
    monkeypatch.setenv(k.CONFIRMATION_ENV, k.CONFIRMATION_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.railway:5432/nahla")
    monkeypatch.setattr(job, "database_directory", lambda: "/tmp")
    states = [before] + ([after] if after is not None else [])
    monkeypatch.setattr(job, "observe", lambda url: states.pop(0) if states else before)
    calls = []
    monkeypatch.setattr(job, "run_alembic",
                        lambda **kwargs: (calls.append(kwargs) or rc))
    return calls


def test_a_fresh_accepted_database_is_migrated_and_verified(monkeypatch, capsys):
    calls = _prepare(monkeypatch,
                     observation(revisions=("0107",), present=()),
                     observation(revisions=("0109",), present=k.RUNTIME_RELATIONS))
    assert job.main([]) == k.EXIT_SUCCESS
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert f"RESULT={k.RESULT_SUCCESS}" in out and "relations=9" in out


def test_an_already_migrated_database_is_a_no_op_and_runs_nothing(monkeypatch, capsys):
    calls = _prepare(monkeypatch, observation(revisions=("0109",), present=k.RUNTIME_RELATIONS))
    assert job.main([]) == k.EXIT_SUCCESS
    assert calls == []
    assert f"RESULT={k.RESULT_ALREADY_APPLIED}" in capsys.readouterr().out


def test_a_partial_schema_is_refused_rather_than_repaired(monkeypatch, capsys):
    calls = _prepare(monkeypatch, observation(revisions=("0107",), present=k.FOUNDATION_RELATIONS))
    assert job.main([]) == k.EXIT_PRECONDITION
    assert calls == []
    out = capsys.readouterr().out
    assert "partial_runtime_schema" in out and f"RESULT={k.RESULT_FAILED_PRECONDITION}" in out


def test_an_unexpected_starting_revision_is_refused_with_what_was_observed(monkeypatch, capsys):
    calls = _prepare(monkeypatch, observation(revisions=("0093",), present=()))
    assert job.main([]) == k.EXIT_PRECONDITION
    assert calls == []
    out = capsys.readouterr().out
    assert "unexpected_start_revision" in out and "0093" in out


def test_a_non_zero_alembic_exit_is_a_failure_even_if_the_tables_appeared(monkeypatch, capsys):
    _prepare(monkeypatch,
             observation(revisions=("0107",), present=()),
             observation(revisions=("0109",), present=k.RUNTIME_RELATIONS),
             rc=1)
    assert job.main([]) == k.EXIT_FAILED
    assert f"RESULT={k.RESULT_FAILED}" in capsys.readouterr().out


def test_a_zero_exit_that_left_the_schema_incomplete_is_still_a_failure(monkeypatch, capsys):
    _prepare(monkeypatch,
             observation(revisions=("0107",), present=()),
             observation(revisions=("0109",), present=k.FOUNDATION_RELATIONS))
    assert job.main([]) == k.EXIT_FAILED
    out = capsys.readouterr().out
    assert f"RESULT={k.RESULT_FAILED}" in out and "missing" in out


def test_a_zero_exit_that_did_not_reach_the_target_revision_is_a_failure(monkeypatch, capsys):
    _prepare(monkeypatch,
             observation(revisions=("0107",), present=()),
             observation(revisions=("0108",), present=k.RUNTIME_RELATIONS))
    assert job.main([]) == k.EXIT_FAILED
    assert f"RESULT={k.RESULT_FAILED}" in capsys.readouterr().out


def test_every_line_the_job_prints_is_grep_able_under_one_prefix(monkeypatch, capsys):
    _prepare(monkeypatch,
             observation(revisions=("0107",), present=()),
             observation(revisions=("0109",), present=k.RUNTIME_RELATIONS))
    job.main([])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines and all(line.startswith(k.LOG_PREFIX) for line in lines)
