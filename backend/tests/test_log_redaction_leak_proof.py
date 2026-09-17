"""Leak proofs that use only the pre-existing redaction API, so the same file
fails on the previous ``core/log_redaction.py`` and passes on the fixed one.

Synthetic canaries only; no real credential appears here."""
from __future__ import annotations

import logging
import os
import sys

import httpx

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from core.log_redaction import SecretRedactingFilter, redact_secrets, redact_value  # noqa: E402

CANARY_APP_SECRET = "notreal-notreal-notreal-notreal-app-secret"
CANARY_EXCHANGE_TOKEN = "EAAcanaryExchangeTokenAAAA1111BBBB2222CCCC3333"
TOKEN_URL = (
    "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token"
    f"&client_id=123456&client_secret={CANARY_APP_SECRET}&fb_exchange_token={CANARY_EXCHANGE_TOKEN}"
)


def _httpx_record(url_arg) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"', args=("GET", url_arg, "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )


def test_httpx_url_object_argument_is_redacted() -> None:
    """The production leak: ``httpx`` passes ``httpx.URL``, not ``str``."""
    record = _httpx_record(httpx.URL(TOKEN_URL))
    SecretRedactingFilter().filter(record)
    line = logging.Formatter("%(message)s").format(record)
    assert CANARY_APP_SECRET not in line
    assert CANARY_EXCHANGE_TOKEN not in line


def test_string_url_argument_is_redacted_too() -> None:
    record = _httpx_record(TOKEN_URL)
    SecretRedactingFilter().filter(record)
    line = logging.Formatter("%(message)s").format(record)
    assert CANARY_APP_SECRET not in line
    assert CANARY_EXCHANGE_TOKEN not in line


def test_exception_text_with_credential_url_is_redacted() -> None:
    request = httpx.Request("GET", TOKEN_URL)
    try:
        raise httpx.HTTPStatusError(
            f"Client error '400' for url '{request.url}'", request=request,
            response=httpx.Response(400, request=request),
        )
    except httpx.HTTPStatusError:
        record = logging.LogRecord(
            name="nahla.whatsapp.token_manager", level=logging.WARNING, pathname=__file__,
            lineno=1, msg="refresh failed", args=(), exc_info=sys.exc_info(),
        )
    SecretRedactingFilter().filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    assert CANARY_APP_SECRET not in rendered
    assert CANARY_EXCHANGE_TOKEN not in rendered


def test_redact_value_handles_url_objects() -> None:
    out = redact_value((httpx.URL(TOKEN_URL),))
    assert CANARY_APP_SECRET not in repr(out)
    assert CANARY_EXCHANGE_TOKEN not in repr(out)


def test_fb_exchange_token_query_value_is_redacted_in_text() -> None:
    out = redact_secrets(TOKEN_URL)
    assert CANARY_EXCHANGE_TOKEN not in out
    assert CANARY_APP_SECRET not in out
    assert "grant_type=fb_exchange_token" in out
