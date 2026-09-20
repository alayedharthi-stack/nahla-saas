"""Revision 0111 on real PostgreSQL: it creates exactly what the package
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
THIS_REVISION = "0111"
MIGRATION_PATH = (REPO_ROOT / "database" / "migrations" / "versions"
                  / "0111_commerce_runtime_handover.py")

# Everything revision 0109 leaves behind. A downgrade of 0111 must not touch
# any of it.
LEDGER_RELATIONS = (
    "commerce_runtime_conversations", "commerce_runtime_turns",
    "commerce_runtime_turn_terminals", "commerce_runtime_effects",
    "commerce_runtime_effect_attempts", "commerce_runtime_effect_results",
    "commerce_runtime_delivery_sequences", "commerce_runtime_delivery_attempts",
    "commerce_runtime_delivery_receipts",
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0111_under_test", MIGRATION_PATH)
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


def test_the_downgrade_removes_what_it_made_and_leaves_0109_alone(database_at_0109) -> None:  # noqa: E501
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


def test_a_relation_with_the_right_columns_and_no_unique_index_is_refused(
        database_at_0109) -> None:
    """Matching columns is not a compatible schema.

    The unique index on the inbound identity is what makes an acknowledgement
    idempotent; a table with every column and no index would accept the same
    provider message twice while looking correct to a column-only check.
    """
    dsn, engine = database_at_0109
    module = _migration_module()
    with engine.begin() as conn:
        # The declared table, created without its constraints or indexes.
        table = next(t for t in hm.HANDOVER_TABLE_OBJECTS if t.name == hm.DEFERRED_TABLE)
        columns = ", ".join(
            f'"{c.name}" {c.type.compile(dialect=conn.dialect)}'
            f'{"" if c.nullable else " NOT NULL"}'
            f'{"" if c.server_default is None else " DEFAULT " + str(c.server_default.arg)}'
            for c in table.columns if c.name != "id")
        conn.execute(text(f"CREATE TABLE {hm.DEFERRED_TABLE} "
                          f"(id BIGSERIAL PRIMARY KEY, {columns})"))

    with engine.connect() as conn:
        diffs = module._differences(conn, table)
    assert any("uq_commerce_runtime_deferred_inbound_identity" in d for d in diffs), diffs
    assert any("ix_commerce_runtime_deferred_inbound_pending" in d for d in diffs), diffs

    with pytest.raises(Exception) as caught:
        _alembic(dsn, THIS_REVISION)
    assert "refuses to reconcile" in str(caught.value)
    assert THIS_REVISION not in _current_revisions(engine)


def test_a_relation_missing_a_check_constraint_is_refused(database_at_0109) -> None:
    """The check is what stops a disposed row naming no disposition."""
    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {hm.DEFERRED_TABLE} "
                          f"DROP CONSTRAINT ck_commerce_runtime_deferred_inbound_disposition"))
    module = _migration_module()
    table = next(t for t in hm.HANDOVER_TABLE_OBJECTS if t.name == hm.DEFERRED_TABLE)
    with engine.connect() as conn:
        diffs = module._differences(conn, table)
    assert any("ck_commerce_runtime_deferred_inbound_disposition" in d for d in diffs), diffs


def test_the_worker_row_carries_its_retirement_evidence(database_at_0109) -> None:
    """Silence never retires a worker, and the reason it did not is on the row."""
    dsn, engine = database_at_0109
    _alembic(dsn, THIS_REVISION)
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns(hm.WORKERS_TABLE)}
    assert "retirement_evidence" in columns
    assert "updated_at" in columns


# ═════════════════════════════════════════════════════════════════════════════
# Guarantees are compared by definition, not by name
# ═════════════════════════════════════════════════════════════════════════════
#
# Each case creates the declared relation from the package's own metadata and
# then alters ONE guarantee while keeping its name — the shape a relation made
# by hand to look right would have — and holds the revision to refusing it.


def _declared(engine) -> None:
    with engine.begin() as conn:
        hm.RuntimeBase.metadata.create_all(conn, tables=list(hm.HANDOVER_TABLE_OBJECTS))


def _table(name: str):
    return next(t for t in hm.HANDOVER_TABLE_OBJECTS if t.name == name)


def _refused(dsn, engine, table_name: str, needle: str) -> None:
    module = _migration_module()
    with engine.connect() as conn:
        diffs = module._differences(conn, _table(table_name))
    assert any(needle in d for d in diffs), diffs
    with pytest.raises(Exception) as caught:
        _alembic(dsn, THIS_REVISION)
    assert "refuses to reconcile" in str(caught.value)
    assert THIS_REVISION not in _current_revisions(engine)


def test_a_correct_pre_existing_relation_has_no_differences(database_at_0109) -> None:
    dsn, engine = database_at_0109
    _declared(engine)
    module = _migration_module()
    with engine.connect() as conn:
        for table in hm.HANDOVER_TABLE_OBJECTS:
            assert module._differences(conn, table) == []
    _alembic(dsn, THIS_REVISION)
    assert THIS_REVISION in _current_revisions(engine)


def test_a_unique_constraint_with_the_right_name_over_the_wrong_columns_is_refused(
        database_at_0109) -> None:
    """The identity constraint that deduplicates acknowledgements, missing a
    column: the same provider message on two connections would collide, and
    the same message twice on one connection would not."""
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {hm.DEFERRED_TABLE} DROP CONSTRAINT "
                          f"uq_commerce_runtime_deferred_inbound_identity"))
        conn.execute(text(f"ALTER TABLE {hm.DEFERRED_TABLE} ADD CONSTRAINT "
                          f"uq_commerce_runtime_deferred_inbound_identity "
                          f"UNIQUE (tenant_id, namespace, provider_message_id)"))
    _refused(dsn, engine, hm.DEFERRED_TABLE,
             "unique constraint uq_commerce_runtime_deferred_inbound_identity is")


def test_a_check_constraint_with_the_right_name_and_a_wider_expression_is_refused(
        database_at_0109) -> None:
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {hm.DEFERRED_TABLE} DROP CONSTRAINT "
                          f"ck_commerce_runtime_deferred_inbound_state"))
        conn.execute(text(f"ALTER TABLE {hm.DEFERRED_TABLE} ADD CONSTRAINT "
                          f"ck_commerce_runtime_deferred_inbound_state "
                          f"CHECK (state IN ('pending', 'resolved', 'disposed', 'forgotten'))"))
    _refused(dsn, engine, hm.DEFERRED_TABLE,
             "check constraint ck_commerce_runtime_deferred_inbound_state is")


def test_the_pending_index_with_the_right_name_but_no_predicate_is_refused(
        database_at_0109) -> None:
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_commerce_runtime_deferred_inbound_pending"))
        conn.execute(text(f"CREATE INDEX ix_commerce_runtime_deferred_inbound_pending "
                          f"ON {hm.DEFERRED_TABLE} (tenant_id, namespace)"))
    _refused(dsn, engine, hm.DEFERRED_TABLE,
             "index constraint ix_commerce_runtime_deferred_inbound_pending is")


def test_a_check_constraint_differing_only_in_literal_case_is_refused(database_at_0109) -> None:
    """``'OPEN'`` is not ``'open'``: a barrier that accepted the upper-case
    word would accept a state the runtime never writes and refuse the one it
    does. The comparison keeps what is inside the quotes byte for byte."""
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {hm.BARRIER_TABLE} DROP CONSTRAINT "
                          f"ck_commerce_runtime_handover_barrier_state"))
        conn.execute(text(f"ALTER TABLE {hm.BARRIER_TABLE} ADD CONSTRAINT "
                          f"ck_commerce_runtime_handover_barrier_state "
                          f"CHECK (state IN ('OPEN', 'draining', 'settled', 'released'))"))
    _refused(dsn, engine, hm.BARRIER_TABLE,
             "check constraint ck_commerce_runtime_handover_barrier_state is")


def test_a_predicate_differing_only_in_literal_whitespace_is_refused(database_at_0109) -> None:
    """``state = 'pending '`` indexes nothing the runtime ever writes; an index
    with the right name over that predicate is not the pending index."""
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_commerce_runtime_deferred_inbound_pending"))
        conn.execute(text(f"CREATE INDEX ix_commerce_runtime_deferred_inbound_pending "
                          f"ON {hm.DEFERRED_TABLE} (tenant_id, namespace) "
                          f"WHERE state = 'pending '"))
    _refused(dsn, engine, hm.DEFERRED_TABLE,
             "index constraint ix_commerce_runtime_deferred_inbound_pending is")


def test_spelling_outside_the_quotes_is_not_a_difference(database_at_0109) -> None:
    """The same guarantee written with other keyword case and spacing — and
    the same literals — reflects to the same definition and is accepted."""
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {hm.DEFERRED_TABLE} DROP CONSTRAINT "
                          f"ck_commerce_runtime_deferred_inbound_state"))
        conn.execute(text(f"ALTER TABLE {hm.DEFERRED_TABLE} ADD CONSTRAINT "
                          f"ck_commerce_runtime_deferred_inbound_state "
                          f"check  (  STATE   in ('pending','resolved','disposed')  )"))
        conn.execute(text("DROP INDEX ix_commerce_runtime_deferred_inbound_pending"))
        conn.execute(text(f"create index ix_commerce_runtime_deferred_inbound_pending "
                          f"on {hm.DEFERRED_TABLE} (tenant_id,namespace) where STATE='pending'"))
    module = _migration_module()
    with engine.connect() as conn:
        assert module._differences(conn, _table(hm.DEFERRED_TABLE)) == []
    _alembic(dsn, THIS_REVISION)
    assert THIS_REVISION in _current_revisions(engine)


def test_the_normaliser_keeps_literals_and_folds_the_rest() -> None:
    normalise = _migration_module()._normalized_sql
    assert normalise("state IN ('open', 'draining')") == normalise("STATE  in ('open',   'draining')")
    assert normalise("state IN ('open')") != normalise("state IN ('OPEN')")
    assert normalise("x = 'a b'") != normalise("x = 'a  b'")
    assert normalise('"State" = 1') != normalise('"state" = 1')
    assert normalise("x = 'it''s'") == "x = 'it''s'"


def test_a_primary_key_over_the_wrong_columns_is_refused(database_at_0109) -> None:
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {hm.BARRIER_TABLE} DROP CONSTRAINT "
                          f"commerce_runtime_handover_barrier_pkey"))
        conn.execute(text(f"ALTER TABLE {hm.BARRIER_TABLE} ADD PRIMARY KEY (tenant_id)"))
    _refused(dsn, engine, hm.BARRIER_TABLE, "primary key is")


def test_an_undeclared_constraint_is_refused_rather_than_tolerated(database_at_0109) -> None:
    """A stricter relation refuses rows the runtime writes; it is not compatible."""
    dsn, engine = database_at_0109
    _declared(engine)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {hm.WORKERS_TABLE} ADD CONSTRAINT "
                          f"ck_somebody_elses_rule CHECK (observed_generation < 5)"))
    _refused(dsn, engine, hm.WORKERS_TABLE, "undeclared check constraint ck_somebody_elses_rule")


# ═════════════════════════════════════════════════════════════════════════════
# The address sibling: either order applies, and only 0111@-1 rolls back the
# runtime alone
# ═════════════════════════════════════════════════════════════════════════════

import datetime as _dt
import os
import re
import tempfile
import uuid

from scripts.operators import commerce_runtime_pilot_migration_contract as contract

ADDRESS_REVISION = contract.ADDRESS_SIBLING_REVISION
STAND_IN_TABLE = "stand_in_address_sibling"


def _script_has(revision: str) -> bool:
    from alembic.script import ScriptDirectory
    from alembic.script.revision import ResolutionError
    from alembic.util.exc import CommandError
    from tests.commerce_reliability.test_commerce_runtime_foundation_pg import _alembic_config

    script = ScriptDirectory.from_config(_alembic_config("postgresql://unused"))
    try:
        return script.get_revision(revision) is not None
    except (ResolutionError, CommandError):
        # ``ScriptDirectory`` wraps the resolution error in a ``CommandError``;
        # either spelling means the same thing here: not in this repository.
        return False


@pytest.fixture
def sibling_location():
    """Where the address sibling lives for this run.

    Once PR #1096 has merged, ``0110`` is in the repository and is used as it
    is. Until then a stand-in with the same graph position — revision ``0110``
    revising ``0109``, creating one table — stands in for it, so the proof is
    about the branch topology and not about which PR merged first. Two ``0110``
    revisions would be an Alembic error, so exactly one is ever in play.
    """
    if _script_has(ADDRESS_REVISION):
        yield None, "customer_address_provenance"
        return
    actual = os.environ.get(ACTUAL_SIBLING_ENV, "").strip()
    if actual:
        # The **actual** address revision from #1096, copied into an isolated
        # version location: the proof then runs against the real table, the
        # real foreign keys and the real indexes, not a stand-in.
        with open(actual, "r", encoding="utf-8") as handle:
            source = handle.read()
        found = re.search(r'^_TABLE\s*=\s*"([^"]+)"', source, re.M)
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, os.path.basename(actual)), "w",
                      encoding="utf-8") as handle:
                handle.write(source)
            yield folder, (found.group(1) if found else "customer_address_provenance")
        return
    with tempfile.TemporaryDirectory() as folder:
        with open(os.path.join(folder, "0110_stand_in_address_sibling.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(
                '"""Stand-in for the customer-address provenance sibling (test only)."""\n'
                "from alembic import op\nimport sqlalchemy as sa\n\n"
                f'revision = "{ADDRESS_REVISION}"\ndown_revision = "0109"\n'
                "branch_labels = None\ndepends_on = None\n\n\n"
                "def upgrade() -> None:\n"
                f'    op.create_table("{STAND_IN_TABLE}", sa.Column("id", sa.Integer, primary_key=True))\n\n\n'
                "def downgrade() -> None:\n"
                f'    op.drop_table("{STAND_IN_TABLE}")\n')
        yield folder, STAND_IN_TABLE


# Point this at #1096's real ``0110_customer_address_provenance.py`` to run the
# sibling proofs against the actual address revision instead of the stand-in.
ACTUAL_SIBLING_ENV = "NAHLA_ADDRESS_SIBLING_MIGRATION"


def _seed_row(conn, table_name: str, *, seen=None) -> int:
    """Insert one row into ``table_name``, satisfying its NOT NULL columns and
    foreign keys with the smallest values that fit — recursively for the
    tables it references — and return its id. Generic on purpose: the address
    table is #1096's to define, and this proof only has to put real rows into
    whatever it defines and see them survive the runtime's rollback."""
    from sqlalchemy import MetaData, Table, insert, select
    from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, Numeric, Text, String
    from sqlalchemy.dialects.postgresql import JSONB

    seen = seen if seen is not None else set()
    table = Table(table_name, MetaData(), autoload_with=conn)
    values = {}
    for column in table.columns:
        if column.primary_key or column.nullable or column.server_default is not None:
            continue
        foreign = next(iter(column.foreign_keys), None)
        if foreign is not None:
            target = foreign.column.table.name
            if target in seen:
                continue
            existing = conn.execute(select(foreign.column).limit(1)).scalar()
            values[column.name] = existing if existing is not None else _seed_row(
                conn, target, seen=seen | {table_name})
            continue
        kind = column.type
        if isinstance(kind, (JSON, JSONB)):
            values[column.name] = {}
        elif isinstance(kind, Boolean):
            values[column.name] = False
        elif isinstance(kind, (Integer, Numeric)):
            values[column.name] = 1
        elif isinstance(kind, DateTime):
            values[column.name] = _dt.datetime.now(_dt.timezone.utc)
        elif isinstance(kind, Date):
            values[column.name] = _dt.date.today()
        elif isinstance(kind, (String, Text)):
            values[column.name] = f"seed-{uuid.uuid4().hex[:8]}"
        else:
            values[column.name] = f"seed-{uuid.uuid4().hex[:8]}"
    pk = next(iter(table.primary_key.columns))
    return int(conn.execute(insert(table).values(**values).returning(pk)).scalar_one())


