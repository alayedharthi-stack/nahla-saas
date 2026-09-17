"""Credential redaction — regression proofs with synthetic canary secrets only.

Production runtime logs exposed the Meta app secret and access tokens inside
outbound token-refresh URLs. Root cause: ``httpx`` logs every request as
``HTTP Request: <method> <url> "<status>"`` with the URL as an ``httpx.URL``
*object*; the previous ``SecretRedactingFilter`` only scrubbed string
arguments, so the credential-bearing URL was formatted into the line intact.
Related paths: ``str(exc)`` of httpx exceptions embeds the URL; uvicorn's access
log prints the inbound query string (``hub.verify_token``, OAuth ``code``).

Every value below is a synthetic canary. No real credential appears here.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from core import log_redaction  # noqa: E402
from core.log_redaction import (  # noqa: E402
    REDACTED,
    SecretRedactingFilter,
    install_log_redaction,
    redact_exception,
    redact_secrets,
    redact_value,
)

# ── Synthetic canaries (never real) ────────────────────────────────────
CANARY_APP_SECRET = "notreal-notreal-notreal-notreal-app-secret"
CANARY_EXCHANGE_TOKEN = "EAAcanaryExchangeTokenAAAA1111BBBB2222CCCC3333"
CANARY_NEW_TOKEN = "EAAcanaryLongLivedTokenDDDD4444EEEE5555FFFF6666"
CANARY_INPUT_TOKEN = "EAAcanaryInputTokenGGGG7777HHHH8888IIII9999"
CANARY_VERIFY_TOKEN = "notreal-notreal-notreal-notreal-verify"
CANARY_OAUTH_CODE = "notreal-notreal-notreal-notreal-code"
CANARY_REFRESH_TOKEN = "notreal-notreal-notreal-notreal-refresh"
CANARY_API_KEY = "notreal-notreal-notreal-notreal-api-key"
CANARY_COOKIE = "session=notreal-notreal-notreal-notreal-cookie"
CANARY_BEARER = "notreal-notreal-notreal-notreal-bearer"
ALL_CANARIES = (
    CANARY_APP_SECRET, CANARY_EXCHANGE_TOKEN, CANARY_NEW_TOKEN, CANARY_INPUT_TOKEN,
    CANARY_VERIFY_TOKEN, CANARY_OAUTH_CODE, CANARY_REFRESH_TOKEN, CANARY_API_KEY,
    CANARY_COOKIE.split("=", 1)[1], CANARY_BEARER,
)

TOKEN_URL = (
    "https://graph.facebook.com/v21.0/oauth/access_token"
    f"?grant_type=fb_exchange_token&client_id=123456&client_secret={CANARY_APP_SECRET}"
    f"&fb_exchange_token={CANARY_EXCHANGE_TOKEN}"
)


def _assert_no_canary(text: str, *canaries: str) -> None:
    for canary in canaries or ALL_CANARIES:
        assert canary not in text, f"canary leaked: {canary[:4]}…"
        # No fingerprints either: neither the head nor the tail of a secret.
        assert canary[:8] not in text and canary[-8:] not in text


def _record(name: str, msg: str, args=(), exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=exc_info,
    )


# ── Root cause: httpx logs the URL as an object, not a string ──────────

def test_filter_redacts_httpx_url_object_argument() -> None:
    """Exact shape of httpx's request log: ``'HTTP Request: %s %s "%s %d %s"'``
    with an ``httpx.URL`` argument."""
    record = _record(
        "httpx",
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", httpx.URL(TOKEN_URL), "HTTP/1.1", 200, "OK"),
    )
    assert SecretRedactingFilter().filter(record) is True
    line = logging.Formatter("%(message)s").format(record)
    _assert_no_canary(line, CANARY_APP_SECRET, CANARY_EXCHANGE_TOKEN)
    # Safe diagnostics survive: method, host, path, status, parameter names.
    assert "GET https://graph.facebook.com/v21.0/oauth/access_token" in line
    assert "grant_type=fb_exchange_token" in line
    assert "client_id=123456" in line
    assert f"client_secret={REDACTED}" in line
    assert f"fb_exchange_token={REDACTED}" in line
    assert '"HTTP/1.1 200 OK"' in line


def test_exact_token_refresh_path_logs_no_credential(caplog) -> None:
    """The production code path: ``_refresh_merchant_long_lived_token`` through a
    real ``httpx.AsyncClient`` (mock transport) with the redaction installed the
    way ``backend/main.py`` installs it."""
    from services.whatsapp_platform import token_manager

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": CANARY_NEW_TOKEN, "expires_in": 100})

    real_client = httpx.AsyncClient

    def _client(**kwargs):
        return real_client(transport=httpx.MockTransport(_handler), **kwargs)

    conn = SimpleNamespace(
        tenant_id=1, provider="meta", connection_type="embedded",
        access_token=CANARY_EXCHANGE_TOKEN, token_type="long_lived",
        token_expires_at=None, wa_provider="meta",
    )
    install_log_redaction()
    with patch.object(token_manager, "META_APP_ID", "123456"), patch.object(
        token_manager, "META_APP_SECRET", CANARY_APP_SECRET
    ), patch.object(token_manager, "read_access_token", return_value=CANARY_EXCHANGE_TOKEN), patch.object(
        token_manager, "store_access_token"
    ), patch.object(token_manager, "wa_provider", return_value="meta"), patch.object(
        token_manager, "_merchant_token_health", return_value=("expiring_soon", None)
    ), patch.object(token_manager, "build_token_context", return_value="ctx"), patch.object(
        token_manager.httpx, "AsyncClient", side_effect=_client
    ), caplog.at_level(logging.DEBUG):
        result = asyncio.run(token_manager._refresh_merchant_long_lived_token(conn))

    assert result == "ctx"
    httpx_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert httpx_lines, "httpx must have logged the request (the exact production line)"
    for line in httpx_lines:
        assert "oauth/access_token" in line
        _assert_no_canary(line)
    for record in caplog.records:
        _assert_no_canary(record.getMessage())


def test_exact_token_refresh_exception_path_logs_no_credential(caplog) -> None:
    """Failure path: an httpx error whose ``str()`` embeds the request URL."""
    from services.whatsapp_platform import token_manager

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPStatusError(
            f"Server error '500' for url '{request.url}'",
            request=request, response=httpx.Response(500, request=request),
        )

    real_client = httpx.AsyncClient

    def _client(**kwargs):
        return real_client(transport=httpx.MockTransport(_handler), **kwargs)

    conn = SimpleNamespace(tenant_id=1, access_token=CANARY_EXCHANGE_TOKEN)
    with patch.object(token_manager, "META_APP_ID", "123456"), patch.object(
        token_manager, "META_APP_SECRET", CANARY_APP_SECRET
    ), patch.object(token_manager, "read_access_token", return_value=CANARY_EXCHANGE_TOKEN), patch.object(
        token_manager, "wa_provider", return_value="meta"
    ), patch.object(
        token_manager, "_merchant_token_health", return_value=("expiring_soon", None)
    ), patch.object(token_manager.httpx, "AsyncClient", side_effect=_client), caplog.at_level(
        logging.DEBUG
    ):
        assert asyncio.run(token_manager._refresh_merchant_long_lived_token(conn)) is None

    warnings = [r.getMessage() for r in caplog.records if "refresh failed" in r.getMessage()]
    assert warnings, "the network-error warning must still be logged"
    for line in warnings:
        assert "HTTPStatusError" in line
        _assert_no_canary(line)


# ── Exception text and stack text ──────────────────────────────────────

def test_filter_redacts_exception_text_rendered_from_exc_info() -> None:
    request = httpx.Request("GET", TOKEN_URL)
    try:
        raise httpx.HTTPStatusError(
            f"Client error '400' for url '{request.url}'", request=request,
            response=httpx.Response(400, request=request),
        )
    except httpx.HTTPStatusError:
        record = _record("nahla.test", "refresh failed", exc_info=sys.exc_info())
    SecretRedactingFilter().filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    assert "HTTPStatusError" in rendered
    _assert_no_canary(rendered, CANARY_APP_SECRET, CANARY_EXCHANGE_TOKEN)


def test_redact_exception_helper() -> None:
    request = httpx.Request("GET", TOKEN_URL)
    exc = httpx.HTTPStatusError("boom for url '%s'" % request.url, request=request,
                                response=httpx.Response(500, request=request))
    out = redact_exception(exc)
    assert out.startswith("HTTPStatusError: ")
    assert "graph.facebook.com/v21.0/oauth/access_token" in out
    _assert_no_canary(out, CANARY_APP_SECRET, CANARY_EXCHANGE_TOKEN)


# ── Inbound access log (uvicorn) ───────────────────────────────────────

def test_uvicorn_access_log_redacts_inbound_query_secrets() -> None:
    logger = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        install_log_redaction()
        logger.info(
            '%s - "%s %s HTTP/%s" %d', "10.0.0.1:1234", "GET",
            f"/webhook/whatsapp?hub.mode=subscribe&hub.verify_token={CANARY_VERIFY_TOKEN}&hub.challenge=42",
            "1.1", 200,
        )
        logger.info(
            '%s - "%s %s HTTP/%s" %d', "10.0.0.1:1234", "GET",
            f"/auth/salla/callback?code={CANARY_OAUTH_CODE}&state=abc", "1.1", 302,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    out = stream.getvalue()
    _assert_no_canary(out, CANARY_VERIFY_TOKEN, CANARY_OAUTH_CODE)
    assert "GET /webhook/whatsapp?hub.mode=subscribe&hub.verify_token=" in out
    assert "hub.challenge=42" in out
    assert '" 200' in out and '" 302' in out


# ── Headers, cookies, bodies, mappings ─────────────────────────────────

def test_redact_value_covers_headers_cookies_and_body_fields() -> None:
    payload = {
        "Authorization": f"Bearer {CANARY_BEARER}",
        "Cookie": CANARY_COOKIE,
        "X-API-Key": CANARY_API_KEY,
        "access_token": CANARY_NEW_TOKEN,
        "refresh_token": CANARY_REFRESH_TOKEN,
        "fb_exchange_token": CANARY_EXCHANGE_TOKEN,
        "client_secret": CANARY_APP_SECRET,
        "app_secret": CANARY_APP_SECRET,
        "appsecret_proof": "deadbeef" * 8,
        "input_token": CANARY_INPUT_TOKEN,
        "password": "notreal-notreal-notreal-notreal-pass",
        "nested": {"token": CANARY_NEW_TOKEN, "phone_number_id": "1122"},
        "items": [{"api_key": CANARY_API_KEY}, "plain"],
        "tenant_id": 33,
        "status": "ok",
    }
    out = redact_value(payload)
    for key in (
        "Authorization", "Cookie", "X-API-Key", "access_token", "refresh_token",
        "fb_exchange_token", "client_secret", "app_secret", "appsecret_proof",
        "input_token", "password",
    ):
        assert out[key] == REDACTED, key
    assert out["nested"] == {"token": REDACTED, "phone_number_id": "1122"}
    assert out["items"] == [{"api_key": REDACTED}, "plain"]
    assert out["tenant_id"] == 33 and out["status"] == "ok"
    _assert_no_canary(repr(out))


def test_redact_secrets_free_text_forms() -> None:
    text = (
        f"Authorization: Bearer {CANARY_BEARER}; Cookie: {CANARY_COOKIE}; "
        f'{{"access_token": "{CANARY_NEW_TOKEN}", "refresh_token": "{CANARY_REFRESH_TOKEN}"}} '
        f"api_key={CANARY_API_KEY} bare token {CANARY_EXCHANGE_TOKEN} "
        f"https://user:{CANARY_APP_SECRET}@example.test/x?verify_token={CANARY_VERIFY_TOKEN}#code={CANARY_OAUTH_CODE}"
    )
    out = redact_secrets(text)
    _assert_no_canary(out)
    assert f"Bearer {REDACTED}" in out
    assert "example.test/x" in out


def test_no_prefix_suffix_length_or_hash_fingerprint_is_emitted() -> None:
    line = redact_secrets(f"client_secret={CANARY_APP_SECRET}")
    assert line == f"client_secret={REDACTED}"
    assert str(len(CANARY_APP_SECRET)) not in line


# ── Fail-closed behaviour and installation ─────────────────────────────

def test_filter_fails_closed_on_unformattable_record() -> None:
    record = _record("nahla.test", "tenant=%d token=%s", ("not-an-int",))
    assert SecretRedactingFilter().filter(record) is True
    assert record.msg == log_redaction.REDACTED_RECORD
    assert record.args == ()


def test_unparseable_url_collapses_instead_of_leaking(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise ValueError("parse failure")

    monkeypatch.setattr(log_redaction, "urlsplit", _boom)
    out = redact_secrets(f"see https://x.test/a?client_secret={CANARY_APP_SECRET}")
    assert log_redaction.REDACTED_URL in out
    _assert_no_canary(out, CANARY_APP_SECRET)


def test_install_covers_root_handlers_and_named_loggers() -> None:
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    try:
        install_log_redaction()
        install_log_redaction()  # idempotent
        assert sum(isinstance(f, SecretRedactingFilter) for f in handler.filters) == 1
        for name in log_redaction.DEFAULT_REDACTED_LOGGERS:
            assert any(isinstance(f, SecretRedactingFilter) for f in logging.getLogger(name).filters), name
        app_logger = logging.getLogger("nahla.some.module")
        app_logger.setLevel(logging.INFO)
        app_logger.warning("refresh failed: %s", httpx.URL(TOKEN_URL))
    finally:
        root.removeHandler(handler)
    _assert_no_canary(stream.getvalue(), CANARY_APP_SECRET, CANARY_EXCHANGE_TOKEN)
    assert "oauth/access_token" in stream.getvalue()


def test_main_installs_redaction_on_root_handlers() -> None:
    """``backend/main.py`` must call ``install_log_redaction`` right after ``basicConfig``."""
    import inspect
    import re

    source = open(os.path.join(_BACKEND_ROOT, "main.py"), encoding="utf-8").read()
    assert re.search(r"^_secret_redact_filter = install_log_redaction\(\)", source, re.M)
    assert "addFilter(_secret_redact_filter)" not in source
    assert inspect.isfunction(install_log_redaction)


@pytest.mark.parametrize("key", [
    "access_token", "TOKEN", "refresh_token", "fb_exchange_token", "client_secret",
    "app_secret", "appsecret_proof", "api_key", "apikey", "X-Api-Key", "Authorization",
    "Cookie", "Set-Cookie", "hub.verify_token", "input_token", "password", "code", "state",
])
def test_required_keys_are_sensitive(key: str) -> None:
    assert log_redaction.is_sensitive_key(key)


@pytest.mark.parametrize("key", [
    "tenant_id", "phone_number_id", "grant_type", "client_id", "fields", "status",
    "token_status", "token_type", "token_expiry", "access_token_set", "verify_token_set",
    "input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "status_code",
    "error_code", "state_source",
])
def test_safe_diagnostic_keys_are_retained(key: str) -> None:
    assert not log_redaction.is_sensitive_key(key)


def test_operational_diagnostics_survive_in_free_text() -> None:
    line = (
        "[WA token] op=media_download tenant=33 source=merchant_oauth token_status=expiring_soon "
        "token_expiry=None token_type=long_lived access_token_set=True "
        '{"estimated_input_tokens": 467, "output_tokens": 12, "status_code": 200, "error_code": 190} '
        "state_source=persisted stage=exploring"
    )
    assert redact_secrets(line) == line
