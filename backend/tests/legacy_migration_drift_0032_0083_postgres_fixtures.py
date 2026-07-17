"""PostgreSQL fixtures for legacy 0032→0083 migration drift recovery tests."""
from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from scripts.operators.staging_migration_0032_to_0083_contract import (
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    TARGET_REVISION,
)
from tests.legacy_migration_drift_postgres_fixtures import (
    assert_revision,
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)

BASE_REVISION = "0032"

# Conservative create_all / forward-ORM drift shapes for 0033–0083.
# Guard boundary: covers likely collision objects only; arbitrary partial
# schemas whose required columns or constraints are absent are out of scope.
_DRIFT_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS cross_merchant_signals (
        id              SERIAL PRIMARY KEY,
        tenant_hash     VARCHAR(64) NOT NULL,
        industry        VARCHAR(64) NOT NULL DEFAULT 'unknown',
        intent          VARCHAR(64) NOT NULL DEFAULT 'unknown',
        action          VARCHAR(64) NOT NULL DEFAULT 'unknown',
        ui_mode         VARCHAR(32) NOT NULL DEFAULT 'unknown',
        outcome         VARCHAR(32) NOT NULL DEFAULT 'unknown',
        value_bucket    VARCHAR(32) NOT NULL DEFAULT 'unknown',
        turn_index      INTEGER NOT NULL DEFAULT 0,
        model_path      VARCHAR(32) NOT NULL DEFAULT 'rule',
        latency_ms      INTEGER NOT NULL DEFAULT 0,
        tier            VARCHAR(16) NOT NULL DEFAULT 'global',
        extra           JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_cross_merchant_signals_tenant_hash ON cross_merchant_signals (tenant_hash)",
    "CREATE INDEX IF NOT EXISTS ix_xms_industry_action ON cross_merchant_signals (industry, action)",
    "CREATE INDEX IF NOT EXISTS ix_xms_action_outcome ON cross_merchant_signals (action, outcome)",
    "CREATE INDEX IF NOT EXISTS ix_xms_tier_industry ON cross_merchant_signals (tier, industry)",
    "CREATE INDEX IF NOT EXISTS ix_xms_created_at ON cross_merchant_signals (created_at)",
    """
    CREATE TABLE IF NOT EXISTS learned_sales_policies (
        id                  SERIAL PRIMARY KEY,
        scope               VARCHAR(16) NOT NULL DEFAULT 'global',
        industry            VARCHAR(64) NOT NULL DEFAULT '*',
        intent              VARCHAR(64) NOT NULL DEFAULT 'unknown',
        recommended_action  VARCHAR(64) NOT NULL DEFAULT 'unknown',
        recommended_ui      VARCHAR(32) NOT NULL DEFAULT 'unknown',
        confidence          DOUBLE PRECISION NOT NULL DEFAULT 0,
        sample_size         INTEGER NOT NULL DEFAULT 0,
        extra               JSONB,
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_lsp_scope_industry_intent UNIQUE (scope, industry, intent)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_lsp_intent ON learned_sales_policies (intent)",
    "CREATE INDEX IF NOT EXISTS ix_lsp_industry_intent ON learned_sales_policies (industry, intent)",
    """
    CREATE TABLE IF NOT EXISTS product_variants (
        id              SERIAL PRIMARY KEY,
        tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
        product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        salla_variant_id VARCHAR(64),
        sku             VARCHAR(128),
        retailer_id     VARCHAR(255),
        price           VARCHAR(32),
        currency        VARCHAR(8),
        stock_quantity  INTEGER,
        in_stock        BOOLEAN NOT NULL DEFAULT true,
        options         JSONB,
        option_summary  VARCHAR(255),
        image_url       VARCHAR(2048),
        is_default      BOOLEAN NOT NULL DEFAULT false,
        metadata        JSONB,
        created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_variants_product_salla UNIQUE (product_id, salla_variant_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_variants_tenant_retailer ON product_variants (tenant_id, retailer_id)",
    """
    CREATE TABLE IF NOT EXISTS product_groups (
        id              SERIAL PRIMARY KEY,
        tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        slug            VARCHAR(64) NOT NULL,
        label           VARCHAR(255) NOT NULL,
        description     TEXT,
        catalog_match   VARCHAR(255),
        priority        INTEGER NOT NULL DEFAULT 100,
        is_active       BOOLEAN NOT NULL DEFAULT true,
        source          VARCHAR(32) NOT NULL DEFAULT 'manual',
        metadata_json   JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted_at      TIMESTAMPTZ,
        CONSTRAINT uq_product_groups_tenant_slug UNIQUE (tenant_id, slug)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_product_groups_tenant_active ON product_groups (tenant_id, is_active)",
    "CREATE INDEX IF NOT EXISTS ix_product_groups_tenant_priority ON product_groups (tenant_id, priority)",
    """
    CREATE TABLE IF NOT EXISTS product_group_items (
        id              SERIAL PRIMARY KEY,
        group_id        INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,
        product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        variant_id      INTEGER REFERENCES product_variants(id) ON DELETE SET NULL,
        priority        INTEGER NOT NULL DEFAULT 0,
        label_override  VARCHAR(255) NOT NULL DEFAULT '',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_product_group_items_group_product UNIQUE (group_id, product_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_product_group_items_group_priority ON product_group_items (group_id, priority)",
    """
    CREATE TABLE IF NOT EXISTS product_relations (
        id                  SERIAL PRIMARY KEY,
        tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        source_product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        target_product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        relation_type       VARCHAR(32) NOT NULL,
        priority            INTEGER NOT NULL DEFAULT 0,
        source              VARCHAR(32) NOT NULL DEFAULT 'manual',
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_product_relations_tenant_pair_type
            UNIQUE (tenant_id, source_product_id, target_product_id, relation_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_product_relations_tenant_source ON product_relations (tenant_id, source_product_id)",
    "CREATE INDEX IF NOT EXISTS ix_product_relations_tenant_target ON product_relations (tenant_id, target_product_id)",
    """
    CREATE TABLE IF NOT EXISTS product_rankings (
        id                  SERIAL PRIMARY KEY,
        tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        is_best_seller      BOOLEAN NOT NULL DEFAULT false,
        sales_rank          INTEGER,
        sales_score         DOUBLE PRECISION,
        merchant_priority   INTEGER NOT NULL DEFAULT 0,
        stats_source        VARCHAR(32) NOT NULL DEFAULT 'manual',
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_product_rankings_tenant_product UNIQUE (tenant_id, product_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_product_rankings_tenant_best_seller ON product_rankings (tenant_id, is_best_seller)",
)


def seed_create_all_drift_0033_0083(engine: Engine) -> None:
    """Pre-create likely collision objects without merchant PII."""
    with engine.begin() as conn:
        for stmt in _DRIFT_STATEMENTS:
            conn.execute(text(stmt))


def seed_inequivalent_product_variants_drift(engine: Engine) -> None:
    """Broken drift shape: product_variants exists but lacks product_id."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS product_variants (
                    id SERIAL PRIMARY KEY,
                    tenant_id INTEGER NOT NULL REFERENCES tenants(id)
                )
                """
            )
        )


def assert_schema_at_0083(engine: Engine) -> None:
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
def ephemeral_legacy_migration_engine_0032() -> Iterator[Engine]:
    """Ephemeral PG database pinned at Alembic 0032."""
    admin_engine = connect_engine()
    db_name, _ = create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(test_engine, BASE_REVISION)
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
    "assert_schema_at_0083",
    "ephemeral_legacy_migration_engine_0032",
    "run_alembic",
    "seed_create_all_drift_0033_0083",
    "seed_inequivalent_product_variants_drift",
]
