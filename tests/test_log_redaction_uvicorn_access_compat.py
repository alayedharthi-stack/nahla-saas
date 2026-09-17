"""Formatter-compatibility proofs for ``SecretRedactingFilter``.

Production finding (deployment 84953470, main e49c27a): the filter pre-formatted
the message and cleared ``record.args``.  ``uvicorn.logging.AccessFormatter``
unpacks the original five access-log arguments itself, so every access record
raised ``ValueError: not enough values to unpack (expected 5, got 0)`` and
Python printed a "Logging error" traceback instead of the access line.

These tests drive the *real* uvicorn and httpx objects through the filter and
prove that formatting succeeds, that argument structure and cardinality are
preserved, and that every credential canary is still replaced by the fixed
``REDACTED`` marker — on single and double application, and in the fail-closed
path.  Every value below is synthetic.
"""

from __future__ import annotations

import io
import logging
import os
import sys
from unittest.mock import patch

import httpx
import pytest
import uvicorn.config
import uvicorn.logging

# Root pytest.ini puts ``backend`` on ``pythonpath``; this keeps direct invocation working too.
_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core import log_redaction  # noqa: E402
from core.log_redaction import (  # noqa: E402
    REDACTED,
    REDACTED_RECORD,
    SecretRedactingFilter,
    install_log_redaction,
)

CANARY_APP_SECRET = "notreal-notreal-notreal-notreal-app-secret"
CANARY_EXCHANGE_TOKEN = "EAAcanaryExchangeTokenAAAA1111BBBB2222CCCC3333"
CANARY_INPUT_TOKEN = "EAAcanaryInputTokenGGGG7777HHHH8888IIII9999"
CANARY_ACCESS_TOKEN = "EAAcanaryAccessTokenJJJJ0000KKKK1111LLLL2222"
CANARY_VERIFY_TOKEN = "notreal-notreal-notreal-notreal-verify"
CANARY_OAUTH_CODE = "notreal-notreal-notreal-notreal-code"
CANARY_STATE = "notreal-notreal-notreal-notreal-state"
ALL_CANARIES = (
    CANARY_APP_SECRET,
    CANARY_EXCHANGE_TOKEN,
    CANARY_INPUT_TOKEN,
    CANARY_ACCESS_TOKEN,
    CANARY_VERIFY_TOKEN,
    CANARY_OAUTH_CODE,
    CANARY_STATE,
)

CLIENT_ADDR = "100.64.0.2:61644"
UVICORN_ACCESS_MSG = '%s - "%s %s HTTP/%s" %d'
UVICORN_ACCESS_FMT = uvicorn.config.LOGGING_CONFIG["formatters"]["access"]["fmt"]
HTTPX_MSG = 'HTTP Request: %s %s "%s %d %s"'


def _assert_no_canary(text: str) -> None:
    for canary in ALL_CANARIES:
        assert canary not in text, f"canary leaked: {canary[:8]}…"


def _record(name: str, msg, args, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, exc_info)


def _access_formatter() -> logging.Formatter:
    # The exact formatter uvicorn installs on ``uvicorn.access`` in production.
    return uvicorn.logging.AccessFormatter(fmt=UVICORN_ACCESS_FMT, use_colors=False)


def _webhook_verify_record() -> logging.LogRecord:
    path = (
        "/webhook/whatsapp?hub.mode=subscribe"
        f"&hub.verify_token={CANARY_VERIFY_TOKEN}&hub.challenge=1234567890"
    )
    return _record("uvicorn.access", UVICORN_ACCESS_MSG, (CLIENT_ADDR, "GET", path, "1.1", 200))


def _oauth_callback_record() -> logging.LogRecord:
    path = f"/auth/meta/callback?code={CANARY_OAUTH_CODE}&state={CANARY_STATE}"
    return _record("uvicorn.access", UVICORN_ACCESS_MSG, (CLIENT_ADDR, "GET", path, "1.1", 302))


def _httpx_refresh_record() -> logging.LogRecord:
    url = httpx.URL(
        "https://graph.facebook.com/v21.0/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": "1234567890",
            "client_secret": CANARY_APP_SECRET,
            "fb_exchange_token": CANARY_EXCHANGE_TOKEN,
        },
    )
    return _record("httpx", HTTPX_MSG, ("GET", url, "HTTP/1.1", 200, "OK"))


