"""Real-route transaction, nonce, and Cloud select regressions for PR 877."""
from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Tenant, WhatsAppConnection, WhatsAppOAuthNonce  # noqa: E402

WABA = "TEST-WABA-877"
PHONE = "TEST-PHONE-877"
PORTFOLIO = "TEST-BUSINESS-877"
E164 = "+966501234567"
CLOUD_PHONE = "TEST-PHONE-CLOUD-DUP"
REDIRECT = "https://api.example.test/whatsapp/embedded/oauth/callback"
DASH = "https://dash.example.test"


def _remap_jsonb(engine) -> None:
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _remap_jsonb(engine)
    return sessionmaker(bind=engine), engine


class GraphScript:
    def __init__(self, *, mode: str = "success"):
        self.mode = mode
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method.upper()
        with self.lock:
            self.calls.append((method, url))
            self.requests.append(request)
        path = urlparse(url).path
        if method in {"DELETE", "PUT"}:
            return httpx.Response(500, json={"error": {"message": "mutation forbidden in test"}})
        if path.endswith("/oauth/access_token"):
            if method not in {"GET", "POST"}:
                return httpx.Response(405, json={"error": {"message": "method not allowed"}})
            return httpx.Response(
                200,
                json={"access_token": "user-long-token", "token_type": "bearer", "expires_in": 5183944},
            )
        if path.endswith("/debug_token"):
            if method != "GET":
                return httpx.Response(405, json={"error": {"message": "debug_token requires GET"}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "is_valid": True,
                        "type": "USER",
                        "granular_scopes": [
                            {"scope": "whatsapp_business_management", "target_ids": [WABA]},
                            {"scope": "business_management", "target_ids": [PORTFOLIO]},
                        ],
                    }
                },
            )
        if path.rstrip("/").endswith(WABA) and "phone_numbers" not in path:
            if self.mode == "boom":
                raise RuntimeError("graph boom")
            return httpx.Response(
                200,
                json={"id": WABA, "name": "Coex WABA", "owner_business_info": {"id": PORTFOLIO}},
            )
        if path.endswith(f"{WABA}/phone_numbers"):
            display = "+966509999999" if self.mode == "wrong_phone" else E164
            return httpx.Response(
                200,
                json={"data": [{"id": PHONE, "display_phone_number": display, "verified_name": "generic-shop"}]},
            )
        if path.endswith(f"{PHONE}/subscribed_apps"):
            if self.mode == "webhook_fail":
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": (
                                f"webhook failed phone_id={PHONE} waba_id={WABA} "
                                f"phone={E164} business_id={PORTFOLIO} "
                                "access_token=user-long-token"
                            )
                        }
                    },
                )
            return httpx.Response(200, json={"success": True})
        if path.endswith(f"{PHONE}/smb_app_data"):
            if self.mode == "smb_wait":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"request_id": "smb-req-1"})
        if path.endswith("/request_code"):
            return httpx.Response(200, json={"success": True})
        if path.rstrip("/").endswith(PHONE):
            on_app = False if self.mode == "ineligible" else True
            display = "+966509999999" if self.mode == "wrong_phone" else E164
            return httpx.Response(
                200,
                json={
                    "id": PHONE,
                    "display_phone_number": display,
                    "verified_name": "generic-shop",
                    "is_on_biz_app": on_app,
                    "platform_type": "CLOUD_API",
                    "code_verification_status": "NOT_VERIFIED",
                },
            )
        if path.rstrip("/").endswith(CLOUD_PHONE):
            return httpx.Response(
                200,
                json={
                    "id": CLOUD_PHONE,
                    "display_phone_number": "+966501111222",
                    "verified_name": "Cloud Shop",
                    "code_verification_status": "NOT_VERIFIED",
                    "is_on_biz_app": False,
                    "platform_type": "CLOUD_API",
                },
            )
        return httpx.Response(404, json={"error": {"message": f"unmocked {method} {path}"}})

    def assert_no_mutations(self) -> None:
        for method, url in self.calls:
            lowered = url.lower()
            assert method not in {"DELETE", "PUT"}
            assert "deregister" not in lowered
            assert "unlink" not in lowered
            assert "/assigned_users" not in lowered


