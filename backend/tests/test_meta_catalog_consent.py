"""Catalog-only Meta consent: entry, callback, verification, storage and binding.

No network: Graph is replaced by an in-process fake that records every call.
Every credential (app secret, OAuth code, tokens, encryption key, JWT secret)
is generated at runtime. Generic merchant data only (متجر تجريبي عام,
«حذاء رياضي أبيض»).
"""
from __future__ import annotations

import json
import logging
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import core.config as core_config  # noqa: E402
import core.review_environment as review_env_mod  # noqa: E402
import core.whatsapp_oauth_nonce as nonce_mod  # noqa: E402
from core.meta_catalog_consent_config import (  # noqa: E402
    CALLBACK_PATH,
    evaluate_consent_availability,
    parse_approved_assets,
)
from models import Base, MetaCatalogAuthorization, Tenant, WhatsAppConnection, WhatsAppOAuthNonce  # noqa: E402
from routers import meta_catalog_consent as router_mod  # noqa: E402
from services import meta_catalog_consent as consent  # noqa: E402

API_HOST = "api.catalog-review.example.test"
DASH_HOST = "catalog-review.example.test"
REDIRECT = f"https://{API_HOST}{CALLBACK_PATH}"
RETURN = f"https://{DASH_HOST}/catalog"
CATALOG = "880000000000001"
BUSINESS = "770000000000001"
OTHER_CATALOG = "880000000000002"
OTHER_BUSINESS = "770000000000002"
APP_ID = "600000000000001"
USER_ID = "500000000000001"
TENANT = 1
OTHER_TENANT = 2

_FORBIDDEN_GRAPH_FRAGMENTS = ("whatsapp", "phone_number", "subscribed_apps", "register", "/messages",
                              "message_templates", "smb_app_data", "client_whatsapp")


def _remap_jsonb(target, connection, **kw):  # SQLite cannot CREATE JSONB
    for table in target.tables.values():
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


class FakeGraph:
    """Records every Graph call; answers like Meta for the approved assets."""

    def __init__(self, secrets_seen: List[str]):
        self.calls: List[Tuple[str, Dict[str, Any], bool]] = []
        self.overrides: Dict[str, Any] = {}
        self.short = "EAA" + secrets.token_hex(24)
        self.long = "EAA" + secrets.token_hex(24)
        secrets_seen += [self.short, self.long]
        now = int(time.time())
        self.debug = {
            "app_id": APP_ID, "type": "USER", "user_id": USER_ID, "is_valid": True,
            "expires_at": now + 5_000_000, "data_access_expires_at": now + 7_000_000,
            "scopes": ["catalog_management", "business_management", "public_profile"],
            "granular_scopes": [
                {"scope": "catalog_management", "target_ids": [CATALOG]},
                {"scope": "business_management", "target_ids": [BUSINESS]},
            ],
        }

    def answer(self, path: str, params: Dict[str, Any]):
        if path in self.overrides:
            value = self.overrides[path]
            return value(params) if callable(value) else value
        if path == "oauth/access_token":
            if params.get("grant_type") == "fb_exchange_token":
                return 200, {"access_token": self.long, "token_type": "bearer", "expires_in": 5184000}
            return 200, {"access_token": self.short, "token_type": "bearer", "expires_in": 3600}
        if path == "debug_token":
            return 200, {"data": dict(self.debug)}
        if path == "me":
            return 200, {"id": USER_ID}
        if path == "me/permissions":
            return 200, {"data": [{"permission": "catalog_management", "status": "granted"},
                                  {"permission": "business_management", "status": "granted"}]}
        if path == CATALOG:
            return 200, {"id": CATALOG, "business": {"id": BUSINESS}}
        if path == f"{BUSINESS}/owned_product_catalogs":
            return 200, {"data": [{"id": CATALOG}]}
        if path == "me/business_users":
            return 200, {"data": [{"id": "900000000000001", "business": {"id": BUSINESS}, "role": "ADMIN"}]}
        return 404, {"error": {"message": "unexpected path in fake", "code": 803}}

    async def __call__(self, path: str, params: Dict[str, Any], *, token: Optional[str] = None):
        self.calls.append((path, dict(params), bool(token)))
        return self.answer(path, params)

    def paths(self) -> List[str]:
        return [c[0] for c in self.calls]


@pytest.fixture()
def world(monkeypatch, caplog):
    seen: List[str] = []
    app_secret = secrets.token_urlsafe(32)
    jwt_secret = secrets.token_urlsafe(48)
    enc_key = Fernet.generate_key().decode()
    seen += [app_secret, jwt_secret, enc_key]
    env = {
        "NAHLA_CATALOG_REVIEW_ENV": "1",
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "ENVIRONMENT": "staging",
        "DATABASE_URL": "postgresql+psycopg2://review_user:pw@postgres-catalog-review.railway.internal:5432/railway",
        "DASHBOARD_URL": f"https://{DASH_HOST}",
        "NAHLA_META_CATALOG_CONSENT_ENABLED": "1",
        "META_CATALOG_CONSENT_CONFIG_ID": "400000000000001",
        "META_CATALOG_CONSENT_REDIRECT_URI": REDIRECT,
        "META_CATALOG_CONSENT_APPROVED_ASSETS": f"{TENANT}:{CATALOG}:{BUSINESS}",
        "META_APP_ID": APP_ID,
        "META_APP_SECRET": app_secret,
        "WA_TOKEN_ENC_KEY": enc_key,
    }
    for name in ("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", "NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS"):
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(core_config, "JWT_SECRET", jwt_secret)
    monkeypatch.setattr(
        review_env_mod, "read_database_marker",
        lambda _url: review_env_mod.MarkerReading("catalog-review", "catalog-review", "railway"),
    )

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    event.listen(Base.metadata, "before_create", _remap_jsonb)
    try:
        Base.metadata.create_all(engine, tables=[
            Tenant.__table__, WhatsAppConnection.__table__, WhatsAppOAuthNonce.__table__,
            MetaCatalogAuthorization.__table__,
        ])
    finally:
        event.remove(Base.metadata, "before_create", _remap_jsonb)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as s:
        s.add(Tenant(id=TENANT, name="متجر تجريبي عام"))
        s.add(Tenant(id=OTHER_TENANT, name="متجر تجريبي آخر"))
        s.commit()
    monkeypatch.setattr(nonce_mod, "_independent_session", factory)
    consent._TABLE_SEEN.clear()

    class _Ent:
        def has_feature(self, key):
            return key == "meta_catalog_sync"

    import core.plan_entitlements as ent_mod

    monkeypatch.setattr(ent_mod, "get_entitlements", lambda db, tid, strict_lookup=True: _Ent())

    graph = FakeGraph(seen)
    monkeypatch.setattr(consent, "_graph_get", graph)

    app = FastAPI()

    @app.middleware("http")
    async def _fake_jwt(request: Request, call_next):
        raw = request.headers.get("x-test-jwt")
        if raw:
            request.state.jwt_payload = json.loads(raw)
            request.state.tenant_id = str(json.loads(raw).get("tenant_id"))
        return await call_next(request)

    app.include_router(router_mod.router)

    def _db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[router_mod.get_db] = _db
    client = TestClient(app)
    # The production log path: main.py installs this at import.
    from core.log_redaction import install_log_redaction

    install_log_redaction()
    caplog.set_level(logging.DEBUG)

    class W:
        pass

    w = W()
    w.client, w.factory, w.graph, w.secrets, w.engine = client, factory, graph, seen, engine
    return w


def _jwt(tenant_id: int = TENANT, **extra) -> Dict[str, str]:
    return {"x-test-jwt": json.dumps({"tenant_id": tenant_id, "role": "merchant", "user_id": 7, **extra})}


def _start(w, tenant_id: int = TENANT) -> str:
    resp = w.client.post("/merchant/catalog/meta-consent/start", headers=_jwt(tenant_id))
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorize_url"]
    return parse_qs(urlsplit(url).query)["state"][0]


def _callback(w, **params) -> Tuple[int, str]:
    resp = w.client.get(f"{CALLBACK_PATH}", params=params, follow_redirects=False)
    return resp.status_code, resp.headers.get("location", "")


def _code(w) -> str:
    value = secrets.token_urlsafe(40)
    w.secrets.append(value)
    return value


def _result(location: str) -> str:
    assert location.startswith(RETURN + "#"), location
    return parse_qs(urlsplit(location).fragment)["meta_catalog_consent"][0]


def _rows(w) -> List[MetaCatalogAuthorization]:
    with w.factory() as s:
        return s.query(MetaCatalogAuthorization).all()


def _no_secret_in(text: str, w) -> None:
    for value in w.secrets:
        assert value not in text


# ── availability (default off, production denial) ───────────────────────────

def test_default_environment_is_disabled_without_touching_the_database():
    calls = []
    out = evaluate_consent_availability({}, marker_reader=lambda u: calls.append(u))
    assert (out.available, out.reason) == (False, "disabled")
    assert calls == []


def test_flag_without_review_environment_is_refused():
    out = evaluate_consent_availability({"NAHLA_META_CATALOG_CONSENT_ENABLED": "1", "ENVIRONMENT": "production"})
    assert (out.available, out.reason) == (False, "review_environment_required")


def test_unmarked_database_is_refused(world, monkeypatch):
    monkeypatch.setattr(review_env_mod, "read_database_marker",
                        lambda _u: review_env_mod.MarkerReading(None, None, "railway"))
    assert evaluate_consent_availability(tenant_id=TENANT).reason == "review_environment_unverified"


