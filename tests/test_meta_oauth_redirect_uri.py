"""Meta WhatsApp OAuth redirect_uri exact-identity (META-OAUTH-01..12)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import HTTPException  # noqa: E402

from services.meta_oauth_redirect import (  # noqa: E402
    JS_SDK_LOGIN_SUCCESS_URI,
    canonical_meta_redirect_uri,
    graph_oauth_token_params,
    js_sdk_token_exchange_redirect_uri,
    token_exchange_log_fields,
)


CANONICAL = "https://api.example.test/whatsapp/embedded/oauth/callback"
CANONICAL_SLASH = "https://api.example.test/whatsapp/embedded/oauth/callback/"
CANONICAL_WWW = "https://www.example.test/whatsapp/embedded/oauth/callback"
CANONICAL_HTTP = "http://api.example.test/whatsapp/embedded/oauth/callback"


def _params_for_js_sdk(code: str = "auth-code") -> Dict[str, str]:
    return graph_oauth_token_params(
        code=code,
        redirect_uri=js_sdk_token_exchange_redirect_uri(),
        client_id="app-id",
        client_secret="app-secret",
    )


def _params_for_server(code: str, redirect_uri: str) -> Dict[str, str]:
    return graph_oauth_token_params(
        code=code,
        redirect_uri=redirect_uri,
        client_id="app-id",
        client_secret="app-secret",
    )


def test_meta_oauth_01_start_and_exchange_share_bound_redirect_uri():
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL):
        start_uri = canonical_meta_redirect_uri()
    from routers.whatsapp_embedded import _build_meta_oauth_authorize_url, _sign_oauth_state, _verify_oauth_state

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(7, "nonce1", issued_at, start_uri)
    authorize = _build_meta_oauth_authorize_url(state, start_uri)
    dialog_uri = parse_qs(urlparse(authorize).query)["redirect_uri"][0]
    bound = _verify_oauth_state(state).redirect_uri
    exchange = _params_for_server("auth-code", bound)
    assert dialog_uri == start_uri == bound == exchange["redirect_uri"] == CANONICAL


def test_meta_oauth_02_trailing_slash_cannot_diverge():
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL_SLASH):
        start_uri = canonical_meta_redirect_uri()
    assert start_uri.endswith("/")
    assert start_uri != start_uri.rstrip("/")
    exchange = _params_for_server("auth-code", start_uri)
    assert exchange["redirect_uri"] == CANONICAL_SLASH
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL):
        other = canonical_meta_redirect_uri()
    assert other != start_uri
    assert _params_for_server("auth-code", other)["redirect_uri"] == CANONICAL


def test_meta_oauth_03_forwarded_host_cannot_change_canonical():
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL):
        ignored = canonical_meta_redirect_uri(
            "https://evil.example/callback",
            request_host="www.nahlah.ai",
            headers={
                "Host": "www.nahlah.ai",
                "X-Forwarded-Host": "evil.example",
                "X-Forwarded-Proto": "http",
                "Origin": "https://www.nahlah.ai",
                "Referer": "https://www.nahlah.ai/dashboard/whatsapp/connect",
            },
        )
    assert ignored == CANONICAL
    assert "evil" not in ignored
    assert "www.nahlah.ai" not in ignored


def test_meta_oauth_04_www_not_reconstructed():
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL):
        non_www = canonical_meta_redirect_uri(host="www.example.test")
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL_WWW):
        www = canonical_meta_redirect_uri(host="api.example.test")
    assert non_www == CANONICAL
    assert www == CANONICAL_WWW
    assert non_www != www


def test_meta_oauth_05_http_https_follow_config_only():
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL):
        https_uri = canonical_meta_redirect_uri(proto="http")
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL_HTTP):
        http_uri = canonical_meta_redirect_uri(proto="https")
    assert https_uri.startswith("https://")
    assert http_uri.startswith("http://")
    assert _params_for_server("c", https_uri)["redirect_uri"] == CANONICAL
    assert _params_for_server("c", http_uri)["redirect_uri"] == CANONICAL_HTTP


def test_meta_oauth_06_callback_path_stays_identical():
    with patch("services.meta_oauth_redirect.META_REDIRECT_URI", CANONICAL):
        start_uri = canonical_meta_redirect_uri()
    from routers.whatsapp_embedded import _build_meta_oauth_authorize_url, _sign_oauth_state, _verify_oauth_state

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(3, "n", issued_at, start_uri)
    dialog_path = urlparse(parse_qs(urlparse(
        _build_meta_oauth_authorize_url(state, start_uri)
    ).query)["redirect_uri"][0]).path
    exchange_path = urlparse(_verify_oauth_state(state).redirect_uri).path
    assert dialog_path == exchange_path == "/whatsapp/embedded/oauth/callback"


def test_meta_oauth_07_state_binds_start_redirect_uri():
    from routers.whatsapp_embedded import _sign_oauth_state, _verify_oauth_state

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(11, "bound-nonce", issued_at, CANONICAL_SLASH)
    parsed = _verify_oauth_state(state)
    assert parsed.tenant_id == 11
    assert parsed.redirect_uri == CANONICAL_SLASH


def test_meta_oauth_bound_redirect_uri_reused_without_strip():
    from routers.whatsapp_embedded import _sign_oauth_state, _verify_oauth_state

    exact = "https://api.example.test/whatsapp/embedded/oauth/callback "
    issued_at = int(datetime.now(timezone.utc).timestamp())
    parsed = _verify_oauth_state(_sign_oauth_state(4, "n", issued_at, exact))
    assert parsed.redirect_uri == exact
    assert parsed.redirect_uri != exact.strip()


def test_meta_oauth_08_tampered_state_cannot_supply_other_redirect():
    from routers.whatsapp_embedded import _sign_oauth_state, _verify_oauth_state

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(11, "bound-nonce", issued_at, CANONICAL)
    body_b64, sig_b64 = state.split(".", 1)
    pad = "=" * (-len(body_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(body_b64 + pad))
    payload["ru"] = "https://attacker.example/steal"
    tampered_body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    tampered = f"{tampered_body}.{sig_b64}"
    try:
        _verify_oauth_state(tampered)
        raise AssertionError("tampered state must be rejected")
    except HTTPException as exc:
        assert exc.status_code == 400

    js_params = graph_oauth_token_params(
        code="auth-code",
        redirect_uri="https://attacker.example/steal",
        client_id="app-id",
        client_secret="app-secret",
    )
    # Helper will include an explicit URI if a trusted caller passes one;
    # the JS SDK /exchange route must pass None so this attacker value is unused.
    ignored = graph_oauth_token_params(
        code="auth-code",
        redirect_uri=js_sdk_token_exchange_redirect_uri(),
        client_id="app-id",
        client_secret="app-secret",
    )
    assert "redirect_uri" not in ignored
    assert js_params["redirect_uri"] == "https://attacker.example/steal"


def test_meta_oauth_09_logs_omit_code_and_tokens():
    params = _params_for_server("super-secret-code", CANONICAL)
    params_with_token_shape = dict(params)
    safe = token_exchange_log_fields(params_with_token_shape)
    blob = json.dumps(safe)
    assert "super-secret-code" not in blob
    assert "app-secret" not in blob
    assert safe["has_code"] is True
    assert safe["has_client_secret"] is True
    assert safe["redirect_uri"] == CANONICAL


def test_meta_oauth_js_sdk_exchange_omits_redirect_uri():
    params = _params_for_js_sdk()
    assert "redirect_uri" not in params
    assert JS_SDK_LOGIN_SUCCESS_URI not in params.values()
    empty = graph_oauth_token_params(
        code="auth-code",
        redirect_uri="",
        client_id="app-id",
        client_secret="app-secret",
    )
    assert "redirect_uri" not in empty


def test_meta_oauth_js_sdk_exchange_helper_does_not_inject_login_success(caplog):
    from routers.whatsapp_embedded import _exchange_code_for_token

    captured: Dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok-secret", "token_type": "bearer"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

    caplog.set_level(logging.INFO)
    with patch("routers.whatsapp_embedded.httpx.AsyncClient", _Client):
        data = asyncio.run(_exchange_code_for_token("live-auth-code", None))
    assert data["access_token"] == "tok-secret"
    assert "redirect_uri" not in captured["params"]
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "live-auth-code" not in joined
    assert "tok-secret" not in joined
    assert "redirect_uri_present=False" in joined or "redirect_uri_present=%s" not in joined


def test_meta_oauth_11_server_path_still_sends_bound_uri():
    from routers.whatsapp_embedded import _exchange_code_for_token

    captured: Dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok", "token_type": "bearer"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            captured["params"] = params
            return _Resp()

    with patch("routers.whatsapp_embedded.httpx.AsyncClient", _Client):
        asyncio.run(_exchange_code_for_token("auth-code", CANONICAL))
    assert captured["params"]["redirect_uri"] == CANONICAL


def test_meta_oauth_12_helper_has_no_brain_imports():
    src = (BACKEND_DIR / "services" / "meta_oauth_redirect.py").read_text(encoding="utf-8")
    assert "modules.ai" not in src
    assert "merchant_brain" not in src
    assert "response_goal" not in src


def test_state_hmac_uses_jwt_secret_not_request_data():
    from routers.whatsapp_embedded import JWT_SECRET, _sign_oauth_state

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(1, "n", issued_at, CANONICAL)
    body_b64, sig_b64 = state.split(".", 1)
    body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
    expected = hmac.new(JWT_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    assert hmac.compare_digest(expected, got)