def _httpx_debug_token_record() -> logging.LogRecord:
    url = httpx.URL(
        "https://graph.facebook.com/v21.0/debug_token",
        params={"input_token": CANARY_INPUT_TOKEN, "access_token": CANARY_ACCESS_TOKEN},
    )
    return _record("httpx", HTTPX_MSG, ("GET", url, "HTTP/1.1", 200, "OK"))


# ── A. Real uvicorn AccessFormatter ────────────────────────────────────

def test_a_real_uvicorn_access_formatter_formats_after_filter() -> None:
    record = _webhook_verify_record()
    assert SecretRedactingFilter().filter(record) is True

    assert isinstance(record.args, tuple)
    assert len(record.args) == 5
    client_addr, method, path, http_version, status = record.args
    assert client_addr == CLIENT_ADDR
    assert method == "GET"
    assert http_version == "1.1"
    assert status == 200 and isinstance(status, int)
    assert path.startswith("/webhook/whatsapp?hub.mode=subscribe")
    assert f"hub.verify_token={REDACTED}" in path
    assert "hub.challenge=1234567890" in path

    rendered = _access_formatter().format(record)  # must not raise
    _assert_no_canary(rendered)
    assert CLIENT_ADDR in rendered
    assert 'GET /webhook/whatsapp?hub.mode=subscribe' in rendered
    assert "HTTP/1.1" in rendered
    assert "200 OK" in rendered
    assert REDACTED in rendered


def test_a_real_uvicorn_access_formatter_redacts_oauth_code_and_state() -> None:
    record = _oauth_callback_record()
    SecretRedactingFilter().filter(record)
    rendered = _access_formatter().format(record)
    _assert_no_canary(rendered)
    assert "GET /auth/meta/callback?" in rendered
    assert f"code={REDACTED}" in rendered
    assert f"state={REDACTED}" in rendered
    assert "302" in rendered


def test_a_uvicorn_access_record_through_real_logger_and_handler() -> None:
    """End to end: uvicorn's private access handler with the filter installed."""
    logger = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_access_formatter())
    saved = (logger.handlers[:], logger.propagate, logger.level, logger.filters[:])
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    err = io.StringIO()
    try:
        install_log_redaction(logger_names=("uvicorn.access",), root=logging.getLogger("nahla.compat.noroot"))
        with patch("sys.stderr", err):
            path = f"/webhook/whatsapp?hub.verify_token={CANARY_VERIFY_TOKEN}&hub.challenge=42"
            logger.info(UVICORN_ACCESS_MSG, CLIENT_ADDR, "GET", path, "1.1", 200)
    finally:
        logger.handlers, logger.propagate, logger.level, logger.filters = saved
    out = stream.getvalue()
    assert "--- Logging error ---" not in err.getvalue()
    assert "not enough values to unpack" not in err.getvalue()
    assert 'GET /webhook/whatsapp?' in out
    assert f"hub.verify_token={REDACTED}" in out
    assert "200 OK" in out
    _assert_no_canary(out + err.getvalue())


# ── B. Real httpx.URL request log ──────────────────────────────────────

def test_b_httpx_refresh_url_object_formats_and_is_redacted() -> None:
    record = _httpx_refresh_record()
    SecretRedactingFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert record.args[0] == "GET"
    assert record.args[3] == 200 and isinstance(record.args[3], int)
    rendered = logging.Formatter("%(message)s").format(record)
    _assert_no_canary(rendered)
    assert "https://graph.facebook.com/v21.0/oauth/access_token?" in rendered
    assert "grant_type=fb_exchange_token" in rendered
    assert f"client_secret={REDACTED}" in rendered
    assert f"fb_exchange_token={REDACTED}" in rendered
    assert '"HTTP/1.1 200 OK"' in rendered


