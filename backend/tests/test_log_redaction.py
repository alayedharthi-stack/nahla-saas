"""Tests for secret redaction in logs."""

from __future__ import annotations

import logging
import os
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from core.log_redaction import SecretRedactingFilter, redact_secrets  # noqa: E402


def test_redact_access_token_query_param():
    url = (
        "HTTP Request: GET https://graph.facebook.com/v21.0/123/products"
        "?filter=x&access_token=EAABsupersecret1234567890"
    )
    out = redact_secrets(url)
    assert "EAABsupersecret" not in out
    assert "access_token=REDACTED" in out


def test_redact_bearer_header():
    line = "Authorization: Bearer EAABsupersecret1234567890"
    out = redact_secrets(line)
    assert "EAABsupersecret" not in out
    assert "Bearer REDACTED" in out


def test_filter_scrubs_httpx_style_record():
    filt = SecretRedactingFilter()
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "HTTP Request: GET https://graph.facebook.com/v21.0/cat/products"
            "?access_token=EAAB-leaked-token"
        ),
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "EAAB-leaked" not in record.msg
    assert "access_token=REDACTED" in record.msg


def test_graph_error_sanitized_no_token_in_message():
    from services.meta_catalog_sync_confirm import _sanitize_sync_error  # noqa: PLC0415

    msg = _sanitize_sync_error(
        {
            "error": "meta_http_error",
            "meta": {
                "response": {
                    "error": {
                        "message": "Invalid OAuth access token - Cannot parse access token",
                        "type": "OAuthException",
                    }
                }
            },
        }
    )
    assert "EAAB" not in msg
    assert "access_token=" not in msg.lower()