@pytest.mark.parametrize("name,value,reason", [
    ("META_APP_SECRET", "", "app_credentials_missing"),
    ("META_CATALOG_CONSENT_CONFIG_ID", "", "config_id_missing"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"http://{API_HOST}{CALLBACK_PATH}", "redirect_uri_invalid"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"https://api.nahlah.ai{CALLBACK_PATH}", "redirect_uri_invalid"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"https://{API_HOST}/whatsapp/embedded/oauth/callback", "redirect_uri_invalid"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"https://{API_HOST}{CALLBACK_PATH}?next=x", "redirect_uri_invalid"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"https://{API_HOST}:8443{CALLBACK_PATH}", "redirect_uri_invalid"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"https://u:p@{API_HOST}{CALLBACK_PATH}", "redirect_uri_invalid"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"https://{API_HOST}{CALLBACK_PATH}/", "redirect_uri_invalid"),
    ("META_CATALOG_CONSENT_REDIRECT_URI", f"https://{API_HOST.upper()}{CALLBACK_PATH}", "redirect_uri_invalid"),
    ("WA_TOKEN_ENC_KEY", "", "encryption_key_missing"),
    ("WA_TOKEN_ENC_KEY", "not-a-fernet-key", "encryption_key_invalid"),
    ("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{TENANT}:{CATALOG}", "approvals_invalid"),
    ("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{TENANT}:{CATALOG}:{BUSINESS},{TENANT}:{OTHER_CATALOG}:{BUSINESS}",
     "approvals_invalid"),
    ("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{TENANT}:{CATALOG}:{BUSINESS},{OTHER_TENANT}:{CATALOG}:{BUSINESS}",
     "approvals_invalid"),
    ("META_CATALOG_CONSENT_APPROVED_ASSETS", f"{OTHER_TENANT}:{CATALOG}:{BUSINESS}", "tenant_not_approved"),
])
def test_each_precondition_fails_closed(world, monkeypatch, name, value, reason):
    monkeypatch.setenv(name, value)
    out = evaluate_consent_availability(tenant_id=TENANT)
    assert (out.available, out.reason) == (False, reason)


def test_production_dashboard_is_refused(world, monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://app.nahlah.ai")
    out = evaluate_consent_availability(tenant_id=TENANT)
    assert not out.available  # the review guard itself refuses a production dashboard


def test_approvals_parser_never_trusts_ambiguity():
    assert parse_approved_assets("") == {}
    assert parse_approved_assets("1:abc:123456") is None
    assert parse_approved_assets("0:12345:12345") is None
    parsed = parse_approved_assets(f"1:{CATALOG}:{BUSINESS}, 2:{OTHER_CATALOG}:{OTHER_BUSINESS}")
    assert parsed[2].catalog_id == OTHER_CATALOG


def test_status_hidden_when_disabled(world, monkeypatch):
    monkeypatch.setenv("NAHLA_META_CATALOG_CONSENT_ENABLED", "0")
    body = world.client.get("/merchant/catalog/meta-consent/status", headers=_jwt()).json()
    assert body["available"] is False and body["reason"] == "disabled" and "approved" not in body
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt())
    assert resp.status_code == 404
    with world.factory() as s:
        assert s.query(WhatsAppOAuthNonce).count() == 0


# ── entry: authenticated, tenant from JWT, server-side asset ─────────────────

def test_start_requires_authentication(world):
    assert world.client.post("/merchant/catalog/meta-consent/start").status_code == 401


@pytest.mark.parametrize("extra", [{"role": "super_admin"}, {"impersonation": True}])
def test_start_refuses_platform_admin_and_impersonation(world, extra):
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt(**extra))
    assert resp.status_code == 403


def test_start_ignores_caller_tenant_and_asset(world):
    headers = {**_jwt(TENANT), "X-Tenant-ID": str(OTHER_TENANT)}
    resp = world.client.post(
        f"/merchant/catalog/meta-consent/start?tenant_id={OTHER_TENANT}&catalog_id={OTHER_CATALOG}",
        headers=headers, json={"tenant_id": OTHER_TENANT, "catalog_id": OTHER_CATALOG},
    )
    assert resp.status_code == 200
    state = parse_qs(urlsplit(resp.json()["authorize_url"]).query)["state"][0]
    parsed = consent.verify_state(state)
    assert (parsed.tenant_id, parsed.catalog_id, parsed.business_id) == (TENANT, CATALOG, BUSINESS)


def test_unapproved_tenant_cannot_start(world):
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt(OTHER_TENANT))
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "tenant_not_approved"


def test_authorize_url_requests_only_catalog_scopes(world):
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt())
    url = urlsplit(resp.json()["authorize_url"])
    q = parse_qs(url.query)
    assert url.scheme == "https" and url.netloc == "www.facebook.com" and url.path.endswith("/dialog/oauth")
    assert q["scope"] == ["catalog_management,business_management"]
    assert q["redirect_uri"] == [REDIRECT] and q["client_id"] == [APP_ID]
    assert q["config_id"] == ["400000000000001"] and q["response_type"] == ["code"]
    assert "whatsapp" not in resp.text.lower()
    assert resp.headers["cache-control"] == "no-store"


def test_start_fails_closed_without_nonce_table(world):
    WhatsAppOAuthNonce.__table__.drop(world.engine)
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt())
    assert resp.status_code == 503 and resp.json()["detail"]["reason"] == "storage_unavailable"


def test_entitlement_and_sync_scope_are_enforced(world, monkeypatch):
    import services.whatsapp_catalog_sync_scope as scope_mod

    monkeypatch.setenv(scope_mod.TENANT_SCOPE_ENV, str(OTHER_TENANT))
    assert not scope_mod.tenant_in_sync_scope(TENANT)
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt())
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "sync_scope_excluded"
    with world.factory() as s:
        assert s.query(WhatsAppOAuthNonce).count() == 0


# ── state integrity ──────────────────────────────────────────────────────────

def _approval():
    from core.meta_catalog_consent_config import CatalogApproval

    return CatalogApproval(TENANT, CATALOG, BUSINESS)


def test_state_round_trip_and_tampering(world):
    state = consent.sign_state(tenant_id=TENANT, nonce="n" * 20, redirect_uri=REDIRECT, approval=_approval(),
                               app_id=APP_ID)
    assert consent.verify_state(state).catalog_id == CATALOG
    body, sig = state.split(".")
    forged = json.loads(consent._unb64(body))
    forged["t"] = OTHER_TENANT
    forged_body = consent._b64(json.dumps(forged, separators=(",", ":"), sort_keys=True).encode())
    for bad in (f"{forged_body}.{sig}", state[:-3] + "AAA", "garbage", "", f"{body}."):
        with pytest.raises(consent.ConsentError) as exc:
            consent.verify_state(bad)
        assert exc.value.code == consent.C_INVALID_STATE


def test_state_expiry_and_future_issue(world):
    old = int(time.time()) - consent.STATE_TTL_SECONDS - 5
    with pytest.raises(consent.ConsentError) as exc:
        consent.verify_state(consent.sign_state(tenant_id=TENANT, nonce="n" * 20, redirect_uri=REDIRECT,
                                                approval=_approval(), app_id=APP_ID, issued_at=old))
    assert exc.value.code == consent.C_EXPIRED
    future = int(time.time()) + 3600
    with pytest.raises(consent.ConsentError) as exc:
        consent.verify_state(consent.sign_state(tenant_id=TENANT, nonce="n" * 20, redirect_uri=REDIRECT,
                                                approval=_approval(), app_id=APP_ID, issued_at=future))
    assert exc.value.code == consent.C_INVALID_STATE


def test_whatsapp_and_catalog_states_never_cross_verify(world):
    from routers.whatsapp_embedded import _sign_oauth_state, _verify_oauth_state
    from fastapi import HTTPException

    wa_state = _sign_oauth_state(TENANT, "nonce-wa", int(time.time()), REDIRECT, "embedded")
    with pytest.raises(consent.ConsentError):
        consent.verify_state(wa_state)
    cat_state = consent.sign_state(tenant_id=TENANT, nonce="n" * 20, redirect_uri=REDIRECT,
                                   approval=_approval(), app_id=APP_ID)
    with pytest.raises(HTTPException):
        _verify_oauth_state(cat_state)


