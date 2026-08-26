"""Security caplog tests for Embedded Signup / Meta Graph logging (no raw payloads)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_BACKEND_ROOT)
for entry in (_REPO_ROOT, _BACKEND_ROOT):
    if entry not in sys.path:
        sys.path.insert(0, entry)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.log_redaction import redact_graph_id  # noqa: E402
from routers.whatsapp_embedded import (  # noqa: E402
    _get_waba_id_from_token,
    _log_embedded_graph_result,
    _log_select_phone_otp_result,
)

SYNTH_TOKEN = "SYNTH-EMBEDDED-ACCESS-TOKEN-877-FINAL"
WABA = "WABA-CANARY-877-FINAL"
BUSINESS = "BUSINESS-CANARY-877-FINAL"
PHONE = "PHONE-CANARY-877-FINAL"
E164 = "+966507778899"
CODE = "OAUTH-CODE-CANARY-877-FINAL"
TENANT = 990877

CANARIES = (SYNTH_TOKEN, WABA, BUSINESS, PHONE, E164, CODE)


def _assert_canaries_absent(log_text: str) -> None:
    for value in CANARIES:
        assert value not in log_text


@pytest.mark.parametrize(
    "response,level",
    [
        ({"success": True}, "info"),
        ({"error": {"code": 100, "error_subcode": 2388091, "message": "rate limited"}}, "warning"),
    ],
)
def test_log_embedded_graph_result_excludes_sensitive_values(caplog, response, level):
    caplog.set_level(logging.INFO if level == "info" else logging.WARNING, logger="routers.whatsapp_embedded")
    _log_embedded_graph_result(
        stage="unit-test-stage",
        tenant_id=TENANT,
        phone_number_id=PHONE,
        response=response,
        level=level,
    )
    combined = caplog.text
    _assert_canaries_absent(combined)
    assert str(response) not in combined
    assert redact_graph_id(PHONE) in combined


def test_select_phone_otp_wrapper_excludes_sensitive_values(caplog):
    caplog.set_level(logging.INFO, logger="routers.whatsapp_embedded")
    _log_select_phone_otp_result(
        tenant_id=TENANT,
        phone_number_id=PHONE,
        otp_data={"success": True, "access_token": SYNTH_TOKEN},
    )
    combined = caplog.text
    _assert_canaries_absent(combined)
    assert SYNTH_TOKEN not in combined


def test_business_lookup_success_logs_no_raw_payload(caplog):
    caplog.set_level(logging.INFO, logger="routers.whatsapp_embedded")

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def _fake_get(url, **kwargs):
        if url.endswith("/me/businesses"):
            return _Resp({"data": [{"id": BUSINESS, "name": "Canary Biz"}]})
        if url.endswith("/whatsapp_business_accounts"):
            return _Resp({"data": [{"id": WABA, "name": "Canary WABA"}]})
        raise AssertionError(f"unexpected url {url}")

    with patch("routers.whatsapp_embedded.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        client.get.side_effect = _fake_get
        client_cls.return_value = client

        result = asyncio.run(_get_waba_id_from_token(SYNTH_TOKEN, debug_info={"granular_scopes": []}))

    assert result == WABA
    combined = caplog.text
    _assert_canaries_absent(combined)
    assert '{"data"' not in combined
    assert redact_graph_id(WABA) in combined
    assert redact_graph_id(BUSINESS) in combined


def test_business_lookup_failure_logs_no_raw_payload(caplog):
    caplog.set_level(logging.WARNING, logger="routers.whatsapp_embedded")

    async def _boom_get(*_a, **_k):
        raise httpx.ConnectError(
            f"failed token={SYNTH_TOKEN} waba={WABA} phone={E164}",
            request=httpx.Request("GET", f"https://graph.facebook.com/v20.0/{WABA}"),
        )

    with patch("routers.whatsapp_embedded.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        client.get.side_effect = _boom_get
        client_cls.return_value = client

        with pytest.raises(HTTPException):
            asyncio.run(_get_waba_id_from_token(SYNTH_TOKEN, debug_info={"granular_scopes": []}))

    combined = caplog.text
    _assert_canaries_absent(combined)
