"""A1-Expand migration tests — 0086 → 0087 only (0088 deferred to A1-Validate PR)."""
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

from tests.order_customer_identity_postgres_fixtures import (  # noqa: E402
    _connect_engine,
)

MIGRATION_TENANT_ID = 880_001
LEGACY_CUSTOMER_REF = "LEG-CUST-1"
LEGACY_ORDER_EXT_ID = "LEG-ORD-1"
_EXPAND_MIGRATION_TARGET = "0087"
from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    REPOSITORY_ALEMBIC_HEADS,
)

_0087_CONSTRAINTS = (
    "chk_orders_external_no_canonical_customer",
    "chk_orders_external_profile_authoritative",
    "chk_orders_external_no_customer_link_authoritative",
    "chk_orders_nahla_internal_authoritative",
    "chk_orders_internal_no_external_authoritative",
    "chk_orders_untrusted_no_authoritative",
    "chk_orders_untrusted_kinds_no_links",
)

_0087_INDEXES = (
    "uq_customers_tenant_id",
    "uq_integrations_tenant_id_id",
    "uq_external_customer_profiles_identity",
    "uq_external_customer_profiles_tenant_id_connection",
)

_DEFERRED_ORDER_INDEXES = (
    "ix_orders_tenant_customer_id",
    "ix_orders_tenant_external_tuple",
    "ix_orders_tenant_order_source_kind",
)

_0087_FKS = (
    "fk_orders_tenant_customer",
    "fk_orders_external_profile_connection",
    "fk_ecp_tenant_integration",
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


def _downgrade_one(engine: Engine) -> None:
    cfg = _alembic_config(engine)
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        command.downgrade(cfg, "-1")
    finally:
        os.chdir(prev_cwd)


def _create_ephemeral_database(admin_engine: Engine) -> tuple[str, str]:
    db_name = f"a1_mig_{uuid.uuid4().hex[:12]}"
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


def _seed_legacy_rows_at_0086(engine: Engine) -> dict:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active, is_platform_tenant)
                VALUES (:tid, 'Migration Legacy Tenant', true, false)
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )
        intg_id = conn.execute(
            text(
                """
                INSERT INTO integrations (tenant_id, provider, external_store_id, config, enabled)
                VALUES (:tid, 'salla', 'MIG-STORE-880', '{"api_key":"legacy"}'::jsonb, true)
                RETURNING id
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        ).scalar_one()
        cust_id = conn.execute(
            text(
                """
                INSERT INTO customers (tenant_id, name, salla_customer_id, acquisition_channel)
                VALUES (:tid, 'عميل قديم', :ref, 'salla_sync')
                RETURNING id
                """
            ),
            {"tid": MIGRATION_TENANT_ID, "ref": LEGACY_CUSTOMER_REF},
        ).scalar_one()
        order_id = conn.execute(
            text(
                """
                INSERT INTO orders (tenant_id, external_id, status, total, source, customer_name)
                VALUES (:tid, :ext_id, 'pending', '99.00', 'salla', 'عميل قديم')
                RETURNING id
                """
            ),
            {
                "tid": MIGRATION_TENANT_ID,
                "ext_id": LEGACY_ORDER_EXT_ID,
            },
        ).scalar_one()
    return {
        "integration_id": int(intg_id),
        "customer_id": int(cust_id),
        "order_id": int(order_id),
    }


def _constraint_validated(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).first()
    return bool(row and row[0])


@pytest.fixture()
def ephemeral_migration_engine() -> Iterator[Engine]:
    admin_engine = _connect_engine()
    db_name, _ = _create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        _run_alembic(test_engine, "0086")
        yield test_engine
    finally:
        test_engine.dispose()
        _drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


def test_migration_0087_expand_not_valid_constraints(ephemeral_migration_engine: Engine) -> None:
    _seed_legacy_rows_at_0086(ephemeral_migration_engine)
    _run_alembic(ephemeral_migration_engine, "0087")

    for chk_name in _0087_CONSTRAINTS:
        assert _has_check(ephemeral_migration_engine, chk_name), f"missing CHECK {chk_name}"
        assert not _constraint_validated(ephemeral_migration_engine, chk_name)

    for fk_name in _0087_FKS:
        assert _has_fk(ephemeral_migration_engine, fk_name)
        assert not _constraint_validated(ephemeral_migration_engine, fk_name), (
            f"FK {fk_name} must remain NOT VALID at 0087; validation deferred to 0088"
        )

    for idx_name in _DEFERRED_ORDER_INDEXES:
        assert not _has_index(ephemeral_migration_engine, "orders", idx_name), idx_name


def test_migration_0087_new_writes_enforced_while_not_valid(ephemeral_migration_engine: Engine) -> None:
    _seed_legacy_rows_at_0086(ephemeral_migration_engine)
    _run_alembic(ephemeral_migration_engine, "0087")

    with ephemeral_migration_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO orders (
                            tenant_id, external_id, status, total, source,
                            order_source_kind, customer_link_evidence_class
                        ) VALUES (
                            :tid, 'FAIL-WA-0087', 'pending', '10', 'whatsapp',
                            'whatsapp', 'authoritative'
                        )
                        """
                    ),
                    {"tid": MIGRATION_TENANT_ID},
                )