def test_nonce_purposes_are_isolated(world):
    with pytest.raises(nonce_mod.NonceRejected):
        nonce_mod.normalize_connection_mode("meta_catalog_consent")
    assert nonce_mod.ALLOWED_CONNECTION_MODES == frozenset({"embedded", "coexistence"})
    nonce = secrets.token_urlsafe(16)
    with world.factory() as s:
        nonce_mod.persist_catalog_consent_nonce(
            s, nonce=nonce, tenant_id=TENANT, redirect_uri=REDIRECT, catalog_id=CATALOG,
            business_id=BUSINESS, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        s.commit()
    for mode in ("embedded", "coexistence"):
        with pytest.raises(nonce_mod.NonceRejected):
            nonce_mod.consume_oauth_nonce(nonce=nonce, tenant_id=TENANT, connection_mode=mode, redirect_uri=REDIRECT)
    for kwargs in ({"tenant_id": OTHER_TENANT}, {"catalog_id": OTHER_CATALOG}, {"business_id": OTHER_BUSINESS},
                   {"redirect_uri": REDIRECT + "x"}):
        args = {"nonce": nonce, "tenant_id": TENANT, "redirect_uri": REDIRECT, "catalog_id": CATALOG,
                "business_id": BUSINESS, **kwargs}
        with pytest.raises(nonce_mod.NonceRejected):
            nonce_mod.consume_catalog_consent_nonce(**args)
    assert nonce_mod.consume_catalog_consent_nonce(nonce=nonce, tenant_id=TENANT, redirect_uri=REDIRECT,
                                                   catalog_id=CATALOG, business_id=BUSINESS) > 0
    with pytest.raises(nonce_mod.NonceRejected):
        nonce_mod.consume_catalog_consent_nonce(nonce=nonce, tenant_id=TENANT, redirect_uri=REDIRECT,
                                                catalog_id=CATALOG, business_id=BUSINESS)


# ── callback: success, persistence, no WhatsApp side effects ─────────────────

def test_complete_success_stores_encrypted_authorization_only(world, caplog):
    state = _start(world)
    code = _code(world)
    status, location = _callback(world, code=code, state=state)
    assert status == 302 and _result(location) == "connected"
    rows = _rows(world)
    assert len(rows) == 1
    row = rows[0]
    assert (row.tenant_id, row.catalog_id, row.business_id, row.meta_app_id, row.meta_user_id) == (
        TENANT, CATALOG, BUSINESS, APP_ID, USER_ID)
    assert row.access_token_enc.startswith("enc1:") and world.graph.long not in row.access_token_enc
    assert sorted(row.granted_scopes) == ["business_management", "catalog_management", "public_profile"]
    assert row.status == "active"
    with world.factory() as s:
        assert s.query(WhatsAppConnection).count() == 0
    paths = world.graph.paths()
    assert paths == ["oauth/access_token", "oauth/access_token", "debug_token", "me", "me/permissions",
                     CATALOG, f"{BUSINESS}/owned_product_catalogs", "me/business_users"]
    assert not any(f in p for p in paths for f in _FORBIDDEN_GRAPH_FRAGMENTS)
    exchange = world.graph.calls[0][1]
    assert exchange["redirect_uri"] == REDIRECT and exchange["code"] == code
    for path, params, has_token in world.graph.calls[3:]:
        assert has_token and "appsecret_proof" in params and "access_token" not in params
    _no_secret_in(caplog.text + location, world)
    assert state not in caplog.text
    body = world.client.get("/merchant/catalog/meta-consent/status", headers=_jwt()).json()
    assert body["authorization"]["state"] == "active" and body["approved"]["catalog_id"] == CATALOG
    _no_secret_in(json.dumps(body), world)


def test_success_replay_is_refused_without_a_second_exchange(world):
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "connected"
    calls = len(world.graph.calls)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "replayed"
    assert len(world.graph.calls) == calls


def test_denial_consumes_the_nonce_and_cannot_be_replayed(world):
    state = _start(world)
    status, location = _callback(world, state=state, error="access_denied", error_reason="user_denied",
                                 error_description="<script>alert(1)</script>")
    assert _result(location) == "denied" and "script" not in location and "user_denied" not in location
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "replayed"
    assert world.graph.calls == [] and _rows(world) == []


@pytest.mark.parametrize("params,expected", [
    ({}, "invalid_state"),
    ({"state": "x.y"}, "invalid_state"),
])
def test_missing_or_garbage_state(world, params, expected):
    assert _result(_callback(world, code=_code(world), **params)[1]) == expected
    assert world.graph.calls == []


def test_expired_state_is_refused_before_consumption(world):
    old = int(time.time()) - consent.STATE_TTL_SECONDS - 10
    state = consent.sign_state(tenant_id=TENANT, nonce="n" * 20, redirect_uri=REDIRECT, approval=_approval(),
                               app_id=APP_ID, issued_at=old)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "expired"
    assert world.graph.calls == []


def test_state_without_a_persisted_nonce_is_refused(world):
    state = consent.sign_state(tenant_id=TENANT, nonce=secrets.token_urlsafe(16), redirect_uri=REDIRECT,
                               approval=_approval(), app_id=APP_ID)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "replayed"
    assert world.graph.calls == []


def test_cross_tenant_state_cannot_use_another_tenants_nonce(world, monkeypatch):
    monkeypatch.setenv("META_CATALOG_CONSENT_APPROVED_ASSETS",
                       f"{TENANT}:{CATALOG}:{BUSINESS},{OTHER_TENANT}:{OTHER_CATALOG}:{OTHER_BUSINESS}")
    state = _start(world, OTHER_TENANT)
    parsed = consent.verify_state(state)
    forged = consent.sign_state(tenant_id=TENANT, nonce=parsed.nonce, redirect_uri=REDIRECT,
                                approval=_approval(), app_id=APP_ID)
    assert _result(_callback(world, code=_code(world), state=forged)[1]) == "replayed"
    assert world.graph.calls == [] and _rows(world) == []


@pytest.mark.parametrize("change,expected", [
    ({"META_CATALOG_CONSENT_APPROVED_ASSETS": f"{TENANT}:{OTHER_CATALOG}:{BUSINESS}"}, "asset_changed"),
    ({"META_CATALOG_CONSENT_APPROVED_ASSETS": f"{TENANT}:{CATALOG}:{OTHER_BUSINESS}"}, "asset_changed"),
    ({"META_CATALOG_CONSENT_APPROVED_ASSETS": f"{OTHER_TENANT}:{CATALOG}:{BUSINESS}"}, "not_approved"),
    ({"META_CATALOG_CONSENT_REDIRECT_URI": f"https://other-api.example.test{CALLBACK_PATH}"}, "callback_mismatch"),
    ({"META_APP_ID": "600000000000009"}, "app_mismatch"),
])
def test_changed_configuration_after_start_is_refused_after_consuming(world, monkeypatch, change, expected):
    import os

    state = _start(world)
    original = {key: os.environ[key] for key in change}
    for key, value in change.items():
        monkeypatch.setenv(key, value)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == expected
    assert world.graph.calls == [] and _rows(world) == []
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    # Restoring the configuration does not resurrect the spent nonce.
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "replayed"
    assert world.graph.calls == [] and _rows(world) == []


def test_callback_when_feature_disabled_does_nothing(world, monkeypatch):
    state = _start(world)
    monkeypatch.setenv("NAHLA_META_CATALOG_CONSENT_ENABLED", "0")
    status, location = _callback(world, code=_code(world), state=state)
    assert _result(location) == "unavailable" and world.graph.calls == []


def test_missing_code_is_refused(world):
    state = _start(world)
    assert _result(_callback(world, state=state)[1]) == "missing_code"
    assert world.graph.calls == []


def _debug(world, **changes):
    data = dict(world.graph.debug)
    data.update(changes)
    world.graph.overrides["debug_token"] = (200, {"data": data})


@pytest.mark.parametrize("setup,expected", [
    (lambda w: w.graph.overrides.update({"oauth/access_token": (400, {"error": {"message": "bad code"}})}),
     "token_exchange_failed"),
    (lambda w: _debug(w, is_valid=False), "token_invalid"),
    (lambda w: _debug(w, app_id="600000000000009"), "app_mismatch"),
    (lambda w: _debug(w, type="PAGE"), "token_type_invalid"),
    (lambda w: _debug(w, user_id=""), "token_identity_mismatch"),
    (lambda w: _debug(w, expires_at=int(time.time()) - 10), "token_expired"),
    (lambda w: _debug(w, data_access_expires_at=int(time.time()) - 10), "token_expired"),
    (lambda w: _debug(w, scopes=["catalog_management"]), "scope_missing"),
    (lambda w: _debug(w, scopes=["business_management", "whatsapp_business_management"]), "scope_missing"),
    (lambda w: _debug(w, granular_scopes=[{"scope": "catalog_management", "target_ids": [OTHER_CATALOG]}]),
     "asset_not_granted"),
    (lambda w: _debug(w, granular_scopes=[{"scope": "business_management", "target_ids": [OTHER_BUSINESS]}]),
     "asset_not_granted"),
    (lambda w: w.graph.overrides.update({"me": (200, {"id": "500000000000009"})}), "token_identity_mismatch"),
    (lambda w: w.graph.overrides.update({"me/permissions": (200, {"data": [
        {"permission": "catalog_management", "status": "granted"},
        {"permission": "business_management", "status": "declined"}]})}), "scope_missing"),
    (lambda w: w.graph.overrides.update({CATALOG: (400, {"error": {"code": 100}})}), "catalog_not_accessible"),
    (lambda w: w.graph.overrides.update({CATALOG: (200, {"id": CATALOG, "business": {"id": OTHER_BUSINESS}})}),
     "catalog_owner_mismatch"),
    (lambda w: w.graph.overrides.update({f"{BUSINESS}/owned_product_catalogs": (200, {"data": [{"id": OTHER_CATALOG}]})}),
     "catalog_not_owned_by_business"),
    (lambda w: w.graph.overrides.update({f"{BUSINESS}/owned_product_catalogs": (403, {"error": {"code": 200}})}),
     "catalog_ownership_unverified"),
    (lambda w: w.graph.overrides.update({"me/business_users": (200, {"data": [
        {"id": "9001", "business": {"id": BUSINESS}, "role": "EMPLOYEE"}]})}), "business_admin_missing"),
    (lambda w: w.graph.overrides.update({"me/business_users": (200, {"data": [
        {"id": "9001", "business": {"id": OTHER_BUSINESS}, "role": "ADMIN"}]})}), "business_admin_missing"),
    (lambda w: w.graph.overrides.update({"me/business_users": (200, {"data": []})}), "business_admin_missing"),
    (lambda w: w.graph.overrides.update({"me/business_users": (403, {"error": {"code": 200,
                                                                                "message": "needs permission"}})}),
     "business_admin_unverified"),
])
def test_verification_failures_store_nothing(world, setup, expected, caplog):
    state = _start(world)
    setup(world)
    status, location = _callback(world, code=_code(world), state=state)
    assert _result(location) == expected
    assert _rows(world) == []
    _no_secret_in(caplog.text + location, world)
    assert not any(f in p for p in world.graph.paths() for f in _FORBIDDEN_GRAPH_FRAGMENTS)


def test_admin_role_on_a_later_page_is_found_by_cursor(world):
    pages = []

    def business_users(params):
        pages.append(params.get("after"))
        if not params.get("after"):
            return 200, {"data": [{"id": "9001", "business": {"id": OTHER_BUSINESS}, "role": "ADMIN"}],
                         "paging": {"cursors": {"after": "CURSOR2"}, "next": "https://graph.facebook.com/next"}}
        return 200, {"data": [{"id": "9002", "business": {"id": BUSINESS}, "role": "ADMIN"}]}

    world.graph.overrides["me/business_users"] = business_users
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "connected"
    assert pages == [None, "CURSOR2"]


def test_unbounded_paging_is_not_proof(world):
    world.graph.overrides["me/business_users"] = lambda params: (200, {
        "data": [], "paging": {"cursors": {"after": secrets.token_hex(4)}, "next": "https://graph.facebook.com/n"}})
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "business_admin_unverified"


def test_encryption_failure_stores_nothing(world, monkeypatch):
    import core.wa_token_crypto as crypto

    def _boom(_plain):
        raise RuntimeError("synthetic encryption failure")

    monkeypatch.setattr(crypto, "encrypt_access_token", _boom)
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "encryption_unavailable"
    assert _rows(world) == []


def test_catalog_bound_to_another_tenant_is_refused(world):
    with world.factory() as s:
        s.add(WhatsAppConnection(tenant_id=OTHER_TENANT, meta_catalog_id=CATALOG))
        s.commit()
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "catalog_claimed_by_other_tenant"
    assert _rows(world) == []


def test_catalog_consented_by_another_tenant_blocks_whatsapp_claim(world):
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "connected"
    from services.meta_catalog_claim import CatalogClaimError, guard_catalog_claim

    with world.factory() as s:
        with pytest.raises(CatalogClaimError):
            guard_catalog_claim(s, OTHER_TENANT, CATALOG)
        guard_catalog_claim(s, TENANT, CATALOG)  # its own consent is not a foreign claim


def test_entitlement_missing_at_callback_stores_nothing(world, monkeypatch):
    state = _start(world)
    import core.plan_entitlements as ent_mod

    class _NoEnt:
        def has_feature(self, key):
            return False

    monkeypatch.setattr(ent_mod, "get_entitlements", lambda db, tid, strict_lookup=True: _NoEnt())
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "entitlement_missing"
    assert world.graph.calls == []


def test_unexpected_exception_is_sanitised(world, caplog, monkeypatch):
    secret_text = secrets.token_urlsafe(24)
    world.secrets.append(secret_text)

    async def _explode(**_kw):
        raise ValueError(f"provider said {secret_text}")

    monkeypatch.setattr(consent, "verify_access", _explode)
    state = _start(world)
    status, location = _callback(world, code=_code(world), state=state)
    assert _result(location) == "error"
    _no_secret_in(caplog.text + location, world)


def test_existing_authorization_is_replaced_not_duplicated(world):
    for _ in range(2):
        state = _start(world)
        assert _result(_callback(world, code=_code(world), state=state)[1]) == "connected"
    assert len(_rows(world)) == 1


# ── binding used by the catalog push path ────────────────────────────────────

def _connect(world):
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "connected"


def test_no_authorization_keeps_the_whatsapp_connection_path(world):
    from services.meta_catalog_push import MetaCatalogPushError, _resolve_connection

    with world.factory() as s:
        with pytest.raises(MetaCatalogPushError) as exc:
            _resolve_connection(s, TENANT)
        assert exc.value.code == "connection_not_found"
        s.add(WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=OTHER_CATALOG))
        s.commit()
        conn = _resolve_connection(s, TENANT)
        assert isinstance(conn, WhatsAppConnection)


