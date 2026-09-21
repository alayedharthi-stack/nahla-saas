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


TARGET = "db.railway:5432/nahla"
TARGET_URL = "postgresql://u:p@db.railway:5432/nahla"


def observation(*, revisions=("0107",), present=()) -> Dict[str, Any]:
    return {
        "alembic_version": tuple(sorted(revisions)),
        "present": tuple(present),
        "missing": tuple(name for name in k.RUNTIME_RELATIONS if name not in present),
    }


# ── The contract ─────────────────────────────────────────────────────────────


def test_the_job_targets_a_pinned_revision_and_never_head():
    assert k.TARGET_REVISION == "0111"
    argv = k.build_upgrade_argv(python_executable="python")
    assert argv == ["python", "-m", "alembic", "upgrade", "0111"]
    assert "head" not in argv


def test_the_confirmation_token_names_the_revision_the_job_applies():
    """The token is the operator's statement of *which* revision they authorise."""
    assert k.CONFIRMATION_TOKEN == f"RUN_COMMERCE_RUNTIME_{k.TARGET_REVISION}"
    assert k.LOG_PREFIX == f"[commerce-runtime-{k.TARGET_REVISION}]"


def test_the_rollback_is_this_branch_alone_never_the_common_ancestor():
    """``0111@-1`` steps back along this branch; ``0109`` or ``0111-1`` would
    also remove the address sibling when it is applied (proved on PostgreSQL in
    ``test_commerce_runtime_handover_migration_pg``)."""
    argv = k.build_downgrade_argv(python_executable="python")
    assert argv == ["python", "-m", "alembic", "downgrade", "0111@-1"]
    assert k.RUNTIME_ONLY_DOWNGRADE_TARGET.startswith(k.TARGET_REVISION + "@")
    assert "0109" not in argv and "head" not in argv


def test_the_address_sibling_is_a_start_state_and_never_a_dependency():
    """Runtime first, address first, or both: all valid; none required."""
    assert k.ADDRESS_SIBLING_REVISION == "0110"
    assert k.start_state_accepted(frozenset({"0110"}))
    assert k.start_state_accepted(frozenset({"0088", "0110"}))
    assert k.expected_relations_at(frozenset({"0110"})) == (
        k.FOUNDATION_RELATIONS + k.LEDGER_RELATIONS)
    assert k.already_applied(frozenset({"0110", "0111"}))
    assert k.already_applied(frozenset({"0088", "0110", "0111"}))
    # A database at 0109 and 0110 at once cannot exist: 0110 replaces 0109 on
    # its branch. It is not accepted rather than guessed at.
    assert not k.start_state_accepted(frozenset({"0109", "0110"}))


def test_the_twelve_relations_are_the_whole_change():
    assert len(k.RUNTIME_RELATIONS) == 12
    assert k.RUNTIME_RELATIONS[-3:] == k.HANDOVER_RELATIONS
    assert set(k.RUNTIME_RELATIONS) == (set(k.FOUNDATION_RELATIONS)
                                        | set(k.LEDGER_RELATIONS)
                                        | set(k.HANDOVER_RELATIONS))
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

    # And the reason the target may stop where it does: the revisions
    # beyond it touch none of the relations this job is contracted to
    # create, so not applying them leaves nothing of this job's undone.
    # The count itself is asserted once, above, against the module.
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

    # The ledger relations arrive somewhere in the range this job
    # applies — not necessarily at the target, which has since advanced
    # past them to the handover revision. What matters is that upgrading
    # to the target creates them, so the walk covers the target and every
    # revision below it down to the earliest accepted start.
    earliest_start = min(
        revision for accepted in k.ACCEPTED_START_REVISIONS for revision in accepted
    )
    applied: list[str] = []
    node = script.get_revision(k.TARGET_REVISION)
    while node is not None:
        applied.append(node.revision)
        if node.revision <= earliest_start:
            break
        down = node.down_revision
        node = script.get_revision(down) if isinstance(down, str) else None
    applied_sources = "".join(
        path.read_text(encoding="utf-8")
        for revision in applied
        for path in versions.glob(f"{revision}_*.py")
    )
    assert applied_sources
    for relation in k.LEDGER_RELATIONS:
        assert relation in applied_sources, relation


@pytest.mark.parametrize("revisions, accepted", [
    (frozenset({"0107"}), True),
    (frozenset({"0088", "0107"}), True),
    (frozenset({"0108"}), True),
    (frozenset({"0110"}), True),
    (frozenset({"0088", "0110"}), True),
    (frozenset({"0093"}), False),
    (frozenset({"0105"}), False),
    (frozenset({"0092", "0107"}), False),
    (frozenset(), False),
])
def test_only_known_starting_revisions_are_accepted(revisions, accepted):
    assert k.start_state_accepted(revisions) is accepted