def _install_httpx(monkeypatch, script: GraphScript) -> None:
    transport = httpx.MockTransport(script)
    orig_async = httpx.AsyncClient
    orig_client = httpx.Client

    class _AsyncClient(orig_async):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    def _get(url, **kwargs):
        with orig_client(transport=transport) as client:
            return client.get(url, **kwargs)

    def _post(url, **kwargs):
        with orig_client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)
    async def _live_graph_get(graph_base, token, node, fields):
        resp = httpx.get(
            f"{graph_base}/{node}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": fields},
            timeout=20,
        )
        data = resp.json()
        if "error" in data:
            err = data.get("error") or {}
            return {
                "ok": False,
                "status": resp.status_code,
                "code": err.get("code"),
                "message": err.get("message") or f"HTTP {resp.status_code}",
            }
        return {"ok": True, "status": resp.status_code, "data": data}

    monkeypatch.setattr("services.embedded_waba_resolution._graph_get", _live_graph_get)
    monkeypatch.setattr("services.embedded_waba_resolution.httpx.get", _get)
    monkeypatch.setattr("services.meta_coexistence.httpx.get", _get)
    monkeypatch.setattr("services.meta_coexistence.httpx.post", _post)
    monkeypatch.setattr("services.whatsapp_connection_service.httpx.post", _post)
    monkeypatch.setattr("routers.whatsapp_embedded.httpx.AsyncClient", _AsyncClient)


def _patch_meta_env(monkeypatch) -> None:
    import routers.whatsapp_embedded as emb
    import services.meta_graph_oauth_client as oauth_client

    monkeypatch.setattr(emb, "META_APP_ID", "app-test", raising=False)
    monkeypatch.setattr(emb, "META_APP_SECRET", "secret-test", raising=False)
    monkeypatch.setattr(oauth_client, "META_APP_ID", "app-test", raising=False)
    monkeypatch.setattr(oauth_client, "META_APP_SECRET", "secret-test", raising=False)
    monkeypatch.setattr(emb, "META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", "coex-cfg", raising=False)
    monkeypatch.setattr(emb, "META_EMBEDDED_SIGNUP_CONFIG_ID", "cloud-cfg", raising=False)
    monkeypatch.setattr(emb, "is_meta_embedded_signup_enabled", lambda: True)
    monkeypatch.setattr(emb, "is_coexistence_embedded_signup_available", lambda: True)
    monkeypatch.setattr(emb, "canonical_meta_redirect_uri", lambda: REDIRECT)
    monkeypatch.setenv("DASHBOARD_URL", DASH)


