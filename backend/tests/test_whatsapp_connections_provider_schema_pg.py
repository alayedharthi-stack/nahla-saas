"""PostgreSQL migration/integration tests for whatsapp_connections.provider."""
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
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from core.native_catalog_capability import load_whatsapp_connection  # noqa: E402
from database.models import Tenant, WhatsAppConnection  # noqa: E402
from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_META  # noqa: E402
from tests.order_customer_identity_postgres_fixtures import _connect_engine  # noqa: E402


def _alembic_config(engine: Engine) -> Config:
    cfg = Config(str(_DATABASE / "alembic.ini"))
    url = str(engine.url.render_as_string(hide_password=False))
    cfg.set_main_option("script_location", str(_DATABASE / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["DATABASE_URL"] = url
    return cfg


def _upgrade(engine: Engine, revision: str) -> None:
    command.upgrade(_alembic_config(engine), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    command.downgrade(_alembic_config(engine), revision)


def _create_ephemeral_database(admin_engine: Engine) -> tuple[str, Engine]:
    db_name = f"wa_provider_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    return db_name, engine


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


def _provider_column_state(engine: Engine) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'whatsapp_connections'
                  AND column_name = 'provider'
                """
            )
        ).mappings().first()
    return dict(row or {})


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Engine]:
    admin_engine = _connect_engine()
    db_name, engine = _create_ephemeral_database(admin_engine)
    try:
        _upgrade(engine, "0093")
        yield engine
    finally:
        engine.dispose()
        _drop_ephemeral_database(admin_engine, db_name)
        admin_engine.dispose()


def test_migration_0093_adds_provider_column(pg_engine: Engine) -> None:
    state = _provider_column_state(pg_engine)
    assert state.get("is_nullable") == "NO"
    assert "meta" in str(state.get("column_default") or "")


def test_migration_0092_sibling_branch_adds_provider(pg_engine: Engine) -> None:
    _upgrade(pg_engine, "0092")
    state = _provider_column_state(pg_engine)
    assert state.get("is_nullable") == "NO"
    assert "meta" in str(state.get("column_default") or "")


def test_orm_lookup_succeeds_on_empty_whatsapp_connections_table(pg_engine: Engine) -> None:
    Session = sessionmaker(bind=pg_engine)
    operational = Session()
    try:
        assert load_whatsapp_connection(operational, 991_502) is None
        operational.execute(
            text("SELECT id FROM tenants LIMIT 1")
        )
        operational.flush()
    finally:
        operational.close()


def test_orm_lookup_returns_row_after_insert(pg_engine: Engine) -> None:
    Session = sessionmaker(bind=pg_engine)
    operational = Session()
    try:
        tenant = Tenant(id=991_503, name="متجر تجريبي عام", is_active=True)
        operational.merge(tenant)
        operational.flush()
        operational.add(
            WhatsAppConnection(
                tenant_id=991_503,
                status="connected",
                provider=WHATSAPP_PROVIDER_META,
                sending_enabled=True,
                phone_number_id="pn-991503",
                catalog_enabled=True,
                meta_catalog_id="CAT-991503",
            )
        )
        operational.flush()

        conn = load_whatsapp_connection(operational, 991_503)
        assert conn is not None
        assert conn.provider == WHATSAPP_PROVIDER_META
    finally:
        operational.rollback()
        operational.close()


def test_existing_compatible_provider_column_is_idempotent(pg_engine: Engine) -> None:
    _upgrade(pg_engine, "0093")
    before = _provider_column_state(pg_engine)
    _upgrade(pg_engine, "0093")
    after = _provider_column_state(pg_engine)
    assert before == after


def test_downgrading_one_sibling_keeps_provider_column(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        tenant_id = conn.execute(
            text(
                """
                INSERT INTO tenants (name, is_active)
                VALUES ('متجر تجريبي عام', true)
                RETURNING id
                """
            )
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO whatsapp_connections (
                    tenant_id, status, provider, sending_enabled, phone_number_id
                ) VALUES (
                    :tenant_id, 'connected', 'meta', true, 'pn-downgrade'
                )
                """
            ),
            {"tenant_id": tenant_id},
        )

    _downgrade(pg_engine, "0092-1")

    with pg_engine.connect() as conn:
        revisions = {
            row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))
        }
        provider = conn.execute(
            text(
                "SELECT provider FROM whatsapp_connections WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
        state = _provider_column_state(pg_engine)

    assert revisions == {"0088", "0093"}
    assert provider == WHATSAPP_PROVIDER_META
    assert state.get("is_nullable") == "NO"
