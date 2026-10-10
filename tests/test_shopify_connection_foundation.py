"""Dormant Shopify connection foundation — database-free unit tests.

No network: outbound calls go through ``httpx.MockTransport``. Every secret
is generated at runtime. The PostgreSQL lifecycle proofs live in
``backend/tests/test_shopify_connection_pg.py``.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pickle
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from services.shopify_connection import config as cfgmod
from services.shopify_connection import crypto, recovery
from services.shopify_connection.oauth import (
    OAuthQueryError,
    ShopifyApi,
    ShopifyApiError,
    build_authorize_url,
    compute_query_hmac,
    parse_callback_query,
    parse_token_grant,
    query_hmac_message,
    verify_callback,
)
from services.shopify_connection.shop_domain import canonical_shop_domain
from services.shopify_connection.webhooks import (
    WebhookRejected,
    parse_uninstall_body,
    verify_webhook_hmac,
)

_REPO = Path(__file__).resolve().parents[1]
SECRET = "unit-secret-" + secrets.token_hex(12)
KEY = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
SHOP = "generic-shoes-store.myshopify.com"
ENV = {
    cfgmod.ENABLED_ENV: "1",
    cfgmod.CLIENT_ID_ENV: "unit-client",
    cfgmod.CLIENT_SECRET_ENV: SECRET,
    cfgmod.REDIRECT_URI_ENV: "https://api.shop-review.example.test/merchant/integrations/shopify/callback",
    cfgmod.DASHBOARD_URL_ENV: "https://app.shop-review.example.test",
    cfgmod.ENCRYPTION_KEY_ENV: KEY,
}


# ── Shop identity ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("generic-shoes-store.myshopify.com", "generic-shoes-store.myshopify.com"),
    ("Generic-Shoes-Store.MyShopify.com", "generic-shoes-store.myshopify.com"),
    ("a1.myshopify.com", "a1.myshopify.com"),
])
def test_canonical_shop_accepts(raw, expected):
    assert canonical_shop_domain(raw) == expected


@pytest.mark.parametrize("raw", [
    "", None, 123, "myshopify.com", ".myshopify.com", "-shop.myshopify.com", "shop-.myshopify.com",
    "a.b.myshopify.com", "shop.myshopify.com.", "shop.myshopify.com.evil.example",
    "shop.myshopify.com\n", " shop.myshopify.com", "shop.myshopify.com/", "https://shop.myshopify.com",
    "shop.myshopify.com:443", "user@shop.myshopify.com", "shop.myshopify.com?x=1", "shop.myshopify.com#x",
    "evil.example#.myshopify.com", "shop．myshopify.com", "shоp.myshopify.com", "xn--shop.myshopify.com",
    "shop_name.myshopify.com", "s" * 64 + ".myshopify.com", "shop.myshopify.co", "shop.shopify.com",
    "127.0.0.1", "localhost",
])
def test_canonical_shop_refuses(raw):
    assert canonical_shop_domain(raw) is None


# ── Configuration ─────────────────────────────────────────────────────────────

def test_disabled_by_default_and_every_precondition_named():
    assert cfgmod.evaluate_availability({}).reason == cfgmod.R_DISABLED
    assert cfgmod.evaluate_availability({**ENV, cfgmod.ENABLED_ENV: "false"}).reason == cfgmod.R_DISABLED
    assert cfgmod.evaluate_availability({**ENV, cfgmod.CLIENT_SECRET_ENV: ""}).reason == cfgmod.R_CLIENT_CREDENTIALS_MISSING
    assert cfgmod.evaluate_availability({**ENV, cfgmod.ENCRYPTION_KEY_ENV: ""}).reason == cfgmod.R_ENCRYPTION_KEY_MISSING
    assert cfgmod.evaluate_availability({**ENV, cfgmod.ENCRYPTION_KEY_ENV: "short"}).reason == cfgmod.R_ENCRYPTION_KEY_INVALID
    assert cfgmod.evaluate_availability({**ENV, cfgmod.API_VERSION_ENV: "2026-05"}).reason == cfgmod.R_API_VERSION_INVALID
    ok = cfgmod.evaluate_availability(ENV)
    assert ok.available and ok.config.api_version == cfgmod.DEFAULT_API_VERSION
    assert SECRET not in repr(ok) and KEY not in repr(ok) and "encryption_key" not in repr(ok.config)


@pytest.mark.parametrize("uri", [
    "http://api.shop-review.example.test/merchant/integrations/shopify/callback",
    "https://api.shop-review.example.test:8443/merchant/integrations/shopify/callback",
    "https://api.shop-review.example.test/merchant/integrations/shopify/callback?x=1",
    "https://api.shop-review.example.test/merchant/integrations/shopify/callback#f",
    "https://api.shop-review.example.test/merchant/integrations/shopify/callback/",
    "https://API.shop-review.example.test/merchant/integrations/shopify/callback",
    "https://u:p@api.shop-review.example.test/merchant/integrations/shopify/callback",
    "https://api.shop-review.example.test/other",
    "https://[::1/merchant/integrations/shopify/callback",
    "https://[::1]/merchant/integrations/shopify/callback",
    "https://127.0.0.1/merchant/integrations/shopify/callback",
    "https://localhost/merchant/integrations/shopify/callback",
])
def test_redirect_uri_must_be_exact(uri):
    assert cfgmod.canonical_redirect_uri({cfgmod.REDIRECT_URI_ENV: uri}) is None
    assert cfgmod.evaluate_availability({**ENV, cfgmod.REDIRECT_URI_ENV: uri}).reason == cfgmod.R_REDIRECT_URI_INVALID


@pytest.mark.parametrize("url", ["http://app.example.test", "https://app.example.test/x", "https://[::1",
                                 "https://app.example.test?next=https://evil.example"])
def test_dashboard_url_is_a_fixed_origin(url):
    assert cfgmod.dashboard_complete_url({cfgmod.DASHBOARD_URL_ENV: url}) is None


def test_completion_target_is_fixed():
    assert cfgmod.dashboard_complete_url({cfgmod.DASHBOARD_URL_ENV: "https://app.shop-review.example.test/"}) == \
        "https://app.shop-review.example.test/integrations/shopify/complete"


def test_encryption_key_reuse_is_detected_across_encodings():
    raw = os.urandom(32)
    url_nopad = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    url_pad = base64.urlsafe_b64encode(raw).decode()
    std = base64.standard_b64encode(raw).decode()
    for foreign_name in ("WA_TOKEN_ENC_KEY", "TOTP_ENC_KEY", "JWT_SECRET"):
        for foreign in (url_pad, url_nopad, std):
            env = {**ENV, cfgmod.ENCRYPTION_KEY_ENV: url_nopad, foreign_name: foreign}
            assert cfgmod.encryption_key_reason(env) == cfgmod.R_ENCRYPTION_KEY_REUSED, (foreign_name, foreign)
    other = base64.urlsafe_b64encode(os.urandom(32)).decode()
    assert cfgmod.encryption_key_reason({**ENV, "WA_TOKEN_ENC_KEY": other}) is None


# ── Callback query HMAC (hex) ─────────────────────────────────────────────────

def _signed(params, secret=SECRET):
    out = dict(params)
    out["hmac"] = compute_query_hmac(params, secret)
    return out


def _params(**overrides):
    base = {"code": "0907a61c0c8d55e99db179b68161bc00", "host": "YWRtaW4uc2hvcGlmeS5jb20vc3RvcmUvZ2VuZXJpYw",
            "shop": SHOP, "state": secrets.token_urlsafe(32), "timestamp": str(int(time.time()))}
    base.update(overrides)
    return base


def test_query_hmac_matches_shopify_documented_message():
    params = {"code": "0907a61c0c8d55e99db179b68161bc00", "shop": "some-shop.myshopify.com",
              "state": "0.6784241404160823", "timestamp": "1337178173", "hmac": "ignored"}
    message = "code=0907a61c0c8d55e99db179b68161bc00&shop=some-shop.myshopify.com&state=0.6784241404160823&timestamp=1337178173"
    assert query_hmac_message(params) == message
    assert compute_query_hmac(params, "hush") == hmac.new(b"hush", message.encode(), hashlib.sha256).hexdigest()


def test_verify_callback_accepts_a_signed_query():
    params = _signed(_params())
    verified = verify_callback(urlencode(params), secret=SECRET)
    assert verified.shop_domain == SHOP and verified.code == params["code"]
    assert params["state"] not in repr(verified) and params["code"] not in repr(verified)


@pytest.mark.parametrize("mutate,code", [
    (lambda q: q + "&shop=" + SHOP, "parameter_duplicated"),
    (lambda q: q + "&ids%5B%5D=1", "parameter_invalid"),
    (lambda q: q.replace("shop=", "shop=evil-"), "hmac_invalid"),
    (lambda q: q + "&extra=1", "hmac_invalid"),
    (lambda q: q.replace("timestamp=", "x_timestamp="), "parameter_missing"),
    (lambda q: q + "&note=%26amp", "parameter_invalid"),
    (lambda q: q + "&note=%FF", "query_invalid"),
    (lambda q: "", "query_invalid"),
])
def test_verify_callback_refusals(mutate, code):
    query = urlencode(_signed(_params()))
    with pytest.raises(OAuthQueryError) as info:
        verify_callback(mutate(query), secret=SECRET)
    assert info.value.code == code


def test_verify_callback_missing_secret_and_wrong_secret():
    query = urlencode(_signed(_params()))
    with pytest.raises(OAuthQueryError, match="secret_missing"):
        verify_callback(query, secret="")
    with pytest.raises(OAuthQueryError, match="hmac_invalid"):
        verify_callback(query, secret="another-secret")
    upper = _signed(_params())
    upper["hmac"] = upper["hmac"].upper()
    with pytest.raises(OAuthQueryError, match="hmac_invalid"):
        verify_callback(urlencode(upper), secret=SECRET)


def test_verify_callback_timestamp_window_and_shop_format():
    stale = _signed(_params(timestamp=str(int(time.time()) - 3600)))
    with pytest.raises(OAuthQueryError, match="timestamp_out_of_window"):
        verify_callback(urlencode(stale), secret=SECRET)
    future = _signed(_params(timestamp=str(int(time.time()) + 3600)))
    with pytest.raises(OAuthQueryError, match="timestamp_out_of_window"):
        verify_callback(urlencode(future), secret=SECRET)
    for shop in ("evil.example", "GENERIC-SHOES-STORE.myshopify.com", "a.b.myshopify.com"):
        with pytest.raises(OAuthQueryError, match="shop_invalid"):
            verify_callback(urlencode(_signed(_params(shop=shop))), secret=SECRET)


def test_authorize_url_is_built_only_for_a_canonical_shop():
    url = build_authorize_url(shop_domain=SHOP, client_id="cid", redirect_uri=ENV[cfgmod.REDIRECT_URI_ENV], state="s")
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == SHOP and parts.path == "/admin/oauth/authorize"
    query = parse_qs(parts.query)
    assert query == {"client_id": ["cid"], "scope": ["read_products"],
                     "redirect_uri": [ENV[cfgmod.REDIRECT_URI_ENV]], "state": ["s"]}
    assert "grant_options" not in parts.query
    with pytest.raises(OAuthQueryError):
        build_authorize_url(shop_domain="evil.example", client_id="cid", redirect_uri="x", state="s")


# ── Webhook HMAC (base64 over the raw body) ───────────────────────────────────

def _body(**extra):
    payload = {"id": 7100000001, "myshopify_domain": SHOP}
    payload.update(extra)
    return json.dumps(payload).encode()


def _sig(body, secret=SECRET):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_webhook_hmac_over_raw_body():
    body = _body()
    verify_webhook_hmac(body, _sig(body), SECRET)
    with pytest.raises(WebhookRejected, match="hmac_invalid"):
        verify_webhook_hmac(body + b" ", _sig(body), SECRET)        # re-serialised / altered body
    hex_digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    with pytest.raises(WebhookRejected, match="hmac_invalid"):
        verify_webhook_hmac(body, hex_digest, SECRET)               # the OAuth (hex) form is not accepted
    with pytest.raises(WebhookRejected, match="hmac_invalid"):
        verify_webhook_hmac(body, None, SECRET)
    with pytest.raises(WebhookRejected, match="hmac_invalid"):
        verify_webhook_hmac(body, "not base64!!", SECRET)
    with pytest.raises(WebhookRejected, match="secret_missing"):
        verify_webhook_hmac(body, _sig(body), "")


def test_uninstall_identity_comes_from_the_signed_body():
    identity = parse_uninstall_body(_body(domain="shop.example"))
    assert identity.shop_id == 7100000001 and identity.shop_domain == SHOP
    assert identity.shop_gid == "gid://shopify/Shop/7100000001"
    for bad in (b"[]", b"not json", json.dumps({"id": True, "myshopify_domain": SHOP}).encode(),
                json.dumps({"id": 1, "myshopify_domain": SHOP.upper()}).encode(),
                json.dumps({"id": 1, "myshopify_domain": "evil.example"}).encode(),
                json.dumps({"myshopify_domain": SHOP}).encode()):
        with pytest.raises(WebhookRejected):
            parse_uninstall_body(bad)


# ── Credential encryption ─────────────────────────────────────────────────────

def _ctx(**overrides):
    base = dict(tenant_id=7, shop_domain=SHOP, generation=3)
    base.update(overrides)
    return crypto.token_context(crypto.PURPOSE_ACCESS_TOKEN, **base)


def test_cipher_round_trip_and_context_binding():
    cipher = crypto.TokenCipher(os.urandom(32))
    token = "shpat_" + secrets.token_hex(16)
    stored = cipher.encrypt(token, _ctx())
    assert stored.startswith(crypto.PREFIX) and token not in stored
    assert cipher.decrypt(stored, _ctx()) == token
    for other in (_ctx(tenant_id=8), _ctx(shop_domain="cotton-shirts-demo.myshopify.com"), _ctx(generation=4),
                  crypto.token_context(crypto.PURPOSE_REFRESH_TOKEN, tenant_id=7, shop_domain=SHOP, generation=3)):
        with pytest.raises(crypto.CredentialCryptoError):
            cipher.decrypt(stored, other)
    with pytest.raises(crypto.CredentialCryptoError):
        crypto.TokenCipher(os.urandom(32)).decrypt(stored, _ctx())
    tampered = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")
    with pytest.raises(crypto.CredentialCryptoError):
        cipher.decrypt(tampered, _ctx())


def test_cipher_never_exposes_key():
    key = os.urandom(32)
    cipher = crypto.TokenCipher(key)
    assert "withheld" in repr(cipher) and key.hex() not in repr(cipher)
    with pytest.raises(TypeError):
        pickle.dumps(cipher)
    with pytest.raises(crypto.CredentialCryptoError):
        crypto.TokenCipher(b"short")


# ── Token grant validation ────────────────────────────────────────────────────

def _grant(**overrides):
    body = {"access_token": "shpat_" + secrets.token_hex(16), "refresh_token": "shprt_" + secrets.token_hex(16),
            "expires_in": 3600, "refresh_token_expires_in": 7776000, "scope": "read_products"}
    body.update(overrides)
    return body


def test_expiring_offline_grant_is_validated():
    grant = parse_token_grant(_grant())
    assert grant.expires_in == 3600 and grant.scopes == frozenset({"read_products"})
    assert "shpat_" not in repr(grant) and "shprt_" not in repr(grant)


@pytest.mark.parametrize("overrides,code", [
    ({"refresh_token": None}, "expiring_token_missing"),
    ({"expires_in": None}, "expiring_token_missing"),
    ({"expires_in": True}, "expiring_token_missing"),
    ({"expires_in": 0}, "expiring_token_missing"),
    ({"associated_user": {"id": 1}}, "online_token_refused"),
    ({"scope": "read_products,write_products"}, "scope_excess"),
    ({"scope": "read_products,read_orders"}, "scope_excess"),
    ({"scope": "read_customers"}, "scope_missing"),
    ({"access_token": "has space"}, "access_token_invalid"),
])
def test_grant_refusals(overrides, code):
    with pytest.raises(ShopifyApiError) as info:
        parse_token_grant(_grant(**overrides))
    assert info.value.code == code


# ── Admin API client (mock transport, no network) ─────────────────────────────

def _api(handler):
    return ShopifyApi(client_id="cid", client_secret=SECRET, api_version="2026-07",
                      transport=httpx.MockTransport(handler))


def test_exchange_posts_credentials_in_the_body_with_expiring_1():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_grant())

    grant = asyncio.run(_api(handler).exchange_code(shop_domain=SHOP, code="c0de"))
    assert seen["url"] == f"https://{SHOP}/admin/oauth/access_token"
    assert seen["body"] == {"client_id": "cid", "client_secret": SECRET, "code": "c0de", "expiring": 1}
    assert SECRET not in seen["url"] and "c0de" not in seen["url"]
    assert grant.refresh_token.startswith("shprt_")
    assert SECRET not in repr(_api(handler))


def test_refresh_request_shape():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_grant())

    asyncio.run(_api(handler).refresh(shop_domain=SHOP, refresh_token="shprt_old"))
    assert seen["body"] == {"client_id": "cid", "client_secret": SECRET, "grant_type": "refresh_token",
                            "refresh_token": "shprt_old"}


@pytest.mark.parametrize("status,kind", [(400, "permanent"), (401, "permanent"), (429, "transient"),
                                         (503, "transient"), (302, "invalid_response")])
def test_token_failure_classification_never_follows_redirects(status, kind):
    def handler(request):
        headers = {"location": "https://evil.example/steal"} if status == 302 else {}
        return httpx.Response(status, headers=headers, json={"error": "invalid_request"})

    with pytest.raises(ShopifyApiError) as info:
        asyncio.run(_api(handler).exchange_code(shop_domain=SHOP, code="c"))
    assert info.value.kind == kind and "invalid_request" not in str(info.value)


def test_transport_error_and_oversized_response():
    def broken(request):
        raise httpx.ConnectError("boom shpat_secretvalue0000000000", request=request)

    with pytest.raises(ShopifyApiError) as info:
        asyncio.run(_api(broken).exchange_code(shop_domain=SHOP, code="c"))
    assert info.value.kind == "transient" and "shpat_" not in str(info.value)

    def huge(request):
        return httpx.Response(200, content=b"{" + b" " * 70000 + b"}")

    with pytest.raises(ShopifyApiError, match="response_too_large"):
        asyncio.run(_api(huge).exchange_code(shop_domain=SHOP, code="c"))


def test_identity_check_uses_header_token_and_validates_the_answer():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("x-shopify-access-token")
        return httpx.Response(200, json={"data": {"shop": {"id": "gid://shopify/Shop/42", "myshopifyDomain": SHOP}}})

    identity = asyncio.run(_api(handler).shop_identity(shop_domain=SHOP, access_token="shpat_abc"))
    assert identity.shop_gid == "gid://shopify/Shop/42" and identity.shop_domain == SHOP
    assert seen["url"] == f"https://{SHOP}/admin/api/2026-07/graphql.json" and seen["token"] == "shpat_abc"

    for status, body, kind in ((401, {}, "rejected"), (200, {"errors": [{"message": "x"}]}, "invalid_response"),
                               (200, {"data": {"shop": {"id": "1", "myshopifyDomain": SHOP}}}, "invalid_response"),
                               (200, {"data": {"shop": {"id": "gid://shopify/Shop/1",
                                                        "myshopifyDomain": "evil.example"}}}, "invalid_response")):
        with pytest.raises(ShopifyApiError) as info:
            asyncio.run(_api(lambda r, s=status, b=body: httpx.Response(s, json=b)).shop_identity(
                shop_domain=SHOP, access_token="t"))
        assert info.value.kind == kind


def test_client_refuses_non_canonical_shop_hosts():
    def handler(request):  # pragma: no cover — must never be reached
        raise AssertionError("no request may be sent")

    for shop in ("evil.example", "169.254.169.254", "shop.myshopify.com.evil.example"):
        with pytest.raises(ShopifyApiError):
            asyncio.run(_api(handler).exchange_code(shop_domain=shop, code="c"))


# ── Redaction (logs, Sentry) ──────────────────────────────────────────────────

def test_log_redaction_covers_shopify_material():
    from core.log_redaction import SecretRedactingFilter, redact_secrets

    access, refresh = "shpat_" + secrets.token_hex(16), "shprt_" + secrets.token_hex(16)
    state, code, hmac_value = secrets.token_urlsafe(32), secrets.token_hex(16), secrets.token_hex(32)
    line = (f'GET /merchant/integrations/shopify/callback?code={code}&hmac={hmac_value}&shop={SHOP}'
            f'&state={state}&timestamp=1 X-Shopify-Access-Token: {access} refresh={refresh} '
            f'X-Shopify-Hmac-Sha256: {hmac_value} Authorization: Bearer {access} client_secret={SECRET} '
            f'Cookie: sid={state}')
    out = redact_secrets(line)
    for value in (access, refresh, state, code, hmac_value, SECRET):
        assert value not in out
    assert SHOP in out
    assert access not in redact_secrets(f"token rotated to {access}")

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "upstream said %s", (access,), None)
    SecretRedactingFilter().filter(record)
    assert access not in record.getMessage()


def test_sentry_withholds_shopify_frames():
    from core.observability_sentry import _withheld_frame

    for module in ("services.shopify_connection.lifecycle", "services.shopify_connection.oauth",
                   "routers.shopify_connection"):
        assert _withheld_frame({"module": module})


# ── Routing boundaries ────────────────────────────────────────────────────────

def _client(monkeypatch, env, payload=None):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from core.database import get_db
    from routers import shopify_connection as router_module

    for key in list(ENV) + [cfgmod.API_VERSION_ENV]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = FastAPI()

    @app.middleware("http")
    async def inject(request: Request, call_next):
        if payload is not None:
            request.state.jwt_payload = payload
        return await call_next(request)

    class _NoDb:
        def get_bind(self):
            raise RuntimeError("no database in unit tests")

        def rollback(self):
            return None

    app.dependency_overrides[get_db] = lambda: _NoDb()
    app.include_router(router_module.router)
    app.include_router(router_module.webhook_router)
    return TestClient(app)


def test_every_route_is_off_by_default(monkeypatch):
    client = _client(monkeypatch, {}, payload={"tenant_id": 1, "user_id": 1, "role": "merchant", "jti": "j"})
    calls = [
        ("get", "/merchant/integrations/shopify/status", None),
        ("post", "/merchant/integrations/shopify/start", {"shop": SHOP}),
        ("get", "/merchant/integrations/shopify/callback?" + urlencode(_signed(_params())), None),
        ("post", "/merchant/integrations/shopify/complete", {"handle": "h" * 43}),
        ("post", "/merchant/integrations/shopify/disconnect", {"shop": SHOP}),
        ("post", "/webhooks/shopify/app-uninstalled", None),
    ]
    for method, path, body in calls:
        resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert resp.status_code == 404, (path, resp.status_code)


def test_mutations_refuse_support_impersonation_and_platform_staff(monkeypatch):
    for payload in ({"tenant_id": 1, "user_id": 1, "role": "support_impersonation", "impersonation": True, "jti": "j"},
                    {"tenant_id": 1, "user_id": 1, "role": "admin", "jti": "j"},
                    {"tenant_id": 1, "user_id": 1, "role": "merchant", "impersonation": True, "jti": "j"}):
        client = _client(monkeypatch, ENV, payload=payload)
        for path, body in (("/merchant/integrations/shopify/start", {"shop": SHOP}),
                           ("/merchant/integrations/shopify/complete", {"handle": "h" * 43}),
                           ("/merchant/integrations/shopify/disconnect", {"shop": SHOP})):
            assert client.post(path, json=body).status_code == 403, (payload, path)


def test_callback_never_reflects_and_returns_only_to_the_fixed_target(monkeypatch):
    client = _client(monkeypatch, ENV)
    bad = _params()
    bad["hmac"] = "0" * 64
    bad["return_to"] = "https%3A%2F%2Fevil.example"
    resp = client.get("/merchant/integrations/shopify/callback?" + urlencode(bad), follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location == "https://app.shop-review.example.test/integrations/shopify/complete#shopify_connection=callback_invalid"
    assert bad["state"] not in location and bad["code"] not in location
    assert resp.headers["cache-control"] == "no-store" and resp.headers["referrer-policy"] == "no-referrer"


def test_webhook_refusals_before_any_database_access(monkeypatch):
    body = _body()
    good = {"X-Shopify-Hmac-Sha256": _sig(body), "X-Shopify-Topic": "app/uninstalled",
            "X-Shopify-Shop-Domain": SHOP, "X-Shopify-Webhook-Id": "w-1"}
    client = _client(monkeypatch, {cfgmod.ENABLED_ENV: "1"})
    assert client.post("/webhooks/shopify/app-uninstalled", content=body, headers=good).status_code == 503
    client = _client(monkeypatch, ENV)
    post = client.post
    assert post("/webhooks/shopify/app-uninstalled", content=body,
                headers={**good, "X-Shopify-Hmac-Sha256": _sig(body, "other")}).status_code == 401
    assert post("/webhooks/shopify/app-uninstalled", content=body,
                headers=[*good.items(), ("X-Shopify-Hmac-Sha256", _sig(body))]).status_code == 401
    assert post("/webhooks/shopify/app-uninstalled", content=body,
                headers={**good, "X-Shopify-Shop-Domain": "cotton-shirts-demo.myshopify.com"}).status_code == 400
    assert post("/webhooks/shopify/app-uninstalled", content=body,
                headers={**good, "X-Shopify-Topic": "shop/update"}).status_code == 400
    assert post("/webhooks/shopify/app-uninstalled", content=body,
                headers={**good, "Content-Length": "999999"}).status_code == 413

    def chunked():
        for _ in range(70):
            yield b"x" * 1024

    assert post("/webhooks/shopify/app-uninstalled", content=chunked(), headers=good).status_code == 413
    # Valid signature but no tables (no database here): not acknowledged, retried by Shopify.
    assert post("/webhooks/shopify/app-uninstalled", content=body, headers=good).status_code == 503


def test_public_paths_are_exact():
    from core.middleware import is_jwt_public_path

    assert is_jwt_public_path("/merchant/integrations/shopify/callback")
    assert is_jwt_public_path("/webhooks/shopify/app-uninstalled")
    for path in ("/merchant/integrations/shopify/status", "/merchant/integrations/shopify/start",
                 "/merchant/integrations/shopify/complete", "/merchant/integrations/shopify/disconnect",
                 "/merchant/integrations/shopify/callback/x"):
        assert not is_jwt_public_path(path), path


# ── Capability boundaries ─────────────────────────────────────────────────────

def test_no_store_adapter_or_catalog_registration():
    import routers.shopify_connection  # noqa: F401
    from store_integration import registry

    assert "shopify" not in registry._ADAPTER_REGISTRY
    source = (_REPO / "backend" / "store_integration" / "registry.py").read_text(encoding="utf-8")
    assert "shopify" not in source.lower()


def test_foundation_modules_import_nothing_from_ai_salla_meta_whatsapp_or_payments():
    forbidden = ("modules.ai", "salla", "meta_catalog", "whatsapp", "moyasar", "payment", "store_integration",
                 "store_adapters", "integrations.shared", "catalog", "prompt", "persona")
    files = list((_REPO / "backend" / "services" / "shopify_connection").glob("*.py"))
    files.append(_REPO / "backend" / "routers" / "shopify_connection.py")
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(f in name.lower() for f in forbidden), (path.name, name)


def test_startup_queues_the_recovery_runner_only_when_available():
    source = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
    block = source[source.index("_f_shopify_uninstall_recovery"):source.index("_f_emitters")]
    assert "if _shopify_gate.available:" in block
    assert block.index("if _shopify_gate.available:") < block.index('_start("shopify_uninstall_recovery"')


def test_recovery_tick_is_dormant_without_configuration():
    def explode():
        raise AssertionError("the database must not be touched")

    assert asyncio.run(recovery.run_recovery_tick(session_factory=explode, env={})) == {"skipped": "disabled"}
    assert recovery.backoff_seconds(1) == recovery.BACKOFF_BASE_SECONDS
    assert recovery.backoff_seconds(2) == 2 * recovery.BACKOFF_BASE_SECONDS
    assert recovery.backoff_seconds(50) == recovery.BACKOFF_CAP_SECONDS


def test_shopify_tables_are_not_part_of_startup_create_all():
    from models import Base

    from services.shopify_connection.models import SHOPIFY_TABLE_NAMES

    assert not (set(SHOPIFY_TABLE_NAMES) & set(Base.metadata.tables))
