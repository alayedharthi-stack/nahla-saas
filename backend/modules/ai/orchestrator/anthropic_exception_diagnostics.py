"""
anthropic_exception_diagnostics.py
──────────────────────────────────
PII-safe exception-chain fields for Anthropic runtime logging.

Logs only exception type names and stable errno/category hints — never
messages, URLs, headers, API keys, prompts, or response bodies.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

_LOCAL_PROTOCOL_CATEGORIES = frozenset({
    "local_protocol_invalid_header_name",
    "local_protocol_invalid_header_value",
    "local_protocol_invalid_request_line",
    "local_protocol_content_length_mismatch",
    "local_protocol_state_machine",
    "local_protocol_receive_buffer",
    "local_protocol_malformed_proxy_response",
    "local_protocol_other",
})

# Narrow, case-insensitive substring patterns. Order matters: first match wins.
_LOCAL_PROTOCOL_PATTERNS: Tuple[Tuple[str, ...], str] = (
    (("illegal header name",), "local_protocol_invalid_header_name"),
    (("illegal header value",), "local_protocol_invalid_header_value"),
    (
        ("illegal request line", "no request line received", "no response line received"),
        "local_protocol_invalid_request_line",
    ),
    (
        (
            "conflicting content-length",
            "bad content-length",
            "too much data for declared content-length",
            "too little data for declared content-length",
        ),
        "local_protocol_content_length_mismatch",
    ),
    (
        (
            "can't handle event type",
            "not in a reusable state",
            "can't send data when our state is error",
        ),
        "local_protocol_state_machine",
    ),
    (("got data when expecting eof",), "local_protocol_receive_buffer"),
    (
        ("malformed proxy response", "invalid proxy response"),
        "local_protocol_malformed_proxy_response",
    ),
)


def anthropic_exception_diagnostics(exc: BaseException) -> Dict[str, Any]:
    """Return structured, safe diagnostics for an exception chain."""
    cause = exc.__cause__
    context = exc.__context__
    category = _safe_category(exc)
    if category is None and cause is not None:
        category = _safe_category(cause)
    if category is None and context is not None and context is not cause:
        category = _safe_category(context)
    return {
        "exc_type": type(exc).__name__,
        "cause_type": type(cause).__name__ if cause is not None else None,
        "context_type": type(context).__name__ if context is not None else None,
        "category": category,
    }


def _classify_local_protocol_error(exc: BaseException) -> Optional[str]:
    if type(exc).__name__ != "LocalProtocolError":
        return None
    lowered = str(exc).lower()
    for needles, category in _LOCAL_PROTOCOL_PATTERNS:
        if any(needle in lowered for needle in needles):
            return category
    return "local_protocol_other"


def _safe_category(exc: Optional[BaseException]) -> Optional[str]:
    if exc is None:
        return None
    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None)
        if errno is not None:
            return f"errno_{errno}"
    return _classify_local_protocol_error(exc)
