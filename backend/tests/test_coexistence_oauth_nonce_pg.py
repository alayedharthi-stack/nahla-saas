"""PostgreSQL integration tests for WhatsApp OAuth nonce migration and coexistence security."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from database.models import Tenant, WhatsAppConnection, WhatsAppOAuthNonce  # noqa: E402
from services.coexistence_embedded_exchange import (  # noqa: E402
    COEXISTENCE_TENANT_LOCK_CLASS,
    acquire_tenant_transaction_lock,
    consume_oauth_nonce,
    hash_oauth_nonce,
    load_connection_for_update,
    persist_oauth_nonce,
)

WABA = "PG-WABA-877"
PHONE = "PG-PHONE-877"
PORTFOLIO = "PG-BUSINESS-877"
E164 = "+966509876543"
SYNTH_TOKEN = "synthetic-user-token-pg-877"
SYNTH_APP = "app-pg-test"
SYNTH_SECRET = "secret-pg-test"


def _candidate_database_urls() -> list[str]:
    urls: list[str] = []
    explicit = (os.getenv("A1_PG_TEST_DATABASE_URL") or "").strip()
    if explicit:
        urls.append(explicit)
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if db_url and db_url not in urls:
        urls.append(db_url)
    default = "postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas"
    if default not in urls:
        urls.append(default)
    return urls


def _integration_required() -> bool:
    return (os.getenv("A1_PG_INTEGRATION_REQUIRED") or "").strip() == "1"


def _connect_engine() -> Engine:
    last_error: Exception | None = None
    for url in _candidate_database_urls():
        try:
            engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    message = f"PostgreSQL unavailable for coexistence OAuth PG tests: {last_error}"
    if _integration_required():
        pytest.fail(message)
    pytest.skip(message)


def _run_alembic(engine: Engine, revision: str) -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        cfg = Config("alembic.ini")
        url = engine.url.render_as_string(hide_password=False)
        cfg.set_main_option("sqlalchemy.url", url)
        os.environ["DATABASE_URL"] = url
        command.upgrade(cfg, revision)
    finally:
        os.chdir(prev_cwd)


def _assert_nonce_table(engine: Engine) -> None:
    insp = inspect(engine)
    assert "whatsapp_oauth_nonces" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("whatsapp_oauth_nonces")}
    assert {"nonce_hash", "tenant_id", "connection_mode", "expires_at", "consumed_at"} <= cols


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    engine = _connect_engine()
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_session(postgres_engine: Engine) -> Iterator[Session]:
    connection = postgres_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def _admin_engine() -> Engine:
    base = (_candidate_database_urls()[0]).rsplit("/", 1)[0]
    return create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _fresh_db_url(db_name: str) -> str:
    return f"{_candidate_database_urls()[0].rsplit('/', 1)[0]}/{db_name}"


def test_fresh_database_upgrade_to_0101_creates_nonce_table(postgres_engine: Engine) -> None:
    admin = _admin_engine()
    db_name = f"coex_nonce_fresh_{hashlib.sha256(os.urandom(8)).hexdigest()[:8]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    fresh = create_engine(_fresh_db_url(db_name), poolclass=NullPool)
    try:
        _run_alembic(fresh, "0101")
        _assert_nonce_table(fresh)
        with fresh.connect() as conn:
            revs = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
        assert "0101" in revs
    finally:
        fresh.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{db_name}" WITH (FORCE)'))
    admin.dispose()


def test_existing_salla_0100_upgrade_to_0101_creates_nonce_table(postgres_engine: Engine) -> None:
    admin = _admin_engine()
    db_name = f"coex_nonce_salla_{hashlib.sha256(os.urandom(8)).hexdigest()[:8]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    fresh = create_engine(_fresh_db_url(db_name), poolclass=NullPool)
    try:
        _run_alembic(fresh, "0100")
        with fresh.connect() as conn:
            assert "salla_embedded_identity_bindings" in inspect(fresh).get_table_names()
            assert "whatsapp_oauth_nonces" not in inspect(fresh).get_table_names()
        _run_alembic(fresh, "0101")
        _assert_nonce_table(fresh)
    finally:
        fresh.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{db_name}" WITH (FORCE)'))
    admin.dispose()


def test_pg_advisory_xact_lock_serializes(postgres_engine: Engine, pg_session: Session) -> None:
    pg_session.add(Tenant(id=990877, name="lock-tenant", is_active=True))
    pg_session.commit()
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = postgres_engine.connect()
        trans = conn.begin()
        local = sessionmaker(bind=conn, expire_on_commit=False)()
        try:
            acquire_tenant_transaction_lock(local, 990877)
            with lock:
                results.append(1)
            local.execute(text("SELECT pg_sleep(0.1)"))
            trans.commit()
        finally:
            local.close()
            conn.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert len(results) == 2


def test_load_connection_for_update_uses_row_lock(pg_session: Session) -> None:
    pg_session.add(Tenant(id=990878, name="for-update", is_active=True))
    pg_session.commit()
    conn, existed = load_connection_for_update(pg_session, 990878)
    assert existed is False
    assert conn.tenant_id == 990878
    pg_session.rollback()


def test_atomic_nonce_consume(pg_session: Session) -> None:
    pg_session.add(Tenant(id=990879, name="nonce", is_active=True))
    pg_session.commit()
    nonce = "pg-nonce-atomic-877"
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    persist_oauth_nonce(pg_session, nonce=nonce, tenant_id=990879, connection_mode="coexistence", expires_at=expires)
    pg_session.commit()
    assert consume_oauth_nonce(pg_session, nonce=nonce, tenant_id=990879, connection_mode="coexistence") == "consumed"
    assert consume_oauth_nonce(pg_session, nonce=nonce, tenant_id=990879, connection_mode="coexistence") == "already_consumed"


def test_replay_rejected_after_consume(pg_session: Session) -> None:
    pg_session.add(Tenant(id=990880, name="replay", is_active=True))
    pg_session.commit()
    nonce = "pg-nonce-replay-877"
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    persist_oauth_nonce(pg_session, nonce=nonce, tenant_id=990880, connection_mode="coexistence", expires_at=expires)
    pg_session.commit()
    first = consume_oauth_nonce(pg_session, nonce=nonce, tenant_id=990880, connection_mode="coexistence")
    pg_session.commit()
    second = consume_oauth_nonce(pg_session, nonce=nonce, tenant_id=990880, connection_mode="coexistence")
    assert first == "consumed"
    assert second == "already_consumed"


def test_new_tenant_finalization_failure_leaves_no_row(pg_session: Session) -> None:
    pg_session.add(Tenant(id=990881, name="new-fail", is_active=True))
    pg_session.commit()
    conn, existed = load_connection_for_update(pg_session, 990881)
    assert existed is False
    pg_session.rollback()
    count = pg_session.query(WhatsAppConnection).filter_by(tenant_id=990881).count()
    assert count == 0


def test_existing_dialog360_snapshot_restored_on_rollback(pg_session: Session) -> None:
    from services.coexistence_embedded_exchange import stage_coexistence_credentials

    pg_session.add(Tenant(id=990882, name="restore", is_active=True))
    connected_at = datetime(2026, 1, 10, tzinfo=timezone.utc)
    conn = WhatsAppConnection(
        tenant_id=990882,
        status="disconnected",
        provider="dialog360",
        phone_number=E164,
        connected_at=connected_at,
        extra_metadata={"legacy": "pg-877"},
    )
    pg_session.add(conn)
    pg_session.commit()
    original = {
        "provider": conn.provider,
        "status": conn.status,
        "phone_number": conn.phone_number,
        "connected_at": conn.connected_at,
        "extra_metadata": dict(conn.extra_metadata or {}),
    }
    loaded, _ = load_connection_for_update(pg_session, 990882)
    stage_coexistence_credentials(loaded, waba_id=WABA, access_token=SYNTH_TOKEN, token_type="user")
    pg_session.flush()
    pg_session.rollback()
    restored = pg_session.query(WhatsAppConnection).filter_by(tenant_id=990882).one()
    for key, value in original.items():
        assert getattr(restored, key) == value


def test_exactly_one_commit_on_success_route(pg_session: Session, monkeypatch) -> None:
    commits: list[str] = []
    original_commit = pg_session.commit

    def tracked_commit() -> None:
        commits.append("commit")
        return original_commit()

    monkeypatch.setattr(pg_session, "commit", tracked_commit)
    pg_session.add(Tenant(id=990883, name="one-commit", is_active=True))
    pg_session.commit()
    nonce = "pg-one-commit-877"
    persist_oauth_nonce(
        pg_session,
        nonce=nonce,
        tenant_id=990883,
        connection_mode="coexistence",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    consume_oauth_nonce(pg_session, nonce=nonce, tenant_id=990883, connection_mode="coexistence")
    pg_session.commit()
    assert commits.count("commit") == 2