def test_binding_uses_consent_token_only_for_the_approved_catalog(world, monkeypatch):
    import services.meta_catalog_access as access
    from services.meta_catalog_push import (
        _resolve_catalog_and_token,
        _resolve_connection,
        find_meta_catalog_item_by_retailer_id,
    )

    _connect(world)
    platform = "EAA" + secrets.token_hex(20)
    monkeypatch.setattr(access, "WA_TOKEN", platform)
    with world.factory() as s:
        binding = _resolve_connection(s, TENANT)
    assert consent.is_consent_binding(binding) and binding.meta_catalog_id == CATALOG
    assert binding.catalog_enabled is True
    assert world.graph.long not in repr(binding)
    cid, token = _resolve_catalog_and_token(binding, require_catalog_readable=False)
    assert (cid, token) == (CATALOG, world.graph.long)
    assert access.catalog_token_candidates(binding) == [
        {"token": world.graph.long, "token_source": "merchant_catalog_consent"}]
    from services.meta_catalog_import import _select_graph_token

    assert _select_graph_token(binding)["token"] == world.graph.long
    meta_id, lookup = find_meta_catalog_item_by_retailer_id(binding, OTHER_CATALOG, "SHOE-WHITE-42")
    assert meta_id is None and lookup["error"] == "catalog_not_authorized"
    with pytest.raises(consent.CatalogConsentInactive):
        binding.token_for(OTHER_CATALOG)


def test_whatsapp_connection_on_another_catalog_keeps_its_path(world):
    from services.meta_catalog_push import _resolve_connection

    _connect(world)
    with world.factory() as s:
        s.add(WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=OTHER_CATALOG, catalog_enabled=True))
        s.commit()
        assert isinstance(_resolve_connection(s, TENANT), WhatsAppConnection)


def test_same_catalog_whatsapp_connection_respects_catalog_enabled(world):
    from services.meta_catalog_push import _resolve_connection

    with world.factory() as s:
        s.add(WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=CATALOG, catalog_enabled=False))
        s.commit()
    _connect(world)
    with world.factory() as s:
        binding = _resolve_connection(s, TENANT)
    assert consent.is_consent_binding(binding) and binding.catalog_enabled is False


@pytest.mark.parametrize("change", [
    {"NAHLA_META_CATALOG_CONSENT_ENABLED": "0"},
    {"NAHLA_CATALOG_REVIEW_ENV": "0"},
    {"META_CATALOG_CONSENT_APPROVED_ASSETS": f"{TENANT}:{OTHER_CATALOG}:{BUSINESS}"},
    {"WA_TOKEN_ENC_KEY": Fernet.generate_key().decode()},
])
def test_inactive_authorization_fails_closed_without_fallback(world, monkeypatch, change):
    from services.meta_catalog_push import MetaCatalogPushError, _resolve_connection

    _connect(world)
    for key, value in change.items():
        monkeypatch.setenv(key, value)
    with world.factory() as s:
        s.add(WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=CATALOG, catalog_enabled=True,
                                 access_token="EAA" + secrets.token_hex(20), provider="meta"))
        s.commit()
        with pytest.raises(MetaCatalogPushError) as exc:
            _resolve_connection(s, TENANT)
    assert exc.value.code == "catalog_consent_inactive"


def test_expired_authorization_fails_closed(world):
    from services.meta_catalog_push import MetaCatalogPushError, _resolve_connection

    _connect(world)
    with world.factory() as s:
        row = s.query(MetaCatalogAuthorization).one()
        row.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.commit()
        with pytest.raises(MetaCatalogPushError) as exc:
            _resolve_connection(s, TENANT)
    assert exc.value.code == "catalog_consent_inactive"
    body = world.client.get("/merchant/catalog/meta-consent/status", headers=_jwt()).json()
    assert body["authorization"]["state"] == "expired"


# ── middleware boundary ──────────────────────────────────────────────────────

def test_only_the_exact_callback_is_jwt_public():
    from core.middleware import JWT_PUBLIC_EXACT_PATHS, JWT_PUBLIC_PREFIXES, is_jwt_public_path

    assert CALLBACK_PATH in JWT_PUBLIC_EXACT_PATHS
    assert not any(CALLBACK_PATH.startswith(p) for p in JWT_PUBLIC_PREFIXES)
    for path in ("/merchant/catalog/meta-consent/start", "/merchant/catalog/meta-consent/status",
                 CALLBACK_PATH + "/", CALLBACK_PATH + "x", "/merchant/catalog/meta-consent",
                 "/merchant/catalog/status"):
        assert not is_jwt_public_path(path), path
    assert "/whatsapp/embedded/oauth/callback" in JWT_PUBLIC_EXACT_PATHS


# ── pre-0120 schema: existing modes unaffected; consent fails closed ────────

def _drop_authorization_table(world):
    MetaCatalogAuthorization.__table__.drop(world.engine)
    consent._TABLE_SEEN.clear()


def test_pre_0120_schema_with_feature_disabled_keeps_existing_catalog_paths(world, monkeypatch):
    import services.meta_catalog_access as access
    from services.meta_catalog_claim import CatalogClaimError, guard_catalog_claim
    from services.meta_catalog_push import _resolve_catalog_and_token, _resolve_connection

    _drop_authorization_table(world)
    monkeypatch.setenv("NAHLA_META_CATALOG_CONSENT_ENABLED", "0")
    monkeypatch.delenv("NAHLA_CATALOG_REVIEW_ENV")
    merchant = "EAA" + secrets.token_hex(20)
    platform = "EAA" + secrets.token_hex(20)
    monkeypatch.setattr(access, "WA_TOKEN", platform)
    with world.factory() as s:
        conn = WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=CATALOG, catalog_enabled=True, provider="meta")
        from services.whatsapp_platform.wa_connection_secrets import store_access_token

        store_access_token(conn, merchant)
        s.add(conn)
        s.add(WhatsAppConnection(tenant_id=OTHER_TENANT, meta_catalog_id=OTHER_CATALOG))
        s.commit()
        resolved = _resolve_connection(s, TENANT)
        assert isinstance(resolved, WhatsAppConnection)
        assert _resolve_catalog_and_token(resolved, require_catalog_readable=False) == (CATALOG, merchant)
        assert [c["token"] for c in access.catalog_token_candidates(resolved)] == [merchant, platform]
        guard_catalog_claim(s, TENANT, CATALOG)
        with pytest.raises(CatalogClaimError):
            guard_catalog_claim(s, TENANT, OTHER_CATALOG)
        # The session is still usable: no failed query against a missing table.
        assert s.query(WhatsAppConnection).count() == 2
    body = world.client.get("/merchant/catalog/meta-consent/status", headers=_jwt()).json()
    assert body["available"] is False and body["reason"] == "disabled"