def test_b_httpx_debug_token_url_object_formats_and_is_redacted() -> None:
    record = _httpx_debug_token_record()
    SecretRedactingFilter().filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    _assert_no_canary(rendered)
    assert f"input_token={REDACTED}" in rendered
    assert f"access_token={REDACTED}" in rendered
    assert "graph.facebook.com/v21.0/debug_token" in rendered


def test_b_httpx_mock_transport_request_log_formats_through_named_logger() -> None:
    """The exact production path: httpx's own 'HTTP Request' log with the filter."""
    logger = logging.getLogger("httpx")
    stream, err = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    saved = (logger.handlers[:], logger.propagate, logger.level, logger.filters[:])
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        install_log_redaction(logger_names=("httpx",), root=logging.getLogger("nahla.compat.noroot"))
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with patch("sys.stderr", err), httpx.Client(transport=transport) as client:
            client.get(
                "https://graph.facebook.com/v21.0/oauth/access_token",
                params={"client_secret": CANARY_APP_SECRET, "fb_exchange_token": CANARY_EXCHANGE_TOKEN},
            )
    finally:
        logger.handlers, logger.propagate, logger.level, logger.filters = saved
    out = stream.getvalue()
    assert "HTTP Request: GET https://graph.facebook.com/v21.0/oauth/access_token?" in out
    assert out.count(REDACTED) == 2
    assert "--- Logging error ---" not in err.getvalue()
    _assert_no_canary(out + err.getvalue())


# ── C. Double application (named logger + root handler) ────────────────

@pytest.mark.parametrize(
    "make_record, formatter_factory",
    [
        (_webhook_verify_record, _access_formatter),
        (_oauth_callback_record, _access_formatter),
        (_httpx_refresh_record, lambda: logging.Formatter("%(message)s")),
        (_httpx_debug_token_record, lambda: logging.Formatter("%(message)s")),
    ],
)
def test_c_double_application_is_idempotent_and_keeps_diagnostics(make_record, formatter_factory) -> None:
    once = make_record()
    SecretRedactingFilter().filter(once)
    rendered_once = formatter_factory().format(once)

    twice = make_record()
    filt_a, filt_b = SecretRedactingFilter(), SecretRedactingFilter()
    filt_a.filter(twice)
    filt_b.filter(twice)
    filt_b.filter(twice)  # a third pass must be harmless too
    rendered_twice = formatter_factory().format(twice)

    assert rendered_twice == rendered_once
    assert isinstance(twice.args, tuple) and len(twice.args) == 5
    _assert_no_canary(rendered_twice)
    assert REDACTED in rendered_twice
    assert REDACTED_RECORD not in rendered_twice
    assert "GET" in rendered_twice


# ── D. Fail-closed collapse and logging-error diagnostics ──────────────

def test_d_malformed_record_collapses_and_hides_original_args() -> None:
    record = _record("nahla.test", "tenant=%d secret=%s", ("not-an-int", CANARY_APP_SECRET))
    assert SecretRedactingFilter().filter(record) is True
    assert record.msg == REDACTED_RECORD
    assert record.args == ()
    rendered = logging.Formatter("%(levelname)s %(message)s").format(record)
    assert rendered == f"INFO {REDACTED_RECORD}"
    _assert_no_canary(rendered)
    assert "not-an-int" not in rendered

    # Even if a downstream handler fails later, ``Handler.handleError`` only has
    # the collapsed record to print: no original argument can surface.
    err = io.StringIO()
    handler = logging.StreamHandler(io.StringIO())
    with patch("sys.stderr", err), patch.object(logging, "raiseExceptions", True):
        try:
            raise ValueError("simulated downstream formatter failure")
        except ValueError:
            handler.handleError(record)
    diagnostics = err.getvalue()
    _assert_no_canary(diagnostics)
    assert "not-an-int" not in diagnostics
    assert REDACTED_RECORD in diagnostics


def test_d_literal_message_text_carrying_a_secret_still_formats_without_leaking() -> None:
    # A secret in the *format string itself* (not in args) is the one case where
    # in-place arg redaction cannot help; the record collapses to its redacted
    # pre-formatted text and remains formattable.
    record = _record("nahla.test", f"client_secret={CANARY_APP_SECRET} tenant=%s", (7,))
    SecretRedactingFilter().filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    _assert_no_canary(rendered)
    assert rendered == f"client_secret={REDACTED} tenant=7"