def test_an_already_applied_database_is_recognised_rather_than_migrated_again():
    assert k.already_applied(frozenset({"0111"})) is True
    assert k.already_applied(frozenset({"0088", "0111"})) is True
    assert k.already_applied(frozenset({"0110", "0111"})) is True
    assert k.already_applied(frozenset({"0108"})) is False
    assert k.already_applied(frozenset({"0110"})) is False


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
    monkeypatch.setenv(k.TARGET_ENV, "db.internal/nahla")
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
    ("postgresql://u:p@localhost.localdomain:5432/nahla", "DATABASE_URL_is_local"),
    ("postgresql://u:p@127.0.0.1:5433/nahla", "DATABASE_URL_is_local"),
    ("postgresql://u:p@127.1.2.3:5433/nahla", "DATABASE_URL_is_local"),
    ("postgresql://u:p@[::1]:5432/nahla", "DATABASE_URL_is_local"),
    ("postgresql://u:p@[::ffff:127.0.0.1]:5432/nahla", "DATABASE_URL_is_local"),
    ("postgresql://u:p@0.0.0.0:5432/nahla", "DATABASE_URL_is_local"),
    ("sqlite:////tmp/nahla.db", "DATABASE_URL_unsupported_dialect"),
    ("mysql://u:p@db.railway:3306/nahla", "DATABASE_URL_unsupported_dialect"),
    ("db.railway:5432/nahla", "DATABASE_URL_unparsable"),      # not a URL at all
    ("postgres://u:p@db.railway:5432/nahla", "DATABASE_URL_unsupported_dialect"),
    ("postgresql:///nahla", "DATABASE_URL_host_missing"),
    ("postgresql://u:p@db.railway:5432/", "DATABASE_URL_database_missing"),
    ("postgresql://", "DATABASE_URL_host_missing"),
    ("postgresql://u:p@db.railway:not-a-port/nahla", "DATABASE_URL_unparsable"),
])
def test_a_url_that_is_not_a_remote_postgres_database_is_refused_by_reason(url, reason):
    resolved, refusal = job.database_url({"DATABASE_URL": url, k.TARGET_ENV: TARGET})
    assert resolved is None and refusal == reason


@pytest.mark.parametrize("url", [
    "postgresql://u:localhost@db.railway:5432/nahla",       # password, not host
    "postgresql+psycopg2://u:p@db.railway:5432/nahla",      # a driver, same dialect
    "postgresql://u:p@DB.RAILWAY:5432/nahla",               # host case is not identity
    "postgresql://u:p@db.railway:5432/nahla?connect_timeout=5",   # harmless parameter
    "postgresql://u:p@db.railway:5432/nahla?sslmode=require",     # harmless parameter
])
def test_a_remote_database_url_is_accepted_however_it_is_spelled(url):
    resolved, refusal = job.database_url({"DATABASE_URL": url, k.TARGET_ENV: TARGET})
    assert refusal is None
    # What comes back is the validated target bound explicitly, not the spelling
    # that was handed in: both paths are given the same fully specified URL.
    rebound = job.parse_database_url(resolved, {})[0]
    assert (rebound["host"], rebound["port"], rebound["database"]) == (
        "db.railway", 5432, "nahla")


def test_a_url_naming_a_host_that_merely_contains_localhost_is_not_local():
    parsed, refusal = job.parse_database_url("postgresql://u:p@localhostings.example/nahla")
    assert refusal is None and parsed["host"] == "localhostings.example"


# ── One validated target for inspection and for Alembic ─────────────────────


@pytest.mark.parametrize("name, value", [
    ("PGPORT", "6543"),
    ("PGHOSTADDR", "10.0.0.9"),
    ("PGHOST", "other.internal"),
    ("PGDATABASE", "other_database"),
    ("PGSERVICE", "elsewhere"),
    ("PGSERVICEFILE", "/tmp/pgservice.conf"),
    ("PGOPTIONS", "-c search_path=other"),
])
def test_an_inherited_libpq_target_variable_is_refused(name, value):
    """The driver reads these, not the URL parser: an omitted URL port with
    PGPORT=6543 connects to 6543 while every URL check still passes."""
    resolved, refusal = job.database_url(
        {"DATABASE_URL": "postgresql://u:p@db.railway/nahla", k.TARGET_ENV: TARGET, name: value})
    assert resolved is None and refusal == "DATABASE_URL_environment_override"