def test_consent_enabled_without_0120_table_fails_closed_before_nonce_or_graph(world):
    _drop_authorization_table(world)
    status = world.client.get("/merchant/catalog/meta-consent/status", headers=_jwt()).json()
    assert status["available"] is False and status["reason"] == "storage_unavailable"
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt())
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "storage_unavailable"
    with world.factory() as s:
        assert s.query(WhatsAppOAuthNonce).count() == 0
    # A state issued before the table disappeared: refused before any Graph call.
    nonce = secrets.token_urlsafe(16)
    with world.factory() as s:
        nonce_mod.persist_catalog_consent_nonce(
            s, nonce=nonce, tenant_id=TENANT, redirect_uri=REDIRECT, catalog_id=CATALOG, business_id=BUSINESS,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        s.commit()
    state = consent.sign_state(tenant_id=TENANT, nonce=nonce, redirect_uri=REDIRECT, approval=_approval(),
                               app_id=APP_ID)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "storage_unavailable"
    assert world.graph.calls == []


# ── logging and Sentry: no code / state / token / secret leaves the process ──

def _credential_url(world) -> Tuple[str, Dict[str, str]]:
    values = {
        "code": secrets.token_urlsafe(40),
        "state": secrets.token_urlsafe(60),
        "client_secret": secrets.token_urlsafe(32),
        "input_token": "EAA" + secrets.token_hex(24),
        "access_token": "EAA" + secrets.token_hex(24),
        "fb_exchange_token": "EAA" + secrets.token_hex(24),
        "appsecret_proof": secrets.token_hex(32),
    }
    world.secrets.extend(values.values())
    query = "&".join(f"{k}={v}" for k, v in values.items())
    return query, values


def test_uvicorn_access_line_for_the_callback_is_redacted(world):
    from core.log_redaction import SecretRedactingFilter

    query, values = _credential_url(world)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:443", "GET", f"{CALLBACK_PATH}?{query}", "1.1", 302), None,
    )
    assert SecretRedactingFilter().filter(record)
    line = record.getMessage()
    assert CALLBACK_PATH in line
    _no_secret_in(line, world)


def test_httpx_url_objects_in_log_arguments_are_redacted(world):
    import httpx

    from core.log_redaction import SecretRedactingFilter

    query, _values = _credential_url(world)
    url = httpx.URL(f"https://graph.facebook.com/v20.0/oauth/access_token?{query}")
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"',
                               ("GET", url, "HTTP/1.1 200 OK"), None)
    assert SecretRedactingFilter().filter(record)
    _no_secret_in(record.getMessage(), world)


def test_raw_asgi_query_preview_is_redacted_before_truncation(world):
    from core.log_redaction import redacted_query_preview

    query, _values = _credential_url(world)
    for raw in (query.encode("latin-1"), query):
        preview = redacted_query_preview(raw, limit=80)
        assert len(preview) <= 80
        _no_secret_in(preview, world)
    # A value cut by truncation alone would still leak a prefix of the code.
    code = secrets.token_urlsafe(120)
    world.secrets.append(code[:40])
    _no_secret_in(redacted_query_preview(f"code={code}".encode(), limit=60), world)


def _sentry_event(world) -> Dict[str, Any]:
    query, values = _credential_url(world)
    return {
        "level": "error",
        "request": {
            "url": f"https://{API_HOST}{CALLBACK_PATH}?{query}",
            "query_string": query,
            "method": "GET",
            "headers": {"Authorization": f"Bearer {values['access_token']}", "Referer": f"https://x/?{query}"},
            "data": {"client_secret": values["client_secret"], "nested": {"code": values["code"]}},
        },
        "exception": {"values": [{
            "type": "ValueError",
            "value": f"Graph said bad token {values['input_token']} for {CALLBACK_PATH}?{query}",
            "stacktrace": {"frames": [{"function": "consent_callback", "vars": {
                "code": values["code"], "state": values["state"], "token": values["access_token"],
                "app_secret": values["client_secret"],
                "params": {"client_secret": values["client_secret"], "fb_exchange_token": values["fb_exchange_token"]},
                "url": f"https://graph.facebook.com/v20.0/debug_token?{query}",
            }}]},
        }]},
        "breadcrumbs": {"values": [
            {"type": "http", "category": "httpx", "data": {
                "url": f"https://graph.facebook.com/v20.0/oauth/access_token?{query}",
                "http.query": query, "method": "GET"}},
            {"category": "log", "message": f"callback {CALLBACK_PATH}?{query}"},
        ]},
        "logentry": {"message": "callback %s", "params": [f"?{query}"], "formatted": f"callback ?{query}"},
        "extra": {"redirect": f"{CALLBACK_PATH}?{query}", "access_token": values["access_token"]},
        "spans": [{"op": "http.client", "description": f"GET https://graph.facebook.com/v20.0/me?{query}",
                   "data": {"url": f"https://graph.facebook.com/v20.0/me?{query}"}}],
    }


def test_sentry_before_send_scrubs_callback_and_graph_credentials(world):
    from core.observability_sentry import _before_send

    out = _before_send(_sentry_event(world), {})
    text = json.dumps(out)
    _no_secret_in(text, world)
    assert CALLBACK_PATH in out["request"]["url"]  # the event keeps its diagnostic shape
    assert out["exception"]["values"][0]["type"] == "ValueError"
    assert out["request"]["headers"]["Authorization"] == "[scrubbed]"


def test_sentry_transactions_and_breadcrumbs_are_scrubbed_too(world):
    from core.observability_sentry import _before_breadcrumb, _before_send

    event = _sentry_event(world)
    crumb = event["breadcrumbs"]["values"][0]
    _no_secret_in(json.dumps(_before_breadcrumb(dict(crumb), {})), world)
    event["type"] = "transaction"
    _no_secret_in(json.dumps(_before_send(event, {})), world)


def test_sentry_scrub_failure_withholds_the_payload(world, monkeypatch):
    import core.observability_sentry as sentry_mod

    def _broken(_event):
        raise RuntimeError("scrubber bug")

    monkeypatch.setattr(sentry_mod, "_scrub_event", _broken)
    event = _sentry_event(world)
    out = sentry_mod._before_send(event, {})
    _no_secret_in(json.dumps(out), world)
    assert out["tags"] == {"scrub_failed": "true"}
    assert out["extra"] == {"exception_types": ["ValueError"]}


def test_sentry_init_registers_every_scrub_hook(monkeypatch):
    import core.observability_sentry as sentry_mod
    import sentry_sdk

    captured = {}
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.invalid/1")
    monkeypatch.setattr(sentry_mod, "_INITIALISED", False)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    assert sentry_mod.init_sentry() is True
    assert captured["before_send"] is sentry_mod._before_send
    assert captured["before_send_transaction"] is sentry_mod._before_send
    assert captured["before_breadcrumb"] is sentry_mod._before_breadcrumb
    assert captured["send_default_pii"] is False


# ── independent review round: app identity, key redaction, encoded keys ────

def test_stored_consent_for_another_app_fails_closed_before_decryption(world, monkeypatch):
    import services.meta_catalog_access as access
    from services.meta_catalog_push import MetaCatalogPushError, _resolve_connection
    from services.whatsapp_platform.wa_connection_secrets import store_access_token

    _connect(world)
    merchant = "EAA" + secrets.token_hex(20)
    platform = "EAA" + secrets.token_hex(20)
    world.secrets += [merchant, platform]
    monkeypatch.setattr(access, "WA_TOKEN", platform)
    with world.factory() as s:
        conn = WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=CATALOG, catalog_enabled=True, provider="meta")
        store_access_token(conn, merchant)
        s.add(conn)
        s.commit()
    monkeypatch.setenv("META_APP_ID", "600000000000009")

    def _no_decrypt(_stored):
        raise AssertionError("token decrypted for a consent issued to another app")

    monkeypatch.setattr(consent, "_decrypt", _no_decrypt)
    with world.factory() as s:
        with pytest.raises(MetaCatalogPushError) as exc:
            _resolve_connection(s, TENANT)
    assert exc.value.code == "catalog_consent_inactive"
    assert exc.value.detail == {"reason": "app_changed"}
    body = world.client.get("/merchant/catalog/meta-consent/status", headers=_jwt()).json()
    assert body["authorization"]["state"] == "inactive"
    assert body["authorization"]["inactive_reason"] == "app_changed"
    assert world.graph.paths().count("oauth/access_token") == 2  # only the original consent


