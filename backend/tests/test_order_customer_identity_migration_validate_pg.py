"""A1-Validate migration tests — 0087 → 0088 only (never head/0089)."""
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.order_customer_identity_postgres_fixtures import _connect_engine  # noqa: E402
from scripts.operators.staging_migration_0087_to_0088 import (  # noqa: E402
    collect_constraint_violation_counts,
    validate_constraint_violation_preflight,
)

MIGRATION_TENANT_ID = 880_002
_REPOSITORY_ALEMBIC_HEADS = frozenset({"0092", "0094"})

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
    db_name = f"a1_val_{uuid.uuid4().hex[:12]}"
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


def _seed_minimal_tenant_at_0087(engine: Engine) -> None:
    _run_alembic(engine, "0086")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active, is_platform_tenant)
                VALUES (:tid, 'Validate Tenant', true, false)
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )
    _run_alembic(engine, "0087")


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


def _has_index(engine: Engine, table: str, name: str) -> bool:
    insp = inspect(engine)
    try:
        return name in {i.get("name") for i in insp.get_indexes(table)}
    except Exception:
        return False


@pytest.fixture()
def ephemeral_validate_engine() -> Iterator[Engine]:
    admin_engine = _connect_engine()
    db_name, _ = _create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        _seed_minimal_tenant_at_0087(test_engine)
        yield test_engine
    finally:
        test_engine.dispose()
        _drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


def test_repository_has_parallel_heads_0092_and_0094() -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        script = ScriptDirectory(str(_DATABASE / "migrations"))
    finally:
        os.chdir(prev_cwd)
    heads = set(script.get_heads())
    assert heads == _REPOSITORY_ALEMBIC_HEADS


def test_migration_0088_creates_concurrent_indexes(ephemeral_validate_engine: Engine) -> None:
    for idx_name in _ORDER_INDEXES:
        assert not _has_index(ephemeral_validate_engine, "orders", idx_name)

    _run_alembic(ephemeral_validate_engine, "0088")

    for idx_name in _ORDER_INDEXES:
        assert _has_index(ephemeral_validate_engine, "orders", idx_name), idx_name
        assert _index_valid(ephemeral_validate_engine, idx_name), idx_name


def test_migration_0088_validates_orders_constraints_and_capability(
    ephemeral_validate_engine: Engine,
) -> None:
    for name in _ORDER_CONSTRAINTS:
        assert not _constraint_validated(ephemeral_validate_engine, name), name

    _run_alembic(ephemeral_validate_engine, "0088")

    for name in _ORDER_CONSTRAINTS:
        assert _constraint_validated(ephemeral_validate_engine, name), name

    with ephemeral_validate_engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == "0088"
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


def test_migration_0088_idempotent_rerun(ephemeral_validate_engine: Engine) -> None:
    _run_alembic(ephemeral_validate_engine, "0088")
    _run_alembic(ephemeral_validate_engine, "0088")

    for name in _ORDER_CONSTRAINTS:
        assert _constraint_validated(ephemeral_validate_engine, name), name
    for idx_name in _ORDER_INDEXES:
        assert _index_valid(ephemeral_validate_engine, idx_name), idx_name


def test_migration_0088_blocked_when_constraint_violation_present(
    ephemeral_validate_engine: Engine,
) -> None:
    """An orphaned tenant/customer tuple blocks preflight and 0088 validation.

    ``session_replication_role`` is transaction-local and only permits this
    throwaway PostgreSQL fixture to model legacy FK drift. CHECK constraints
    remain active, so the row is valid for every non-target 0087 CHECK.
    """
    with ephemeral_validate_engine.begin() as conn:
        conn.execute(
            text(
                """
                SET LOCAL session_replication_role = replica
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO orders (
                    tenant_id, external_id, status, total, source,
                    customer_id, order_source_kind, identity_namespace,
                    customer_link_state, customer_link_evidence_class
                ) VALUES (
                    :tid, 'VIOLATE-0088', 'pending', '10', 'internal',
                    :orphan_customer_id, 'nahla_internal',
                    'nahla_internal_order_v1', 'verified', 'authoritative'
                )
                """
            ),
            {"tid": MIGRATION_TENANT_ID, "orphan_customer_id": 9_999_999},
        )

    with ephemeral_validate_engine.connect() as conn:
        violation_counts = collect_constraint_violation_counts(conn)
        assert violation_counts["fk_orders_tenant_customer"] == 1
        assert violation_counts["violation_rows_total"] == 1
        assert all(
            count == 0
            for name, count in violation_counts.items()
            if name not in {"fk_orders_tenant_customer", "violation_rows_total"}
        )
        preflight_failure = validate_constraint_violation_preflight(conn)
        assert preflight_failure is not None
        assert preflight_failure.error_class == "constraint_violation_preflight_failed"
        assert preflight_failure.stage == "orders_constraint_violations_present"

    with pytest.raises(IntegrityError, match="fk_orders_tenant_customer"):
        _run_alembic(ephemeral_validate_engine, "0088")

    with ephemeral_validate_engine.connect() as conn:
        assert not _constraint_validated(ephemeral_validate_engine, "fk_orders_tenant_customer")
        cap = conn.execute(
            text(
                """
                SELECT state, validation_revision
                FROM order_customer_identity_capability_state
                WHERE capability_key = 'order_customer_identity'
                """
            )
        ).mappings().one()
        assert cap["state"] == "expand"
        assert cap["validation_revision"] is None
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == "0087"


def test_migration_0089_coexists_without_altering_0088_path(
    ephemeral_validate_engine: Engine,
) -> None:
    _run_alembic(ephemeral_validate_engine, "0089")

    with ephemeral_validate_engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == "0089"
        cap = conn.execute(
            text(
                """
                SELECT state, validation_revision
                FROM order_customer_identity_capability_state
                WHERE capability_key = 'order_customer_identity'
                """
            )
        ).mappings().one()
        assert cap["state"] == "expand"
        assert cap["validation_revision"] is None

    for idx_name in _ORDER_INDEXES:
        assert not _has_index(ephemeral_validate_engine, "orders", idx_name), idx_name
    for name in _ORDER_CONSTRAINTS:
        assert not _constraint_validated(ephemeral_validate_engine, name), name

    insp = inspect(ephemeral_validate_engine)
    assert "conversation_a1_subject_bindings" in insp.get_table_names()


def test_migration_0088_never_selected_by_head_literal() -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        script = ScriptDirectory(str(_DATABASE / "migrations"))
    finally:
        os.chdir(prev_cwd)
    for head in script.get_heads():
        assert head in _REPOSITORY_ALEMBIC_HEADS
    assert "head" not in {"0092", "0094"}


def test_new_writes_still_enforced_after_0088(ephemeral_validate_engine: Engine) -> None:
    _run_alembic(ephemeral_validate_engine, "0088")

    with ephemeral_validate_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO orders (
                            tenant_id, external_id, status, total, source,
                            order_source_kind, customer_link_evidence_class
                        ) VALUES (
                            :tid, 'FAIL-WA-POST-0088', 'pending', '10', 'whatsapp',
                            'whatsapp', 'authoritative'
                        )
                        """
                    ),
                    {"tid": MIGRATION_TENANT_ID},
                )
