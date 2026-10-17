"""Protocol and actual middleware/router boundaries; no external network."""
import asyncio
import base64
import os
import secrets
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "backend", ROOT / "database"):
    sys.path.insert(0, str(path))

from services.shopify_connection.install_config import CALLBACK, PREFIX, configuration
from services.shopify_connection.oauth import ShopifyApi, ShopifyApiError, parse_owner_grant, verify_callback, OAuthQueryError

PILOT_CODE = secrets.token_hex(32)


def pilot_env(shops, *, code=PILOT_CODE, now=None):
    import json
    from datetime import datetime, timedelta, timezone
    from services.shopify_connection.install_admission import ENV_KEY, code_hash
    deadline = (now or datetime.now(timezone.utc)) + timedelta(days=1)
    return {ENV_KEY: json.dumps({shop: {"sha256": code_hash(shop, code),
                                       "expires_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ")} for shop in shops})}


def env():
    return {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "1", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": "1",
            "SHOPIFY_CLIENT_ID": "synthetic-client", "SHOPIFY_CLIENT_SECRET": secrets.token_hex(32),
            "SHOPIFY_OAUTH_REDIRECT_URI": "https://api.example.test/merchant/integrations/shopify/callback",
            "SHOPIFY_INSTALL_REDIRECT_URI": "https://api.example.test" + CALLBACK,
            "DASHBOARD_URL": "https://app.example.test",
            "SHOPIFY_TOKEN_ENC_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode()}


def owner_body():
    return {"access_token": "shpat_" + secrets.token_hex(24), "expires_in": 86400, "scope": "read_products",
            "associated_user_scope": "read_products", "associated_user": {"id": 42, "account_owner": True, "collaborator": False}}


@pytest.mark.parametrize("field,value", [("id", True), ("id", "42"), ("id", 0), ("id", 2**63),
                                        ("account_owner", False), ("account_owner", "true"),
                                        ("collaborator", True), ("collaborator", None)])
def test_owner_proof_rejects_staff_and_malformed_identity(field, value):
    body = owner_body()
    body["associated_user"][field] = value
    with pytest.raises(ShopifyApiError):
        parse_owner_grant(body)


@pytest.mark.parametrize("field,value", [("expires_in", 0), ("expires_in", True), ("expires_in", 86401),
                                        ("scope", "read_products,read_orders"), ("associated_user_scope", ""),
                                        ("access_token", "invalid token"), ("associated_user", None)])
def test_owner_grant_fails_closed(field, value):
    body = owner_body()
    body[field] = value
    with pytest.raises(ShopifyApiError):
        parse_owner_grant(body)


def test_owner_email_is_not_identity_and_repr_withholds_token():
    body = owner_body()
    body["associated_user"].update(email="platform-admin@example.test", email_verified=False)
    grant = parse_owner_grant(body)
    assert grant.user_id == 42
    assert body["access_token"] not in repr(grant)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true ", "\tON\n"])
def test_pilot_closes_all_legacy_acquisition_before_config_or_storage(monkeypatch, value):
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from routers import shopify_connection as legacy
    from services.shopify_connection.install_config import enabled
    assert enabled({"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": value})
    monkeypatch.setenv("NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED", value)
    # Missing keys/admission must not reopen the legacy acquisition fallback.
    monkeypatch.delenv("SHOPIFY_INSTALL_PILOT_ADMISSION", raising=False)
    touched = MagicMock(side_effect=AssertionError("legacy acquisition reached storage/config"))
    monkeypatch.setattr(legacy, "_require_config", touched)
    monkeypatch.setattr(legacy, "_require_flag", touched)
    monkeypatch.setattr(legacy, "_actor", touched)
    monkeypatch.setattr(legacy, "_require_tables", touched)
    request, db = MagicMock(), MagicMock()
    calls = [legacy.shopify_start(legacy.ShopBody(shop="unadmitted.myshopify.com"), request, db),
             legacy.shopify_callback(request, db),
             legacy.shopify_complete(legacy.CompleteBody(handle="h" * 64), request, db)]
    for call in calls:
        with pytest.raises(HTTPException) as error:
            asyncio.run(call)
        assert error.value.status_code == 404
    touched.assert_not_called()
    assert db.mock_calls == []


@pytest.mark.parametrize("value", ["", "0", "false", "off"])
def test_legacy_acquisition_guard_does_not_change_dormant_connection(monkeypatch, value):
    from routers import shopify_connection as legacy
    monkeypatch.setenv("NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED", value)
    legacy._require_legacy_acquisition()


@pytest.mark.parametrize("change", [{"NAHLA_SHOPIFY_CONNECTION_ENABLED": "0"}, {"NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": "0"},
                                   {"SHOPIFY_CLIENT_SECRET": ""}, {"SHOPIFY_INSTALL_REDIRECT_URI": "https://evil.test/callback"},
                                   {"SHOPIFY_INSTALL_REDIRECT_URI": "https://api.example.test" + CALLBACK + "?next=evil"}])
def test_disabled_or_invalid_config(change):
    e = env(); e.update(change)
    assert configuration(e) is None


def test_owner_exchange_uses_online_mode_not_expiring_offline():
    body = owner_body()
    seen = []
    def transport(request):
        import json
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json=body)
    api = ShopifyApi(client_id="mock-client", client_secret="mock-secret", api_version="2026-07", transport=httpx.MockTransport(transport))
    result = asyncio.run(api.exchange_owner_code(shop_domain="demo.myshopify.com", code="test-code"))
    assert result.user_id == 42
    assert seen[0][0] == "https://demo.myshopify.com/admin/oauth/access_token"
    assert set(seen[0][1]) == {"client_id", "client_secret", "code"}