def test_encryption_key_names_and_values_never_escape_redaction(world):
    from core.log_redaction import SecretRedactingFilter, is_sensitive_key, redact_secrets

    key = Fernet.generate_key().decode()
    totp = Fernet.generate_key().decode()
    world.secrets += [key, totp]
    for name in ("WA_TOKEN_ENC_KEY", "TOTP_ENC_KEY", "wa_token_enc_key", "ENCRYPTION_KEY", "fernet_key"):
        assert is_sensitive_key(name), name
    for text in (f"WA_TOKEN_ENC_KEY={key}", f"{{'WA_TOKEN_ENC_KEY': '{key}', 'TOTP_ENC_KEY': '{totp}'}}",
                 f"key={key}", f"loaded {key} ok", f'"wa_token_enc_key": "{key}"'):
        _no_secret_in(redact_secrets(text), world)
    record = logging.LogRecord("nahla", logging.WARNING, __file__, 1, "env %s key %s",
                               ({"WA_TOKEN_ENC_KEY": key}, key), None)
    assert SecretRedactingFilter().filter(record)
    _no_secret_in(record.getMessage(), world)


def _frame(module: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    return {"function": "f", "module": module, "filename": module.replace(".", "/") + ".py",
            "abs_path": "/app/backend/" + module.replace(".", "/") + ".py", "vars": variables}


def test_sentry_withholds_sensitive_frames_and_redacts_key_locals_elsewhere(world):
    from core.observability_sentry import _before_send

    key = Fernet.generate_key().decode()
    code = secrets.token_urlsafe(30)
    world.secrets += [key, code]
    frames = [
        _frame("services.meta_catalog_consent", {"tenant_id": "1", "nonce": "x", "anything": code}),
        _frame("routers.meta_catalog_consent", {"parsed": f"ConsentState(n={code})"}),
        _frame("core.wa_token_crypto", {"wa_key": key}),
        _frame("services.unrelated_module", {"key": key, "e": {"WA_TOKEN_ENC_KEY": key},
                                             "e_repr": f"{{'WA_TOKEN_ENC_KEY': '{key}'}}",
                                             "tenant_id": "7", "attempt": "2"}),
    ]
    event = {"exception": {"values": [{"type": "RuntimeError", "value": "x",
                                       "stacktrace": {"frames": frames}}]}}
    out = _before_send(event, {})
    _no_secret_in(json.dumps(out), world)
    out_frames = out["exception"]["values"][0]["stacktrace"]["frames"]
    for f in out_frames[:3]:
        assert f["vars"] == {"[withheld]": "sensitive frame"}
    assert out_frames[3]["vars"]["tenant_id"] == "7" and out_frames[3]["vars"]["attempt"] == "2"
    assert out_frames[3]["vars"]["key"] == "[scrubbed]"
    assert out_frames[3]["function"] == "f" and out_frames[0]["module"] == "services.meta_catalog_consent"


_ENCODED_KEY_CASES = [
    ("co%64e", "code"), ("sta%74e", "state"), ("%63%6F%64%65", "code"), ("CODE", "code"), ("State", "state"),
    ("ACCESS_TOKEN", "access_token"), ("access%5Ftoken", "access_token"), ("Client%5FSecret", "client_secret"),
    ("input+token", None), ("fb_exchange_%74oken", "fb_exchange_token"), ("TOKEN", "token"),
]


def _encoded_query(world) -> str:
    parts = []
    for raw_key, _decoded in _ENCODED_KEY_CASES:
        value = secrets.token_urlsafe(24)
        if _decoded:
            world.secrets.append(value)
        parts.append(f"{raw_key}={value}")
    parts.append("tenant=7")
    return "&".join(parts)


def test_uvicorn_access_line_redacts_percent_encoded_and_mixed_case_keys(world):
    from core.log_redaction import SecretRedactingFilter

    query = _encoded_query(world)
    args = ("203.0.113.9:443", "GET", f"{CALLBACK_PATH}?{query}", "1.1", 302)
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    assert SecretRedactingFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5 and record.args[4] == 302
    line = record.getMessage()
    _no_secret_in(line, world)
    assert CALLBACK_PATH in line and "tenant=7" in line


def test_raw_query_preview_redacts_encoded_keys_and_fails_closed_on_malformed(world):
    from core.log_redaction import redact_raw_query, redacted_query_preview

    query = _encoded_query(world)
    for raw in (query, query.encode("latin-1")):
        _no_secret_in(redacted_query_preview(raw, limit=4096), world)
        _no_secret_in(redacted_query_preview(raw, limit=80), world)
    malformed_value = secrets.token_urlsafe(24)
    world.secrets.append(malformed_value)
    assert redact_raw_query(f"co%ffde={malformed_value}&x=1") == "REDACTED"
    _no_secret_in(redacted_query_preview(f"co%ffde={malformed_value}".encode()), world)
    assert redact_raw_query("tenant=7&page=2") == "tenant=7&page=2"


def test_sentry_redacts_percent_encoded_keys_in_query_string_and_breadcrumbs(world):
    from core.observability_sentry import _before_breadcrumb, _before_send

    query = _encoded_query(world)
    bad_value = secrets.token_urlsafe(24)
    world.secrets.append(bad_value)
    raw = f"{query}&co%zzde={bad_value}"
    event = {
        "request": {"url": f"https://{API_HOST}{CALLBACK_PATH}", "query_string": raw},
        "breadcrumbs": {"values": [{"type": "http", "data": {"http.query": raw, "url": f"{CALLBACK_PATH}?{raw}"}},
                                   {"category": "query", "message": raw}]},
    }
    out = _before_send(event, {})
    _no_secret_in(json.dumps(out), world)
    assert "tenant=7" in out["request"]["query_string"]
    crumb = _before_breadcrumb({"type": "http", "data": {"http.query": raw}}, {})
    _no_secret_in(json.dumps(crumb), world)
    assert "tenant=7" in crumb["data"]["http.query"]


# ── auth-scheme markers survive no earlier value pass ───────────────────────

# Marker chains: same-marker recursion and mixed chains, where a
# non-overlapping scan would take the inner marker as the outer credential.
_AUTH_MARKER_CHAINS = (
    "Bearer {v}", "Authorization: {v}", "Cookie: {v}", "Proxy-Authorization: {v}", "Set-Cookie: {v}",
    "Bearer Bearer {v}", "Authorization: Authorization: {v}", "Cookie: Cookie: {v}",
    "Authorization:Authorization: {v}", "Bearer x=Bearer {v}", "Bearer x=Authorization: {v}",
    "Authorization: x=Bearer {v}", "Cookie: Bearer x=Cookie: {v}", "Authorization: Bearer Bearer {v}",
    "Cookie: \"{v}\"", "Bearer Authorization= Cookie: \"{v}\"",
    # A marker's value that is itself a sensitive key: the key pass still sees it.
    "Cookie: secret=  {v}", "Authorization: x_token:  {v}",
)
# Contexts whose own value pass could otherwise consume the first marker.
_AUTH_MARKER_CONTEXTS = (
    "{c}", "co%64e={c}", "x%={c}", "a%zz={c}", "q=1&co%64e={c}", "sta%74e={c}", "code={c}",
    "/p?code={c}", "/p?co%64e={c}", "https://h.example/p?code={c}", "https://h.example/p?state={c}",
    "{\"code\": \"{c}\"}", "secret={c}&x=1",
)


def test_auth_marker_after_a_redacted_key_never_leaves_its_credential(world):
    """A URL / request-target / encoded-key / plain-key pass must not consume
    ``Bearer`` or ``Authorization:`` and leave the credential after it."""
    from core.log_redaction import (SecretRedactingFilter, redact_raw_query, redact_secrets,
                                    redacted_query_preview)
    from core.observability_sentry import _before_breadcrumb, _before_send

    shapes = [ctx.replace("{c}", chain) for ctx in _AUTH_MARKER_CONTEXTS for chain in _AUTH_MARKER_CHAINS]
    assert len(shapes) == len(_AUTH_MARKER_CONTEXTS) * len(_AUTH_MARKER_CHAINS)
    for shape in shapes:
        value = secrets.token_urlsafe(24)
        world.secrets.append(value)
        text = shape.replace("{v}", value)
        _no_secret_in(redact_secrets(text), world)
        _no_secret_in(redacted_query_preview(text, limit=4096), world)
        _no_secret_in(redact_secrets(redact_raw_query(text)), world)
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                   ("203.0.113.9:443", "GET", text, "1.1", 302), None)
        assert SecretRedactingFilter().filter(record)
        _no_secret_in(record.getMessage(), world)
        plain = logging.LogRecord("httpx", logging.INFO, __file__, 1, "%s", (text,), None)
        assert SecretRedactingFilter().filter(plain)
        _no_secret_in(plain.getMessage(), world)
        event = {
            "request": {"url": f"https://{API_HOST}{CALLBACK_PATH}?{text}", "query_string": text},
            "message": text, "logentry": {"message": text, "params": [text]},
            "exception": {"values": [{"type": "ValueError", "value": text}]},
            "extra": {"detail": text},
            "breadcrumbs": {"values": [{"type": "http", "data": {"http.query": text, "url": text}},
                                       {"category": "log", "message": text}]},
        }
        _no_secret_in(json.dumps(_before_send(event, {})), world)
        crumb = _before_breadcrumb({"type": "http", "message": text, "data": {"http.query": text, "url": text}}, {})
        _no_secret_in(json.dumps(crumb), world)


# ── previously surviving mutants (N4) ────────────────────────────────────────

def test_persist_read_back_mismatch_is_unverified_never_connected(world, monkeypatch):
    """The committed row must read back as exactly the verified grant before
    the callback may report success (a skipped read-back would say connected)."""
    monkeypatch.setattr(consent, "_decrypt", lambda _stored: "read-back-differs")
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "persist_unverified"


