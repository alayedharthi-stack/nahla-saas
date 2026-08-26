"""caplog tests for select-phone OTP structured logging (no raw Meta payloads)."""
from __future__ import annotations

import logging
import os
import sys

import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.log_redaction import redact_graph_id  # noqa: E402
from routers.whatsapp_embedded import _log_select_phone_otp_result  # noqa: E402

RAW_PHONE_ID = "PHONE-RAW-SELECT-877"
RAW_E164 = "+966501234567"
SYNTH_TOKEN = "synthetic-access-token-select-phone-877"
TENANT = 990877


@pytest.mark.parametrize(
    "otp_data,level",
    [
        ({"success": True}, logging.INFO),
        ({"error": {"code": 100, "error_subcode": 2388091, "message": "rate limited"}}, logging.WARNING),
    ],
)
def test_select_phone_otp_logging_excludes_sensitive_values(caplog, otp_data, level):
    caplog.set_level(level, logger="routers.whatsapp_embedded")
    _log_select_phone_otp_result(
        tenant_id=TENANT,
        phone_number_id=RAW_PHONE_ID,
        otp_data=otp_data,
    )
    combined = caplog.text
    assert SYNTH_TOKEN not in combined
    assert RAW_PHONE_ID not in combined
    assert RAW_E164 not in combined
    assert str(otp_data) not in combined
    assert redact_graph_id(RAW_PHONE_ID) in combined