def _build_client(Session, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.database import get_db
    import routers.whatsapp_embedded as emb

    _patch_meta_env(monkeypatch)

    app = FastAPI()
    app.include_router(emb.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app), emb


def _seed_tenant(Session, tenant_id: int, *, with_dialog360: bool = True, **conn_kwargs):
    db = Session()
    db.add(Tenant(id=tenant_id, name=f"tenant-{tenant_id}", is_active=True))
    conn = None
    if with_dialog360:
        payload = {
            "tenant_id": tenant_id,
            "status": "disconnected",
            "provider": "dialog360",
            "phone_number": E164,
        }
        payload.update(conn_kwargs)
        conn = WhatsAppConnection(**payload)
        db.add(conn)
    db.commit()
    if conn is not None:
        db.refresh(conn)
    db.close()
    return conn


def _start_state(client, tenant_id: int) -> str:
    resp = client.get(
        "/whatsapp/embedded/oauth/start",
        params={"connection_mode": "coexistence"},
        headers={"X-Tenant-ID": str(tenant_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    return qs["state"][0]


def _callback(client, tenant_id: int, state: str):
    return client.get(
        "/whatsapp/embedded/oauth/callback",
        params={"code": "oauth-code-877", "state": state},
        headers={"X-Tenant-ID": str(tenant_id)},
        follow_redirects=False,
    )

def test_coexistence_callback_success_persists_claim_without_typeerror(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8771)
    client, emb = _build_client(Session, monkeypatch)
    with patch("services.whatsapp_connection_service.begin_waba_session") as begin_mock:
        with patch.object(emb, "_get_waba_id_from_token") as legacy:
            state = _start_state(client, 8771)
            resp = _callback(client, 8771, state)
    begin_mock.assert_not_called()
    legacy.assert_not_called()
    assert resp.status_code == 302
    assert "#meta=ok" in resp.headers["location"]
    db = Session()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=8771).one()
    assert conn.status == "connected"
    assert conn.provider == "meta"
    claim = (conn.extra_metadata or {}).get("coexistence_exchange_claim") or {}
    assert claim.get("status") == "completed"
    assert claim.get("trusted_business_portfolio_id") == PORTFOLIO
    assert claim.get("canonical_phone_e164") == E164
    assert claim.get("waba_id") == WABA
    nonce = db.query(WhatsAppOAuthNonce).filter_by(tenant_id=8771).one()
    assert nonce.consumed_at is not None
    db.close()
    script.assert_no_mutations()
    assert script.calls, "MockTransport must observe Graph HTTP"


@pytest.mark.parametrize("mode", ["ineligible", "webhook_fail", "smb_wait", "wrong_phone"])
def test_coexistence_callback_soft_failure_redirects_and_leaves_no_new_row(monkeypatch, mode):
    Session, _engine = _session_factory()
    script = GraphScript(mode=mode)
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8772)
    client, _emb = _build_client(Session, monkeypatch)
    state = _start_state(client, 8772)
    resp = _callback(client, 8772, state)
    assert resp.status_code == 302
    assert "#meta=error" in resp.headers["location"]
    assert "#meta=ok" not in resp.headers["location"]
    db = Session()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=8772).one()
    assert conn.status == "disconnected"
    assert conn.provider == "dialog360"
    assert conn.whatsapp_business_account_id is None
    db.close()


def test_coexistence_callback_exception_rolls_back_existing_dialog360(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="boom")
    _install_httpx(monkeypatch, script)
    from services.whatsapp_platform.wa_connection_secrets import store_access_token

    db = Session()
    db.add(Tenant(id=8773, name="t8773", is_active=True))
    connected_at = datetime(2026, 1, 15, 12, 0, 0)
    live_since = datetime(2026, 1, 15, 12, 5, 0)
    conn = WhatsAppConnection(
        tenant_id=8773,
        status="disconnected",
        provider="dialog360",
        phone_number=E164,
        connected_at=connected_at,
        whatsapp_ai_live_since=live_since,
        extra_metadata={"legacy_channel": "CH-8773"},
    )
    db.add(conn)
    db.commit()
    store_access_token(conn, "dialog-token-8773")
    db.commit()
    original_token = conn.access_token
    original = {
        "provider": conn.provider,
        "status": conn.status,
        "phone_number": conn.phone_number,
        "access_token": conn.access_token,
        "connected_at": conn.connected_at,
        "whatsapp_ai_live_since": conn.whatsapp_ai_live_since,
        "extra_metadata": dict(conn.extra_metadata or {}),
        "whatsapp_business_account_id": conn.whatsapp_business_account_id,
        "phone_number_id": conn.phone_number_id,
    }
    db.close()
    client, _emb = _build_client(Session, monkeypatch)
    state = _start_state(client, 8773)
    with pytest.raises(RuntimeError, match="graph boom"):
        _callback(client, 8773, state)
    db = Session()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=8773).one()
    for key, value in original.items():
        assert getattr(conn, key) == value
    assert conn.access_token == original_token
    db.close()