@pytest.mark.parametrize("kind", ["staff", "offline", "mixed", "empty", "rejected", "server_error"])
def test_owner_exchange_distinguishes_definitive_online_rejection_from_ambiguity(kind):
    from services.shopify_connection.oauth import OwnerGrantRejected
    body = owner_body()
    marker = body["access_token"]
    status = 200
    if kind == "staff":
        body["associated_user"]["account_owner"] = False
    elif kind == "offline":
        body.pop("associated_user"); body.pop("associated_user_scope")
    elif kind == "mixed":
        body["refresh_token"] = "synthetic-refresh"
    elif kind == "empty":
        body = {}
    elif kind in {"rejected", "server_error"}:
        status = 400 if kind == "rejected" else 503
    api = ShopifyApi(client_id="synthetic", client_secret="synthetic", api_version="2026-07",
                     transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    with pytest.raises(ShopifyApiError) as result:
        asyncio.run(api.exchange_owner_code(shop_domain="demo.myshopify.com", code="synthetic-code"))
    assert isinstance(result.value, OwnerGrantRejected) is (kind in {"staff", "rejected"})
    assert marker not in str(result.value)


def test_missing_secret_and_malformed_callback_refuse():
    for query in ("", "shop=evil.test&state=x&code=x", "shop=demo.myshopify.com&shop=other.myshopify.com", "state=%FF"):
        with pytest.raises(OAuthQueryError):
            verify_callback(query, secret="")


def test_install_material_is_redacted():
    from core.log_redaction import redact_value, redact_secrets
    from core.observability_sentry import _scrub_request
    marker = secrets.token_hex(32)
    assert marker not in str(redact_value({"browser": marker, "install_handle": marker, "totp": marker, "pilot_code": marker}))
    assert marker not in redact_secrets("pilot_code=" + marker)
    assert marker not in redact_secrets("https://app.example.test/app/shopify#install_handle=" + marker)
    assert marker not in str(_scrub_request({"url": "https://api.example.test" + PREFIX + "/start", "data": "browser=" + marker}))


def make_app(db_factory):
    from core import middleware
    from core.database import get_db
    from routers.shopify_install import router
    app = FastAPI()
    app.middleware("http")(middleware.api_key_middleware)
    app.middleware("http")(middleware.jwt_enforcement_middleware)
    app.include_router(router)
    def database():
        with db_factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    @app.get(PREFIX + "/private")
    def private():
        return {"private": True}
    return app


def test_exact_asgi_public_paths_and_feature_off(monkeypatch):
    from core import middleware
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    monkeypatch.setattr(middleware, "API_SECRET_KEY", "synthetic-required-key")
    monkeypatch.delenv("NAHLA_SHOPIFY_CONNECTION_ENABLED", raising=False)
    engine = create_engine("sqlite://")
    app = make_app(lambda: Session(engine))
    async def proof():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.example.test") as client:
            for method, path in (("POST", PREFIX + "/start"), ("GET", CALLBACK), ("POST", PREFIX + "/redeem")):
                response = await client.request(method, path)
                assert response.status_code == 404
                assert response.headers["cache-control"] == "no-store"
            for method, path in (("GET", PREFIX + "/start"), ("POST", CALLBACK), ("GET", PREFIX + "/private"),
                                 ("POST", PREFIX + "/redeem/extra"), ("POST", "/merchant/integrations/shopify/start")):
                assert (await client.request(method, path)).status_code == 401
    asyncio.run(proof())


def test_salla_handoffs_cannot_extend_a_shopify_session():
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock, AsyncMock, patch
    from jose import jwt
    from fastapi import HTTPException
    from core.config import JWT_SECRET, JWT_ALGORITHM
    from routers import salla_oauth
    payload = {"sub": "shopify-owner@identity.invalid", "role": "merchant", "tenant_id": 88, "user_id": 90,
               "auth_source": "shopify", "shopify_session": "mock-flow", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    request = MagicMock()
    request.query_params = {"store_id": "123"}
    request.state.jwt_payload = payload
    with patch.object(salla_oauth, "create_token") as mint:
        with pytest.raises(HTTPException) as error:
            asyncio.run(salla_oauth.salla_check_session(request, MagicMock()))
        assert error.value.status_code == 403
        mint.assert_not_called()
    request.json = AsyncMock(return_value={"token": jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), "store_id": "123"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(salla_oauth.launch_dashboard(request, MagicMock()))
    assert error.value.status_code == 403


def test_purpose_tokens_cannot_be_used_as_authenticated_sessions(monkeypatch):
    from core import auth
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    for kind in ("2fa_setup", "2fa_challenge", "invite", "verify_email", "password_reset"):
        monkeypatch.setattr(auth, "decode_token", lambda token, kind=kind: {"type": kind, "user_id": 90})
        with pytest.raises(HTTPException):
            auth.get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials="purpose-token"))


def test_passwordless_shopify_cannot_enter_unrecoverable_mfa_enrollment():
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from routers import twofa
    payload = {"auth_source": "shopify", "shopify_session": "synthetic-flow", "user_id": 90}
    for action in (twofa.two_factor_setup_start, twofa.two_factor_setup_candidates, twofa.two_factor_setup_confirm):
        kwargs = {"request": MagicMock(), "user": payload, "db": MagicMock()}
        if action != twofa.two_factor_setup_start:
            kwargs["body"] = MagicMock()
        with pytest.raises(HTTPException) as error:
            asyncio.run(action(**kwargs))
        assert error.value.status_code == 403
        assert error.value.detail == "shopify_mfa_setup_unavailable"


@pytest.mark.parametrize("mutation", ["empty", "missing", "wrong", "cross_shop", "unknown", "expired", "deadline",
                                     "naive", "far_future", "uppercase", "malformed", "duplicate", "extra", "oversized", "nested"])
def test_pilot_admission_fails_closed(mutation):
    import json
    from datetime import datetime, timedelta, timezone
    from services.shopify_connection.install_admission import ENV_KEY, admitted
    now = datetime.now(timezone.utc).replace(microsecond=0)
    shop, other = "demo.myshopify.com", "another.myshopify.com"
    config = pilot_env([shop, other], now=now)
    entries = json.loads(config[ENV_KEY])
    code = PILOT_CODE
    if mutation == "empty":
        config = {}
    elif mutation == "missing":
        code = None
    elif mutation == "wrong":
        code = secrets.token_hex(32)
    elif mutation == "cross_shop":
        entries[shop]["sha256"] = entries[other]["sha256"]
    elif mutation == "unknown":
        shop = "unknown.myshopify.com"
    elif mutation in {"expired", "deadline", "naive", "far_future"}:
        deadline = now + timedelta(days=8) if mutation == "far_future" else now - timedelta(seconds=mutation == "expired")
        entries[shop]["expires_at"] = deadline.strftime("%Y-%m-%dT%H:%M:%S" + ("" if mutation == "naive" else "Z"))
    elif mutation == "uppercase":
        entries["DEMO.myshopify.com"] = entries.pop(shop)
    elif mutation == "extra":
        entries[shop]["ignored"] = True
    elif mutation == "oversized":
        config = pilot_env([f"shop-{n}.myshopify.com" for n in range(6)], now=now)
    if mutation not in {"empty", "oversized"}:
        config = {ENV_KEY: json.dumps(entries)}
    if mutation == "malformed":
        config[ENV_KEY] = "{" + "x" * 4096
    elif mutation == "duplicate":
        item = json.dumps(entries[shop])
        config[ENV_KEY] = '{"' + shop + '":' + item + ',"' + shop + '":' + item + '}'
    elif mutation == "nested":
        config[ENV_KEY] = "[" * 1500 + "]" * 1500
    assert admitted(shop, code, env=config, now=now) is False


def test_pilot_admission_revocation_and_expired_peer():
    import json
    from datetime import datetime, timedelta, timezone
    from services.shopify_connection.install_admission import ENV_KEY, admitted
    now = datetime.now(timezone.utc)
    shop, other = "demo.myshopify.com", "another.myshopify.com"
    config = pilot_env([shop, other], now=now)
    entries = json.loads(config[ENV_KEY])
    entries[other]["expires_at"] = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config[ENV_KEY] = json.dumps(entries)
    assert admitted(shop, PILOT_CODE, env=config, now=now)
    assert not admitted(other, PILOT_CODE, env=config, now=now)
    assert not admitted(shop, PILOT_CODE, env=pilot_env([shop], code=secrets.token_hex(32)), now=now)


def test_unadmitted_requests_never_reach_database_or_quotas(monkeypatch):
    from unittest.mock import MagicMock
    from routers import shopify_install as routes
    from services.shopify_connection import install
    from services.shopify_connection.install_admission import ENV_KEY
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    shop = "demo.myshopify.com"
    monkeypatch.setenv(ENV_KEY, pilot_env([shop])[ENV_KEY])
    db = MagicMock()
    with pytest.raises(install.InstallRejected, match="install_admission_required"):
        install.begin(db, shop=shop, browser=secrets.token_hex(32), config=configuration(env()))
    assert not db.mock_calls
    monkeypatch.setattr(routes, "enabled", lambda: True)
    monkeypatch.setattr(routes, "configuration", lambda: configuration(env()))
    engine = create_engine("sqlite://")
    queries = []
    event.listen(engine, "before_cursor_execute", lambda *args: queries.append(args[2]))
    app = make_app(lambda: Session(engine))
    async def proof():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.example.test") as client:
            for i in range(60):
                response = await client.post(PREFIX + "/start", data={
                    "shop": shop if i % 2 else f"unknown-{i}.myshopify.com", "browser": secrets.token_hex(32),
                    "pilot_code": secrets.token_hex(32) if i % 2 else PILOT_CODE},
                    headers={"Origin": "https://app.example.test", "X-Forwarded-For": f"198.51.100.{i}", "X-Real-IP": f"192.0.2.{i}"})
                assert response.status_code == 400
                assert "set-cookie" not in response.headers
    asyncio.run(proof())
    assert queries == []


def test_cleanup_without_schema_does_not_create_tables(monkeypatch):
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session
    from services.shopify_connection import install_cleanup
    engine = create_engine("sqlite://")
    for key in env():
        monkeypatch.delenv(key, raising=False)
    assert asyncio.run(install_cleanup.run_cleanup_tick(session_factory=lambda: Session(engine))) == {"skipped": "storage_unavailable"}
    assert inspect(engine).get_table_names() == []


def test_cleanup_registered_outside_activation_conditions():
    import ast
    tree = ast.parse((ROOT / "backend/main.py").read_text(encoding="utf-8"))
    startup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_startup")
    # A direct startup statement, not nested under a feature/config conditional.
    calls = [node.value for node in startup.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]
    assert any(isinstance(call.func, ast.Name) and call.func.id == "_start" and call.args
               and isinstance(call.args[0], ast.Constant) and call.args[0].value == "shopify_install_cleanup" for call in calls)


def test_cleanup_waiting_for_database_keeps_request_loop_responsive():
    import threading
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from services.shopify_connection import install_cleanup
    engine = create_engine("sqlite://")
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()
    def slow_checkout():
        assert threading.get_ident() != caller
        entered.set()
        assert release.wait(3), "request loop failed to release simulated checkout"
        return Session(engine)
    async def proof():
        task = asyncio.create_task(install_cleanup.run_cleanup_tick(session_factory=slow_checkout))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.sleep(0)  # Requests can continue before DB checkout finishes.
            assert not task.done()
        finally:
            release.set()
        assert await task == {"skipped": "storage_unavailable"}
    asyncio.run(proof())
