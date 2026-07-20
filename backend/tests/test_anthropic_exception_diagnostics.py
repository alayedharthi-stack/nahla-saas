"""Regression tests for PII-safe Anthropic exception diagnostics."""
from __future__ import annotations

import logging
from typing import Optional

_SECRET_MARKER = "sk-ant-secret-key-do-not-log"
_CUSTOMER_TEXT = "اسمي أحمد سالم من الرياض"
_API_URL = "https://api.anthropic.com/v1/messages"


class LocalProtocolError(Exception):
    """Test double matching httpcore/h11 LocalProtocolError type name."""


def _make_local_protocol_error(message: str) -> LocalProtocolError:
    return LocalProtocolError(message)


def _make_api_connection_error(
    *,
    cause: Optional[Exception] = None,
    context: Optional[Exception] = None,
) -> Exception:
    class APIConnectionError(Exception):
        pass

    outer = APIConnectionError(f"connection failed for {_CUSTOMER_TEXT} at {_API_URL}")
    if cause is not None:
        outer.__cause__ = cause
    if context is not None:
        outer.__context__ = context
    return outer


def test_local_protocol_representative_h11_httpcore_categories():
    from modules.ai.orchestrator.anthropic_exception_diagnostics import (
        anthropic_exception_diagnostics,
    )

    cases = {
        "Illegal header name b'X-Bad\\n': illegal characters": (
            "local_protocol_invalid_header_name"
        ),
        "Illegal header value b'secret': illegal characters": (
            "local_protocol_invalid_header_value"
        ),
        "illegal request line: malformed": "local_protocol_invalid_request_line",
        "no request line received": "local_protocol_invalid_request_line",
        "conflicting Content-Length headers": (
            "local_protocol_content_length_mismatch"
        ),
        "Too little data for declared Content-Length": (
            "local_protocol_content_length_mismatch"
        ),
        "can't handle event type Response when role=CLIENT and state=DONE": (
            "local_protocol_state_machine"
        ),
        "Got data when expecting EOF": "local_protocol_receive_buffer",
        "malformed proxy response": "local_protocol_malformed_proxy_response",
    }
    for message, expected in cases.items():
        exc = _make_api_connection_error(
            cause=_make_local_protocol_error(message),
        )
        diag = anthropic_exception_diagnostics(exc)
        assert diag["category"] == expected, (message, diag)


def test_local_protocol_unknown_falls_back_to_other():
    from modules.ai.orchestrator.anthropic_exception_diagnostics import (
        anthropic_exception_diagnostics,
    )

    exc = _make_api_connection_error(
        cause=_make_local_protocol_error(
            f"totally novel protocol fault involving {_SECRET_MARKER}"
        ),
    )
    diag = anthropic_exception_diagnostics(exc)
    assert diag["category"] == "local_protocol_other"


def test_local_protocol_category_from_nested_cause():
    from modules.ai.orchestrator.anthropic_exception_diagnostics import (
        anthropic_exception_diagnostics,
    )

    inner = _make_local_protocol_error("illegal header name b'Host'")
    outer = _make_api_connection_error(cause=inner)
    diag = anthropic_exception_diagnostics(outer)
    assert diag["exc_type"] == "APIConnectionError"
    assert diag["cause_type"] == "LocalProtocolError"
    assert diag["category"] == "local_protocol_invalid_header_name"


def test_local_protocol_category_from_context_when_cause_absent():
    from modules.ai.orchestrator.anthropic_exception_diagnostics import (
        anthropic_exception_diagnostics,
    )

    context = _make_local_protocol_error("Got data when expecting EOF")
    outer = _make_api_connection_error(context=context)
    diag = anthropic_exception_diagnostics(outer)
    assert diag["context_type"] == "LocalProtocolError"
    assert diag["cause_type"] is None
    assert diag["category"] == "local_protocol_receive_buffer"


def test_errno_category_preserved_over_local_protocol_on_same_node():
    from modules.ai.orchestrator.anthropic_exception_diagnostics import (
        anthropic_exception_diagnostics,
    )

    inner = OSError(110, f"timed out reaching {_API_URL}")
    outer = _make_api_connection_error(cause=inner)
    diag = anthropic_exception_diagnostics(outer)
    assert diag["category"] == "errno_110"


def test_diagnostics_never_emit_secrets_urls_or_prompts():
    from modules.ai.orchestrator.anthropic_exception_diagnostics import (
        anthropic_exception_diagnostics,
    )

    message = (
        f"Illegal header value b'{_SECRET_MARKER}': prompt={_CUSTOMER_TEXT} "
        f"url={_API_URL}"
    )
    exc = _make_api_connection_error(cause=_make_local_protocol_error(message))
    diag = anthropic_exception_diagnostics(exc)
    rendered = repr(diag)
    assert diag["category"] == "local_protocol_invalid_header_value"
    assert _SECRET_MARKER not in rendered
    assert _CUSTOMER_TEXT not in rendered
    assert _API_URL not in rendered
    assert message not in rendered


def test_provider_logs_local_protocol_category_without_raw_message(caplog):
    from unittest.mock import MagicMock, patch

    from modules.ai.orchestrator.providers import anthropic_provider

    class APIConnectionError(Exception):
        pass

    inner = LocalProtocolError(
        f"illegal request line: GET {_API_URL} Authorization: {_SECRET_MARKER}"
    )
    conn_err = APIConnectionError(f"prompt leak: {_CUSTOMER_TEXT}")
    conn_err.__cause__ = inner

    mock_sdk = MagicMock()
    mock_sdk.Anthropic.return_value.messages.create.side_effect = conn_err
    mock_sdk.AuthenticationError = type("AuthenticationError", (Exception,), {})
    mock_sdk.APIConnectionError = APIConnectionError

    caplog.set_level(logging.WARNING, logger="nahla.ai.orchestrator.engine")

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": _SECRET_MARKER}, clear=False):
        with patch.object(anthropic_provider, "_SDK_AVAILABLE", True):
            with patch.object(anthropic_provider, "_anthropic_sdk", mock_sdk):
                result = anthropic_provider.AnthropicProvider().call(
                    _CUSTOMER_TEXT,
                    f"system prompt with {_SECRET_MARKER}",
                )

    assert result["status"] == "connection_error"
    joined = "\n".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
    assert "local_protocol_invalid_request_line" in joined
    assert _SECRET_MARKER not in joined
    assert _CUSTOMER_TEXT not in joined
    assert _API_URL not in joined
    assert "illegal request line" not in joined
