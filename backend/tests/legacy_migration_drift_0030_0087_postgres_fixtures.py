"""PostgreSQL fixtures for legacy 0030→0087 guarded staging migration tests."""
from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from scripts.operators.staging_migration_0030_to_0032_contract import (  # noqa: E402
    REQUIRED_COLUMNS as STAGE_A_REQUIRED_COLUMNS,
    REQUIRED_INDEXES as STAGE_A_REQUIRED_INDEXES,
    REQUIRED_TABLES as STAGE_A_REQUIRED_TABLES,
    TARGET_REVISION as STAGE_A_TARGET,
)
from scripts.operators.staging_migration_0032_to_0083_contract import (  # noqa: E402
    REQUIRED_COLUMNS as STAGE_B_REQUIRED_COLUMNS,
    REQUIRED_INDEXES as STAGE_B_REQUIRED_INDEXES,
    REQUIRED_TABLES as STAGE_B_REQUIRED_TABLES,
    TARGET_REVISION as STAGE_B_TARGET,
)
from scripts.operators.staging_migration_0083_to_0087_contract import (  # noqa: E402
    DEFERRED_ORDER_INDEXES,
    PRODUCTS_TENANT_EXTERNAL_ID_INDEX,
    REQUIRED_COLUMNS as STAGE_C_REQUIRED_COLUMNS,
    REQUIRED_INDEXES as STAGE_C_REQUIRED_INDEXES,
    REQUIRED_NOT_VALID_CHECK_CONSTRAINTS,
    REQUIRED_NOT_VALID_FOREIGN_KEYS,
    REQUIRED_TABLES as STAGE_C_REQUIRED_TABLES,
    TARGET_REVISION as STAGE_C_TARGET,
)
from tests.legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    assert_revision,
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)

BASE_REVISION = "0030"
FINAL_TARGET = STAGE_C_TARGET
MIGRATION_TENANT_ID = 880_042


def assert_schema_at_0032(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table in STAGE_A_REQUIRED_TABLES:
        assert table in tables, f"missing table {table}"
    for table, columns in STAGE_A_REQUIRED_COLUMNS.items():
        present = {c["name"] for c in insp.get_columns(table)}
        for column in columns:
            assert column in present, f"missing column {table}.{column}"
    for table, indexes in STAGE_A_REQUIRED_INDEXES.items():
        present = {i.get("name") for i in insp.get_indexes(table)}
        for index_name in indexes:
            assert index_name in present, f"missing index {table}.{index_name}"


def assert_schema_at_0083(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table in STAGE_B_REQUIRED_TABLES:
        assert table in tables, f"missing table {table}"
    for table, columns in STAGE_B_REQUIRED_COLUMNS.items():
        present = {c["name"] for c in insp.get_columns(table)}
        for column in columns:
            assert column in present, f"missing column {table}.{column}"
    for table, indexes in STAGE_B_REQUIRED_INDEXES.items():
        present = {i.get("name") for i in insp.get_indexes(table)}
        for index_name in indexes:
            assert index_name in present, f"missing index {table}.{index_name}"


def assert_products_external_id_index_valid(engine: Engine) -> None:
    insp = inspect(engine)
    present = {i.get("name") for i in insp.get_indexes("products")}
    assert PRODUCTS_TENANT_EXTERNAL_ID_INDEX in present
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT i.indisvalid
                FROM pg_class c
                JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = :name
                """
            ),
            {"name": PRODUCTS_TENANT_EXTERNAL_ID_INDEX},
        ).first()
    assert row is not None
    assert row[0] is True


def assert_schema_at_0087_expand(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table in STAGE_C_REQUIRED_TABLES:
        assert table in tables, f"missing table {table}"
    for table, columns in STAGE_C_REQUIRED_COLUMNS.items():
        present = {c["name"] for c in insp.get_columns(table)}
        for column in columns:
            assert column in present, f"missing column {table}.{column}"
    for table, indexes in STAGE_C_REQUIRED_INDEXES.items():
        present = {i.get("name") for i in insp.get_indexes(table)}
        for index_name in indexes:
            assert index_name in present, f"missing index {table}.{index_name}"

    present_order_indexes = {i.get("name") for i in insp.get_indexes("orders")}
    for deferred in DEFERRED_ORDER_INDEXES:
        assert deferred not in present_order_indexes

    with engine.connect() as conn:
        for chk in REQUIRED_NOT_VALID_CHECK_CONSTRAINTS:
            row = conn.execute(
                text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
                {"name": chk},
            ).first()
            assert row is not None, f"missing CHECK {chk}"
            assert row[0] is False, f"CHECK {chk} must remain NOT VALID at 0087"
        for fk in REQUIRED_NOT_VALID_FOREIGN_KEYS:
            row = conn.execute(
                text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
                {"name": fk},
            ).first()
            assert row is not None, f"missing FK {fk}"
            assert row[0] is False, f"FK {fk} must remain NOT VALID at 0087"
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

    assert_products_external_id_index_valid(engine)


def seed_generic_tenant(engine: Engine, tenant_id: int = MIGRATION_TENANT_ID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active, is_platform_tenant)
                VALUES (:tid, 'متجر تجريبي عام', true, false)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"tid": tenant_id},
        )


def seed_duplicate_tenant_phone(engine: Engine, tenant_id: int = MIGRATION_TENANT_ID) -> None:
    seed_generic_tenant(engine, tenant_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO customers (tenant_id, name, phone)
                VALUES
                    (:tid, 'عميل أ', '0500000001'),
                    (:tid, 'عميل ب', '0500000001')
                """
            ),
            {"tid": tenant_id},
        )