def test_migration_chain_0086_seed_0087_target_repository_0098_head(
    ephemeral_migration_engine: Engine,
) -> None:
    seed = _seed_legacy_rows_at_0086(ephemeral_migration_engine)
    _run_alembic(ephemeral_migration_engine, _EXPAND_MIGRATION_TARGET)

    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        script = ScriptDirectory(str(_DATABASE / "migrations"))
    finally:
        os.chdir(prev_cwd)
    heads = set(script.get_heads())
    assert heads == REPOSITORY_ALEMBIC_HEADS
    # Ephemeral DB stops at A1-Expand 0087; current integration head is 0099.
    assert "0099" in heads
    assert script.get_revision("0099").down_revision == "0098"

    with ephemeral_migration_engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == _EXPAND_MIGRATION_TARGET
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

    for idx_name in _0087_INDEXES:
        found = any(
            idx_name in {i.get("name") for i in inspect(ephemeral_migration_engine).get_indexes(table)}
            for table in inspect(ephemeral_migration_engine).get_table_names()
        )
        assert found, f"missing index {idx_name}"

    for fk_name in _0087_FKS:
        assert _has_fk(ephemeral_migration_engine, fk_name), fk_name

    with ephemeral_migration_engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT customer_id, customer_name, customer_link_state, order_source_kind,
                       external_customer_profile_id, external_identity_evidence_class
                FROM orders
                WHERE tenant_id = :tid AND external_id = :ext_id
                """
            ),
            {"tid": MIGRATION_TENANT_ID, "ext_id": LEGACY_ORDER_EXT_ID},
        ).mappings().one()
        assert row["customer_id"] is None
        assert row["customer_name"] == "عميل قديم"
        assert row["customer_link_state"] == "unlinked"
        assert row["order_source_kind"] is None

        cust = conn.execute(
            text("SELECT salla_customer_id FROM customers WHERE id = :cid"),
            {"cid": seed["customer_id"]},
        ).scalar_one()
        assert cust == LEGACY_CUSTOMER_REF


def test_migration_downgrade_0087_to_0086_ephemeral_only(ephemeral_migration_engine: Engine) -> None:
    """Downgrade 0087→0086 on ephemeral DB only; linkage data not safe to restore."""
    _seed_legacy_rows_at_0086(ephemeral_migration_engine)
    _run_alembic(ephemeral_migration_engine, "0087")
    _downgrade_one(ephemeral_migration_engine)

    insp = inspect(ephemeral_migration_engine)
    assert "external_customer_profiles" not in insp.get_table_names()
    assert "order_customer_identity_capability_state" not in insp.get_table_names()

    order_cols = {c["name"] for c in insp.get_columns("orders")}
    assert "external_customer_profile_id" not in order_cols
    assert "customer_link_state" not in order_cols

    with ephemeral_migration_engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == "0086"


def _has_index(engine: Engine, table: str, name: str) -> bool:
    insp = inspect(engine)
    try:
        return name in {i.get("name") for i in insp.get_indexes(table)}
    except Exception:
        return False


def _has_fk(engine: Engine, name: str) -> bool:
    insp = inspect(engine)
    for table in insp.get_table_names():
        for c in insp.get_foreign_keys(table):
            if c.get("name") == name:
                return True
    return False


def _has_check(engine: Engine, name: str) -> bool:
    insp = inspect(engine)
    for table in insp.get_table_names():
        for c in insp.get_check_constraints(table):
            if c.get("name") == name:
                return True
    return False
