"""PostgreSQL fixtures for legacy 0024→0030 migration drift recovery tests."""
from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from tests.legacy_migration_drift_postgres_fixtures import (
    BASE_REVISION as LEGACY_BASE_REVISION,
    TARGET_REVISION as LEGACY_TARGET_REVISION,
    assert_revision,
    connect_engine,
    create_ephemeral_database,
    downgrade_alembic,
    drop_ephemeral_database,
    run_alembic,
)

BASE_REVISION = "0024"
TARGET_REVISION = "0030"

REQUIRED_TABLES = (
    "product_interests",
    "promotions",
    "offer_decisions",
)

REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "product_interests": (
        "ix_product_interests_pending",
        "uq_product_interest_pending_per_customer",
    ),
    "orders": (
        "ix_orders_external_order_number",
        "ix_orders_source",
    ),
    "smart_automations": ("ix_smart_automations_engine",),
    "promotions": (
        "ix_promotions_tenant_id",
        "ix_promotions_status",
        "ix_promotions_tenant_status",
        "ix_promotions_tenant_type",
    ),
    "offer_decisions": (
        "ix_offer_decisions_tenant_id",
        "ix_offer_decisions_decision_id",
        "ix_offer_decisions_automation_id",
        "ix_offer_decisions_customer_id",
        "ix_offer_decisions_tenant_created",
        "ix_offer_decisions_tenant_surface",
        "ix_offer_decisions_tenant_chosen",
        "ix_offer_decisions_tenant_attributed",
        "uq_offer_decisions_decision_id",
    ),
    "tenants": ("ix_tenants_is_platform_tenant",),
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "products": ("stock_quantity", "in_stock"),
    "orders": ("external_order_number", "customer_name", "source"),
    "smart_automations": ("engine",),
    "tenants": ("is_platform_tenant",),
}

# Conservative create_all / forward-ORM drift shapes for 0025–0030.
# Guard boundary: covers likely collision objects only; arbitrary partial
# schemas whose required columns or constraints are absent are out of scope.
_DRIFT_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS product_interests (
        id              SERIAL PRIMARY KEY,
        tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
        product_id      INTEGER NOT NULL REFERENCES products(id),
        customer_id     INTEGER NOT NULL REFERENCES customers(id),
        customer_phone  VARCHAR,
        source          VARCHAR,
        notified        BOOLEAN NOT NULL DEFAULT false,
        notified_at     TIMESTAMP,
        created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        metadata        JSONB
    )
    """,
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_quantity INTEGER",
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS in_stock BOOLEAN NOT NULL DEFAULT true",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS external_order_number VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS source VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_orders_external_order_number ON orders (external_order_number)",
    "CREATE INDEX IF NOT EXISTS ix_orders_source ON orders (source)",
    "ALTER TABLE smart_automations ADD COLUMN IF NOT EXISTS engine VARCHAR NOT NULL DEFAULT 'recovery'",
    "CREATE INDEX IF NOT EXISTS ix_smart_automations_engine ON smart_automations (engine)",
    """
    CREATE TABLE IF NOT EXISTS promotions (
        id              SERIAL PRIMARY KEY,
        tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
        name            VARCHAR NOT NULL,
        description     TEXT,
        promotion_type  VARCHAR NOT NULL,
        discount_value  NUMERIC(10, 2),
        conditions      JSONB,
        starts_at       TIMESTAMP,
        ends_at         TIMESTAMP,
        status          VARCHAR NOT NULL DEFAULT 'draft',
        usage_count     INTEGER NOT NULL DEFAULT 0,
        usage_limit     INTEGER,
        metadata        JSONB,
        created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_promotions_tenant_id ON promotions (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_promotions_status ON promotions (status)",
    """
    CREATE TABLE IF NOT EXISTS offer_decisions (
        id                  SERIAL PRIMARY KEY,
        tenant_id           INTEGER NOT NULL REFERENCES tenants(id),
        decision_id         VARCHAR NOT NULL,
        surface             VARCHAR NOT NULL,
        automation_id       INTEGER,
        event_id            INTEGER,
        customer_id         INTEGER,
        signals_snapshot    JSONB,
        chosen_source       VARCHAR NOT NULL,
        chosen_promotion_id INTEGER,
        chosen_coupon_id    INTEGER,
        discount_type       VARCHAR,
        discount_value      NUMERIC(10, 2),
        validity_days       INTEGER,
        reason_codes        JSONB,
        policy_version      VARCHAR NOT NULL DEFAULT 'v1.0-deterministic',
        experiment_arm      VARCHAR,
        redeemed_at         TIMESTAMP,
        order_id            INTEGER,
        revenue_amount      NUMERIC(12, 2),
        attributed          BOOLEAN NOT NULL DEFAULT false,
        created_at          TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_offer_decisions_tenant_id ON offer_decisions (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_offer_decisions_decision_id ON offer_decisions (decision_id)",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_platform_tenant BOOLEAN NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS ix_tenants_is_platform_tenant ON tenants (is_platform_tenant)",
)


def seed_create_all_drift_0025_0030(engine: Engine) -> None:
    """Pre-create likely collision objects without merchant PII."""
    with engine.begin() as conn:
        for stmt in _DRIFT_STATEMENTS:
            conn.execute(text(stmt))


def assert_schema_at_0030(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table in REQUIRED_TABLES:
        assert table in tables, f"missing table {table}"
    for table, columns in REQUIRED_COLUMNS.items():
        present = {c["name"] for c in insp.get_columns(table)}
        for column in columns:
            assert column in present, f"missing column {table}.{column}"
    for table, indexes in REQUIRED_INDEXES.items():
        present = {i.get("name") for i in insp.get_indexes(table)}
        for index_name in indexes:
            assert index_name in present, f"missing index {table}.{index_name}"


@pytest.fixture()
def ephemeral_legacy_migration_engine_0024() -> Iterator[Engine]:
    """Ephemeral PG database pinned at Alembic 0024."""
    admin_engine = connect_engine()
    db_name, _ = create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(test_engine, LEGACY_BASE_REVISION)
        run_alembic(test_engine, LEGACY_TARGET_REVISION)
        assert_revision(test_engine, BASE_REVISION)
        yield test_engine
    finally:
        test_engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


__all__ = [
    "BASE_REVISION",
    "TARGET_REVISION",
    "REQUIRED_COLUMNS",
    "REQUIRED_INDEXES",
    "REQUIRED_TABLES",
    "assert_revision",
    "assert_schema_at_0030",
    "downgrade_alembic",
    "ephemeral_legacy_migration_engine_0024",
    "run_alembic",
    "seed_create_all_drift_0025_0030",
]
