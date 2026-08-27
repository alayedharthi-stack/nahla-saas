"""PostgreSQL proof for real delete->401->refresh call graph (H6-2).

Only outbound HTTP transport is mocked. All production helpers in the call
graph execute for real against a seeded integration row.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
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
CANARY_OAUTH_BODY = '{"error":"canary-oauth-response-body-secret"}'
CANARY_OAUTH_URL = "https://accounts.salla.sa/oauth2/token"
CANARY_COUPON = "CANARY-COUPON-H6"
CANARY_PAYLOAD = '{"secret":"provider-body-canary"}'

CANARIES = (
    CANARY_COUPON,
    CANARY_TOKEN,
    TEST_STORE_ID,
    str(TEST_TENANT_ID),
    CANARY_OAUTH_URL,
    CANARY_PAYLOAD,
    CANARY_NEW_ACCESS_TOKEN,
    CANARY_OAUTH_BODY,
    CANARY_PROVIDER_ID,
)

FORBIDDEN_PATCH_TARGETS = (
    "_ensure_token_fresh",
    "_refresh_access_token",
    "_persist_refreshed_tokens",
    "_record_refresh_failure",
    "resolve_salla_oauth_client",
    "salla_asyncio_lock",
    "log_metric_success",
    "log_metric_failed",
    "log_metric_needs_reauth",
)

if not _integration_required():
    pytest.skip(
        "PostgreSQL integration tests require A1_PG_INTEGRATION_REQUIRED=1",
        allow_module_level=True,
    )

pytestmark = pytest.mark.usefixtures("postgres_engine")


@pytest.fixture(autouse=True)
def _capture_salla_loggers(caplog):
    """Ensure adapter + alert loggers propagate into caplog."""
    import logging

    caplog.set_level(logging.DEBUG)
    for name in ("nahla.adapter.salla", "nahla.salla_alerts"):
        lg = logging.getLogger(name)
        lg.propagate = True
        lg.setLevel(logging.DEBUG)
    try:
        from core.salla_token_lock import _locks
        _locks.clear()
    except Exception:
        pass


@pytest.fixture(scope="module")
def postgres_engine():
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


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
    first_failed_at: str | None = None,
) -> int:
    now = datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = _iso(now + timedelta(days=7))
    session, connection = _new_session(engine)
    try:
        tenant = session.get(Tenant, TEST_TENANT_ID)
        if tenant is None:
            tenant = Tenant(id=TEST_TENANT_ID, name="H6-2 Refresh Tenant", is_active=True)
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
        if first_failed_at:
            cfg["token_refresh_first_failed_at"] = first_failed_at
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


def _adapter_for_integration(integration_id: int, engine) -> SallaAdapter:
    session, connection = _new_session(engine)
    try:
        intg = session.get(Integration, integration_id)
        assert intg is not None
        cfg = dict(intg.config or {})
        return SallaAdapter(
            api_key=str(cfg.get("api_key") or ""),
            refresh_token=str(cfg.get("refresh_token") or ""),
            store_id=str(cfg.get("store_id") or TEST_STORE_ID),
            tenant_id=TEST_TENANT_ID,
            integration_id=integration_id,
            expires_at=cfg.get("expires_at") or cfg.get("token_expires_at"),
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


def _run_delete_401_flow(
    engine,
    *,
    delete_handler,
    post_handler,
):
    integration_id = _seed_integration(engine)
    adapter = _adapter_for_integration(integration_id, engine)
    client = _RoutingHttpClient(delete_handler, post_handler)
    factory = _session_factory(engine)

    async def _run():
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

    try:
        return asyncio.run(_run())
    finally:
        factory.cleanup()


def _reload_integration_config(engine, integration_id: int) -> dict:
    session, connection = _new_session(engine)
    try:
        intg = session.get(Integration, integration_id)
        assert intg is not None
        return dict(intg.config or {})
    finally:
        session.close()
        connection.close()


def test_source_uses_transport_only_http_mock() -> None:
    source = inspect.getsource(sys.modules[__name__])
    for forbidden in FORBIDDEN_PATCH_TARGETS:
        assert f"patch.object(adapter, \"{forbidden}\"" not in source
        assert f"patch.object(adapter, '{forbidden}'" not in source
        assert f"patch(\"store_adapters.salla_adapter.{forbidden}\"" not in source
        assert f"patch('store_adapters.salla_adapter.{forbidden}'" not in source
    assert "store_adapters.salla_adapter.httpx.AsyncClient" in source


def test_successful_refresh_retry_persists_and_returns_true(postgres_engine, caplog):
    delete_calls = {"count": 0}

    async def delete_handler(url, **_kwargs):
        delete_calls["count"] += 1
        if delete_calls["count"] == 1:
            return _make_http_response(401)
        return _make_http_response(200)

    async def post_handler(url, **_kwargs):
        return _make_http_response(
            200,
            json_data={
                "access_token": CANARY_NEW_ACCESS_TOKEN,
                "refresh_token": "rotated-refresh-safe",
                "expires_in": 3600,
            },
        )

    integration_id = _seed_integration(postgres_engine)
    ok = _run_delete_401_flow(
        postgres_engine,
        delete_handler=delete_handler,
        post_handler=post_handler,
    )
    assert ok is True
    assert delete_calls["count"] == 2

    cfg = _reload_integration_config(postgres_engine, integration_id)
    assert cfg.get("api_key") == CANARY_NEW_ACCESS_TOKEN
    assert cfg.get("refresh_token") == "rotated-refresh-safe"
    assert cfg.get("token_refresh_status") == "success"
    assert cfg.get("token_refresh_attempts") == 0

    assert "salla_delete_unauthorized" in caplog.text
    assert "salla_token_refresh_success" in caplog.text
    assert "salla_delete_completed" in caplog.text
    assert "event=salla_token_refresh_success" in caplog.text
    _assert_no_canaries(caplog.text)


def test_oauth_http_failure_records_safe_metrics(postgres_engine, caplog):
    integration_id = _seed_integration(postgres_engine)

    async def delete_handler(url, **_kwargs):
        return _make_http_response(401)

    async def post_handler(url, **_kwargs):
        return _make_http_response(503, text=CANARY_OAUTH_BODY)

    ok = _run_delete_401_flow(
        postgres_engine,
        delete_handler=delete_handler,
        post_handler=post_handler,
    )
    assert ok is False
    assert "salla_token_refresh_failed" in caplog.text
    assert "oauth_http_error" in caplog.text
    assert "event=salla_token_refresh_failed" in caplog.text
    _assert_no_canaries(caplog.text)

    cfg = _reload_integration_config(postgres_engine, integration_id)
    assert int(cfg.get("token_refresh_attempts") or 0) >= 1
    assert cfg.get("token_refresh_status") == "failed"


def test_refresh_transport_exception_logs_safe_class_only(postgres_engine, caplog):
    _seed_integration(postgres_engine)

    async def delete_handler(url, **_kwargs):
        return _make_http_response(401)

    async def post_handler(url, **_kwargs):
        raise RuntimeError(
            f"coupon={CANARY_COUPON} token={CANARY_TOKEN} store={TEST_STORE_ID} "
            f"tenant={TEST_TENANT_ID} provider={CANARY_PROVIDER_ID} url={CANARY_OAUTH_URL} body={CANARY_OAUTH_BODY}"
        )

    ok = _run_delete_401_flow(
        postgres_engine,
        delete_handler=delete_handler,
        post_handler=post_handler,
    )
    assert ok is False
    assert "salla_token_refresh_failed" in caplog.text
    assert "transport_exception" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "Traceback" not in caplog.text
    _assert_no_canaries(caplog.text)


def test_needs_reauth_metric_uses_hashed_correlation(postgres_engine, caplog):
    now = datetime.now(timezone.utc)
    integration_id = _seed_integration(
        postgres_engine,
        attempts=2,
        expires_at=_iso(now + timedelta(hours=2)),
        first_failed_at=_iso(now - timedelta(hours=1)),
    )

    async def delete_handler(url, **_kwargs):
        return _make_http_response(401)

    async def post_handler(url, **_kwargs):
        return _make_http_response(503, text=CANARY_OAUTH_BODY)

    ok = _run_delete_401_flow(
        postgres_engine,
        delete_handler=delete_handler,
        post_handler=post_handler,
    )
    assert ok is False
    assert "event=salla_token_refresh_needs_reauth" in caplog.text
    assert str(TEST_TENANT_ID) not in caplog.text
    assert TEST_STORE_ID not in caplog.text
    _assert_no_canaries(caplog.text)

    cfg = _reload_integration_config(postgres_engine, integration_id)
    assert cfg.get("needs_reauth") is True
    assert int(cfg.get("token_refresh_attempts") or 0) == 3
