"""PostgreSQL advisory-lock race tests for the Salla coupons poller."""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import Integration, Tenant
from services.salla_coupons_poller import _run_one_tick
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT_POLLER = 991_201

if not _integration_required():
    pytest.skip(
        "PostgreSQL integration tests require A1_PG_INTEGRATION_REQUIRED=1",
        allow_module_level=True,
    )

pytestmark = pytest.mark.usefixtures("postgres_engine")


@pytest.fixture(scope="module")
def postgres_engine():
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


def _new_session(engine):
    connection = engine.connect()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    return session, connection


def _seed_minimal_integration(engine) -> int:
    session, connection = _new_session(engine)
    try:
        if session.get(Tenant, TEST_TENANT_POLLER) is None:
            session.add(Tenant(id=TEST_TENANT_POLLER, name="Poller Race Tenant"))
        existing = (
            session.query(Integration)
            .filter_by(tenant_id=TEST_TENANT_POLLER, provider="salla")
            .first()
        )
        if existing is None:
            existing = Integration(
                tenant_id=TEST_TENANT_POLLER,
                provider="salla",
                enabled=True,
                external_store_id="race-store",
                config={
                    "api_key": "token",
                    "store_id": "race-store",
                    "api_sync_enabled": True,
                },
            )
            session.add(existing)
            session.commit()
        else:
            session.commit()
        return int(existing.id)
    finally:
        session.close()
        connection.close()


def test_poller_tick_skips_when_advisory_lock_held(postgres_engine, monkeypatch) -> None:
    """Two ticks race; only the holder scans tenants."""
    integration_id = _seed_minimal_integration(postgres_engine)

    poll_calls: list[int] = []
    a_inside = threading.Event()
    b_may_run = threading.Event()

    async def _slow_poll(db, intg):
        poll_calls.append(int(intg.id))
        a_inside.set()
        b_may_run.wait(timeout=30)
        return {
            "items_seen": 0,
            "created": 0,
            "updated": 0,
            "upserted": 0,
        }

    monkeypatch.setattr("services.salla_coupons_poller._poll_integration", _slow_poll)

    thread_b_result: dict = {}

    def _session_factory(engine):
        holder: dict = {}

        def _session_local():
            if "session" not in holder:
                session, connection = _new_session(engine)
                holder["session"] = session
                holder["connection"] = connection
            return holder["session"]

        def _cleanup() -> None:
            conn = holder.get("connection")
            if conn is not None:
                conn.close()

        _session_local.cleanup = _cleanup  # type: ignore[attr-defined]
        return _session_local

    def _thread_a() -> None:
        session_local = _session_factory(postgres_engine)
        with patch("core.database.SessionLocal", session_local):
            asyncio.run(_run_one_tick())
            session_local.cleanup()

    def _thread_b() -> None:
        a_inside.wait(timeout=30)
        b_may_run.set()
        session_local = _session_factory(postgres_engine)
        with patch("core.database.SessionLocal", session_local):
            thread_b_result["payload"] = asyncio.run(_run_one_tick())
            session_local.cleanup()

    t_a = threading.Thread(target=_thread_a)
    t_b = threading.Thread(target=_thread_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=60)
    t_b.join(timeout=60)

    payload = thread_b_result.get("payload") or {}
    assert payload.get("skipped") is True
    assert payload.get("reason") == "advisory_lock_held_by_other_worker"
    assert poll_calls == [integration_id]