def test_coexistence_callback_replay_rejected(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8774)
    client, _emb = _build_client(Session, monkeypatch)
    state = _start_state(client, 8774)
    first = _callback(client, 8774, state)
    assert "#meta=ok" in first.headers["location"]
    second = _callback(client, 8774, state)
    assert second.status_code == 302
    assert "#meta=error" in second.headers["location"]
    db = Session()
    assert db.query(WhatsAppConnection).filter_by(tenant_id=8774, status="connected").count() == 1
    db.close()


def test_coexistence_concurrent_callbacks_single_transition(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8775)
    client, emb = _build_client(Session, monkeypatch)
    state = _start_state(client, 8775)
    request = SimpleNamespace(state=SimpleNamespace(tenant_id=8775), headers={})

    async def _one():
        db = Session()
        try:
            resp = await emb.oauth_callback(
                request=request,
                db=db,
                code="oauth-code-877",
                state=state,
            )
            return getattr(resp, "headers", {}).get("location") or str(resp)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            return f"error:{type(exc).__name__}"
        finally:
            db.close()

    async def _both():
        return await asyncio.gather(_one(), _one())

    locations = asyncio.run(_both())
    ok = [loc for loc in locations if isinstance(loc, str) and "#meta=ok" in loc]
    other = [loc for loc in locations if loc not in ok]
    assert len(ok) == 1, locations
    assert len(other) == 1, locations
    db = Session()
    assert db.query(WhatsAppConnection).filter_by(tenant_id=8775, status="connected").count() == 1
    db.close()


def test_flush_then_commit_failure_leaves_no_partial_state(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8776)
    client, emb = _build_client(Session, monkeypatch)

    def _boom(db, tenant_id):
        db.flush()
        raise RuntimeError("commit-fail")

    monkeypatch.setattr(emb, "commit_coexistence_transaction", _boom)
    state = _start_state(client, 8776)
    with pytest.raises(RuntimeError, match="commit-fail"):
        _callback(client, 8776, state)
    db = Session()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=8776).one()
    assert conn.status == "disconnected"
    assert conn.provider == "dialog360"
    db.close()

