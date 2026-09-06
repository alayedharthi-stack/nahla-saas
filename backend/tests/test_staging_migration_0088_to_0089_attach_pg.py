"""PostgreSQL migration tests for attaching 0089 onto validated 0088."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.order_customer_identity_postgres_fixtures import _connect_engine  # noqa: E402
from scripts.operators.staging_migration_0088_to_0089 import (  # noqa: E402
    assert_upgrade_command_safe,
    build_alembic_upgrade_command,
    validate_post_success_attach_invariants,
    validate_pre_attach_validated_invariants,
)
from scripts.operators.staging_migration_0088_to_0089_contract import (  # noqa: E402
    CASB_TABLE,
    EXPECTED_POST_SUCCESS_REVISIONS,
)

MIGRATION_TENANT_ID = 890_002
_REPOSITORY_ALEMBIC_HEADS = frozenset({"0092", "0104"})

_ORDER_INDEXES = (
    "ix_orders_tenant_customer_id",
    "ix_orders_tenant_external_tuple",
    "ix_orders_tenant_order_source_kind",
)

_ORDER_CONSTRAINTS = (
    "chk_orders_external_no_canonical_customer",
    "chk_orders_external_profile_authoritative",
    "chk_orders_external_no_customer_link_authoritative",
    "chk_orders_nahla_internal_authoritative",
    "chk_orders_internal_no_external_authoritative",
    "chk_orders_untrusted_no_authoritative",
    "chk_orders_untrusted_kinds_no_links",
    "fk_orders_tenant_customer",
    "fk_orders_external_profile_connection",
)

_0089_OBJECTS = (
    CASB_TABLE,
    "uq_conversations_tenant_id",
    "uq_casb_tenant_conversation_active",
    "fk_casb_tenant_conversation",
    "fk_casb_tenant_internal_customer",
)


def _alembic_config(engine: Engine) -> Config:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        cfg = Config("alembic.ini")
        url = str(engine.url.render_as_string(hide_password=False))
        cfg.set_main_option("sqlalchemy.url", url)
        os.environ["DATABASE_URL"] = url
        return cfg
    finally:
        os.chdir(prev_cwd)


def _run_alembic(engine: Engine, revision: str) -> None:
    cfg = _alembic_config(engine)
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        command.upgrade(cfg, revision)
    finally:
        os.chdir(prev_cwd)


def _create_ephemeral_database(admin_engine: Engine) -> tuple[str, str]:
    db_name = f"a1_attach_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    return db_name, str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False))


def _drop_ephemeral_database(admin_engine: Engine, db_name: str) -> None:
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = :db_name AND pid <> pg_backend_pid()
                """
            ),
            {"db_name": db_name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


def _seed_validated_0088(engine: Engine) -> None:
    _run_alembic(engine, "0086")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active, is_platform_tenant)
                VALUES (:tid, 'Attach Tenant', true, false)
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )
    _run_alembic(engine, "0087")
    _run_alembic(engine, "0088")


def _read_revisions(engine: Engine) -> frozenset[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version ORDER BY 1")).fetchall()
    return frozenset(row[0] for row in rows)


def _constraint_validated(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).first()
    return bool(row and row[0])


def _index_valid(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT i.indisvalid
                FROM pg_class c
                JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = :name
                LIMIT 1
                """
            ),
            {"name": name},
        ).first()
    return bool(row and row[0])


@pytest.fixture()
def ephemeral_attach_engine() -> Iterator[Engine]:
    admin_engine = _connect_engine()
    db_name, _ = _create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        _seed_validated_0088(test_engine)
        yield test_engine
    finally:
        test_engine.dispose()
        _drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


def test_repository_has_parallel_heads_0092_and_0098() -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        script = ScriptDirectory(str(_DATABASE / "migrations"))
    finally:
        os.chdir(prev_cwd)
    assert set(script.get_heads()) == _REPOSITORY_ALEMBIC_HEADS


def test_upgrade_command_never_uses_head_literal() -> None:
    cmd = build_alembic_upgrade_command("python")
    assert "head" not in " ".join(cmd)
    assert_upgrade_command_safe(cmd)


def test_preflight_accepts_validated_0088_without_0089_objects(ephemeral_attach_engine: Engine) -> None:
    with ephemeral_attach_engine.connect() as conn:
        assert _read_revisions(ephemeral_attach_engine) == frozenset({"0088"})
        failure = validate_pre_attach_validated_invariants(conn)
        assert failure is None
        assert CASB_TABLE not in set(inspect(conn).get_table_names())


def test_migration_0089_attach_from_validated_0088_adds_sibling_head(ephemeral_attach_engine: Engine) -> None:
    _run_alembic(ephemeral_attach_engine, "0089")

    assert _read_revisions(ephemeral_attach_engine) == EXPECTED_POST_SUCCESS_REVISIONS
    assert validate_post_success_attach_invariants(ephemeral_attach_engine) is None

    insp = inspect(ephemeral_attach_engine)
    assert CASB_TABLE in insp.get_table_names()
    assert "uq_conversations_tenant_id" in {
        idx.get("name") for idx in insp.get_indexes("conversations")
    }


def test_migration_0089_attach_idempotent_rerun(ephemeral_attach_engine: Engine) -> None:
    _run_alembic(ephemeral_attach_engine, "0089")
    _run_alembic(ephemeral_attach_engine, "0089")

    assert validate_post_success_attach_invariants(ephemeral_attach_engine) is None
    for name in _ORDER_CONSTRAINTS:
        assert _constraint_validated(ephemeral_attach_engine, name), name
    for idx_name in _ORDER_INDEXES:
        assert _index_valid(ephemeral_attach_engine, idx_name), idx_name


def test_migration_0089_attach_preserves_0088_capability_validated(ephemeral_attach_engine: Engine) -> None:
    _run_alembic(ephemeral_attach_engine, "0089")

    with ephemeral_attach_engine.connect() as conn:
        cap = conn.execute(
            text(
                """
                SELECT state, validation_revision
                FROM order_customer_identity_capability_state
                WHERE capability_key = 'order_customer_identity'
                """
            )
        ).mappings().one()
        assert cap["state"] == "validated"
        assert cap["validation_revision"] == "0088"


def test_wrong_revision_gate_rejects_0087_expand(ephemeral_attach_engine: Engine) -> None:
    admin_engine = _connect_engine()
    db_name, _ = _create_ephemeral_database(admin_engine)
    expand_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        _run_alembic(expand_engine, "0086")
        with expand_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tenants (id, name, is_active, is_platform_tenant)
                    VALUES (:tid, 'Expand Tenant', true, false)
                    """
                ),
                {"tid": MIGRATION_TENANT_ID + 1},
            )
        _run_alembic(expand_engine, "0087")
        with expand_engine.connect() as conn:
            failure = validate_pre_attach_validated_invariants(conn)
        assert failure is not None
        assert failure.stage == "revision_is_0087_not_0088"
    finally:
        expand_engine.dispose()
        _drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


def test_0089_objects_present_after_attach(ephemeral_attach_engine: Engine) -> None:
    _run_alembic(ephemeral_attach_engine, "0089")
    insp = inspect(ephemeral_attach_engine)
    tables = set(insp.get_table_names())
    assert CASB_TABLE in tables
    casb_indexes = {idx.get("name") for idx in insp.get_indexes(CASB_TABLE)}
    assert "uq_casb_tenant_conversation_active" in casb_indexes
    assert "ix_casb_tenant_conversation_state" in casb_indexes
