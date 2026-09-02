"""Durable WhatsApp OAuth nonce: hash-only, atomic consume, migration 0103."""
from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_DATABASE = _REPO / "database"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from core.whatsapp_oauth_nonce import (  # noqa: E402
    NonceRejected,
    NonceStorageUnavailable,
    consume_oauth_nonce,
    expiry_from_now,
    fingerprint_redirect_uri,
    generate_oauth_nonce,
    hash_oauth_nonce,
    nonce_is_consumed,
    persist_oauth_nonce,
)
from database.models import Base, Tenant, WhatsAppOAuthNonce  # noqa: E402
from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    APPLICATION_ALEMBIC_HEAD,
    INTEGRATION_BOOTSTRAP_TARGET,
    REPOSITORY_ALEMBIC_HEADS,
)
from tests.legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    downgrade_alembic,
    drop_ephemeral_database,
    run_alembic,
)

_REDIRECT = "https://api.example.test/whatsapp/embedded/oauth/callback"
_PARENT = "0102"
_REVISION = "0103"


def test_0103_extends_0102_without_merging_0092() -> None:
    from alembic.script import ScriptDirectory

    source = (_DATABASE / "migrations" / "versions" / "0103_whatsapp_oauth_nonces.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision = "0102"' in source
    assert "down_revision = (\"0092\"" not in source
    assert "down_revision = ('0092'" not in source
    assert APPLICATION_ALEMBIC_HEAD == _REVISION
    assert REPOSITORY_ALEMBIC_HEADS == frozenset({"0092", _REVISION})
    assert INTEGRATION_BOOTSTRAP_TARGET == "0093"
    prev = os.getcwd()
    try:
        os.chdir(_DATABASE)
        heads = set(ScriptDirectory("migrations").get_heads())
        rev = ScriptDirectory("migrations").get_revision(_REVISION)
    finally:
        os.chdir(prev)
    assert heads == frozenset({"0092", _REVISION})
    assert rev.down_revision == _PARENT
    assert not isinstance(rev.down_revision, tuple)


def _sqlite_engine() -> Engine:
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, WhatsAppOAuthNonce.__table__],
    )
    for col, orig in saved:
        col.type = orig
    return engine


@pytest.fixture()
def sqlite_nonce_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Session, int]]:
    engine = _sqlite_engine()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(
        "core.whatsapp_oauth_nonce._independent_session",
        lambda: factory(),
    )
    db = factory()
    tenant = Tenant(name="oauth-nonce-shop", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    try:
        yield db, int(tenant.id)
    finally:
        db.close()
        engine.dispose()


def _persist(db: Session, tenant_id: int, nonce: str, *, mode: str = "embedded", uri: str = _REDIRECT) -> None:
    persist_oauth_nonce(
        db,
        nonce=nonce,
        tenant_id=tenant_id,
        connection_mode=mode,
        redirect_uri=uri,
        expires_at=expiry_from_now(),
    )
    db.commit()


def test_nonce_stored_as_hash_only(sqlite_nonce_db: tuple[Session, int]) -> None:
    db, tenant_id = sqlite_nonce_db
    nonce = generate_oauth_nonce()
    _persist(db, tenant_id, nonce)
    row = db.execute(text("SELECT * FROM whatsapp_oauth_nonces")).mappings().one()
    blob = " ".join(str(v) for v in row.values())
    assert nonce not in blob
    assert row["nonce_hash"] == hash_oauth_nonce(nonce)
    assert row["tenant_id"] == tenant_id
    assert row["connection_mode"] == "embedded"
    assert row["redirect_uri_fingerprint"] == fingerprint_redirect_uri(_REDIRECT)
    assert row["consumed_at"] is None
    assert str(_REDIRECT) not in blob


def test_valid_nonce_consumed_once_then_sequential_replay_rejected(
    sqlite_nonce_db: tuple[Session, int],
) -> None:
    db, tenant_id = sqlite_nonce_db
    nonce = generate_oauth_nonce()
    _persist(db, tenant_id, nonce)
    first = consume_oauth_nonce(
        nonce=nonce,
        tenant_id=tenant_id,
        connection_mode="embedded",
        redirect_uri=_REDIRECT,
    )
    assert first > 0
    assert nonce_is_consumed(nonce_hash=hash_oauth_nonce(nonce), db=db)
    with pytest.raises(NonceRejected):
        consume_oauth_nonce(
            nonce=nonce,
            tenant_id=tenant_id,
            connection_mode="embedded",
            redirect_uri=_REDIRECT,
        )
    assert nonce_is_consumed(nonce_hash=hash_oauth_nonce(nonce), db=db)


def test_tenant_mode_and_redirect_mismatch_rejected(sqlite_nonce_db: tuple[Session, int]) -> None:
    db, tenant_id = sqlite_nonce_db
    other = Tenant(name="oauth-nonce-other", is_active=True)
    db.add(other)
    db.commit()
    db.refresh(other)
    nonce = generate_oauth_nonce()
    _persist(db, tenant_id, nonce)
    with pytest.raises(NonceRejected):
        consume_oauth_nonce(
            nonce=nonce,
            tenant_id=int(other.id),
            connection_mode="embedded",
            redirect_uri=_REDIRECT,
        )
    with pytest.raises(NonceRejected):
        consume_oauth_nonce(
            nonce=nonce,
            tenant_id=tenant_id,
            connection_mode="coexistence",
            redirect_uri=_REDIRECT,
        )
    with pytest.raises(NonceRejected):
        consume_oauth_nonce(
            nonce=nonce,
            tenant_id=tenant_id,
            connection_mode="embedded",
            redirect_uri="https://evil.example/cb",
        )
    consume_oauth_nonce(
        nonce=nonce,
        tenant_id=tenant_id,
        connection_mode="embedded",
        redirect_uri=_REDIRECT,
    )


def test_downstream_graph_failure_does_not_resurrect_nonce(
    sqlite_nonce_db: tuple[Session, int],
) -> None:
    from routers.whatsapp_embedded import _sign_oauth_state, oauth_callback

    db, tenant_id = sqlite_nonce_db
    nonce = generate_oauth_nonce()
    _persist(db, tenant_id, nonce)
    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(tenant_id, nonce, issued_at, _REDIRECT, "embedded")
    with patch(
        "routers.whatsapp_embedded._exchange_code_for_token",
        side_effect=HTTPException(status_code=400, detail="graph_down"),
    ):
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/whatsapp/embedded/oauth/callback",
                "headers": [],
                "query_string": b"",
            }
        )
        response = asyncio.run(
            oauth_callback(request, db=db, code="oauth-code-SECRET", state=state)
        )
    assert response.status_code == 302
    assert nonce_is_consumed(nonce_hash=hash_oauth_nonce(nonce), db=db)
    with pytest.raises(NonceRejected):
        consume_oauth_nonce(
            nonce=nonce,
            tenant_id=tenant_id,
            connection_mode="embedded",
            redirect_uri=_REDIRECT,
        )


