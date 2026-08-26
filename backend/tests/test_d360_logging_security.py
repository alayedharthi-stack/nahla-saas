"""Production-reachable security tests for 360dialog logging and diagnostics."""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_BACKEND_ROOT)
for entry in (_REPO_ROOT, _BACKEND_ROOT):
    if entry not in sys.path:
        sys.path.insert(0, entry)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.log_redaction import redact_graph_id  # noqa: E402
from routers.whatsapp_connect import _log_d360_verify  # noqa: E402
from services.d360_logging import (  # noqa: E402
    d360_response_summary,
    d360_safe_error_payload,
    d360_url_flags,
    log_d360_verify,
)

API_KEY = "D360-API-KEY-CANARY-877"
CHANNEL = "D360-CHANNEL-CANARY-877"
WABA = "D360-WABA-CANARY-877"
PHONE_ID = "D360-PHONE-ID-CANARY-877"
PHONE = "+966500008877"
URL = "https://callback.example.test/secret/path?token=D360-URL-TOKEN-877"
PAYLOAD = "D360-ERROR-PAYLOAD-CANARY-877"
TENANT = 990877

CANARIES = (API_KEY, CHANNEL, WABA, PHONE_ID, PHONE, URL, PAYLOAD, "D360-URL-TOKEN-877")


def _assert_absent(log_or_text: str) -> None:
    for value in CANARIES:
        assert value not in log_or_text
    assert "api_key_tail" not in log_or_text
    assert "response_body_preview" not in log_or_text
    assert "channel_body_preview" not in log_or_text
    assert "waba_body_preview" not in log_or_text


def _conn():
    return SimpleNamespace(
        id=42,
        phone_number_id=PHONE_ID,
        whatsapp_business_account_id=WABA,
        access_token=API_KEY,
        extra_metadata={"provider_details": {"channel_id": CHANNEL}},
    )


def test_log_d360_verify_success_no_raw_payload(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    response = {
        "url": URL,
        "waba_id": WABA,
        "numbers_on_this_waba": [PHONE_ID, PHONE],
        "access_token": API_KEY,
        "message": PAYLOAD,
    }
    log_d360_verify(
        operation="unit-success",
        tenant_id=TENANT,
        connection_id=42,
        channel_id=CHANNEL,
        phone_number_id=PHONE_ID,
        waba_id=WABA,
        api_key_present=True,
        endpoint_used="GET /v1/configs/webhook",
        response=response,
        response_status=200,
        parsed_url=URL,
        expected_url=URL,
        result="verified_match",
    )
    _assert_absent(caplog.text)
    assert "url_matches_expected=True" in caplog.text
    assert "api_key_present=True" in caplog.text
    assert redact_graph_id(PHONE_ID) in caplog.text


def test_log_d360_verify_failure_no_raw_payload(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    response = {
        "error": {"code": 401, "message": PAYLOAD, "type": "invalid_api_key"},
        "status_code": 401,
        "url": URL,
    }
    log_d360_verify(
        operation="unit-failure",
        tenant_id=TENANT,
        api_key_present=True,
        endpoint_used="GET /waba_webhook",
        response=response,
        response_status=401,
        parsed_url=URL,
        expected_url="https://api.nahlah.ai/webhook/whatsapp/360dialog",
        result="remote_error",
        extra={"waba_id_remote_present": True, "numbers_on_this_waba_count": 2},
    )
    _assert_absent(caplog.text)
    assert "error_code=401" in caplog.text
    assert "numbers_on_this_waba_count=2" in caplog.text


def test_wrapper_log_d360_verify_uses_safe_fields(caplog):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    _log_d360_verify(
        operation="wrapper",
        tenant_id=TENANT,
        conn=_conn(),
        endpoint_used="POST /v1/configs/webhook",
        response={"error": PAYLOAD, "url": URL, "waba_id": WABA},
        response_status=400,
        parsed_url=URL,
        expected_url=URL,
        result="failed",
        extra={"request_id": "abc123", "injected_secret": API_KEY},
    )
    _assert_absent(caplog.text)
    assert "request_id='abc123'" in caplog.text
    assert "injected_secret" not in caplog.text


def test_channel_and_waba_response_summaries_exclude_payload():
    summary = d360_response_summary(
        {
            "error": {"code": 100, "message": PAYLOAD},
            "status_code": 400,
            "url": URL,
            "waba_id": WABA,
            "numbers_on_this_waba": [PHONE_ID],
        }
    )
    rendered = str(summary)
    _assert_absent(rendered)
    assert summary["numbers_count"] == 1
    assert summary["has_waba_id"] is True


def test_url_flags_preserve_match_without_raw_urls():
    flags = d360_url_flags(URL, URL)
    rendered = str(flags)
    _assert_absent(rendered)
    assert flags["url_matches_expected"] is True
    assert flags["remote_url_present"] is True

    mismatch = d360_url_flags(URL, "https://api.nahlah.ai/webhook/whatsapp/360dialog")
    assert mismatch["url_matches_expected"] is False
    _assert_absent(str(mismatch))


def test_network_exception_redacted_in_safe_payload():
    exc = httpx.ConnectError(
        f"boom key={API_KEY} channel={CHANNEL} url={URL}",
        request=httpx.Request("GET", URL),
    )
    payload = d360_safe_error_payload(exc, secrets=[API_KEY, CHANNEL, URL], operation="diagnose_channel_read")
    rendered = str(payload)
    _assert_absent(rendered)
    assert payload["error_type"] == "ConnectError"
    assert payload["retryable"] is True


def test_diagnostic_registration_block_shape():
    from services.d360_logging import d360_safe_registration_block

    block = d360_safe_registration_block(
        expected_url=URL,
        channel_remote_url=URL,
        waba_remote_url=URL,
        waba_id_remote=WABA,
        numbers_on_waba=[PHONE_ID],
    )
    block["phone_id_drift"] = True
    rendered = str(block)
    _assert_absent(rendered)
    assert block["numbers_on_this_waba_count"] == 1
    assert block["channel_matches"] is True


def test_no_raw_preview_helpers_in_whatsapp_connect_source():
    src = Path(__file__).resolve().parents[1] / "routers" / "whatsapp_connect.py"
    text = src.read_text(encoding="utf-8")
    assert "_d360_body_preview" not in text
    assert "_d360_key_tail" not in text
    for pattern in (
        r"logger\.[a-z]+\([^)]*api_key_tail",
        r"channel_body_preview\s*=",
        r"waba_body_preview\s*=",
        r"response_body_preview",
    ):
        assert not re.search(pattern, text), pattern


@pytest.mark.parametrize("func_name", ["log_d360_verify", "_log_d360_verify"])
def test_numbers_on_this_waba_count_only(caplog, func_name):
    caplog.set_level(logging.INFO, logger="nahla-backend")
    response = {"numbers_on_this_waba": [PHONE_ID, "other"], "waba_id": WABA}
    if func_name == "log_d360_verify":
        log_d360_verify(
            operation="numbers-count",
            tenant_id=TENANT,
            api_key_present=True,
            endpoint_used="GET /waba_webhook",
            response=response,
            result="ok",
            extra={"numbers_on_this_waba_count": 2},
        )
    else:
        _log_d360_verify(
            operation="numbers-count",
            tenant_id=TENANT,
            conn=_conn(),
            endpoint_used="GET /waba_webhook",
            response=response,
            result="ok",
            extra={"numbers_on_this_waba_count": 2, "waba_id_remote_present": True},
        )
    _assert_absent(caplog.text)
    assert PHONE_ID not in caplog.text
    assert "numbers_on_this_waba_count=2" in caplog.text
