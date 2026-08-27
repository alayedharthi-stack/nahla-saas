"""Production-path caplog canaries for coupon observability remediation (H6-1..H6-5)."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Integration, Tenant
from services.salla_coupons_poller import (
    _run_one_tick,
    get_poller_state,
    run_salla_coupons_poller_scheduler,
)
from services.store_sync import StoreSyncService
from store_adapters.salla_adapter import SallaAdapter

CANARY_COUPON = "CANARY-COUPON-H6"
CANARY_TOKEN = "canary-access-token-h6-98765"
CANARY_STORE = "canary-store-id-999888777"
CANARY_TENANT = 888777666
CANARY_URL = "https://canary.example.com/secret"
CANARY_PAYLOAD = '{"secret":"provider-body-canary"}'

CANARIES = (
    CANARY_COUPON,
    CANARY_TOKEN,
    CANARY_STORE,
    str(CANARY_TENANT),
    CANARY_URL,
    CANARY_PAYLOAD,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _assert_no_canaries(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text


def _sensitive_error() -> RuntimeError:
    return RuntimeError(
        f"coupon={CANARY_COUPON} token={CANARY_TOKEN} store={CANARY_STORE} "
        f"tenant={CANARY_TENANT} url={CANARY_URL} body={CANARY_PAYLOAD}"
    )


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    return session


def _reset_poller_state() -> None:
    import services.salla_coupons_poller as poller_mod

    poller_mod._state["tenants"] = {}


def test_h6_1_scheduler_tick_crash_logs_no_sensitive_data(caplog):
    import services.salla_coupons_poller as poller_mod

    async def boom_tick():
        raise _sensitive_error()

    sleep_calls = {"count": 0}

    async def sleep_side(*_args, **_kwargs):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return None
        raise asyncio.CancelledError()

    async def _run():
        with patch.object(poller_mod, "STARTUP_DELAY_SECONDS", 0):
            with patch.object(poller_mod, "_run_one_tick", boom_tick):
                with patch.object(poller_mod.asyncio, "sleep", sleep_side):
                    await run_salla_coupons_poller_scheduler()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run())
    assert sleep_calls["count"] >= 2
    assert "coupon_poller_tick_failed" in caplog.text
    _assert_no_canaries(caplog.text)


CANARY_PROVIDER_ID = "canary-provider-coupon-id-12345"

CANARY_NEW_ACCESS_TOKEN = "canary-new-access-token-refresh-abc"
CANARY_OAUTH_BODY = '{"error":"canary-oauth-response-body-secret"}'
CANARY_OAUTH_URL = "https://accounts.salla.sa/oauth2/token"

REFRESH_CANARIES = CANARIES + (
    CANARY_NEW_ACCESS_TOKEN,
    CANARY_OAUTH_BODY,
    CANARY_OAUTH_URL,
)


def _assert_no_refresh_canaries(text: str) -> None:
    for canary in REFRESH_CANARIES:
        assert canary not in text


def _make_http_response(status, *, text="", json_data=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    return resp


class _RoutingHttpClient:
    def __init__(self, delete_handler, post_handler):
        self._delete_handler = delete_handler
        self._post_handler = post_handler

    async def delete(self, url, **kwargs):
        return await self._delete_handler(url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._post_handler(url, **kwargs)


class _HttpClientFactory:
    def __init__(self, client):
        self._client = client

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _oauth_delete_adapter():
    return SallaAdapter(
        api_key=CANARY_TOKEN,
        refresh_token="refresh-token-safe-original",
        store_id=CANARY_STORE,
        tenant_id=CANARY_TENANT,
        integration_id=991_601,
    )


def _refresh_path_patches(adapter, client):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _always_acquired_lock(*_args, **_kwargs):
        yield True

    return (
        patch.object(adapter, "_ensure_token_fresh", new=AsyncMock()),
        patch(
            "core.salla_oauth_credentials.resolve_salla_oauth_client",
            return_value=("oauth-client-id", "oauth-client-secret", "legacy"),
        ),
        patch("core.salla_token_lock.salla_asyncio_lock", _always_acquired_lock),
        patch(
            "store_adapters.salla_adapter.httpx.AsyncClient",
            _HttpClientFactory(client),
        ),
    )



class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self._client = kwargs.pop("_client")

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _transport_delete_boom(*_args, **_kwargs):
    raise _sensitive_error()


def test_h6_2_delete_coupon_by_id_failure_logs_no_sensitive_data(caplog):
  adapter = SallaAdapter(
      api_key=CANARY_TOKEN,
      store_id=CANARY_STORE,
      tenant_id=CANARY_TENANT,
  )
  provider_id = CANARY_PROVIDER_ID
  mock_client = MagicMock()
  mock_client.delete = AsyncMock(side_effect=_transport_delete_boom)

  async def _run():
      with patch.object(adapter, "_ensure_token_fresh", new=AsyncMock()):
          with patch(
              "store_adapters.salla_adapter.httpx.AsyncClient",
              lambda *args, **kwargs: _FakeAsyncClient(_client=mock_client, **kwargs),
          ):
              with caplog.at_level(logging.WARNING):
                  return await adapter.delete_coupon_by_id(provider_id)

  ok = asyncio.run(_run())
  assert ok is False
  assert "SallaAdapter._delete_failed" in caplog.text
  assert "RuntimeError" in caplog.text
  _assert_no_canaries(caplog.text)
  assert provider_id not in caplog.text
  assert f"/coupons/{provider_id}" not in caplog.text


def test_h6_2_delete_success_logs_no_raw_path_or_provider_id(caplog):
  adapter = SallaAdapter(
      api_key=CANARY_TOKEN,
      store_id=CANARY_STORE,
      tenant_id=CANARY_TENANT,
  )
  provider_id = CANARY_PROVIDER_ID
  raw_path = f"/coupons/{provider_id}"
  mock_resp = MagicMock()
  mock_resp.status_code = 200
  mock_client = MagicMock()
  mock_client.delete = AsyncMock(return_value=mock_resp)

  async def _run():
      with patch.object(adapter, "_ensure_token_fresh", new=AsyncMock()):
          with patch(
              "store_adapters.salla_adapter.httpx.AsyncClient",
              lambda *args, **kwargs: _FakeAsyncClient(_client=mock_client, **kwargs),
          ):
              with caplog.at_level(logging.INFO):
                  return await adapter.delete_coupon_by_id(provider_id)

  ok = asyncio.run(_run())
  assert ok is True
  assert "salla_delete_completed" in caplog.text
  assert raw_path not in caplog.text
  assert provider_id not in caplog.text
  assert str(CANARY_TENANT) not in caplog.text
  assert CANARY_STORE not in caplog.text




def test_h6_2_delete_401_refresh_retry_success_logs_no_sensitive_data(caplog):
    adapter = _oauth_delete_adapter()
    provider_id = CANARY_PROVIDER_ID
    delete_calls = {"count": 0}
    persist_calls = []

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

    client = _RoutingHttpClient(delete_handler, post_handler)

    def _track_persist(access, refresh, expires_in=None):
        persist_calls.append((access, refresh, expires_in))

    async def _run():
        patches = _refresh_path_patches(adapter, client)
        with patches[0], patches[1], patches[2], patches[3]:
            with patch.object(adapter, "_persist_refreshed_tokens", side_effect=_track_persist):
                with caplog.at_level(logging.INFO):
                    return await adapter.delete_coupon_by_id(provider_id)

    ok = asyncio.run(_run())
    assert ok is True
    assert delete_calls["count"] == 2
    assert adapter.api_key == CANARY_NEW_ACCESS_TOKEN
    assert persist_calls and persist_calls[0][0] == CANARY_NEW_ACCESS_TOKEN
    assert "salla_delete_unauthorized" in caplog.text
    assert "salla_token_refresh_success" in caplog.text
    assert "salla_delete_completed" in caplog.text
    _assert_no_refresh_canaries(caplog.text)
    assert provider_id not in caplog.text


def test_h6_2_delete_401_refresh_oauth_http_failure_logs_no_sensitive_data(caplog):
    adapter = _oauth_delete_adapter()
    provider_id = CANARY_PROVIDER_ID

    async def delete_handler(url, **_kwargs):
        return _make_http_response(401)

    async def post_handler(url, **_kwargs):
        return _make_http_response(503, text=CANARY_OAUTH_BODY)

    client = _RoutingHttpClient(delete_handler, post_handler)

    async def _run():
        patches = _refresh_path_patches(adapter, client)
        with patches[0], patches[1], patches[2], patches[3]:
            with caplog.at_level(logging.WARNING):
                return await adapter.delete_coupon_by_id(provider_id)

    ok = asyncio.run(_run())
    assert ok is False
    assert "salla_token_refresh_failed" in caplog.text
    assert "oauth_http_error" in caplog.text
    _assert_no_refresh_canaries(caplog.text)
    assert provider_id not in caplog.text


def test_h6_2_delete_401_refresh_transport_exception_logs_no_sensitive_data(caplog):
    adapter = _oauth_delete_adapter()
    provider_id = CANARY_PROVIDER_ID

    async def delete_handler(url, **_kwargs):
        return _make_http_response(401)

    async def post_handler(url, **_kwargs):
        raise RuntimeError(
            f"coupon={CANARY_COUPON} token={CANARY_TOKEN} store={CANARY_STORE} "
            f"tenant={CANARY_TENANT} provider={provider_id} url={CANARY_OAUTH_URL} body={CANARY_OAUTH_BODY}"
        )

    client = _RoutingHttpClient(delete_handler, post_handler)

    async def _run():
        patches = _refresh_path_patches(adapter, client)
        with patches[0], patches[1], patches[2], patches[3]:
            with caplog.at_level(logging.WARNING):
                return await adapter.delete_coupon_by_id(provider_id)

    ok = asyncio.run(_run())
    assert ok is False
    assert "salla_token_refresh_failed" in caplog.text
    assert "transport_exception" in caplog.text
    assert "RuntimeError" in caplog.text
    _assert_no_refresh_canaries(caplog.text)
    assert provider_id not in caplog.text

def test_h6_3_per_tenant_poll_failure_isolates_and_redacts_logs(caplog):
    _reset_poller_state()
    db = _make_db()
    tenant1 = Tenant(id=991_301, name="Observability Tenant A", is_active=True)
    tenant2 = Tenant(id=991_302, name="Observability Tenant B", is_active=True)
    db.add_all([tenant1, tenant2])
    db.commit()
    tenant1_id = tenant1.id
    tenant2_id = tenant2.id

    store_fail = CANARY_STORE
    store_ok = "store-ok-445566"

    intg1 = Integration(
        tenant_id=tenant1_id,
        provider="salla",
        enabled=True,
        external_store_id=store_fail,
        config={"api_key": "k1", "store_id": store_fail, "api_sync_enabled": True},
    )
    intg2 = Integration(
        tenant_id=tenant2_id,
        provider="salla",
        enabled=True,
        external_store_id=store_ok,
        config={"api_key": "k2", "store_id": store_ok, "api_sync_enabled": True},
    )
    db.add_all([intg1, intg2])
    db.commit()

    polled: list[int] = []

    async def poll_side(_db, intg):
        polled.append(int(intg.tenant_id))
        if int(intg.tenant_id) == tenant1_id:
            raise _sensitive_error()
        return {
            "items_seen": 1,
            "created": 0,
            "updated": 0,
            "duration_ms": 1,
            "fetch_ok": True,
            "partial": False,
        }

    mock_lock = MagicMock()
    mock_lock.try_acquire.return_value = True
    mock_lock.held = True

    with caplog.at_level(logging.WARNING):
        with patch("core.database.SessionLocal", return_value=db):
            with patch(
                "core.pg_advisory_lock.DedicatedAdvisoryLock",
                return_value=mock_lock,
            ):
                with patch("services.salla_coupon_fetch.tenant_poll_due", return_value=True):
                    with patch(
                        "services.salla_coupons_poller._poll_integration",
                        poll_side,
                    ):
                        asyncio.run(_run_one_tick())

    assert tenant1_id in polled
    assert tenant2_id in polled
    assert "coupon_poller_tenant_failed" in caplog.text
    _assert_no_canaries(caplog.text)
    assert str(tenant1_id) not in caplog.text
    assert str(tenant2_id) not in caplog.text
    assert store_fail not in caplog.text
    assert store_ok not in caplog.text


def test_h6_4_coupon_sync_meta_flush_failure_logs_no_sensitive_data(caplog):
    db = _make_db()
    tenant = Tenant(id=991_401, name="Meta Flush Tenant", is_active=True)
    db.add(tenant)
    db.commit()
    tenant_id = tenant.id

    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        enabled=True,
        external_store_id="meta-store",
        config={"api_key": "token", "store_id": "meta-store"},
    )
    db.add(intg)
    db.commit()

    svc = StoreSyncService(db, tenant_id, integration_connection_id=intg.id)

    with db.no_autoflush:
        with patch.object(db, "flush", side_effect=_sensitive_error()):
            with caplog.at_level(logging.WARNING, logger="nahla-backend"):
                svc._record_coupon_sync_meta(
                    triggered_by="test_observability",
                    items_seen=0,
                    created=0,
                    updated=0,
                    failure_class=None,
                )

    assert "coupon_sync_meta_flush_failed" in caplog.text
    _assert_no_canaries(caplog.text)
    assert str(tenant_id) not in caplog.text


def test_h6_5_poller_state_never_exposes_raw_store_id(caplog):
    _reset_poller_state()
    db = _make_db()
    tenant = Tenant(id=991_501, name="State Tenant", is_active=True)
    db.add(tenant)
    db.commit()
    tenant_id = tenant.id
    canary_store = CANARY_STORE
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        enabled=True,
        external_store_id=canary_store,
        config={"api_key": "token", "store_id": canary_store, "api_sync_enabled": True},
    )
    db.add(intg)
    db.commit()

    async def poll_ok(_db, _intg):
        return {
            "items_seen": 0,
            "created": 0,
            "updated": 0,
            "duration_ms": 1,
            "fetch_ok": True,
            "partial": False,
        }

    mock_lock = MagicMock()
    mock_lock.try_acquire.return_value = True
    mock_lock.held = True

    with caplog.at_level(logging.INFO):
        with patch("core.database.SessionLocal", return_value=db):
            with patch(
                "core.pg_advisory_lock.DedicatedAdvisoryLock",
                return_value=mock_lock,
            ):
                with patch("services.salla_coupon_fetch.tenant_poll_due", return_value=True):
                    with patch(
                        "services.salla_coupons_poller._poll_integration",
                        poll_ok,
                    ):
                        asyncio.run(_run_one_tick())

    state = get_poller_state()
    exported = json.dumps(state)
    assert canary_store not in exported
    assert canary_store not in caplog.text
    tenant_entry = state["tenants"].get(tenant_id) or state["tenants"].get(str(tenant_id))
    assert tenant_entry is not None
    assert tenant_entry.get("store_id") is None
    assert tenant_entry.get("store_present") is True
    assert tenant_entry.get("store_hash")
