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


def test_missing_secret_and_malformed_callback_refuse():
    for query in ("", "shop=evil.test&state=x&code=x", "shop=demo.myshopify.com&shop=other.myshopify.com", "state=%FF"):
        with pytest.raises(OAuthQueryError):
            verify_callback(query, secret="")


def test_install_material_is_redacted():
    from core.log_redaction import redact_value, redact_secrets
    from core.observability_sentry import _scrub_request
    marker = secrets.token_hex(32)
    assert marker not in str(redact_value({"browser": marker, "install_handle": marker, "totp": marker}))
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