def test_real_exchange_success_and_soft_fail(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8777)
    client, _emb = _build_client(Session, monkeypatch)
    resp = client.post(
        "/whatsapp/embedded/exchange",
        headers={"X-Tenant-ID": "8777"},
        json={
            "code": "js-sdk-code-877",
            "connection_mode": "coexistence",
            "finish_event": "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
            "waba_id": "client-hint-must-be-ignored",
            "phone_number_id": "client-phone-hint",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("connected") is True
    db = Session()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=8777).one()
    assert conn.status == "connected"
    claim = (conn.extra_metadata or {}).get("coexistence_exchange_claim") or {}
    assert claim.get("trusted_business_portfolio_id") == PORTFOLIO
    db.close()
    script.assert_no_mutations()

    Session2, _engine2 = _session_factory()
    script2 = GraphScript(mode="ineligible")
    _install_httpx(monkeypatch, script2)
    _seed_tenant(Session2, 8778, with_dialog360=False)
    client2, _emb2 = _build_client(Session2, monkeypatch)
    fail = client2.post(
        "/whatsapp/embedded/exchange",
        headers={"X-Tenant-ID": "8778"},
        json={
            "code": "js-sdk-code-fail",
            "connection_mode": "coexistence",
            "finish_event": "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
        },
    )
    assert fail.status_code in {200, 400}
    db = Session2()
    assert db.query(WhatsAppConnection).filter_by(tenant_id=8778).count() == 0
    db.close()


def test_real_select_phone_coexistence_skips_eviction(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    from services.whatsapp_platform.wa_connection_secrets import store_access_token

    db = Session()
    db.add(Tenant(id=8779, name="sel", is_active=True))
    conn = WhatsAppConnection(
        tenant_id=8779,
        status="pending",
        provider="meta",
        connection_type="embedded",
        phone_number=E164,
        extra_metadata={"connection_mode": "coexistence"},
    )
    db.add(conn)
    db.commit()
    store_access_token(conn, "user-long-token")
    db.commit()
    db.close()
    client, _emb = _build_client(Session, monkeypatch)
    with patch("core.tenant_integrity.evict_phone_id_from_other_tenants") as evict:
        resp = client.post(
            "/whatsapp/embedded/select-phone",
            headers={"X-Tenant-ID": "8779"},
            json={"phone_number_id": PHONE},
        )
        evict.assert_not_called()
    assert resp.status_code == 200, resp.text
    assert resp.json().get("connected") is True
    script.assert_no_mutations()


def test_cloud_select_phone_evicts_disconnected_duplicate(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    from services.whatsapp_platform.wa_connection_secrets import store_access_token

    db = Session()
    db.add(Tenant(id=8780, name="cloud-a", is_active=True))
    db.add(Tenant(id=8781, name="cloud-b", is_active=True))
    stale = WhatsAppConnection(
        tenant_id=8780,
        status="disconnected",
        provider="meta",
        phone_number_id=CLOUD_PHONE,
        phone_number="+966501111222",
    )
    live = WhatsAppConnection(
        tenant_id=8781,
        status="pending",
        provider="meta",
        connection_type="embedded",
        extra_metadata={"connection_mode": "cloud_api"},
    )
    db.add_all([stale, live])
    db.commit()
    store_access_token(live, "cloud-token")
    db.commit()
    db.close()
    client, _emb = _build_client(Session, monkeypatch)
    resp = client.post(
        "/whatsapp/embedded/select-phone",
        headers={"X-Tenant-ID": "8781"},
        json={"phone_number_id": CLOUD_PHONE},
    )
    assert resp.status_code == 200, resp.text
    db = Session()
    stale = db.query(WhatsAppConnection).filter_by(tenant_id=8780).one()
    live = db.query(WhatsAppConnection).filter_by(tenant_id=8781).one()
    assert stale.phone_number_id is None
    assert stale.status == "disconnected"
    assert live.phone_number_id == CLOUD_PHONE
    db.close()


def test_integrity_logs_redact_raw_identifiers(caplog):
    Session, _engine = _session_factory()
    db = Session()
    db.add(Tenant(id=8782, name="a", is_active=True))
    db.add(Tenant(id=8783, name="b", is_active=True))
    db.add(
        WhatsAppConnection(
            tenant_id=8782,
            status="disconnected",
            provider="dialog360",
            whatsapp_business_account_id=WABA,
            phone_number_id=PHONE,
        )
    )
    db.commit()
    from core.tenant_integrity import TenantIntegrityError, assert_no_cross_tenant_whatsapp_asset
    from core.log_redaction import redact_graph_id

    caplog.set_level(logging.INFO, logger="nahla.tenant_integrity")
    with pytest.raises(TenantIntegrityError):
        assert_no_cross_tenant_whatsapp_asset(db, 8783, waba_id=WABA, phone_number_id=PHONE)
    combined = caplog.text
    assert WABA not in combined
    assert PHONE not in combined
    assert redact_graph_id(WABA) in combined
    assert redact_graph_id(PHONE) in combined
    db.close()


def test_new_tenant_graph_exception_leaves_no_connection_row(monkeypatch):
    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8784, with_dialog360=False)
    client, _emb = _build_client(Session, monkeypatch)
    state = _start_state(client, 8784)
    resp = _callback(client, 8784, state)
    assert resp.status_code == 302
    assert "#meta=error" in resp.headers["location"]
    db = Session()
    assert db.query(WhatsAppConnection).filter_by(tenant_id=8784).count() == 0
    db.close()


def _add_graph_log_filters():
    from core.log_redaction import SecretRedactingFilter

    redact_filter = SecretRedactingFilter()
    graph_loggers = [logging.getLogger("httpx"), logging.getLogger("httpcore")]
    for graph_logger in graph_loggers:
        graph_logger.addFilter(redact_filter)
    return redact_filter, graph_loggers


def _assert_sensitive_values_absent(log_text: str) -> None:
    raw_values = (
        "user-long-token",
        "app-test|secret-test",
        "secret-test",
        "oauth-code-877",
        PHONE,
        WABA,
        PORTFOLIO,
        E164,
    )
    for raw in raw_values:
        assert raw not in log_text


def test_coexistence_finalize_success_uses_bearer_and_redacts_all_logs(monkeypatch, caplog):
    from core.log_redaction import redact_graph_id

    Session, _engine = _session_factory()
    script = GraphScript(mode="success")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8790)
    client, _emb = _build_client(Session, monkeypatch)
    redact_filter, graph_loggers = _add_graph_log_filters()
    caplog.set_level(logging.INFO)
    try:
        state = _start_state(client, 8790)
        resp = _callback(client, 8790, state)
    finally:
        for graph_logger in graph_loggers:
            graph_logger.removeFilter(redact_filter)

    assert resp.status_code == 302
    assert "#meta=ok" in resp.headers["location"]
    asset_requests = [
        request
        for request in script.requests
        if PHONE in request.url.path or WABA in request.url.path
    ]
    assert asset_requests, "MockTransport must observe finalize Graph requests"
    for request in asset_requests:
        assert "access_token" not in request.url.params
        assert "user-long-token" not in str(request.url)
        assert request.headers.get("authorization") == "Bearer user-long-token"

    combined = caplog.text
    _assert_sensitive_values_absent(combined)
    assert redact_graph_id(PHONE) in combined
    assert redact_graph_id(WABA) in combined
    assert "smb-req-1" not in combined
    assert redact_graph_id("smb-req-1") in combined


def test_coexistence_finalize_failure_redacts_provider_error_logs(monkeypatch, caplog):
    from core.log_redaction import redact_graph_id

    Session, _engine = _session_factory()
    script = GraphScript(mode="webhook_fail")
    _install_httpx(monkeypatch, script)
    _seed_tenant(Session, 8791)
    client, _emb = _build_client(Session, monkeypatch)
    redact_filter, graph_loggers = _add_graph_log_filters()
    caplog.set_level(logging.INFO)
    try:
        state = _start_state(client, 8791)
        resp = _callback(client, 8791, state)
    finally:
        for graph_logger in graph_loggers:
            graph_logger.removeFilter(redact_filter)

    assert resp.status_code == 302
    assert "#meta=error" in resp.headers["location"]
    combined = caplog.text
    _assert_sensitive_values_absent(combined)
    assert redact_graph_id(PHONE) in combined
    assert redact_graph_id(WABA) in combined
    assert redact_graph_id(PORTFOLIO) in combined
    assert redact_graph_id(E164) in combined
    assert "access_token=REDACTED" in combined


def test_httpx_request_and_exception_logging_redacts_url_and_identifiers(caplog):
    from core.log_redaction import redact_graph_id

    redact_filter, graph_loggers = _add_graph_log_filters()
    caplog.set_level(logging.INFO, logger="httpx")
    url = httpx.URL(
        f"https://graph.facebook.com/v20.0/{PHONE}/subscribed_apps"
        "?access_token=user-long-token&input_token=user-long-token"
    )
    request = httpx.Request("POST", url)
    error = httpx.ConnectError(
        f"request failed {url} business_id={PORTFOLIO} phone={E164}",
        request=request,
    )
    try:
        logging.getLogger("httpx").info("HTTP Request: %s", url)
        logging.getLogger("httpx").error("HTTP exception: %s", error)
    finally:
        for graph_logger in graph_loggers:
            graph_logger.removeFilter(redact_filter)

    combined = caplog.text
    _assert_sensitive_values_absent(combined)
    assert redact_graph_id(PHONE) in combined
    assert redact_graph_id(PORTFOLIO) in combined
    assert redact_graph_id(E164) in combined
    assert "access_token=REDACTED" in combined
    assert "input_token=REDACTED" in combined