def _alembic_with(dsn: str, revision: str, *, extra_location, downgrade: bool = False) -> None:
    from alembic import command
    from tests.commerce_reliability.test_commerce_runtime_foundation_pg import _alembic_config

    cfg = _alembic_config(dsn)
    if extra_location:
        cfg.set_main_option(
            "version_locations",
            f"{REPO_ROOT / 'database' / 'migrations' / 'versions'} {extra_location}")
    previous_cwd, previous_url = os.getcwd(), os.environ.get("DATABASE_URL")
    os.chdir(REPO_ROOT / "database")
    os.environ["DATABASE_URL"] = dsn
    try:
        (command.downgrade if downgrade else command.upgrade)(cfg, revision)
    finally:
        os.chdir(previous_cwd)
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.mark.parametrize("order", ["address_first", "runtime_first"])
def test_the_siblings_apply_in_either_order_and_the_runtime_rolls_back_alone(
        database_at_0109, sibling_location, order) -> None:
    dsn, engine = database_at_0109
    location, address_table = sibling_location
    sequence = ([ADDRESS_REVISION, THIS_REVISION] if order == "address_first"
                else [THIS_REVISION, ADDRESS_REVISION])
    for revision in sequence:
        _alembic_with(dsn, revision, extra_location=location)
    assert _current_revisions(engine) == {ADDRESS_REVISION, THIS_REVISION}
    assert set(hm.HANDOVER_TABLES) <= _relations(engine)
    assert address_table in _relations(engine)
    # The contract's start-state and applied-state vocabulary agrees with the
    # database's own account of itself.
    assert contract.already_applied(frozenset(_current_revisions(engine)))

    # Address data lives in the sibling's table before the runtime rolls back.
    with engine.begin() as conn:
        seeded = _seed_row(conn, address_table)
        before = conn.execute(text(f'SELECT COUNT(*) FROM "{address_table}"')).scalar_one()
    assert before == 1

    # The one runtime-only rollback spelling.
    argv = contract.build_downgrade_argv(python_executable="python")
    target = argv[-1]
    assert argv[-2] == "downgrade" and target == contract.RUNTIME_ONLY_DOWNGRADE_TARGET
    _alembic_with(dsn, target, extra_location=location, downgrade=True)

    remaining = _relations(engine)
    assert not (remaining & set(hm.HANDOVER_TABLES))      # the runtime's three are gone
    assert address_table in remaining                       # the sibling's table is not
    assert set(LEDGER_RELATIONS) <= remaining
    assert _current_revisions(engine) == {ADDRESS_REVISION}  # ...and its revision stands
    assert contract.start_state_accepted(frozenset(_current_revisions(engine)))
    with engine.connect() as conn:                          # ...and its rows are intact
        rows = conn.execute(text(f'SELECT id FROM "{address_table}"')).scalars().all()
    assert rows == [seeded]


def test_the_common_ancestor_spellings_would_take_the_sibling_with_them(
        database_at_0109, sibling_location) -> None:
    """Why the contract names ``0111@-1`` and nothing else: proved, not read."""
    dsn, engine = database_at_0109
    location, address_table = sibling_location
    for spelling in ("0109", f"{THIS_REVISION}-1"):
        _alembic_with(dsn, ADDRESS_REVISION, extra_location=location)
        _alembic_with(dsn, THIS_REVISION, extra_location=location)
        _alembic_with(dsn, spelling, extra_location=location, downgrade=True)
        assert address_table not in _relations(engine), spelling
        assert _current_revisions(engine) == {"0109"}, spelling
