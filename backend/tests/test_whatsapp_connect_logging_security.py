"""Production-reachable caplog security tests for WA Direct Meta Graph logging."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_BACKEND_ROOT)
for entry in (_REPO_ROOT, _BACKEND_ROOT):
    if entry not in sys.path:
        sys.path.insert(0, entry)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.log_redaction import redact_graph_id  # noqa: E402
from routers import whatsapp_connect as wc  # noqa: E402
from services.wa_direct_logging import (  # noqa: E402
    log_wa_direct_exception,
    log_wa_direct_graph_result,
)
from services.whatsapp_platform.token_manager import WhatsAppTokenContext  # noqa: E402

TOKEN = "SYNTH-WA-DIRECT-TOKEN-877"
OTP = "OTP-CODE-877"
PHONE_E164 = "+966501234567"
PHONE_NATIONAL = "501234567"
PHONE_ID = "PHONE-ID-CANARY-877"
WABA = "WABA-CANARY-877"
GRAPH_CANARY = "GRAPH-RESPONSE-CANARY-877"
TENANT = 990877

CANARIES = (TOKEN, OTP, PHONE_E164, PHONE_NATIONAL, PHONE_ID, WABA, GRAPH_CANARY)
FORBIDDEN_LOGGER_ARGS = (
    "otp_data",
    "verify_data",
    "verify_payload",
    "add_data",
    "list_data",
    "register_data",
    "reg_data",
)


def _assert_safe_logs(log_text: str) -> None:
    for value in CANARIES:
        assert value not in log_text


def _token_ctx() -> WhatsAppTokenContext:
    return WhatsAppTokenContext(
        token=TOKEN,
        source="platform",
        token_status="valid",
        expires_at=datetime.now(timezone.utc),
        oauth_session_status="ok",
        oauth_session_message=None,
    )


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(tenant_id=TENANT, jwt_payload={"tenant_id": TENANT}))


def _conn(**kwargs):
    base = {
        "tenant_id": TENANT,
        "phone_number": PHONE_E164,
        "phone_number_id": PHONE_ID,
        "whatsapp_business_account_id": WABA,
        "status": "otp_pending",
        "connection_type": "direct",
        "provider": "meta",
        "sending_enabled": False,
        "business_display_name": "Test Store",
        "extra_metadata": {},
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _mock_db(conn=None):
    db = MagicMock()
    q = MagicMock()
    q.filter_by.return_value.first.return_value = conn
    q.filter.return_value.first.return_value = conn
    db.query.return_value = q
    db.commit = MagicMock()
    db.refresh = MagicMock()
    return db


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _mock_client(get_payload=None, post_payload=None, post_exc=None, post_side_effect=None):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    if get_payload is not None:
        client.get = AsyncMock(return_value=_Resp(200, get_payload))
    if post_exc is not None:
        client.post = AsyncMock(side_effect=post_exc)
    elif post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    elif post_payload is not None:
        client.post = AsyncMock(return_value=_Resp(200, post_payload))
    return client


@pytest.fixture(autouse=True)
def _wa_config(monkeypatch):
    monkeypatch.setattr("core.config.WA_BUSINESS_ACCOUNT_ID", WABA, raising=False)
    monkeypatch.setattr("core.config.META_GRAPH_API_VERSION", "v20.0", raising=False)


def test_helper_graph_result_excludes_canaries(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    log_wa_direct_graph_result(
        stage="helper-success",
        tenant_id=TENANT,
        response={"success": True, "access_token": TOKEN, "message": GRAPH_CANARY},
        phone_number_id=PHONE_ID,
        waba_id=WABA,
    )
    _assert_safe_logs(caplog.text)
    assert redact_graph_id(PHONE_ID) in caplog.text


def test_helper_exception_excludes_canaries(caplog):
    caplog.set_level(logging.WARNING, logger="nahla-backend")
    exc = httpx.ConnectError(
        f"boom token={TOKEN} otp={OTP} phone={PHONE_E164} id={PHONE_ID}",
        request=httpx.Request("GET", f"https://graph.facebook.com/v20.0/{WABA}"),
    )
    log_wa_direct_exception("network", exc, tenant_id=TENANT, secrets=[TOKEN, OTP, PHONE_ID, PHONE_E164])
    _assert_safe_logs(caplog.text)


@pytest.mark.parametrize(
    "post_payload",
    [
        {"success": True},
        {"error": {"code": 100, "error_subcode": 2388091, "message": GRAPH_CANARY}},
    ],
)
def test_request_otp_no_raw_payload(caplog, post_payload):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    async def post_side(url, **kw):
        if url.endswith("/request_code"):
            return _Resp(200, post_payload)
        return _Resp(200, {"id": PHONE_ID})

    client = _mock_client(get_payload={"data": []}, post_side_effect=post_side)
    body = wc.DirectOTPRequest(phone_number=PHONE_E164, display_name="Store", method="SMS")
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client):
        asyncio.run(wc.direct_request_otp(body, _request(), _mock_db()))
    _assert_safe_logs(caplog.text)


def test_pending_resume_otp_no_raw_payload(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    conn = _conn(status="pending", phone_number=PHONE_E164)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    async def get_side(url, **kw):
        if url.endswith(f"/{PHONE_ID}"):
            return _Resp(200, {"id": PHONE_ID, "code_verification_status": "NOT_VERIFIED"})
        return _Resp(200, {"data": []})

    client.get = AsyncMock(side_effect=get_side)
    client.post = AsyncMock(return_value=_Resp(200, {"success": True}))
    body = wc.DirectOTPRequest(phone_number=PHONE_E164, display_name="Store", method="SMS")
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client):
        asyncio.run(wc.direct_request_otp(body, _request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)


def test_resend_otp_success_and_failure(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    conn = _conn()
    client = _mock_client(post_payload={"success": True})
    body = wc.DirectVerifyRequest(phone_number_id=PHONE_ID, code=OTP)
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client):
        asyncio.run(wc.direct_resend_otp(body, _request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="nahla-backend")
    client2 = AsyncMock()
    client2.__aenter__.return_value = client2
    client2.__aexit__.return_value = False
    client2.get = AsyncMock(return_value=_Resp(200, {"data": [{"id": PHONE_ID, "display_phone_number": PHONE_E164}]}))
    client2.post = AsyncMock(return_value=_Resp(400, {"error": {"code": 100, "error_subcode": 33, "message": GRAPH_CANARY}}))
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client2):
        with pytest.raises(Exception):
            asyncio.run(wc.direct_resend_otp(body, _request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)


def test_verify_otp_success_and_failure(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    conn = _conn()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False

    async def get_side(url, **kw):
        return _Resp(200, {"id": PHONE_ID, "code_verification_status": "VERIFIED", "display_phone_number": PHONE_E164, "verified_name": "Store"})

    async def post_side(url, **kw):
        if url.endswith("/verify_code"):
            return _Resp(200, {"success": True})
        if url.endswith("/register"):
            return _Resp(200, {"success": True})
        return _Resp(200, {"success": True})

    client.get = AsyncMock(side_effect=get_side)
    client.post = AsyncMock(side_effect=post_side)
    body = wc.DirectVerifyRequest(phone_number_id=PHONE_ID, code=OTP)
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch("services.meta_coexistence.is_coexistence_mode", return_value=False), patch.object(
        wc, "_finalize_connected_or_http"
    ), patch.object(wc.httpx, "AsyncClient", return_value=client):
        asyncio.run(wc.direct_verify_otp(body, _request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="nahla-backend")
    client2 = _mock_client(get_payload={"id": PHONE_ID}, post_payload={"error": {"code": 100, "message": GRAPH_CANARY}})
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch("services.meta_coexistence.is_coexistence_mode", return_value=False), patch.object(
        wc.httpx, "AsyncClient", return_value=client2
    ):
        with pytest.raises(Exception):
            asyncio.run(wc.direct_verify_otp(body, _request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)


def test_refresh_status_success_and_failure(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    conn = _conn(status="pending")
    client = _mock_client(
        get_payload={"id": PHONE_ID, "code_verification_status": "VERIFIED", "display_phone_number": PHONE_E164, "verified_name": "Store", "status": "CONNECTED"}
    )
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc, "_finalize_connected_or_http"), patch.object(wc, "_build_wa_status", return_value={}), patch.object(
        wc.httpx, "AsyncClient", return_value=client
    ):
        asyncio.run(wc.refresh_status_from_meta(_request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="nahla-backend")
    client2 = AsyncMock()
    client2.__aenter__.return_value = client2
    client2.__aexit__.return_value = False
    client2.get = AsyncMock(return_value=_Resp(400, {"error": {"code": 190, "message": GRAPH_CANARY}}))
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client2):
        asyncio.run(wc.refresh_status_from_meta(_request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)


def test_save_profile_success_and_failure(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    conn = _conn(status="connected")
    client = _mock_client(post_payload={"success": True})
    body = wc.SaveProfileRequest(phone_number_id=PHONE_ID, about="about")
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client):
        asyncio.run(wc.direct_save_profile(body, _request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="nahla-backend")
    client2 = _mock_client(post_payload={"error": {"code": 100, "message": GRAPH_CANARY}})
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client2):
        with pytest.raises(Exception):
            asyncio.run(wc.direct_save_profile(body, _request(), _mock_db(conn)))
    _assert_safe_logs(caplog.text)


def test_network_exception_redacted(caplog):
    caplog.set_level(logging.ERROR, logger="nahla-backend")

    async def boom_post(url, **kw):
        raise httpx.ConnectError(
            f"network token={TOKEN} phone={PHONE_E164} graph={WABA}",
            request=httpx.Request("POST", f"https://graph.facebook.com/v20.0/{PHONE_ID}/request_code"),
        )

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get = AsyncMock(return_value=_Resp(200, {"data": []}))
    client.post = AsyncMock(side_effect=boom_post)
    body = wc.DirectOTPRequest(phone_number=PHONE_E164, display_name="Store", method="SMS")
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), patch.object(
        wc, "get_token_for_operation", new=AsyncMock(return_value=_token_ctx())
    ), patch.object(wc.httpx, "AsyncClient", return_value=client):
        with pytest.raises(Exception):
            asyncio.run(wc.direct_request_otp(body, _request(), _mock_db()))
    _assert_safe_logs(caplog.text)


def test_no_forbidden_logger_payload_args_in_source():
    src_path = Path(__file__).resolve().parents[1] / "routers" / "whatsapp_connect.py"
    lines = src_path.read_text(encoding="utf-8").splitlines()
    violations = []
    for idx, line in enumerate(lines, start=1):
        if "logger." not in line:
            continue
        for var in FORBIDDEN_LOGGER_ARGS:
            if re.search(rf",\s*{re.escape(var)}\s*[,)]", line):
                violations.append((idx, line.strip(), var))
    assert not violations, f"unsafe logger args: {violations[:5]}"