def test_the_refusal_names_every_inherited_variable_it_found():
    found = job.libpq_environment_overrides({"PGPORT": "6543", "PGHOSTADDR": "10.0.0.9",
                                             "PGSSLMODE": "require", "PGUSER": "u"})
    assert found == ("PGHOSTADDR", "PGPORT")        # target-affecting ones only


def test_an_empty_libpq_variable_is_not_an_override():
    assert job.libpq_environment_overrides({"PGPORT": "", "PGHOST": "   "}) == ()


def test_the_alembic_subprocess_runs_with_the_validated_target_and_no_inherited_override():
    resolved, refusal = job.database_url({"DATABASE_URL": TARGET_URL, k.TARGET_ENV: TARGET})
    assert refusal is None
    env = job.sanitized_environment(resolved, {"PGPORT": "6543", "PGHOSTADDR": "10.0.0.9",
                                               "PGSERVICE": "elsewhere", "PATH": "/usr/bin",
                                               "DATABASE_URL": "postgresql://u:p@evil/other"})
    for name in k.LIBPQ_TARGET_ENV_VARS:
        assert name not in env, name
    assert env["DATABASE_URL"] == resolved
    assert env["PATH"] == "/usr/bin"                # nothing else is disturbed


def test_the_subprocess_and_the_inspection_engine_are_given_the_same_target(monkeypatch):
    """Not two URLs that usually agree: the same validated string."""
    import sqlalchemy as sa

    seen: Dict[str, Any] = {}
    monkeypatch.setenv(k.CONFIRMATION_ENV, k.CONFIRMATION_TOKEN)
    monkeypatch.setenv(k.TARGET_ENV, TARGET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.railway/nahla")   # no port
    monkeypatch.setattr(job, "database_directory", lambda: "/tmp")
    for name in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def _run(**kwargs: Any) -> int:
        seen["env"] = kwargs["env"]
        return 0

    states = [observation(revisions=("0107",), present=()),
              observation(revisions=("0111",), present=k.RUNTIME_RELATIONS)]

    def _observe(url: str) -> Dict[str, Any]:
        seen.setdefault("observed", url)
        return states.pop(0)

    monkeypatch.setattr(job, "run_alembic", _run)
    monkeypatch.setattr(job, "observe", _observe)
    assert job.main([]) == k.EXIT_SUCCESS
    assert seen["env"]["DATABASE_URL"] == seen["observed"]
    # …and that one string names the port explicitly, so nothing infers it.
    engine = sa.create_engine(seen["observed"])
    try:
        _args, connect = engine.dialect.create_connect_args(engine.url)
    finally:
        engine.dispose()
    assert connect["port"] == 5432 and connect["host"] == "db.railway"
    assert connect["dbname"] == "nahla"


def test_a_database_name_with_leading_whitespace_is_a_different_database():
    """PostgreSQL allows it, so authorising the stripped name authorises another."""
    declared, refusal = job.authorized_target({k.TARGET_ENV: "db.railway:5432/ nahla"})
    assert refusal is None and declared["database"] == " nahla"
    # The declared ' nahla' does not authorise 'nahla'…
    resolved, refusal = job.database_url(
        {"DATABASE_URL": TARGET_URL, k.TARGET_ENV: "db.railway:5432/ nahla"})
    assert resolved is None and refusal == "DATABASE_URL_is_not_the_authorized_target"
    # …and it does authorise the database actually called ' nahla'. SQLAlchemy
    # does not unquote the path, so that name is written literally.
    resolved, refusal = job.database_url(
        {"DATABASE_URL": "postgresql://u:p@db.railway:5432/ nahla",
         k.TARGET_ENV: "db.railway:5432/ nahla"})
    assert refusal is None and resolved is not None
    assert job.parse_database_url(resolved, {})[0]["database"] == " nahla"


def test_the_explicit_url_keeps_the_credentials_it_was_given():
    bound = job.explicit_url("postgresql://u:s3cret@db.railway/nahla",
                             {"host": "db.railway", "port": 5432, "database": "nahla"})
    assert "s3cret" in bound and ":5432/" in bound


def test_the_job_will_not_run_without_an_explicitly_authorized_target(monkeypatch, capsys):
    monkeypatch.setenv(k.CONFIRMATION_ENV, k.CONFIRMATION_TOKEN)
    monkeypatch.setenv("DATABASE_URL", TARGET_URL)
    monkeypatch.delenv(k.TARGET_ENV, raising=False)
    assert job.main([]) == k.EXIT_USAGE
    assert "authorized_target_not_declared" in capsys.readouterr().out


@pytest.mark.parametrize("declared", ["", "   ", "db.railway", "db.railway/nahla/extra",
                                      "/nahla", "db.railway/", "db.railway:abc/nahla",
                                      "[::1/nahla"])
def test_a_target_that_does_not_name_one_host_and_one_database_is_refused(declared):
    resolved, refusal = job.database_url({"DATABASE_URL": TARGET_URL, k.TARGET_ENV: declared})
    assert resolved is None and refusal in {"authorized_target_not_declared",
                                            "authorized_target_malformed"}


def test_a_target_without_a_port_means_the_postgres_default():
    assert job.authorized_target({k.TARGET_ENV: "db.railway/nahla"})[0] == {
        "host": "db.railway", "port": k.DEFAULT_PORT, "database": "nahla"}


def test_an_ipv6_target_keeps_its_colons_out_of_the_port():
    assert job.authorized_target({k.TARGET_ENV: "[2001:db8::1]:6543/nahla"})[0] == {
        "host": "2001:db8::1", "port": 6543, "database": "nahla"}


# ── The effective target, not the URL's authority ────────────────────────────


@pytest.mark.parametrize("url", [
    # The driver honours these over the authority: the URL says approved, the
    # connection goes elsewhere.
    "postgresql://u:p@db.railway:5432/nahla?host=other.internal",
    "postgresql://u:p@db.railway:5432/nahla?hostaddr=10.0.0.9",
    "postgresql://u:p@db.railway:5432/nahla?dbname=other_database",
    "postgresql://u:p@db.railway:5432/nahla?port=6543",
    "postgresql://u:p@db.railway:5432/nahla?host=127.0.0.1",
    "postgresql://u:p@db.railway:5432/nahla?service=elsewhere",
])
def test_a_url_that_redirects_itself_through_a_query_parameter_is_refused(url):
    resolved, refusal = job.database_url({"DATABASE_URL": url, k.TARGET_ENV: TARGET})
    assert resolved is None and refusal == "DATABASE_URL_carries_a_target_override"


def test_the_parameters_checked_are_the_ones_the_driver_is_handed():
    """Not a second parse of the URL: the dialect's own connect arguments."""
    import sqlalchemy as sa

    parsed, refusal = job.parse_database_url(TARGET_URL)
    assert refusal is None
    engine = sa.create_engine(TARGET_URL)
    try:
        _args, connect = engine.dialect.create_connect_args(engine.url)
    finally:
        engine.dispose()
    assert (parsed["host"], parsed["port"], parsed["database"]) == (
        str(connect["host"]).lower(), int(connect["port"]), connect["dbname"])


def test_a_different_port_on_the_authorized_host_is_a_different_database():
    resolved, refusal = job.database_url(
        {"DATABASE_URL": "postgresql://u:p@db.railway:6543/nahla", k.TARGET_ENV: TARGET})
    assert resolved is None and refusal == "DATABASE_URL_is_not_the_authorized_target"


def test_the_database_name_is_compared_with_its_case_intact():
    """PostgreSQL treats ``Nahla`` and ``nahla`` as different databases."""
    resolved, refusal = job.database_url(
        {"DATABASE_URL": "postgresql://u:p@db.railway:5432/Nahla", k.TARGET_ENV: TARGET})
    assert resolved is None and refusal == "DATABASE_URL_is_not_the_authorized_target"
    resolved, refusal = job.database_url(
        {"DATABASE_URL": "postgresql://u:p@db.railway:5432/Nahla",
         k.TARGET_ENV: "db.railway:5432/Nahla"})
    assert refusal is None and resolved is not None


@pytest.mark.parametrize("url", [
    "postgresql://u:p@other.railway:5432/nahla",            # another host
    "postgresql://u:p@db.railway:5432/nahla_staging",       # another database
])
def test_a_database_that_is_not_the_authorized_one_is_refused(url):
    resolved, refusal = job.database_url({"DATABASE_URL": url, k.TARGET_ENV: TARGET})
    assert resolved is None and refusal == "DATABASE_URL_is_not_the_authorized_target"


def test_the_job_refuses_a_local_database_even_when_confirmed(monkeypatch, capsys):
    monkeypatch.setenv(k.CONFIRMATION_ENV, k.CONFIRMATION_TOKEN)
    monkeypatch.setenv(k.TARGET_ENV, "localhost/nahla")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/nahla")
    assert job.main([]) == k.EXIT_USAGE
    assert "DATABASE_URL_is_local" in capsys.readouterr().out


# ── Shape classification and outcomes ────────────────────────────────────────


@pytest.mark.parametrize("present, shape", [
    ((), "fresh"),
    (k.RUNTIME_RELATIONS, "complete"),
    (k.FOUNDATION_RELATIONS, "foundation"),
    (k.RUNTIME_RELATIONS[:1], "partial"),
    (k.LEDGER_RELATIONS, "partial"),
])
def test_the_runtime_schema_shape_is_classified_exactly(present, shape):
    assert job.classify(observation(present=present)) == shape


@pytest.mark.parametrize("revisions, expected", [
    (frozenset({"0107"}), ()),
    (frozenset({"0088", "0107"}), ()),
    (frozenset({"0108"}), k.FOUNDATION_RELATIONS),
    (frozenset({"0088", "0108"}), k.FOUNDATION_RELATIONS),
])
def test_every_accepted_start_declares_the_schema_it_must_already_have(revisions, expected):
    assert k.start_state_accepted(revisions) is True
    assert k.expected_relations_at(revisions) == expected


def test_the_foundation_revision_is_a_startable_state_and_not_a_refused_one(monkeypatch, capsys):
    """``0108`` creates exactly the three foundation relations, so a database at
    ``0108`` holding them is on its way to the target, not half-applied."""
    calls = _prepare(monkeypatch,
                     observation(revisions=("0108",), present=k.FOUNDATION_RELATIONS),
                     observation(revisions=("0111",), present=k.RUNTIME_RELATIONS))
    assert job.main([]) == k.EXIT_SUCCESS
    assert len(calls) == 1
    assert f"RESULT={k.RESULT_SUCCESS}" in capsys.readouterr().out


def test_a_database_holding_relations_its_revision_did_not_create_is_refused(monkeypatch, capsys):
    calls = _prepare(monkeypatch,
                     observation(revisions=("0107",), present=k.FOUNDATION_RELATIONS))
    assert job.main([]) == k.EXIT_PRECONDITION
    assert calls == []
    out = capsys.readouterr().out
    assert "unexpected_runtime_schema_for_revision" in out


def _prepare(monkeypatch, before, after=None, rc=0):
    monkeypatch.setenv(k.CONFIRMATION_ENV, k.CONFIRMATION_TOKEN)
    monkeypatch.setenv(k.TARGET_ENV, TARGET)
    monkeypatch.setenv("DATABASE_URL", TARGET_URL)
    for name in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
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
                     observation(revisions=("0111",), present=k.RUNTIME_RELATIONS))
    assert job.main([]) == k.EXIT_SUCCESS
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert f"RESULT={k.RESULT_SUCCESS}" in out and "relations=12" in out


