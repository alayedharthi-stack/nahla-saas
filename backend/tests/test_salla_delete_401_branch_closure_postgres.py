"""PostgreSQL proofs for H6-2 branch closure (freshness, lock, superseding lookup).

Only outbound HTTP transport is mocked, except one narrowly scoped database-
boundary fault injection for superseding-integration lookup failure.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import Integration, Tenant
import store_adapters.salla_adapter as salla_adapter_mod
from core.salla_token_lock import salla_asyncio_lock
from store_adapters.salla_adapter import SallaAdapter
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT_ID = 888_777_666
TEST_STORE_ID = "canary-store-id-999888777"
TEST_INTEGRATION_EXTERNAL = "canary-store-id-999888777"

CANARY_TOKEN = "canary-access-token-h6-98765"
CANARY_NEW_ACCESS_TOKEN = "canary-new-access-token-refresh-abc"
CANARY_PROVIDER_ID = "canary-provider-coupon-id-12345"
CANARY_OAUTH_BODY = '{"error":"invalid_grant","error_description":"canary-oauth-response-body-secret"}'
CANARY_INVALID_EXPIRY = "canary-invalid-expiry-@@@NOT-ISO"
CANARY_LOOKUP_FAULT = "canary-superseded-lookup-db-fault-xyz"

CANARIES = (
    CANARY_TOKEN,
    TEST_STORE_ID,
    str(TEST_TENANT_ID),
    CANARY_NEW_ACCESS_TOKEN,
    CANARY_OAUTH_BODY,
    CANARY_PROVIDER_ID,
    CANARY_INVALID_EXPIRY,
    CANARY_LOOKUP_FAULT,
)


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _capture_bounded_logs() -> Iterator[_LogCollector]:
    collector = _LogCollector()
    loggers = [
        salla_adapter_mod.logger,
        logging.getLogger("nahla.adapter.salla"),
        logging.getLogger("nahla.salla_alerts"),
        logging.getLogger("nahla.salla_token_lock"),
    ]
    for lg in loggers:
        lg.addHandler(collector)
        lg.setLevel(logging.DEBUG)
    try:
        from core.salla_token_lock import _locks

        _locks.clear()
    except Exception:  # noqa: silent-ok - lock table may be unavailable during import
        pass
    try:
        yield collector
    finally:
        for lg in loggers:
            lg.removeHandler(collector)


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


def _log_text(collector: _LogCollector) -> str:
    return "\n".join(collector.messages)

def _caplog_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)



def _assert_no_canaries(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text


def _new_session(engine):
    connection = engine.connect()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    return session, connection


def _session_factory(engine):
    class _Factory:
        def __call__(self):
            session, connection = _new_session(engine)
            self._holder = (session, connection)
            return session

        def cleanup(self) -> None:
            holder = getattr(self, "_holder", None)
            if not holder:
                return
            session, connection = holder
            session.close()
            connection.close()
            self._holder = None

    return _Factory()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _seed_integration(
    engine,
    *,
    api_key: str = CANARY_TOKEN,
    refresh_token: str = "refresh-token-safe-original",
    attempts: int = 0,
    expires_at: str | None = None,
    integration_id: int | None = None,
) -> int:
    now = datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = _iso(now + timedelta(days=7))
    session, connection = _new_session(engine)
    try:
        tenant = session.get(Tenant, TEST_TENANT_ID)
        if tenant is None:
            tenant = Tenant(id=TEST_TENANT_ID, name="H6-2 Branch Tenant", is_active=True)
            session.add(tenant)
        cfg: dict[str, Any] = {
            "api_key": api_key,
            "refresh_token": refresh_token,
            "store_id": TEST_STORE_ID,
            "store_name": "H6-2 Store",
            "easy_mode": True,
            "token_source": "easy_mode_webhook",
            "token_refresh_attempts": attempts,
            "expires_at": expires_at,
            "token_expires_at": expires_at,
        }
        if integration_id is not None:
            intg = session.get(Integration, integration_id)
            assert intg is not None
            intg.enabled = True
            intg.config = cfg
        else:
            intg = (
                session.query(Integration)
                .filter_by(tenant_id=TEST_TENANT_ID, provider="salla")
                .first()
            )
            if intg is None:
                intg = Integration(
                    tenant_id=TEST_TENANT_ID,
                    provider="salla",
                    enabled=True,
                    external_store_id=TEST_INTEGRATION_EXTERNAL,
                    config=cfg,
                )
                session.add(intg)
            else:
                intg.enabled = True
                intg.config = cfg
        session.commit()
        return int(intg.id)
    finally:
        session.close()
        connection.close()


def _adapter_for_integration(integration_id: int, engine, *, expires_at: str | None = None) -> SallaAdapter:
    session, connection = _new_session(engine)
    try:
        intg = session.get(Integration, integration_id)
        assert intg is not None
        cfg = dict(intg.config or {})
        exp = expires_at or cfg.get("expires_at") or cfg.get("token_expires_at")
        return SallaAdapter(
            api_key=str(cfg.get("api_key") or ""),
            refresh_token=str(cfg.get("refresh_token") or ""),
            store_id=str(cfg.get("store_id") or TEST_STORE_ID),
            tenant_id=TEST_TENANT_ID,
            integration_id=integration_id,
            expires_at=exp,
        )
    finally:
        session.close()
        connection.close()


def _make_http_response(status: int, *, text: str = "", json_data: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    return resp


class _RoutingHttpClient:
    def __init__(self, delete_handler: Callable, post_handler: Callable):
        self._delete_handler = delete_handler
        self._post_handler = post_handler

    async def delete(self, url, **kwargs):
        return await self._delete_handler(url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._post_handler(url, **kwargs)


class _HttpClientFactory:
    def __init__(self, client: _RoutingHttpClient):
        self._client = client

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _IntegrationQueryProxy:
    def __init__(self, real_query, *, fault_on_all: bool, canary: str):
        self._real_query = real_query
        self._fault_on_all = fault_on_all
        self._canary = canary

    def filter(self, *args, **kwargs):
        self._real_query = self._real_query.filter(*args, **kwargs)
        return self

    def order_by(self, *args, **kwargs):
        self._real_query = self._real_query.order_by(*args, **kwargs)
        return self

    def first(self):
        return self._real_query.first()

    def all(self):
        if self._fault_on_all:
            raise RuntimeError(self._canary)
        return self._real_query.all()


def _session_factory_with_lookup_fault(engine):
    base = _session_factory(engine)
    state = {"integration_query_calls": 0}

    class _FaultFactory:
        def __call__(self):
            session = base()
            original_query = session.query

            def query_wrapper(*entities, **kwargs):
                q = original_query(*entities, **kwargs)
                if entities and entities[0] is Integration:
                    state["integration_query_calls"] += 1
                    if state["integration_query_calls"] >= 2:
                        return _IntegrationQueryProxy(
                            q,
                            fault_on_all=True,
                            canary=CANARY_LOOKUP_FAULT,
                        )
                return q

            session.query = query_wrapper  # type: ignore[method-assign]
            return session

        def cleanup(self) -> None:
            base.cleanup()

        def reset(self) -> None:
            state["integration_query_calls"] = 0

    return _FaultFactory()


def test_proactive_freshness_success_emits_safe_events(postgres_engine, caplog):
    now = datetime.now(timezone.utc)
    expired_at = _iso(now - timedelta(minutes=30))
    integration_id = _seed_integration(
        postgres_engine,
        expires_at=expired_at,
    )
    adapter = _adapter_for_integration(integration_id, postgres_engine)
    adapter._expires_at = expired_at
    post_calls = {"count": 0}

    async def delete_handler(url, **_kwargs):
        return _make_http_response(200)

    async def post_handler(url, **_kwargs):
        post_calls["count"] += 1
        return _make_http_response(
            200,
            json_data={
                "access_token": CANARY_NEW_ACCESS_TOKEN,
                "refresh_token": "rotated-refresh-safe",
                "expires_in": 3600,
            },
        )

    client = _RoutingHttpClient(delete_handler, post_handler)
    factory = _session_factory(postgres_engine)

    async def _run():
        with _capture_bounded_logs() as collector:
            with caplog.at_level(logging.DEBUG):
                with patch.dict(
                    os.environ,
                    {
                        "SALLA_CLIENT_ID": "oauth-client-id",
                        "SALLA_CLIENT_SECRET": "oauth-client-secret",
                    },
                    clear=False,
                ):
                    with patch("database.session.SessionLocal", factory):
                        with patch(
                            "store_adapters.salla_adapter.httpx.AsyncClient",
                            _HttpClientFactory(client),
                        ):
                            ok = await adapter.delete_coupon_by_id(CANARY_PROVIDER_ID)
            return ok, collector

    try:
        ok, collector = asyncio.run(_run())
    finally:
        factory.cleanup()

    handler_text = _log_text(collector)
    cap_text = _caplog_text(caplog)
    log_text = handler_text + "\n" + cap_text
    assert ok is True
    assert post_calls["count"] >= 1
    assert "event=salla_token_freshness_due" in log_text
    assert "event=salla_token_freshness_refresh_success" in log_text
    assert "event=salla_token_freshness_refresh_failed" not in log_text
    assert "event=salla_token_refresh_success" in log_text
    assert str(TEST_TENANT_ID) not in log_text
    _assert_no_canaries(log_text)


def test_proactive_freshness_parse_failure_emits_safe_event(postgres_engine, caplog):
    integration_id = _seed_integration(
        postgres_engine,
        expires_at=CANARY_INVALID_EXPIRY,
    )
    adapter = _adapter_for_integration(
        integration_id,
        postgres_engine,
        expires_at=CANARY_INVALID_EXPIRY,
    )
    async def _run():
        with _capture_bounded_logs() as collector:
            with caplog.at_level(logging.DEBUG):
                await adapter._ensure_token_fresh()
            return collector

    collector = asyncio.run(_run())
    log_text = _log_text(collector) + "\n" + _caplog_text(caplog)
    assert "event=salla_token_freshness_parse_failed" in log_text
    assert "error_class=" in log_text
    assert "tenant_hash=" in log_text
    assert CANARY_INVALID_EXPIRY not in log_text
    _assert_no_canaries(log_text)


def test_lock_contention_emits_safe_event_without_raw_integration_id(postgres_engine, caplog):
    now = datetime.now(timezone.utc)
    integration_id = _seed_integration(
        postgres_engine,
        expires_at=_iso(now + timedelta(days=7)),
    )
    adapter = _adapter_for_integration(integration_id, postgres_engine)
    lock_held = asyncio.Event()
    release_lock = asyncio.Event()
    contender_started = asyncio.Event()
    collector = _LogCollector()

    async def delete_handler(url, **_kwargs):
        contender_started.set()
        return _make_http_response(401)

    async def post_handler(url, **_kwargs):
        raise AssertionError("refresh should be deferred while lock is held")

    client = _RoutingHttpClient(delete_handler, post_handler)
    factory = _session_factory(postgres_engine)

    async def holder():
        async with salla_asyncio_lock(integration_id, caller="test_holder") as acquired:
            assert acquired is True
            lock_held.set()
            await release_lock.wait()

    async def contender():
        await lock_held.wait()
        with patch.dict(
            os.environ,
            {
                "SALLA_CLIENT_ID": "oauth-client-id",
                "SALLA_CLIENT_SECRET": "oauth-client-secret",
            },
            clear=False,
        ):
            with patch("database.session.SessionLocal", factory):
                with patch(
                    "store_adapters.salla_adapter.httpx.AsyncClient",
                    _HttpClientFactory(client),
                ):
                    return await adapter.delete_coupon_by_id(CANARY_PROVIDER_ID)

    async def _run():
        with _capture_bounded_logs() as active_collector:
            with caplog.at_level(logging.DEBUG):
                holder_task = asyncio.create_task(holder())
                await lock_held.wait()
                result = await contender()
                release_lock.set()
                await holder_task
            collector.messages.extend(active_collector.messages)
            return result

    try:
        result = asyncio.run(_run())
    finally:
        factory.cleanup()

    log_text = _log_text(collector) + "\n" + _caplog_text(caplog)
    assert result is False
    assert contender_started.is_set()
    assert "event=salla_token_refresh_lock_held" in log_text
    assert "event=salla_token_refresh_deferred" in log_text
    assert "integration_hash=" in log_text
    assert str(integration_id) not in log_text
    _assert_no_canaries(log_text)


def test_invalid_grant_superseding_lookup_db_fault_emits_safe_event(postgres_engine, caplog):
    """Database-boundary fault injection on superseding-integration lookup only."""
    now = datetime.now(timezone.utc)
    integration_id = _seed_integration(
        postgres_engine,
        expires_at=_iso(now + timedelta(days=7)),
    )
    adapter = _adapter_for_integration(integration_id, postgres_engine)
    collector = _LogCollector()
    factory = _session_factory_with_lookup_fault(postgres_engine)

    async def delete_handler(url, **_kwargs):
        return _make_http_response(401)

    async def post_handler(url, **_kwargs):
        return _make_http_response(400, text=CANARY_OAUTH_BODY)

    client = _RoutingHttpClient(delete_handler, post_handler)

    async def _run():
        with _capture_bounded_logs() as active_collector:
            with caplog.at_level(logging.DEBUG):
                with patch.dict(
                    os.environ,
                    {
                        "SALLA_CLIENT_ID": "oauth-client-id",
                        "SALLA_CLIENT_SECRET": "oauth-client-secret",
                    },
                    clear=False,
                ):
                    with patch("database.session.SessionLocal", factory):
                        with patch(
                            "store_adapters.salla_adapter.httpx.AsyncClient",
                            _HttpClientFactory(client),
                        ):
                            result = await adapter.delete_coupon_by_id(CANARY_PROVIDER_ID)
            collector.messages.extend(active_collector.messages)
            return result

    try:
        factory.reset()
        result = asyncio.run(_run())
    finally:
        factory.cleanup()

    log_text = _log_text(collector) + "\n" + _caplog_text(caplog)
    assert result is False
    assert "event=salla_token_refresh_failed" in log_text
    assert "reason=invalid_grant" in log_text
    assert "event=salla_superseded_lookup_failed" in log_text
    assert "error_class=RuntimeError" in log_text
    assert CANARY_LOOKUP_FAULT not in log_text
    assert CANARY_OAUTH_BODY not in log_text
    _assert_no_canaries(log_text)