def seed_duplicate_salla_backfill_collision(engine: Engine, tenant_id: int = MIGRATION_TENANT_ID) -> None:
    """Two rows whose 0031 backfill COALESCE keys collide within a tenant."""
    seed_generic_tenant(engine, tenant_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO customers (tenant_id, name, metadata)
                VALUES
                    (:tid, 'عميل أ', '{"salla_id": "SALLA-880"}'::jsonb),
                    (:tid, 'عميل ب', '{"external_id": "SALLA-880"}'::jsonb)
                """
            ),
            {"tid": tenant_id},
        )


def seed_duplicate_product_external_id(engine: Engine, tenant_id: int = MIGRATION_TENANT_ID) -> None:
    seed_generic_tenant(engine, tenant_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO products (tenant_id, title, price, in_stock, external_id)
                VALUES
                    (:tid, 'حذاء رياضي أبيض', '199.00', true, 'STORE-SKU-1'),
                    (:tid, 'قميص قطني أزرق', '89.00', true, 'STORE-SKU-1')
                """
            ),
            {"tid": tenant_id},
        )


def seed_product_with_metadata_variants(engine: Engine, tenant_id: int = MIGRATION_TENANT_ID) -> int:
    seed_generic_tenant(engine, tenant_id)
    with engine.begin() as conn:
        product_id = conn.execute(
            text(
                """
                INSERT INTO products (
                    tenant_id, title, price, in_stock,
                    metadata
                )
                VALUES (
                    :tid, 'حذاء رياضي أبيض', '199.00', true,
                    '{"variants": [{"id": "v1", "sku": "SKU-M", "price": "199.00"}]}'::jsonb
                )
                RETURNING id
                """
            ),
            {"tid": tenant_id},
        ).scalar_one()
    return int(product_id)


def seed_product_without_variants(engine: Engine, tenant_id: int = MIGRATION_TENANT_ID) -> int:
    seed_generic_tenant(engine, tenant_id)
    with engine.begin() as conn:
        product_id = conn.execute(
            text(
                """
                INSERT INTO products (tenant_id, title, price, in_stock)
                VALUES (:tid, 'عطر ورد 100ml', '149.00', true)
                RETURNING id
                """
            ),
            {"tid": tenant_id},
        ).scalar_one()
    return int(product_id)


@pytest.fixture()
def ephemeral_legacy_migration_engine_0030() -> Iterator[Engine]:
    """Ephemeral PG database pinned at Alembic 0030."""
    admin_engine = connect_engine()
    db_name, _ = create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(test_engine, "0024")
        run_alembic(test_engine, BASE_REVISION)
        assert_revision(test_engine, BASE_REVISION)
        yield test_engine
    finally:
        test_engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


@pytest.fixture()
def ephemeral_legacy_migration_engine_0083() -> Iterator[Engine]:
    """Ephemeral PG database pinned at Alembic 0083 for Stage C operator tests."""
    admin_engine = connect_engine()
    db_name, _ = create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(test_engine, "0024")
        run_alembic(test_engine, BASE_REVISION)
        run_alembic(test_engine, STAGE_A_TARGET)
        run_alembic(test_engine, STAGE_B_TARGET)
        assert_revision(test_engine, STAGE_B_TARGET)
        yield test_engine
    finally:
        test_engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


__all__ = [
    "BASE_REVISION",
    "FINAL_TARGET",
    "MIGRATION_TENANT_ID",
    "STAGE_A_TARGET",
    "STAGE_B_TARGET",
    "STAGE_C_TARGET",
    "assert_products_external_id_index_valid",
    "assert_revision",
    "assert_schema_at_0032",
    "assert_schema_at_0083",
    "assert_schema_at_0087_expand",
    "ephemeral_legacy_migration_engine_0030",
    "ephemeral_legacy_migration_engine_0083",
    "run_alembic",
    "seed_duplicate_product_external_id",
    "seed_duplicate_salla_backfill_collision",
    "seed_duplicate_tenant_phone",
    "seed_generic_tenant",
    "seed_product_with_metadata_variants",
    "seed_product_without_variants",
]
