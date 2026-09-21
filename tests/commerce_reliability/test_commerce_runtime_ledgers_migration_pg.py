"""Revision 0109 reconciliation on real PostgreSQL: fresh installation and
exactly compatible pre-creation pass; every incompatible pre-existing shape is
refused explicitly and never stamped; the append-only trigger is verified on
each ledger relation itself; the foundation tables are required.

Requires ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips and the skip is reported, never counted.
"""
from __future__ import annotations

import importlib.util
from typing import Iterator, Tuple

import pytest
from sqlalchemy import create_engine, text

from core.commerce_runtime import ledger_models as lm
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    REPO_ROOT, _alembic, _alembic_config, _create_database, _current_revisions, _drop_database,
)
from tests.commerce_reliability.test_commerce_runtime_ledgers_pg import (
    FOUNDATION_REVISION, THIS_REVISION, _ledger_function_present, _ledger_trigger_relations,
)

MIGRATION_PATH = REPO_ROOT / "database" / "migrations" / "versions" / "0109_commerce_runtime_ledgers.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0109_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database_at_0108(pg_admin_dsn: str) -> Iterator[Tuple[str, object]]:
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, FOUNDATION_REVISION)
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
    assert "0109 refuses" in message or "0109 requires" in message, message
    return message


def test_fresh_and_compatible_precreation_pass_the_verifier(database_at_0108) -> None:
    dsn, engine = database_at_0108
    migration = _migration_module()
    with engine.connect() as conn:
        assert any(d.endswith("table absent") for d in migration.schema_differences(conn))
    _alembic(dsn, THIS_REVISION)
    with engine.connect() as conn:
        assert migration.schema_differences(conn) == []
        assert migration.trigger_differences(conn) == []
    assert _current_revisions(engine) == {THIS_REVISION}
    _alembic(dsn, FOUNDATION_REVISION, downgrade=True)
    assert _ledger_trigger_relations(engine) == [] and not _ledger_function_present(engine)
    lm.create_ledger_tables(engine)  # exactly compatible pre-creation, triggers included
    with engine.connect() as conn:
        assert migration.schema_differences(conn) == []
    _alembic(dsn, THIS_REVISION)
    assert _current_revisions(engine) == {THIS_REVISION}
    assert _ledger_trigger_relations(engine) == sorted(lm.APPEND_ONLY_TABLES)


@pytest.mark.parametrize("label, drift_sql, expected_fragment", [
    ("missing_key_uniqueness",
     f"ALTER TABLE {lm.EFFECTS_TABLE} DROP CONSTRAINT uq_commerce_runtime_effects_key",
     "constraint uq_commerce_runtime_effects_key absent"),
    ("wrong_default",
     f"ALTER TABLE {lm.EFFECTS_TABLE} ALTER COLUMN status SET DEFAULT 'confirmed'",
     f"{lm.EFFECTS_TABLE}.status"),
    ("disabled_trigger",
     f"ALTER TABLE {lm.EFFECT_RESULTS_TABLE} DISABLE TRIGGER {lm.LEDGER_IMMUTABLE_TRIGGER}",
     "is not enabled"),
    ("narrowed_column",
     f"ALTER TABLE {lm.DELIVERY_RECEIPTS_TABLE} ALTER COLUMN provider_message_id TYPE varchar(64)",
     f"{lm.DELIVERY_RECEIPTS_TABLE}.provider_message_id"),
    ("missing_check",
     f"ALTER TABLE {lm.DELIVERY_RECEIPTS_TABLE} DROP CONSTRAINT ck_commerce_runtime_delivery_receipts_accepted_id",
     "constraint ck_commerce_runtime_delivery_receipts_accepted_id absent"),
    ("missing_confirmed_pair",
     f"ALTER TABLE {lm.EFFECTS_TABLE} DROP CONSTRAINT ck_commerce_runtime_effects_confirmed_pair",
     "constraint ck_commerce_runtime_effects_confirmed_pair absent"),
    ("extra_column",
     f"ALTER TABLE {lm.DELIVERY_SEQUENCES_TABLE} ADD COLUMN legacy_flag boolean",
     f"{lm.DELIVERY_SEQUENCES_TABLE}.legacy_flag: unexpected column"),
    ("missing_not_null",
     f"ALTER TABLE {lm.EFFECTS_TABLE} ALTER COLUMN status DROP NOT NULL",
     f"{lm.EFFECTS_TABLE}.status"),
    ("extra_not_null",
     f"ALTER TABLE {lm.DELIVERY_RECEIPTS_TABLE} ALTER COLUMN provider_message_id SET NOT NULL",
     f"{lm.DELIVERY_RECEIPTS_TABLE}.provider_message_id"),
    ("check_named_like_not_null",
     f"ALTER TABLE {lm.EFFECTS_TABLE} ADD CONSTRAINT synthetic_not_null CHECK (status <> 'unknown')",
     "unexpected constraint synthetic_not_null"),
])
def test_incompatible_precreated_schema_is_refused_and_not_stamped(database_at_0108, label, drift_sql,
                                                                    expected_fragment) -> None:
    dsn, engine = database_at_0108
    lm.create_ledger_tables(engine)
    with engine.connect() as conn:
        conn.execute(text(drift_sql))
    message = _upgrade_refused(dsn)
    assert expected_fragment in message, (label, message)
    assert _current_revisions(engine) == {FOUNDATION_REVISION}
    migration = _migration_module()
    with engine.connect() as conn:
        assert migration.schema_differences(conn) != []   # the drift was left exactly as found


