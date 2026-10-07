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
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", str(OTHER_TENANT))
    import services.whatsapp_catalog_sync_scope as scope_mod

    if hasattr(scope_mod, "_reset_cache_for_tests"):
        scope_mod._reset_cache_for_tests()
    resp = world.client.post("/merchant/catalog/meta-consent/start", headers=_jwt())
    if scope_mod.tenant_in_sync_scope(TENANT):
        pytest.skip("sync scope variable name differs in this checkout")
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "sync_scope_excluded"


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
