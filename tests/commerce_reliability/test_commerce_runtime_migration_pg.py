"""Revision 0108 reconciliation on real PostgreSQL: fresh installation and
exactly compatible pre-creation pass; every incompatible pre-existing shape is
refused explicitly and never stamped; the immutability trigger is verified on
the runtime terminal relation itself.

Requires ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips and the skip is reported, never counted.
"""
from __future__ import annotations

import importlib.util
from typing import Iterator, Tuple

import pytest
from sqlalchemy import create_engine, text

from core.commerce_runtime import models as m
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    PREVIOUS_HEAD, REPO_ROOT, THIS_REVISION, _alembic, _create_database, _current_revisions, _drop_database,
    _function_present, _trigger_count,
)

MIGRATION_PATH = REPO_ROOT / "database" / "migrations" / "versions" / "0108_commerce_runtime_foundation.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0108_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database_at_0107(pg_admin_dsn: str) -> Iterator[Tuple[str, object]]:
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, PREVIOUS_HEAD)
        engine = create_engine(dsn, pool_pre_ping=True, isolation_level="AUTOCOMMIT")
        yield dsn, engine
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _upgrade_refused(dsn: str) -> str:
    with pytest.raises(Exception) as refused:
        _alembic(dsn, THIS_REVISION)
    message = str(refused.value)
    assert "0108 refuses" in message, message
    return message


def _trigger_state(engine) -> Tuple[int, str, str]:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT count(*), coalesce(min(t.tgenabled::text), ''), coalesce(min(p.proname), '') "
            "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE NOT t.tgisinternal AND t.tgrelid = CAST(:rel AS regclass) AND t.tgname = :name"),
            {"rel": f"public.{m.TERMINALS_TABLE}", "name": m.TERMINAL_IMMUTABLE_TRIGGER}).one()
    return int(row[0]), str(row[1]), str(row[2])


def test_fresh_and_compatible_precreation_pass_the_verifier(database_at_0107) -> None:
    dsn, engine = database_at_0107
    migration = _migration_module()
    with engine.connect() as conn:
        assert any(d.endswith("table absent") for d in migration.schema_differences(conn))
    _alembic(dsn, THIS_REVISION)
    with engine.connect() as conn:
        assert migration.schema_differences(conn) == []
        assert migration.trigger_differences(conn) == []
    assert _current_revisions(engine) == {THIS_REVISION}
    _alembic(dsn, PREVIOUS_HEAD, downgrade=True)
    assert _trigger_count(engine) == 0 and not _function_present(engine)
    m.create_runtime_tables(engine)  # exactly compatible pre-creation, trigger included
    with engine.connect() as conn:
        assert migration.schema_differences(conn) == []
    _alembic(dsn, THIS_REVISION)
    assert _current_revisions(engine) == {THIS_REVISION}
    assert _trigger_state(engine) == (1, "O", m.TERMINAL_IMMUTABLE_FUNCTION)


@pytest.mark.parametrize("label, drift_sql, expected_fragment", [
    ("missing_admission_uniqueness",
     f"ALTER TABLE {m.TURNS_TABLE} DROP CONSTRAINT uq_commerce_runtime_turns_admission",
     "constraint uq_commerce_runtime_turns_admission absent"),
    ("wrong_default",
     f"ALTER TABLE {m.CONVERSATIONS_TABLE} ALTER COLUMN state_revision SET DEFAULT 99",
     f"{m.CONVERSATIONS_TABLE}.state_revision"),
    ("disabled_trigger",
     f"ALTER TABLE {m.TERMINALS_TABLE} DISABLE TRIGGER {m.TERMINAL_IMMUTABLE_TRIGGER}",
     "is not enabled"),
    ("narrowed_column",
     f"ALTER TABLE {m.TURNS_TABLE} ALTER COLUMN provider_message_id TYPE varchar(64)",
     f"{m.TURNS_TABLE}.provider_message_id"),
    ("missing_check",
     f"ALTER TABLE {m.TERMINALS_TABLE} DROP CONSTRAINT ck_commerce_runtime_turn_terminals_transport",
     "constraint ck_commerce_runtime_turn_terminals_transport absent"),
    ("extra_column",
     f"ALTER TABLE {m.CONVERSATIONS_TABLE} ADD COLUMN legacy_flag boolean",
     f"{m.CONVERSATIONS_TABLE}.legacy_flag: unexpected column"),
    ("missing_not_null",
     f"ALTER TABLE {m.CONVERSATIONS_TABLE} ALTER COLUMN state_revision DROP NOT NULL",
     f"{m.CONVERSATIONS_TABLE}.state_revision"),
    ("extra_not_null",
     f"ALTER TABLE {m.CONVERSATIONS_TABLE} ALTER COLUMN lease_owner SET NOT NULL",
     f"{m.CONVERSATIONS_TABLE}.lease_owner"),
    ("check_named_like_not_null",
     f"ALTER TABLE {m.CONVERSATIONS_TABLE} ADD CONSTRAINT synthetic_not_null CHECK (next_sequence < 100)",
     "unexpected constraint synthetic_not_null"),
])
def test_incompatible_precreated_schema_is_refused_and_not_stamped(database_at_0107, label, drift_sql,
                                                                   expected_fragment) -> None:
    dsn, engine = database_at_0107
    m.create_runtime_tables(engine)
    with engine.connect() as conn:
        conn.execute(text(drift_sql))
    message = _upgrade_refused(dsn)
    assert expected_fragment in message, (label, message)
    assert _current_revisions(engine) == {PREVIOUS_HEAD}, label
    if label == "disabled_trigger":
        # Refused, and the pre-existing trigger was left exactly as found.
        assert _trigger_state(engine) == (1, "D", m.TERMINAL_IMMUTABLE_FUNCTION)
    if label == "wrong_default":
        with engine.connect() as conn:
            default = conn.execute(text(
                "SELECT column_default FROM information_schema.columns WHERE table_name = :t "
                "AND column_name = 'state_revision'"), {"t": m.CONVERSATIONS_TABLE}).scalar()
        assert default == "99"


