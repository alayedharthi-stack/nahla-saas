"""PostgreSQL advisory-lock and reconciler lifecycle tests for Salla commerce reconciler."""
from __future__ import annotations

import asyncio
import logging
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
from services.salla_commerce_reconciler import ADVISORY_LOCK_KEY, _run_one_tick
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT_RECONCILER = 991_202

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


def _seed_integration(engine) -> int:
    session, connection = _new_session(engine)
    try:
        if session.get(Tenant, TEST_TENANT_RECONCILER) is None:
            session.add(Tenant(id=TEST_TENANT_RECONCILER, name="Commerce Reconciler Tenant"))
        existing = (
            session.query(Integration)
            .filter_by(tenant_id=TEST_TENANT_RECONCILER, provider="salla")
            .first()
        )
        if existing is None:
            existing = Integration(
                tenant_id=TEST_TENANT_RECONCILER,
                provider="salla",
                enabled=True,
                external_store_id="reconcile-store",
                config={"api_key": "token", "store_id": "reconcile-store"},
            )
            session.add(existing)
        session.commit()
        return int(existing.id)
    finally:
        session.close()
        connection.close()


def test_reconciler_tick_skips_when_advisory_lock_held(postgres_engine, monkeypatch) -> None:
    _seed_integration(postgres_engine)
    reconcile_calls: list[int] = []

    async def _reconcile_guard(db, intg):
        reconcile_calls.append(int(intg.tenant_id))
        return {"customers_synced": 0, "products_synced": 0, "duration_ms": 0}

    monkeypatch.setattr("services.salla_commerce_reconciler._reconcile_integration", _reconcile_guard)

    from core.pg_advisory_lock import DedicatedAdvisoryLock

    a_holds = threading.Event()
    b_done = threading.Event()
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
        hold_session, hold_conn = _new_session(postgres_engine)
        lock = DedicatedAdvisoryLock(hold_session, key=ADVISORY_LOCK_KEY)
        try:
            assert lock.try_acquire() is True
            a_holds.set()
            b_done.wait(timeout=30)
        finally:
            if lock.held:
                lock.release()
            hold_session.close()
            hold_conn.close()

    def _thread_b() -> None:
        a_holds.wait(timeout=30)
        session_local = _session_factory(postgres_engine)
        with patch("core.database.SessionLocal", session_local):
            thread_b_result["payload"] = asyncio.run(_run_one_tick())
            session_local.cleanup()
        b_done.set()

    t_a = threading.Thread(target=_thread_a)
    t_b = threading.Thread(target=_thread_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=60)
    t_b.join(timeout=60)

    payload = thread_b_result.get("payload") or {}
    assert payload.get("skipped") is True
    assert payload.get("reason") == "advisory_lock_held_by_other_worker"
    assert reconcile_calls == []


def test_lock_unavailable_skips_without_reconcile(postgres_engine, monkeypatch) -> None:
    _seed_integration(postgres_engine)
    reconcile_calls: list[int] = []

    async def _reconcile_guard(db, intg):
        reconcile_calls.append(1)
        return {"customers_synced": 0, "products_synced": 0, "duration_ms": 0}

    monkeypatch.setattr("services.salla_commerce_reconciler._reconcile_integration", _reconcile_guard)

    class _BrokenLock:
        held = False

        def try_acquire(self):
            raise RuntimeError("lock backend unavailable")

        def release(self):
            return True

    monkeypatch.setattr(
        "services.salla_commerce_reconciler.DedicatedAdvisoryLock",
        lambda db, key: _BrokenLock(),
    )

    payload = asyncio.run(_run_one_tick())
    assert payload.get("skipped") is True
    assert payload.get("reason") == "advisory_lock_unavailable"
    assert reconcile_calls == []


def test_tenant_failure_isolation_and_safe_error_code(postgres_engine, monkeypatch) -> None:
    _seed_integration(postgres_engine)
    session, connection = _new_session(postgres_engine)
    try:
        second = Integration(
            tenant_id=TEST_TENANT_RECONCILER + 1,
            provider="salla",
            enabled=True,
            external_store_id="reconcile-store-b",
            config={"api_key": "token-b", "store_id": "reconcile-store-b"},
        )
        if session.get(Tenant, TEST_TENANT_RECONCILER + 1) is None:
            session.add(Tenant(id=TEST_TENANT_RECONCILER + 1, name="Commerce Reconciler Tenant B"))
        session.add(second)
        session.commit()
    finally:
        session.close()
        connection.close()

    calls: list[int] = []

    async def _reconcile_guard(db, intg):
        calls.append(int(intg.tenant_id))
        if int(intg.tenant_id) == TEST_TENANT_RECONCILER:
            raise RuntimeError("boom tenant a")
        return {"customers_synced": 1, "products_synced": 1, "duration_ms": 1}

    monkeypatch.setattr("services.salla_commerce_reconciler._reconcile_integration", _reconcile_guard)
    payload = asyncio.run(_run_one_tick())
    assert payload.get("errors") == 1
    from services.salla_commerce_reconciler import get_reconciler_state

    state = get_reconciler_state()
    assert state["tenants"][TEST_TENANT_RECONCILER]["error_code"] == "RuntimeError"
    assert "boom" not in str(state)
    assert TEST_TENANT_RECONCILER + 1 in calls


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_reconciler_logs_safe_error_class_only(postgres_engine, monkeypatch) -> None:
    _seed_integration(postgres_engine)

    async def _reconcile_fail(db, intg):
        raise ValueError("token=super-secret-phone=+966500000000")

    monkeypatch.setattr("services.salla_commerce_reconciler._reconcile_integration", _reconcile_fail)

    logger = logging.getLogger("nahla.salla_commerce_reconciler")
    old_level = logger.level
    old_disabled = logging.root.manager.disable
    handler = _RecordingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logging.disable(logging.NOTSET)
    try:
        asyncio.run(_run_one_tick())
        formatted = "\n".join(handler.format(r) for r in handler.records)
        assert "tenant_reconcile_failed" in formatted or "error_class=" in formatted
        assert "super-secret" not in formatted
        assert "+966500000000" not in formatted
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logging.disable(old_disabled)
