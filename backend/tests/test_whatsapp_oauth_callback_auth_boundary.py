"""WhatsApp Embedded OAuth callback JWT boundary and signed-state guards."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_BACKEND)
for _entry in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from core.log_redaction import redact_secrets, redact_value  # noqa: E402
from core.middleware import (  # noqa: E402
    JWT_PUBLIC_EXACT_PATHS,
    JWT_PUBLIC_PREFIXES,
    is_jwt_public_path,
    jwt_enforcement_middleware,
)


CALLBACK = "/whatsapp/embedded/oauth/callback"
_SALLA_PUBLIC_PREFIXES = (
    "/salla/token-login",
    "/salla/start",
    "/salla/session/launch-dashboard",
    "/salla/session/resolve-launch",
    "/salla/app-settings/webhook",
)
_NEIGHBORS = (
    "/whatsapp/embedded/oauth/start",
    "/whatsapp/embedded/exchange",
    "/whatsapp/embedded/status",
    "/whatsapp/embedded/oauth/callback/",
    "/whatsapp/embedded/oauth/callback/extra",
    "/whatsapp/embedded",
)


def _middleware(path: str, method: str = "GET"):
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )
    downstream = AsyncMock(return_value=MagicMock(status_code=200))
    response = asyncio.run(jwt_enforcement_middleware(request, downstream))
    return response, downstream


def test_exact_callback_is_jwt_public_without_prefix() -> None:
    assert CALLBACK in JWT_PUBLIC_EXACT_PATHS
    assert is_jwt_public_path(CALLBACK)
    assert "/whatsapp/embedded" not in JWT_PUBLIC_PREFIXES
    assert "/whatsapp/embedded/" not in JWT_PUBLIC_PREFIXES
    assert not any(p.startswith("/whatsapp/embedded") for p in JWT_PUBLIC_PREFIXES)


def test_jwt_middleware_passes_callback_without_bearer() -> None:
    response, downstream = _middleware(CALLBACK)
    downstream.assert_awaited_once()
    assert response.status_code == 200


@pytest.mark.parametrize("path", _NEIGHBORS)
def test_neighbor_embedded_routes_remain_jwt_protected(path: str) -> None:
    assert not is_jwt_public_path(path)
    response, downstream = _middleware(path, method="POST" if path.endswith("exchange") else "GET")
    downstream.assert_not_awaited()
    assert response.status_code == 401
    assert b"missing_token" in response.body


def test_salla_public_and_protected_paths_unchanged() -> None:
    for prefix in _SALLA_PUBLIC_PREFIXES:
        assert prefix in JWT_PUBLIC_PREFIXES
        assert is_jwt_public_path(prefix)
    assert is_jwt_public_path("/salla/app")
    assert is_jwt_public_path("/api/salla/oauth/callback")
    assert is_jwt_public_path("/api/salla/oauth/start")
    assert not is_jwt_public_path("/salla/subscription/status")
    assert not is_jwt_public_path("/salla/app-settings")
    assert "/salla" not in JWT_PUBLIC_PREFIXES


def test_bad_signature_rejected_before_graph_or_consume() -> None:
    from routers.whatsapp_embedded import _sign_oauth_state, oauth_callback

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(9, "nonce-a", issued_at, "https://api.example.test/cb")
    tampered = state[:-4] + "xxxx"
    request = Request({"type": "http", "method": "GET", "path": CALLBACK, "headers": [], "query_string": b""})
    with (
        patch("routers.whatsapp_embedded._exchange_code_for_token", new_callable=AsyncMock) as graph,
        patch("routers.whatsapp_embedded.consume_oauth_nonce") as consume,
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(oauth_callback(request, db=MagicMock(), code="secret-code", state=tampered))
    assert exc.value.status_code == 400
    graph.assert_not_awaited()
    consume.assert_not_called()


def test_expired_state_rejected_before_graph_or_consume() -> None:
    from routers.whatsapp_embedded import _sign_oauth_state, oauth_callback

    issued_at = int(datetime.now(timezone.utc).timestamp()) - 10_000
    state = _sign_oauth_state(9, "nonce-b", issued_at, "https://api.example.test/cb")
    request = Request({"type": "http", "method": "GET", "path": CALLBACK, "headers": [], "query_string": b""})
    with (
        patch("routers.whatsapp_embedded._exchange_code_for_token", new_callable=AsyncMock) as graph,
        patch("routers.whatsapp_embedded.consume_oauth_nonce") as consume,
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(oauth_callback(request, db=MagicMock(), code="secret-code", state=state))
    assert exc.value.status_code == 400
    graph.assert_not_awaited()
    consume.assert_not_called()


def test_missing_state_rejected_before_graph() -> None:
    from routers.whatsapp_embedded import oauth_callback

    request = Request({"type": "http", "method": "GET", "path": CALLBACK, "headers": [], "query_string": b""})
    with patch("routers.whatsapp_embedded._exchange_code_for_token", new_callable=AsyncMock) as graph:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(oauth_callback(request, db=MagicMock(), code="secret-code", state=None))
    assert exc.value.status_code == 400
    graph.assert_not_awaited()


def test_state_binds_tenant_mode_and_redirect() -> None:
    from routers.whatsapp_embedded import _sign_oauth_state, _verify_oauth_state

    issued_at = int(datetime.now(timezone.utc).timestamp())
    parsed = _verify_oauth_state(
        _sign_oauth_state(
            44,
            "bound-nonce",
            issued_at,
            "https://api.example.test/whatsapp/embedded/oauth/callback",
            "coexistence",
        )
    )
    assert parsed.tenant_id == 44
    assert parsed.connection_mode == "coexistence"
    assert parsed.redirect_uri == "https://api.example.test/whatsapp/embedded/oauth/callback"
    assert parsed.nonce == "bound-nonce"


def test_redaction_scrubs_oauth_secrets_from_text_and_dicts() -> None:
    url = (
        "GET https://graph.facebook.com/v21.0/oauth/access_token"
        "?code=oauth-code-SECRET&state=hmac.signed.STATE"
        "&client_secret=app-secret-SECRET&appsecret_proof=proof-SECRET"
        "&access_token=EAAB-token-SECRET"
    )
    out = redact_secrets(url)
    for leaked in (
        "oauth-code-SECRET",
        "hmac.signed.STATE",
        "app-secret-SECRET",
        "proof-SECRET",
        "EAAB-token-SECRET",
    ):
        assert leaked not in out
    assert "code=REDACTED" in out
    assert "state=REDACTED" in out
    assert "client_secret=REDACTED" in out
    assert "appsecret_proof=REDACTED" in out
    assert "access_token=REDACTED" in out
    assert "Bearer REDACTED" in redact_secrets("Authorization: Bearer EAAB-token-SECRET")
    assert "auth-header-secret" not in redact_secrets("Authorization: auth-header-secret")
    assert "authorization=REDACTED" in redact_secrets("authorization=raw-header-secret")
    assert "raw-header-secret" not in redact_secrets("authorization=raw-header-secret")
    redacted = redact_value(
        {"code": "live-code", "state": "live-state", "token": "live-token", "ok": True}
    )
    assert redacted["code"] == "REDACTED"
    assert redacted["state"] == "REDACTED"
    assert redacted["token"] == "REDACTED"
    assert redacted["ok"] is True


def test_oauth_start_fail_closed_does_not_issue_state() -> None:
    from core.whatsapp_oauth_nonce import NonceStorageUnavailable
    from routers.whatsapp_embedded import oauth_start

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/whatsapp/embedded/oauth/start",
            "headers": [(b"x-tenant-id", b"12")],
            "query_string": b"",
        }
    )
    db = MagicMock()
    with (
        patch("routers.whatsapp_embedded.is_meta_embedded_signup_enabled", return_value=True),
        patch(
            "routers.whatsapp_embedded.canonical_meta_redirect_uri",
            return_value="https://api.example.test/whatsapp/embedded/oauth/callback",
        ),
        patch(
            "routers.whatsapp_embedded.persist_oauth_nonce",
            side_effect=NonceStorageUnavailable("schema_missing"),
        ),
        patch("routers.whatsapp_embedded._sign_oauth_state") as sign,
        patch("routers.whatsapp_embedded._build_meta_oauth_authorize_url") as build,
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(oauth_start(request, db=db, connection_mode="embedded"))
    assert exc.value.status_code == 503
    sign.assert_not_called()
    build.assert_not_called()
    db.commit.assert_not_called()


def test_meta_error_callback_consumes_nonce_without_graph_or_secret_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from routers.whatsapp_embedded import _sign_oauth_state, oauth_callback

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(3, "nonce-err", issued_at, "https://api.example.test/cb")
    request = Request({"type": "http", "method": "GET", "path": CALLBACK, "headers": [], "query_string": b""})
    caplog.set_level(logging.DEBUG)
    with (
        patch("routers.whatsapp_embedded._exchange_code_for_token", new_callable=AsyncMock) as graph,
        patch("routers.whatsapp_embedded.consume_oauth_nonce", return_value=1) as consume,
        patch("routers.whatsapp_embedded.begin_waba_session", create=True) as begin,
    ):
        response = asyncio.run(
            oauth_callback(
                request,
                db=MagicMock(),
                code=None,
                state=state,
                error="access_denied",
                error_reason="user_denied",
            )
        )
    consume.assert_called_once()
    graph.assert_not_awaited()
    begin.assert_not_called()
    assert response.status_code == 302
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert state not in joined
    assert "nonce-err" not in joined
    assert "access_denied" in joined or "user_denied" in joined