def test_an_already_migrated_database_is_a_no_op_and_runs_nothing(monkeypatch, capsys):
    calls = _prepare(monkeypatch, observation(revisions=("0111",), present=k.RUNTIME_RELATIONS))
    assert job.main([]) == k.EXIT_SUCCESS
    assert calls == []
    assert f"RESULT={k.RESULT_ALREADY_APPLIED}" in capsys.readouterr().out


def test_a_partial_schema_is_refused_rather_than_repaired(monkeypatch, capsys):
    calls = _prepare(monkeypatch,
                     observation(revisions=("0107",), present=k.RUNTIME_RELATIONS[:1]))
    assert job.main([]) == k.EXIT_PRECONDITION
    assert calls == []
    out = capsys.readouterr().out
    assert "unexpected_runtime_schema_for_revision" in out
    assert f"RESULT={k.RESULT_FAILED_PRECONDITION}" in out


def test_an_unexpected_starting_revision_is_refused_with_what_was_observed(monkeypatch, capsys):
    calls = _prepare(monkeypatch, observation(revisions=("0093",), present=()))
    assert job.main([]) == k.EXIT_PRECONDITION
    assert calls == []
    out = capsys.readouterr().out
    assert "unexpected_start_revision" in out and "0093" in out


def test_a_non_zero_alembic_exit_is_a_failure_even_if_the_tables_appeared(monkeypatch, capsys):
    _prepare(monkeypatch,
             observation(revisions=("0107",), present=()),
             observation(revisions=("0111",), present=k.RUNTIME_RELATIONS),
             rc=1)
    assert job.main([]) == k.EXIT_FAILED
    assert f"RESULT={k.RESULT_FAILED}" in capsys.readouterr().out


def test_a_zero_exit_that_left_the_schema_incomplete_is_still_a_failure(monkeypatch, capsys):
    _prepare(monkeypatch,
             observation(revisions=("0107",), present=()),
             observation(revisions=("0108",), present=k.FOUNDATION_RELATIONS))
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
             observation(revisions=("0111",), present=k.RUNTIME_RELATIONS))
    job.main([])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines and all(line.startswith(k.LOG_PREFIX) for line in lines)
