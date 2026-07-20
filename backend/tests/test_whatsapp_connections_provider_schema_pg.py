"""PostgreSQL migration/integration tests for whatsapp_connections.provider."""
from __future__ import annotations

import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from core.native_catalog_capability import load_whatsapp_connection  # noqa: E402
from database.models import Tenant, WhatsAppConnection  # noqa: E402
from services.whatsapp_platform.provider_utils import (  # noqa: E402
    WHATSAPP_PROVIDER_META,
)
from tests.order_customer_identity_postgres_fixtures import (  # noqa: E402
    _connect_engine,
)


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


@contextmanager
def _database_at(admin_engine: Engine, revision: str) -> Iterator[Engine]:
    db_name, engine = _create_ephemeral_database(admin_engine)
    try:
        _upgrade(engine, revision)
        yield engine
    finally:
        engine.dispose()
        _drop_ephemeral_database(admin_engine, db_name)


@pytest.fixture(scope="module")
def admin_engine() -> Iterator[Engine]:
    engine = _connect_engine()
    try:
        yield engine
    finally:
        engine.dispose()


def _provider_column_state(engine: Engine) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT data_type, character_maximum_length, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'whatsapp_connections'
                  AND column_name = 'provider'
                """
            )
        ).mappings().first()
    return dict(row or {})


def _assert_provider_contract(engine: Engine) -> None:
    state = _provider_column_state(engine)
    assert state["data_type"] == "character varying"
    assert state["character_maximum_length"] is None
    assert state["is_nullable"] == "NO"
    assert "meta" in str(state["column_default"])


@pytest.mark.parametrize("head", ("0092", "0093"))
def test_each_sibling_branch_adds_provider_from_clean_schema(
    admin_engine: Engine,
    head: str,
) -> None:
    with _database_at(admin_engine, head) as engine:
        _assert_provider_contract(engine)
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT provider FROM whatsapp_connections LIMIT 0")
            ).fetchall() == []


def test_orm_lookup_succeeds_on_empty_whatsapp_connections_table(
    admin_engine: Engine,
) -> None:
    with _database_at(admin_engine, "0093") as engine:
        Session = sessionmaker(bind=engine)
        operational = Session()
        try:
            assert load_whatsapp_connection(operational, 991_502) is None
            operational.execute(text("SELECT id FROM tenants LIMIT 1"))
            operational.flush()
        finally:
            operational.close()


def test_real_missing_column_failure_leaves_caller_transaction_usable(
    admin_engine: Engine,
) -> None:
    with _database_at(admin_engine, "0091") as engine:
        Session = sessionmaker(bind=engine)
        operational = Session()
        try:
            # Start the caller's transaction before the isolated ORM lookup
            # raises PostgreSQL 42703 on the missing provider column.
            assert operational.execute(text("SELECT 1")).scalar_one() == 1
            assert load_whatsapp_connection(operational, 991_504) is None

            # The caller connection was never rolled back or poisoned.
            assert operational.execute(
                text("SELECT COUNT(*) FROM tenant_settings")
            ).scalar_one() >= 0
            operational.flush()
        finally:
            operational.close()


def test_orm_lookup_reads_committed_configuration(admin_engine: Engine) -> None:
    """The isolated optional lookup reads committed config, not caller writes."""
    with _database_at(admin_engine, "0093") as engine:
        Session = sessionmaker(bind=engine)
        writer = Session()
        operational = Session()
        try:
            writer.add(Tenant(id=991_503, name="متجر تجريبي عام", is_active=True))
            writer.add(
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
            writer.commit()

            connection = load_whatsapp_connection(operational, 991_503)
            assert connection is not None
            assert connection.provider == WHATSAPP_PROVIDER_META
        finally:
            writer.close()
            operational.close()
            with engine.begin() as cleanup:
                cleanup.execute(
                    text("DELETE FROM whatsapp_connections WHERE tenant_id = 991503")
                )
                cleanup.execute(text("DELETE FROM tenants WHERE id = 991503"))


def test_nullable_existing_column_backfills_and_enforces_contract(
    admin_engine: Engine,
) -> None:
    with _database_at(admin_engine, "0091") as engine:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE whatsapp_connections ADD COLUMN provider TEXT"))
            tenant_ids = [
                conn.execute(
                    text(
                        "INSERT INTO tenants (name, is_active) "
                        "VALUES (:name, true) RETURNING id"
                    ),
                    {"name": f"متجر تجريبي عام {suffix}"},
                ).scalar_one()
                for suffix in ("أ", "ب")
            ]
            conn.execute(
                text(
                    "INSERT INTO whatsapp_connections (tenant_id, status, provider) "
                    "VALUES (:first, 'connected', NULL), (:second, 'connected', '')"
                ),
                {"first": tenant_ids[0], "second": tenant_ids[1]},
            )

        _upgrade(engine, "0093")

        _assert_provider_contract(engine)
        with engine.connect() as conn:
            values = conn.execute(
                text(
                    "SELECT provider FROM whatsapp_connections "
                    "WHERE tenant_id = ANY(:tenant_ids) ORDER BY tenant_id"
                ),
                {"tenant_ids": tenant_ids},
            ).scalars().all()
        assert values == [WHATSAPP_PROVIDER_META, WHATSAPP_PROVIDER_META]


@pytest.mark.parametrize("existing_default", (None, "dialog360"))
def test_non_null_column_default_is_reconciled_independently(
    admin_engine: Engine,
    existing_default: str | None,
) -> None:
    with _database_at(admin_engine, "0091") as engine:
        default_ddl = (
            ""
            if existing_default is None
            else f" DEFAULT '{existing_default}'"
        )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE whatsapp_connections "
                    f"ADD COLUMN provider VARCHAR NOT NULL{default_ddl}"
                )
            )
            tenant_id = None
            if existing_default is not None:
                tenant_id = conn.execute(
                    text(
                        "INSERT INTO tenants (name, is_active) "
                        "VALUES ('متجر تجريبي عام', true) RETURNING id"
                    )
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO whatsapp_connections "
                        "(tenant_id, status, provider) "
                        "VALUES (:tenant_id, 'connected', :provider)"
                    ),
                    {"tenant_id": tenant_id, "provider": existing_default},
                )

        _upgrade(engine, "0093")

        _assert_provider_contract(engine)
        if tenant_id is not None:
            with engine.connect() as conn:
                assert conn.execute(
                    text(
                        "SELECT provider FROM whatsapp_connections "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                ).scalar_one() == existing_default


def test_incompatible_existing_column_fails_closed(admin_engine: Engine) -> None:
    with _database_at(admin_engine, "0091") as engine:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE whatsapp_connections "
                    "ADD COLUMN provider INTEGER NOT NULL DEFAULT 7"
                )
            )

        with pytest.raises(RuntimeError, match="incompatible_existing_column"):
            _upgrade(engine, "0093")

        with engine.connect() as conn:
            revisions = {
                row[0]
                for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            }
        assert revisions == {"0091"}


def test_missing_base_table_fails_without_stamping_revision(
    admin_engine: Engine,
) -> None:
    with _database_at(admin_engine, "0091") as engine:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE whatsapp_connections"))

        with pytest.raises(RuntimeError, match="required_table_missing"):
            _upgrade(engine, "0093")

        with engine.connect() as conn:
            revisions = {
                row[0]
                for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            }
        assert revisions == {"0091"}


def test_second_upgrade_is_idempotent(admin_engine: Engine) -> None:
    with _database_at(admin_engine, "0093") as engine:
        before = _provider_column_state(engine)
        _downgrade(engine, "0091")
        _upgrade(engine, "0093")
        after = _provider_column_state(engine)
        assert before == after


def test_downgrading_one_sibling_keeps_provider_column(
    admin_engine: Engine,
) -> None:
    with _database_at(admin_engine, "0093") as engine:
        _upgrade(engine, "0092")
        with engine.begin() as conn:
            tenant_id = conn.execute(
                text(
                    "INSERT INTO tenants (name, is_active) "
                    "VALUES ('متجر تجريبي عام', true) RETURNING id"
                )
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO whatsapp_connections "
                    "(tenant_id, status, provider, sending_enabled, phone_number_id) "
                    "VALUES (:tenant_id, 'connected', 'meta', true, 'pn-downgrade')"
                ),
                {"tenant_id": tenant_id},
            )

        _downgrade(engine, "0092-1")

        with engine.connect() as conn:
            revisions = {
                row[0]
                for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            }
            provider = conn.execute(
                text(
                    "SELECT provider FROM whatsapp_connections "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            ).scalar_one()

        assert revisions == {"0090", "0093"}
        assert provider == WHATSAPP_PROVIDER_META
        _assert_provider_contract(engine)