def test_same_named_trigger_on_an_unrelated_table_does_not_satisfy_the_verifier(database_at_0107) -> None:
    dsn, engine = database_at_0107
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE unrelated_probe (id integer primary key, note text)"))
        conn.execute(text(
            "CREATE FUNCTION unrelated_probe_noop() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RETURN NEW; END $$"))
        conn.execute(text(
            f"CREATE TRIGGER {m.TERMINAL_IMMUTABLE_TRIGGER} BEFORE UPDATE ON unrelated_probe "
            "FOR EACH ROW EXECUTE FUNCTION unrelated_probe_noop()"))
    m.create_runtime_tables(engine)
    with engine.connect() as conn:
        conn.execute(text(f"DROP TRIGGER {m.TERMINAL_IMMUTABLE_TRIGGER} ON {m.TERMINALS_TABLE}"))
    assert _trigger_state(engine)[0] == 0                         # ours is absent on the runtime relation
    migration = _migration_module()
    with engine.connect() as conn:
        assert any("absent on the runtime terminal relation" in d for d in migration.trigger_differences(conn))
    _alembic(dsn, THIS_REVISION)
    assert _current_revisions(engine) == {THIS_REVISION}
    assert _trigger_state(engine) == (1, "O", m.TERMINAL_IMMUTABLE_FUNCTION)
    with engine.connect() as conn:
        assert migration.schema_differences(conn) == []
        unrelated = conn.execute(text(
            "SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE t.tgrelid = 'public.unrelated_probe'::regclass AND t.tgname = :name"),
            {"name": m.TERMINAL_IMMUTABLE_TRIGGER}).scalar()
    assert unrelated == "unrelated_probe_noop"                     # untouched
    # And the runtime terminal rows are actually immutable on this database.
    with engine.connect() as conn:
        tenant_id = conn.execute(text(
            "INSERT INTO tenants (name, is_active, is_platform_tenant) VALUES ('متجر تجريبي عام mig', true, false) "
            "RETURNING id")).scalar_one()
        conv_id = conn.execute(text(
            f"INSERT INTO {m.CONVERSATIONS_TABLE} (tenant_id, namespace, conversation_ref) "
            "VALUES (:t, 'live', 'conv:mig') RETURNING id"), {"t": tenant_id}).scalar_one()
        turn_id = conn.execute(text(
            f"INSERT INTO {m.TURNS_TABLE} (tenant_id, namespace, conversation_id, channel_connection_ref, "
            "provider_message_id, sequence) VALUES (:t, 'live', :c, 'wa:1', 'wamid.mig', 1) RETURNING id"),
            {"t": tenant_id, "c": conv_id}).scalar_one()
        conn.execute(text(
            f"INSERT INTO {m.TERMINALS_TABLE} (turn_id, tenant_id, namespace, conversation_id, processing_outcome, "
            "transport_outcome, customer_reach, recorded_fence, recorded_epoch, recorded_by) "
            "VALUES (:turn, :t, 'live', :c, 'completed', 'accepted', 'reached', 1, 0, 'w')"),
            {"turn": turn_id, "t": tenant_id, "c": conv_id})
        with pytest.raises(Exception, match="immutable"):
            conn.execute(text(f"UPDATE {m.TERMINALS_TABLE} SET transport_outcome = 'unknown' WHERE turn_id = :turn"),
                         {"turn": turn_id})


def test_same_named_function_with_a_different_body_is_refused(database_at_0107) -> None:
    dsn, engine = database_at_0107
    with engine.connect() as conn:
        conn.execute(text(
            f"CREATE FUNCTION {m.TERMINAL_IMMUTABLE_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RETURN NEW; END $$"))
    message = _upgrade_refused(dsn)
    assert "whose body differs" in message
    assert _current_revisions(engine) == {PREVIOUS_HEAD}
    assert not m.CONVERSATIONS_TABLE in _table_names(engine)     # the aborted upgrade created nothing


def test_missing_terminal_index_is_reconciled(database_at_0107) -> None:
    dsn, engine = database_at_0107
    m.create_runtime_tables(engine)
    with engine.connect() as conn:
        conn.execute(text("DROP INDEX ix_commerce_runtime_turn_terminals_conversation"))
    _alembic(dsn, THIS_REVISION)
    assert _current_revisions(engine) == {THIS_REVISION}
    with engine.connect() as conn:
        assert _migration_module().schema_differences(conn) == []


def _table_names(engine) -> set:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}