# ── E. Exception / stack redaction with preserved args ─────────────────

def test_e_exception_text_is_redacted_while_args_stay_intact() -> None:
    request = httpx.Request(
        "GET",
        httpx.URL(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            params={"client_secret": CANARY_APP_SECRET, "fb_exchange_token": CANARY_EXCHANGE_TOKEN},
        ),
    )
    try:
        raise httpx.ConnectError(f"boom for {request.url}", request=request)
    except httpx.ConnectError:
        record = _record("nahla.test", "refresh failed tenant=%s attempt=%d", ("35", 2), sys.exc_info())
    SecretRedactingFilter().filter(record)
    assert record.args == ("35", 2)
    rendered = logging.Formatter("%(message)s").format(record)
    assert rendered.startswith("refresh failed tenant=35 attempt=2")
    assert "ConnectError" in rendered
    _assert_no_canary(rendered)
    assert f"client_secret={REDACTED}" in rendered


def test_e_stack_info_is_redacted() -> None:
    record = _record("nahla.test", "x=%s", ("1",))
    record.stack_info = f"Stack (most recent call last):\n  url=https://h/x?access_token={CANARY_ACCESS_TOKEN}"
    SecretRedactingFilter().filter(record)
    assert record.args == ("1",)
    _assert_no_canary(record.stack_info)
    assert f"access_token={REDACTED}" in record.stack_info


# ── Structure preservation ─────────────────────────────────────────────

def test_mapping_args_keep_mapping_formatting_and_types() -> None:
    record = _record(
        "nahla.test",
        "tenant=%(tenant)d ok=%(ok)s token=%(token)s url=%(url)s",
        {"tenant": 35, "ok": True, "token": CANARY_ACCESS_TOKEN, "url": f"https://h/x?code={CANARY_OAUTH_CODE}"},
    )
    SecretRedactingFilter().filter(record)
    assert isinstance(record.args, dict)
    assert record.args["tenant"] == 35 and isinstance(record.args["tenant"], int)
    assert record.args["ok"] is True
    assert record.args["token"] == REDACTED
    rendered = logging.Formatter("%(message)s").format(record)
    _assert_no_canary(rendered)
    assert rendered == f"tenant=35 ok=True token={REDACTED} url=https://h/x?code={REDACTED}"


def test_nested_tuple_args_are_redacted_recursively_with_cardinality_kept() -> None:
    record = _record(
        "nahla.test",
        "a=%s b=%r c=%d d=%s",
        ({"client_secret": CANARY_APP_SECRET, "keep": 1}, [CANARY_EXCHANGE_TOKEN, "safe"], 3, False),
    )
    SecretRedactingFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 4
    assert record.args[0] == {"client_secret": REDACTED, "keep": 1}
    assert record.args[1] == [REDACTED, "safe"]
    assert record.args[2] == 3 and record.args[3] is False
    rendered = logging.Formatter("%(message)s").format(record)
    _assert_no_canary(rendered)
    assert rendered.endswith(" c=3 d=False")


def test_safe_diagnostics_survive_in_args() -> None:
    record = _record(
        "nahla.test",
        "[WA token] op=%s tenant=%s token_status=%s input_tokens=%d",
        ("template_sync", 33, "expiring_soon", 120),
    )
    SecretRedactingFilter().filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    assert rendered == "[WA token] op=template_sync tenant=33 token_status=expiring_soon input_tokens=120"


def test_previous_collapse_behaviour_reproduces_uvicorn_failure_signature() -> None:
    """Documents the defect: a record whose args were collapsed to () cannot be
    rendered by uvicorn's AccessFormatter.  Guards against regressing to it."""
    record = _webhook_verify_record()
    record.msg = record.getMessage()
    record.args = ()
    with pytest.raises(ValueError, match="not enough values to unpack"):
        _access_formatter().format(record)
    # …and the fixed filter never produces that shape for a well-formed record.
    fixed = _webhook_verify_record()
    SecretRedactingFilter().filter(fixed)
    assert fixed.args != ()
    _access_formatter().format(fixed)
