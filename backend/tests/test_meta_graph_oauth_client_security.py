"""Security tests for secure Meta Graph OAuth transport (MockTransport + caplog)."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
for entry in (str(REPO), str(REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from core.log_redaction import SecretRedactingFilter, redact_graph_id  # noqa: E402
from services.meta_graph_oauth_client import (  # noqa: E402
    DEBUG_TOKEN_QUERY_EXCEPTION,
    assert_no_sensitive_query_params,
    debug_token,
    exchange_code_for_token,
    exchange_for_long_lived_token,
)

SYNTH_USER = "SYNTH-USER-TOKEN-877-ABCDEF"
SYNTH_APP = "app-test-877"
SYNTH_SECRET = "secret-test-877"
SYNTH_CODE = "oauth-code-synth-877"
WABA = "TEST-WABA-OAUTH-877"


def _ensure_production_httpx_redaction_filters() -> None:
    """Mirror backend/main.py — only httpx/httpcore get SecretRedactingFilter."""
    filt = SecretRedactingFilter()
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, SecretRedactingFilter) for f in logger.filters):
            logger.addFilter(filt)



@pytest.fixture(autouse=True)
def meta_env(monkeypatch):
    monkeypatch.setattr("services.meta_graph_oauth_client.META_APP_ID", SYNTH_APP)
    monkeypatch.setattr("services.meta_graph_oauth_client.META_APP_SECRET", SYNTH_SECRET)


def _capture(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/debug_token"):
            return httpx.Response(200, json={"data": {"is_valid": True, "type": "USER"}})
        if path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "long-lived-synth", "expires_in": 3600})
        return httpx.Response(404, json={"error": {"message": "unmocked"}})

    return httpx.MockTransport(handler)


def test_debug_token_uses_get_with_documented_input_token_query_exception():
    captured: list[httpx.Request] = []
    transport = _capture(captured)
    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            await debug_token(SYNTH_USER, client=client)
    asyncio.run(_run())
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert_no_sensitive_query_params(req)
    assert req.url.params.get("input_token") == SYNTH_USER
    assert "access_token" not in req.url.params
    assert "input_token" in DEBUG_TOKEN_QUERY_EXCEPTION
    assert req.headers.get("authorization", "").startswith("Bearer ")
    assert SYNTH_SECRET not in str(req.url)
    assert f"{SYNTH_APP}|{SYNTH_SECRET}" not in str(req.url)


def test_code_exchange_uses_get_query_not_access_token():
    captured: list[httpx.Request] = []
    transport = _capture(captured)
    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            await exchange_code_for_token(
                {
                    "client_id": SYNTH_APP,
                    "client_secret": SYNTH_SECRET,
                    "code": SYNTH_CODE,
                },
                client=client,
            )
    asyncio.run(_run())
    req = captured[0]
    assert req.method == "GET"
    assert_no_sensitive_query_params(req)
    assert req.url.params.get("code") == SYNTH_CODE
    assert "access_token" not in req.url.params


def test_fb_exchange_uses_get_query_not_access_token():
    captured: list[httpx.Request] = []
    transport = _capture(captured)
    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            await exchange_for_long_lived_token(SYNTH_USER, client=client)
    asyncio.run(_run())
    req = captured[0]
    assert req.method == "GET"
    assert_no_sensitive_query_params(req)
    assert req.url.params.get("fb_exchange_token") == SYNTH_USER
    assert "access_token" not in req.url.params


def test_oauth_success_logs_exclude_tokens_without_test_only_filters(caplog):
    """Prove safe source logs + production httpx/httpcore filters only (no test-only filter)."""
    captured: list[httpx.Request] = []
    transport = _capture(captured)
    caplog.set_level(logging.INFO, logger="nahla.meta_graph_oauth")
    caplog.set_level(logging.INFO, logger="httpx")
    _ensure_production_httpx_redaction_filters()
    oauth_logger = logging.getLogger("nahla.meta_graph_oauth")
    assert not any(isinstance(f, SecretRedactingFilter) for f in oauth_logger.filters)

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            await debug_token(SYNTH_USER, client=client)
            await exchange_for_long_lived_token(SYNTH_USER, client=client)

    asyncio.run(_run())
    combined = caplog.text
    for secret in (SYNTH_USER, SYNTH_SECRET, SYNTH_CODE, f"{SYNTH_APP}|{SYNTH_SECRET}"):
        assert secret not in combined


def test_httpx_exception_logging_sanitized_via_production_filter(caplog):
    caplog.set_level(logging.ERROR, logger="httpx")
    _ensure_production_httpx_redaction_filters()
    url = httpx.URL(
        f"https://graph.facebook.com/v20.0/{WABA}/subscribed_apps"
        f"?access_token={SYNTH_USER}&input_token={SYNTH_USER}"
    )
    request = httpx.Request("POST", url)
    error = httpx.ConnectError(f"request failed {url} waba={WABA}", request=request)
    logging.getLogger("httpx").error("HTTP exception: %s", error)
    combined = caplog.text
    assert SYNTH_USER not in combined
    assert WABA not in combined
    assert redact_graph_id(WABA) in combined
    assert "access_token=REDACTED" in combined


def test_graphscript_oauth_access_token_get_only():
    tests_dir = str(REPO / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from test_coexistence_transaction_and_replay import GraphScript

    script = GraphScript(mode="success")
    get_req = httpx.Request("GET", "https://graph.facebook.com/v20.0/oauth/access_token")
    get_resp = script(get_req)
    assert get_resp.status_code == 200
    post_req = httpx.Request("POST", "https://graph.facebook.com/v20.0/oauth/access_token")
    post_resp = script(post_req)
    assert post_resp.status_code == 405
    assert "requires GET" in post_resp.json()["error"]["message"]