def test_same_named_trigger_on_an_unrelated_table_does_not_satisfy_the_verifier(database_at_0108) -> None:
    dsn, engine = database_at_0108
    lm.create_ledger_tables(engine)
    with engine.connect() as conn:
        conn.execute(text(f"DROP TRIGGER {lm.LEDGER_IMMUTABLE_TRIGGER} ON {lm.EFFECT_RESULTS_TABLE}"))
        conn.execute(text(
            f"CREATE TRIGGER {lm.LEDGER_IMMUTABLE_TRIGGER} BEFORE UPDATE OR DELETE ON {lm.EFFECTS_TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION {lm.LEDGER_IMMUTABLE_FUNCTION}()"))
    migration = _migration_module()
    with engine.connect() as conn:
        assert any("absent on the ledger relation" in d for d in migration.trigger_differences(conn))
    _alembic(dsn, THIS_REVISION)
    assert _current_revisions(engine) == {THIS_REVISION}
    # Installed on the ledger relation; the unrelated one is neither counted nor touched.
    assert sorted(set(_ledger_trigger_relations(engine))) == sorted(set(lm.APPEND_ONLY_TABLES) | {lm.EFFECTS_TABLE})
    with engine.connect() as conn:
        assert migration.trigger_differences(conn) == []


def test_same_named_function_with_a_different_body_is_refused(database_at_0108) -> None:
    dsn, engine = database_at_0108
    lm.create_ledger_tables(engine)
    with engine.connect() as conn:
        conn.execute(text(
            f"CREATE OR REPLACE FUNCTION {lm.LEDGER_IMMUTABLE_FUNCTION}() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"))
    message = _upgrade_refused(dsn)
    assert lm.LEDGER_IMMUTABLE_FUNCTION in message
    assert _current_revisions(engine) == {FOUNDATION_REVISION}
    with engine.connect() as conn:
        body = conn.execute(text("SELECT prosrc FROM pg_proc WHERE proname = :n"),
                            {"n": lm.LEDGER_IMMUTABLE_FUNCTION}).scalar()
    assert "RETURN NULL" in body   # left exactly as found


def test_missing_ledger_index_is_reconciled(database_at_0108) -> None:
    dsn, engine = database_at_0108
    lm.create_ledger_tables(engine)
    with engine.connect() as conn:
        conn.execute(text("DROP INDEX ix_commerce_runtime_effects_turn"))
    _alembic(dsn, THIS_REVISION)
    assert _current_revisions(engine) == {THIS_REVISION}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_commerce_runtime_effects_turn'")
                            ).scalar() == 1
        assert _migration_module().schema_differences(conn) == []


def _stamp(dsn: str, revision: str) -> None:
    """Stamp through the same environment handling as the chain runner."""
    import os  # noqa: PLC0415

    from alembic import command  # noqa: PLC0415

    previous_cwd, previous_url = os.getcwd(), os.environ.get("DATABASE_URL")
    os.chdir(REPO_ROOT / "database")
    os.environ["DATABASE_URL"] = dsn
    try:
        command.stamp(_alembic_config(dsn), revision)
    finally:
        os.chdir(previous_cwd)
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def test_0109_requires_the_foundation_tables(pg_admin_dsn: str) -> None:
    name, dsn = _create_database(pg_admin_dsn)
    try:
        _alembic(dsn, "0107")
        _stamp(dsn, FOUNDATION_REVISION)   # version says 0108, tables say otherwise
        engine = create_engine(dsn, pool_pre_ping=True, isolation_level="AUTOCOMMIT")
        message = _upgrade_refused(dsn)
        assert "requires revision 0108's table" in message
        assert _current_revisions(engine) == {FOUNDATION_REVISION}
        engine.dispose()
    finally:
        _drop_database(pg_admin_dsn, name)