@pytest.mark.parametrize("dedicated", ["missing", "invalid"])
def test_encrypt_refuses_any_key_but_the_dedicated_one(world, monkeypatch, dedicated):
    """``wa_token_crypto`` falls back to TOTP_ENC_KEY / a JWT-derived key in
    dev; the consent token is only ever encrypted with WA_TOKEN_ENC_KEY."""
    if dedicated == "missing":
        monkeypatch.delenv("WA_TOKEN_ENC_KEY", raising=False)
    else:
        monkeypatch.setenv("WA_TOKEN_ENC_KEY", "not-a-fernet-key")
    monkeypatch.setenv("TOTP_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(consent.ConsentError) as exc:
        consent._encrypt("EAA" + secrets.token_hex(24))
    assert exc.value.code == consent.C_ENCRYPTION_UNAVAILABLE


def test_consent_nonce_is_stored_under_its_own_purpose(world):
    nonce = secrets.token_urlsafe(16)
    with world.factory() as s:
        nonce_mod.persist_catalog_consent_nonce(
            s, nonce=nonce, tenant_id=TENANT, redirect_uri=REDIRECT, catalog_id=CATALOG,
            business_id=BUSINESS, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        s.commit()
        modes = {row.connection_mode for row in s.query(WhatsAppOAuthNonce).all()}
    assert modes == {nonce_mod.CATALOG_CONSENT_NONCE_PURPOSE} == {"meta_catalog_consent"}
    assert not modes & nonce_mod.ALLOWED_CONNECTION_MODES


def test_dashboard_return_url_refuses_production_hosts_on_its_own():
    """Independent of the review-environment guard: a production dashboard is
    never a consent return target."""
    from core.meta_catalog_consent_config import _PRODUCTION_HOSTS, dashboard_return_url

    assert _PRODUCTION_HOSTS
    for host in sorted(_PRODUCTION_HOSTS):
        assert dashboard_return_url({"DASHBOARD_URL": f"https://{host}"}) is None
    assert dashboard_return_url({"DASHBOARD_URL": "https://review.example.org"}) == "https://review.example.org/catalog"


def test_consent_governs_only_its_own_catalog(world):
    """A stored consent governs the WhatsApp connection on the same catalog
    (or with none); another catalog's WhatsApp path is untouched."""
    now = datetime.now(timezone.utc)
    with world.factory() as s:
        s.add(MetaCatalogAuthorization(
            tenant_id=TENANT, catalog_id=CATALOG, business_id=BUSINESS, meta_app_id=APP_ID, meta_user_id=USER_ID,
            access_token_enc="enc1:placeholder", granted_scopes=["catalog_management", "business_management"],
            status="active", verified_at=now, created_at=now, updated_at=now))
        s.commit()
        consent._TABLE_SEEN.clear()
        same = WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=CATALOG)
        other = WhatsAppConnection(tenant_id=TENANT, meta_catalog_id=OTHER_CATALOG)
        assert consent.consent_governs_catalog(s, TENANT, same) is True
        assert consent.consent_governs_catalog(s, TENANT, None) is True
        assert consent.consent_governs_catalog(s, TENANT, other) is False
        assert consent.consent_governs_catalog(s, OTHER_TENANT, same) is False


def test_cached_schema_presence_expires_after_a_downgrade(world, monkeypatch):
    """A positive schema answer is cached for a bounded time only: once the
    table is gone (0120 downgraded under a running process) the next
    inspection reports it absent instead of querying a missing table forever."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(consent.time, "monotonic", lambda: clock["t"])
    consent._TABLE_SEEN.clear()
    with world.factory() as s:
        assert consent.authorization_schema_state(s) == consent.SCHEMA_PRESENT
    MetaCatalogAuthorization.__table__.drop(world.engine)
    clock["t"] += 299  # a fixed bound: presence is cached for at most five minutes
    with world.factory() as s:
        assert consent.authorization_schema_state(s) == consent.SCHEMA_PRESENT  # still within the window
    clock["t"] += 2
    with world.factory() as s:
        assert consent.authorization_schema_state(s) == consent.SCHEMA_ABSENT


# ── further mutation gaps (final review) ─────────────────────────────────────

def _forged_state(**overrides) -> str:
    """A correctly signed consent state whose payload differs from sign_state's."""
    iat = int(time.time())
    payload = {"v": consent.STATE_VERSION, "p": consent.PURPOSE, "t": TENANT, "n": secrets.token_urlsafe(16),
               "iat": iat, "exp": iat + consent.STATE_TTL_SECONDS, "ru": REDIRECT, "c": CATALOG, "b": BUSINESS,
               "a": APP_ID}
    payload.update(overrides)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{consent._b64(body)}.{consent._b64(consent._state_mac(body))}"


@pytest.mark.parametrize("overrides", [
    {"p": "embedded"},                                             # another purpose under the same key
    {"exp": int(time.time()) + 10 * consent.STATE_TTL_SECONDS},    # lifetime other than the fixed TTL
    {"c": ""}, {"b": ""}, {"a": ""}, {"ru": ""}, {"n": ""},        # a missing binding field
])
def test_signed_state_with_wrong_purpose_lifetime_or_missing_binding_is_refused(world, overrides):
    assert consent.verify_state(_forged_state()).tenant_id == TENANT  # the forge itself is valid
    with pytest.raises(consent.ConsentError) as exc:
        consent.verify_state(_forged_state(**overrides))
    assert exc.value.code == consent.C_INVALID_STATE


def test_encrypt_refuses_output_that_is_not_an_enc1_round_trip(world, monkeypatch):
    import core.wa_token_crypto as crypto

    monkeypatch.setattr(crypto, "encrypt_access_token", lambda plain: plain)  # would store plaintext
    with pytest.raises(consent.ConsentError) as exc:
        consent._encrypt("EAA" + secrets.token_hex(24))
    assert exc.value.code == consent.C_ENCRYPTION_UNAVAILABLE


def test_decrypt_never_returns_a_value_that_was_not_stored_encrypted(world):
    plain = "EAA" + secrets.token_hex(24)
    assert consent._decrypt(plain) is None
    assert consent._decrypt(consent._encrypt(plain)) == plain


def test_non_active_row_status_is_inactive_before_anything_else(world):
    class _Row:
        status = "revoked"

    assert consent._row_inactive_reason(_Row(), datetime.now(timezone.utc)) == "authorization_inactive"


def test_callback_for_a_deleted_tenant_reports_tenant_missing(world):
    state = _start(world)
    with world.factory() as s:
        s.query(Tenant).filter(Tenant.id == TENANT).delete()
        s.commit()
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "tenant_missing"
    assert _rows(world) == []


def test_finish_never_reflects_an_unknown_code():
    location = router_mod._finish(RETURN, "<script>x</script>").headers["location"]
    assert location == RETURN + "#meta_catalog_consent=error"


@pytest.mark.parametrize("url", [
    "https://review.example.org:8443/merchant/catalog/meta-consent/callback",
    "https://user:pw@review.example.org/merchant/catalog/meta-consent/callback",
    "https://user@review.example.org/merchant/catalog/meta-consent/callback",
])
def test_callback_uri_with_a_port_or_credentials_is_refused(url):
    from core.meta_catalog_consent_config import canonical_redirect_uri, dashboard_return_url

    assert canonical_redirect_uri({"META_CATALOG_CONSENT_REDIRECT_URI": url}) is None
    origin = url.split("/merchant/")[0]
    assert dashboard_return_url({"DASHBOARD_URL": origin}) is None


def _consent_nonce(world, *, expires_in: timedelta) -> str:
    nonce = secrets.token_urlsafe(16)
    with world.factory() as s:
        nonce_mod.persist_catalog_consent_nonce(
            s, nonce=nonce, tenant_id=TENANT, redirect_uri=REDIRECT, catalog_id=CATALOG,
            business_id=BUSINESS, expires_at=datetime.now(timezone.utc) + expires_in)
        s.commit()
    return nonce


def test_consent_nonce_consume_requires_its_purpose_and_an_unexpired_row(world):
    kwargs = {"tenant_id": TENANT, "redirect_uri": REDIRECT, "catalog_id": CATALOG, "business_id": BUSINESS}
    relabelled = _consent_nonce(world, expires_in=timedelta(minutes=5))
    with world.factory() as s:
        s.query(WhatsAppOAuthNonce).update({WhatsAppOAuthNonce.connection_mode: "embedded"})
        s.commit()
    with pytest.raises(nonce_mod.NonceRejected):
        nonce_mod.consume_catalog_consent_nonce(nonce=relabelled, **kwargs)
    expired = _consent_nonce(world, expires_in=timedelta(minutes=-1))
    with pytest.raises(nonce_mod.NonceRejected):
        nonce_mod.consume_catalog_consent_nonce(nonce=expired, **kwargs)


def test_catalog_binding_fingerprint_is_domain_separated_from_the_redirect_fingerprint(world):
    joined = "\n".join([REDIRECT, CATALOG, BUSINESS])
    assert nonce_mod.catalog_consent_binding_fingerprint(REDIRECT, CATALOG, BUSINESS) != \
        nonce_mod.fingerprint_redirect_uri(joined)


def test_push_with_a_binding_probes_readability_with_the_consent_token(world, monkeypatch):
    from services import meta_catalog_push as push

    token = "EAAC" + secrets.token_hex(20)
    binding = consent.CatalogConsentBinding(tenant_id=TENANT, meta_catalog_id=CATALOG, business_id=BUSINESS,
                                            catalog_enabled=True, access_token=token)
    seen = []

    def _probe(tok, catalog_id, client=None):
        seen.append((tok, catalog_id))
        return {"ok": False, "error": "meta_http_error"}

    monkeypatch.setattr(push, "probe_catalog_readable", _probe)
    with pytest.raises(push.MetaCatalogPushError) as exc:
        push._resolve_catalog_and_token(binding, require_catalog_readable=True)
    assert exc.value.code == "catalog_permission_denied" and seen == [(token, CATALOG)]
    assert push._resolve_catalog_and_token(binding, require_catalog_readable=False) == (CATALOG, token)
    assert len(seen) == 1


def _migration_0120():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "database/migrations/versions/0120_meta_catalog_authorizations.py"
    spec = importlib.util.spec_from_file_location("migration_0120_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _InspectorWithExtras:
    """A real inspector plus one injected drift (check constraint or partial unique index)."""

    def __init__(self, real, *, checks=(), extra_indexes=()):
        self._real, self._checks, self._extra = real, list(checks), list(extra_indexes)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get_check_constraints(self, table):
        return list(self._real.get_check_constraints(table)) + self._checks

    def get_indexes(self, table):
        return list(self._real.get_indexes(table)) + self._extra


def test_0120_adoption_refuses_a_check_constraint_or_partial_unique_index(world):
    """Offline complement to the PG drift proofs: adoption of a pre-existing
    table is refused when it carries constraints this revision never creates."""
    from sqlalchemy import inspect as sa_inspect

    migration = _migration_0120()
    real = sa_inspect(world.engine)
    baseline = set(migration.existing_table_mismatches(real))
    with_check = set(migration.existing_table_mismatches(
        _InspectorWithExtras(real, checks=[{"name": "ck_extra", "sqltext": "status <> ''"}])))
    assert with_check - baseline == {"unexpected check constraint"}
    partial = {"name": "ux_partial", "column_names": ["business_id"], "unique": True,
               "dialect_options": {"postgresql_where": "status = 'active'"}}
    with_partial = set(migration.existing_table_mismatches(_InspectorWithExtras(real, extra_indexes=[partial])))
    assert "partial unique index present" in with_partial - baseline


@pytest.mark.parametrize("shape", [
    "Bearer https://h.example/p?a;{v}",         # a credential holding a URL extends over the whole URL
    "secret=https://h.example/p?a;{v}",
    ")https://h.example/p?code#{v}",            # URL normalization exposes a key; the key pass runs again
    "https://h.example/p?sta%74etoken {v}",
])
def test_redaction_keeps_every_base_redaction(world, shape):
    """Shapes the pre-PR redactor removed: the reordered pipeline must too."""
    from core.log_redaction import redact_secrets

    value = secrets.token_urlsafe(18)
    world.secrets.append(value)
    _no_secret_in(redact_secrets(shape.replace("{v}", value)), world)


def test_raw_query_redacts_a_bearer_credential_that_runs_past_an_ampersand(world):
    """The marker pass runs before the per-pair split, so a sensitive pair
    replaced up to ``&`` never leaves the rest of a Bearer credential."""
    from core.log_redaction import redact_raw_query

    first, rest = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
    world.secrets += [first, rest]
    _no_secret_in(redact_raw_query(f"code=Bearer {first}&{rest}"), world)


@pytest.mark.parametrize("boundary", ['"', "'", "(", "[", "=", ",", ";", " ", ""])
def test_bare_target_after_any_boundary_fails_closed_on_an_undecodable_query(world, boundary):
    """A request target quoted or bracketed in a log line is still a target:
    an undecodable query key redacts the whole query."""
    from core.log_redaction import redact_secrets

    value = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    world.secrets.append(value)
    _no_secret_in(redact_secrets(f"{boundary}/p?{value}%ff%fe=1"), world)


def test_raw_preview_redacts_before_truncating_unkeyed_credentials(world):
    """Truncating first would cut a Fernet key or Meta token below its
    pattern length and leak the prefix."""
    from core.log_redaction import redacted_query_preview

    fernet = Fernet.generate_key().decode()
    meta = "EAA" + secrets.token_hex(24)
    world.secrets += [fernet[:16], meta[:18]]
    _no_secret_in(redacted_query_preview(f"k={secrets.token_hex(2)}&x {fernet}", limit=30), world)
    _no_secret_in(redacted_query_preview(f"x {meta}", limit=22), world)


def test_header_credential_containing_an_ampersand_is_redacted_whole(world):
    from core.log_redaction import redact_secrets

    first, rest = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
    world.secrets += [first, rest]
    for header in ("Authorization: ", "Proxy-Authorization: ", "Cookie: ", "Set-Cookie: "):
        _no_secret_in(redact_secrets(f"{header}{first}&{rest}"), world)


def test_sentry_scrubs_thread_stack_frames_too(world):
    """Thread stacktraces carry the same frame variables as exceptions."""
    from core.observability_sentry import _before_send

    secret_local, token_local = secrets.token_urlsafe(20), "EAA" + secrets.token_hex(24)
    world.secrets += [secret_local, token_local]
    event = {"threads": {"values": [{"stacktrace": {"frames": [
        {"module": "services.meta_catalog_consent", "function": "exchange_code", "vars": {"code": secret_local}},
        {"module": "services.catalog_misc", "function": "f", "vars": {"access_token": token_local, "n": 3}},
    ]}}]}}
    out = _before_send(event, {})
    _no_secret_in(json.dumps(out), world)


# ── final review B1 / transaction field / S2 ─────────────────────────────────

_COLON_KEY_SHAPES = (
    "Authorization:k=,{v}", "Authorization: k=,{v}", "Authorization:k=;{v}", "Cookie:k=,{v}", "Cookie:sid=1,{v}",
    "cookie:a=b;{v}", "Set-Cookie:k=,{v}", "code=Bearer k=,{v}", "secret:x=,{v}",
)
_COLON_KEY_CONTEXTS = (
    "https://h.example/cb?{s}", "https://h.example/cb?x=1&{s}", "https://h.example/cb#{s}", "/p?{s}",
)


def test_marker_or_key_with_a_separator_inside_a_url_never_leaks(world):
    """B1: an earlier pass must never move credential text into a query key
    that a URL parser prints back (``?Authorization:k=,<cred>``)."""
    from core.log_redaction import (SecretRedactingFilter, redact_raw_query, redact_secrets,
                                    redacted_query_preview)
    from core.observability_sentry import _before_breadcrumb, _before_send

    for shape in _COLON_KEY_SHAPES:
        for ctx in _COLON_KEY_CONTEXTS:
            for value in (secrets.token_urlsafe(18), Fernet.generate_key().decode()):
                world.secrets.append(value)
                url = ctx.replace("{s}", shape.replace("{v}", value))
                query = url.split("?", 1)[-1]
                _no_secret_in(redact_secrets(url), world)
                _no_secret_in(redacted_query_preview(url, limit=4096), world)
                _no_secret_in(redact_secrets(redact_raw_query(query)), world)
                record = logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"',
                                           ("GET", url, "HTTP/1.1 200 OK"), None)
                assert SecretRedactingFilter().filter(record)
                _no_secret_in(record.getMessage(), world)
                _no_secret_in(json.dumps(_before_breadcrumb({"type": "http", "data": {"url": url}}, {})), world)
                event = {"request": {"url": url, "query_string": query},
                         "exception": {"values": [{"type": "HTTPError", "value": url}]}}
                _no_secret_in(json.dumps(_before_send(event, {})), world)
                _no_secret_in(json.dumps(_before_send({"type": "transaction", "transaction": url}, {})), world)


def test_sentry_scrubs_transaction_culprit_and_tags(world):
    """Performance events reuse before_send; their transaction name (and an
    error's culprit) can be the request URL with its query."""
    from core.observability_sentry import _before_send

    value = secrets.token_urlsafe(24)
    world.secrets.append(value)
    url = f"https://{API_HOST}{CALLBACK_PATH}?co%64e={value}&tenant=7"
    out = _before_send({"type": "transaction", "transaction": url, "culprit": url, "tags": {"url": url},
                        "request": {"url": url}}, {})
    _no_secret_in(json.dumps(out), world)
    assert out["transaction"].startswith(f"https://{API_HOST}{CALLBACK_PATH}")


def test_claim_lock_failure_is_a_storage_failure_not_another_store(world, monkeypatch):
    """S2: only proven ownership by another tenant may say so."""
    import services.meta_catalog_claim as claim

    def _lock_failed(_db, _catalog_id):
        raise claim.CatalogClaimError(claim.ERROR_CATALOG_CLAIM_LOCK_FAILED, {})

    monkeypatch.setattr(claim, "acquire_catalog_claim_lock", _lock_failed)
    state = _start(world)
    assert _result(_callback(world, code=_code(world), state=state)[1]) == "storage_unavailable"
    assert _rows(world) == []


@pytest.mark.parametrize("other_tenant_holds_catalog", [False, True])
def test_integrity_error_reports_a_claim_only_with_evidence(world, monkeypatch, other_tenant_holds_catalog):
    import services.meta_catalog_claim as claim
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    if other_tenant_holds_catalog:
        with world.factory() as s:
            s.add(WhatsAppConnection(tenant_id=OTHER_TENANT, meta_catalog_id=CATALOG))
            s.commit()
    monkeypatch.setattr(claim, "guard_catalog_claim", lambda *_a, **_k: None)  # the race window
    real_flush = Session.flush

    def _flush(self, *args, **kwargs):
        if any(isinstance(obj, MetaCatalogAuthorization) for obj in self.new):
            raise IntegrityError("INSERT", {}, Exception("synthetic constraint"))
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", _flush)
    state = _start(world)
    expected = "catalog_claimed_by_other_tenant" if other_tenant_holds_catalog else "storage_unavailable"
    assert _result(_callback(world, code=_code(world), state=state)[1]) == expected