def test_fail_closed_without_schema() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = factory()
    try:
        with pytest.raises(NonceStorageUnavailable):
            persist_oauth_nonce(
                db,
                nonce="n1",
                tenant_id=1,
                connection_mode="embedded",
                redirect_uri=_REDIRECT,
                expires_at=expiry_from_now(),
            )
    finally:
        db.close()
        engine.dispose()

    with patch("core.whatsapp_oauth_nonce._independent_session", factory):
        with pytest.raises(NonceStorageUnavailable):
            consume_oauth_nonce(
                nonce="n1",
                tenant_id=1,
                connection_mode="embedded",
                redirect_uri=_REDIRECT,
            )


def test_fail_closed_when_hmac_key_missing(sqlite_nonce_db: tuple[Session, int]) -> None:
    db, tenant_id = sqlite_nonce_db
    with patch("core.config.JWT_SECRET", ""):
        with pytest.raises(NonceStorageUnavailable):
            persist_oauth_nonce(
                db,
                nonce="n-missing-key",
                tenant_id=tenant_id,
                connection_mode="embedded",
                redirect_uri=_REDIRECT,
                expires_at=expiry_from_now(),
            )


def test_expired_row_not_consumable(sqlite_nonce_db: tuple[Session, int]) -> None:
    db, tenant_id = sqlite_nonce_db
    nonce = generate_oauth_nonce()
    persist_oauth_nonce(
        db,
        nonce=nonce,
        tenant_id=tenant_id,
        connection_mode="embedded",
        redirect_uri=_REDIRECT,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    db.commit()
    with pytest.raises(NonceRejected):
        consume_oauth_nonce(
            nonce=nonce,
            tenant_id=tenant_id,
            connection_mode="embedded",
            redirect_uri=_REDIRECT,
        )


def _pg_admin() -> Engine:
    return connect_engine()


def test_postgres_concurrent_replay_succeeds_once() -> None:
    admin = _pg_admin()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(engine, _REVISION)
        with engine.begin() as conn:
            tenant_id = int(
                conn.execute(
                    text(
                        "INSERT INTO tenants (name, is_active, is_platform_tenant) "
                        "VALUES (:name, true, false) RETURNING id"
                    ),
                    {"name": f"oauth-nonce-{os.urandom(4).hex()}"},
                ).scalar_one()
            )
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        nonce = generate_oauth_nonce()
        db = factory()
        try:
            persist_oauth_nonce(
                db,
                nonce=nonce,
                tenant_id=tenant_id,
                connection_mode="embedded",
                redirect_uri=_REDIRECT,
                expires_at=expiry_from_now(),
            )
            db.commit()
        finally:
            db.close()

        def _attempt() -> str:
            try:
                consume_oauth_nonce(
                    nonce=nonce,
                    tenant_id=tenant_id,
                    connection_mode="embedded",
                    redirect_uri=_REDIRECT,
                )
                return "ok"
            except NonceRejected:
                return "rejected"

        with patch("core.whatsapp_oauth_nonce._independent_session", factory):
            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(lambda _: _attempt(), range(8)))
        assert outcomes.count("ok") == 1
        assert outcomes.count("rejected") == 7
        check = factory()
        try:
            assert nonce_is_consumed(nonce_hash=hash_oauth_nonce(nonce), db=check)
        finally:
            check.close()
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()


def test_migration_0102_to_0103_upgrade_and_downgrade() -> None:
    admin = _pg_admin()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(engine, _PARENT)
        tables = set(inspect(engine).get_table_names())
        assert "whatsapp_oauth_nonces" not in tables
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _PARENT
        run_alembic(engine, _REVISION)
        insp = inspect(engine)
        assert "whatsapp_oauth_nonces" in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns("whatsapp_oauth_nonces")}
        assert {
            "id",
            "nonce_hash",
            "tenant_id",
            "connection_mode",
            "redirect_uri_fingerprint",
            "expires_at",
            "consumed_at",
            "created_at",
        } <= cols
        indexes = {i.get("name") for i in insp.get_indexes("whatsapp_oauth_nonces")}
        assert "ix_whatsapp_oauth_nonces_expires_at" in indexes
        assert "ix_whatsapp_oauth_nonces_tenant_id" in indexes
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _REVISION
            heads = {
                str(row[0])
                for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            }
        assert "0092" not in heads
        downgrade_alembic(engine, _PARENT)
        assert "whatsapp_oauth_nonces" not in set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _PARENT
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()
