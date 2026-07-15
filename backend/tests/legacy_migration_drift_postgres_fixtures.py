"""PostgreSQL fixtures for legacy 0020–0024 migration drift recovery tests."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"

for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

TARGET_REVISION = "0024"
BASE_REVISION = "0016"

# Objects that 0020–0023 must converge on after upgrade (clean or drifted).
REQUIRED_TABLES = (
    "automation_executions",
    "webhook_guardian_log",
    "integrity_events",
    "webhook_events",
)

REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "smart_automations": ("ix_smart_automations_trigger_event",),
    "automation_executions": (
        "ix_automation_executions_event_automation",
        "ix_automation_executions_tenant_id",
    ),
    "webhook_guardian_log": (
        "ix_webhook_guardian_log_tenant_created",
        "ix_webhook_guardian_log_event",
    ),
    "integrity_events": ("ix_integrity_events_created_at",),
    "webhook_events": (
        "ix_webhook_events_status_retry",
        "ix_webhook_events_tenant_received",
        "uq_webhook_events_provider_event",
    ),
    "orders": ("uq_orders_tenant_external_id",),
    "whatsapp_connections": ("uq_wa_conn_waba_id",),
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "smart_automations": ("trigger_event",),
    "whatsapp_connections": ("last_webhook_received_at",),
}

# Mirrors startup safe-alters + create_all drift seen when alembic_version=0016.
# Guard boundary: this fixture covers known collision shapes only. The migration
# guards make those objects compatible; they do not reconcile arbitrary partial
# schemas whose required columns or constraints are absent.
_DRIFT_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE smart_automations ADD COLUMN IF NOT EXISTS trigger_event VARCHAR",
    """
    CREATE TABLE IF NOT EXISTS automation_executions (
        id              SERIAL PRIMARY KEY,
        tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
        automation_id   INTEGER NOT NULL REFERENCES smart_automations(id),
        event_id        INTEGER NOT NULL REFERENCES automation_events(id),
        customer_id     INTEGER REFERENCES customers(id),
        status          VARCHAR NOT NULL,
        skip_reason     VARCHAR,
        action_taken    JSONB,
        error_message   TEXT,
        executed_at     TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS last_webhook_received_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS webhook_guardian_log (
        id              SERIAL PRIMARY KEY,
        tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        phone_number_id VARCHAR,
        waba_id         VARCHAR,
        event           VARCHAR NOT NULL,
        success         BOOLEAN NOT NULL DEFAULT true,
        detail          TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_webhook_guardian_log_tenant_created ON webhook_guardian_log (tenant_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_webhook_guardian_log_event ON webhook_guardian_log (event)",
    """
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE tablename='whatsapp_connections'
            AND indexname='uq_wa_conn_waba_id'
        ) THEN
            CREATE UNIQUE INDEX uq_wa_conn_waba_id
            ON whatsapp_connections (whatsapp_business_account_id)
            WHERE whatsapp_business_account_id IS NOT NULL;
        END IF;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS integrity_events (
        id              SERIAL PRIMARY KEY,
        event           VARCHAR NOT NULL,
        tenant_id       INTEGER,
        other_tenant_id INTEGER,
        phone_number_id VARCHAR,
        waba_id         VARCHAR,
        store_id        VARCHAR,
        provider        VARCHAR,
        action          VARCHAR,
        result          VARCHAR,
        detail          TEXT,
        actor           VARCHAR,
        dry_run         BOOLEAN,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_integrity_events_event ON integrity_events (event)",
    "CREATE INDEX IF NOT EXISTS ix_integrity_events_tenant_id ON integrity_events (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_integrity_events_created_at ON integrity_events (created_at)",
    """
    CREATE TABLE IF NOT EXISTS webhook_events (
        id                 SERIAL PRIMARY KEY,
        tenant_id          INTEGER,
        provider           VARCHAR NOT NULL,
        event_type         VARCHAR,
        external_event_id  VARCHAR,
        store_id           VARCHAR,
        raw_headers        JSONB,
        raw_body           TEXT,
        parsed_payload     JSONB,
        signature_valid    BOOLEAN,
        status             VARCHAR NOT NULL DEFAULT 'received',
        attempts           INTEGER NOT NULL DEFAULT 0,
        last_error         TEXT,
        last_error_at      TIMESTAMPTZ,
        next_retry_at      TIMESTAMPTZ,
        received_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processed_at       TIMESTAMPTZ,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_webhook_events_tenant_id ON webhook_events (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_webhook_events_provider ON webhook_events (provider)",
    "CREATE INDEX IF NOT EXISTS ix_webhook_events_event_type ON webhook_events (event_type)",
    "CREATE INDEX IF NOT EXISTS ix_webhook_events_status ON webhook_events (status)",
)


def _candidate_database_urls() -> list[str]:
    urls: list[str] = []
    explicit = (os.getenv("LEGACY_MIG_PG_TEST_DATABASE_URL") or "").strip()
    if explicit:
        urls.append(explicit)
    a1_url = (os.getenv("A1_PG_TEST_DATABASE_URL") or "").strip()
    if a1_url and a1_url not in urls:
        urls.append(a1_url)
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if db_url and db_url not in urls:
        urls.append(db_url)
    default = "postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas"
    if default not in urls:
        urls.append(default)
    return urls


def _integration_required() -> bool:
    return (os.getenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED") or "").strip() == "1"


def connect_engine() -> Engine:
    last_error: Exception | None = None
    for url in _candidate_database_urls():
        try:
            engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    message = f"PostgreSQL unavailable for legacy migration drift tests: {last_error}"
    if _integration_required():
        pytest.fail(message)
    pytest.skip(message)


def alembic_config(engine: Engine) -> Config:
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


def run_alembic(engine: Engine, revision: str) -> None:
    cfg = alembic_config(engine)
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        command.upgrade(cfg, revision)
    finally:
        os.chdir(prev_cwd)


def downgrade_alembic(engine: Engine, revision: str) -> None:
    cfg = alembic_config(engine)
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        command.downgrade(cfg, revision)
    finally:
        os.chdir(prev_cwd)


def create_ephemeral_database(admin_engine: Engine) -> tuple[str, str]:
    db_name = f"legacy_mig_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    return db_name, str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False))


def drop_ephemeral_database(admin_engine: Engine, db_name: str) -> None:
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


def seed_create_all_safe_alter_drift(engine: Engine) -> None:
    """Pre-create collision objects without customer/order PII."""
    with engine.begin() as conn:
        for stmt in _DRIFT_STATEMENTS:
            conn.execute(text(stmt))


def assert_schema_at_0024(engine: Engine) -> None:
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


def assert_revision(engine: Engine, revision: str) -> None:
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert current == revision


@pytest.fixture()
def ephemeral_legacy_migration_engine() -> Iterator[Engine]:
    admin_engine = connect_engine()
    db_name, _ = create_ephemeral_database(admin_engine)
    test_engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(test_engine, BASE_REVISION)
        yield test_engine
    finally:
        test_engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


__all__ = [
    "BASE_REVISION",
    "TARGET_REVISION",
    "alembic_config",
    "assert_revision",
    "assert_schema_at_0024",
    "connect_engine",
    "create_ephemeral_database",
    "downgrade_alembic",
    "drop_ephemeral_database",
    "ephemeral_legacy_migration_engine",
    "run_alembic",
    "seed_create_all_safe_alter_drift",
]
