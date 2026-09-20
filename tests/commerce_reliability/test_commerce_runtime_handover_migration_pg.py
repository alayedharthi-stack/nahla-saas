"""Revision 0110 on real PostgreSQL: it creates exactly what the package
declares, refuses an incompatible pre-existing relation rather than reconciling
it, is reversible, and leaves revision 0109's schema untouched.

There is only one definition of this schema — ``handover_models`` — and the
revision creates the tables from it, so revision and package cannot drift. What
is proved here is that the definition really lands on PostgreSQL, that a
pre-existing relation which differs is refused by name, and that a downgrade
removes everything it made and nothing else.

Requires ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips and the skip is reported, never counted.
"""
from __future__ import annotations

import importlib.util
from typing import Iterator, Tuple

import pytest
from sqlalchemy import create_engine, inspect, text

from core.commerce_runtime import handover_models as hm
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    REPO_ROOT, _alembic, _create_database, _current_revisions, _drop_database,
)

PREVIOUS_REVISION = "0109"
THIS_REVISION = "0110"
MIGRATION_PATH = (REPO_ROOT / "database" / "migrations" / "versions"
                  / "0110_commerce_runtime_handover.py")

# Everything revision 0109 leaves behind. A downgrade of 0110 must not touch
# any of it.
LEDGER_RELATIONS = (
    "commerce_runtime_conversations", "commerce_runtime_turns",
    "commerce_runtime_turn_terminals", "commerce_runtime_effects",
    "commerce_runtime_effect_attempts", "commerce_runtime_effect_results",
    "commerce_runtime_delivery_sequences", "commerce_runtime_delivery_attempts",
    "commerce_runtime_delivery_receipts",
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0110_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database_at_0109(pg_admin_dsn: str) -> Iterator[Tuple[str, object]]:
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, PREVIOUS_REVISION)
        engine = create_engine(dsn, future=True)
        yield dsn, engine
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _relations(engine) -> set:
    return set(inspect(engine).get_table_names())


def test_a_fresh_upgrade_creates_exactly_the_declared_relations(database_at_0109) -> None:
    dsn, engine = database_at_0109
    before = _relations(engine)
    assert not (before & set(hm.HANDOVER_TABLES))

    _alembic(dsn, THIS_REVISION)
    after = _relations(engine)
    assert set(hm.HANDOVER_TABLES) <= after
    assert after - before == set(hm.HANDOVER_TABLES)          # and nothing else
    assert THIS_REVISION in _current_revisions(engine)


def test_the_created_columns_are_the_ones_the_package_declares(database_at_0109) -> None:
    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    module = _migration_module()
    with engine.connect() as conn:
        for table in hm.HANDOVER_TABLE_OBJECTS:
            assert module._differences(conn, table) == []


def test_the_identity_and_pending_indexes_exist(database_at_0109) -> None:
    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    inspector = inspect(engine)
    deferred = {index["name"] for index in inspector.get_indexes(hm.DEFERRED_TABLE)}
    workers = {index["name"] for index in inspector.get_indexes(hm.WORKERS_TABLE)}
    assert "uq_commerce_runtime_deferred_inbound_identity" in deferred
    assert "ix_commerce_runtime_deferred_inbound_pending" in deferred
    assert "uq_commerce_runtime_handover_workers_identity" in workers
    assert "ix_commerce_runtime_handover_workers_active" in workers


def test_one_inbound_identity_can_only_be_recorded_once(database_at_0109) -> None:
    """The unique constraint is what makes a provider retry idempotent."""
    from sqlalchemy.exc import IntegrityError

    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    row = ("INSERT INTO commerce_runtime_deferred_inbound "
           "(tenant_id, namespace, channel_connection_ref, phone_number_id, recipient, "
           " provider_message_id, payload, reason, state) "
           "VALUES (1, 'live', 'wa:PID', 'PID', '+9665', 'wamid.1', '{}'::jsonb, "
           "        'accepted', 'pending')")
    with engine.begin() as conn:
        conn.execute(text(row))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(row))


def test_a_disposed_row_must_name_its_disposition(database_at_0109) -> None:
    """A disposition without a kind is not evidence of anything."""
    from sqlalchemy.exc import IntegrityError

    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO commerce_runtime_deferred_inbound "
                "(tenant_id, namespace, channel_connection_ref, phone_number_id, recipient, "
                " provider_message_id, payload, reason, state) "
                "VALUES (1, 'live', 'wa:PID', 'PID', '+9665', 'wamid.x', '{}'::jsonb, "
                "        'accepted', 'disposed')"))


def test_a_retired_worker_must_name_who_retired_it(database_at_0109) -> None:
    from sqlalchemy.exc import IntegrityError

    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO commerce_runtime_handover_workers "
                "(tenant_id, namespace, worker_id, observed_generation, observed_state, "
                " retired_at) VALUES (1, 'live', 'w', 0, 'open', now())"))


def test_an_incompatible_pre_existing_relation_is_refused_and_not_stamped(
        database_at_0109) -> None:
    dsn, engine = database_at_0109
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {hm.BARRIER_TABLE} (tenant_id integer)"))
    with pytest.raises(Exception) as caught:
        _alembic(dsn, THIS_REVISION)
    assert "refuses to reconcile" in str(caught.value)
    assert THIS_REVISION not in _current_revisions(engine)


def test_the_downgrade_removes_what_it_made_and_leaves_0109_alone(database_at_0109) -> None:
    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    _alembic(dsn, PREVIOUS_REVISION, downgrade=True)
    remaining = _relations(engine)
    assert not (remaining & set(hm.HANDOVER_TABLES))
    assert set(LEDGER_RELATIONS) <= remaining
    assert PREVIOUS_REVISION in _current_revisions(engine)


def test_the_upgrade_is_idempotent_on_a_database_that_already_has_it(database_at_0109) -> None:
    """A re-run finds its own relations compatible and changes nothing."""
    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    _alembic(dsn, PREVIOUS_REVISION, downgrade=True)
    with engine.begin() as conn:
        hm.RuntimeBase.metadata.create_all(conn, tables=list(hm.HANDOVER_TABLE_OBJECTS))
    _alembic(dsn, THIS_REVISION)
    assert set(hm.HANDOVER_TABLES) <= _relations(engine)
    assert THIS_REVISION in _current_revisions(engine)
