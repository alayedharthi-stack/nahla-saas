"""Redact secrets from log messages (Graph tokens, Bearer headers, query params)."""
from __future__ import annotations

import logging
import re
import hashlib
from typing import Any, Iterable, Optional

_SENSITIVE_QUERY_PARAM = re.compile(
    r"((?:access_token|input_token|fb_exchange_token|client_secret|"
    r"appsecret_proof|code)=)[^&\s\"']+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE)
_GRAPH_NODE_URL = re.compile(
    r"(https?://graph\.facebook\.com/(?:v\d+(?:\.\d+)?/)?"
    r"(?!oauth(?:/|$)|debug_token(?:[/?]|$)|me(?:[/?]|$)))"
    r"([^/?\s\"']+)",
    re.IGNORECASE,
)
_LONG_IDENTIFIER = re.compile(r"(?<!\w)\+?\d(?:[\d\s().-]{7,}\d)(?!\w)")
_LABELED_IDENTIFIER = re.compile(
    r"(\b(?:business(?:_id)?|waba(?:_id)?|phone(?:_number)?(?:_id)?)"
    r"\s*[=:]\s*)([+\w.-]+)",
    re.IGNORECASE,
)


def redact_graph_id(value: Optional[str]) -> str:
    """Hash a Graph WABA/phone identifier for logs and audit persistence."""
    token = str(value or "").strip()
    if not token:
        return "∅"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"#{digest}"


def redact_secrets(text: str) -> str:
    """Remove secrets and Graph node identifiers from request/log text."""
    if not text:
        return text
    out = _SENSITIVE_QUERY_PARAM.sub(r"\1REDACTED", text)
    out = _BEARER.sub(r"\1REDACTED", out)
    return _GRAPH_NODE_URL.sub(
        lambda match: f"{match.group(1)}{redact_graph_id(match.group(2))}",
        out,
    )


def redact_sensitive_log_text(
    value: Any,
    *,
    graph_ids: Iterable[Optional[str]] = (),
    secrets: Iterable[Optional[str]] = (),
) -> str:
    """Scrub provider errors before logging or carrying them into metadata."""
    out = redact_secrets(str(value or ""))
    for secret in secrets:
        token = str(secret or "")
        if token:
            out = out.replace(token, "REDACTED")
    for graph_id in graph_ids:
        token = str(graph_id or "")
        if token:
            out = out.replace(token, redact_graph_id(token))
    out = _LABELED_IDENTIFIER.sub(
        lambda match: f"{match.group(1)}{redact_graph_id(match.group(2))}",
        out,
    )
    return _LONG_IDENTIFIER.sub(
        lambda match: redact_graph_id(match.group(0)),
        out,
    )


def _redact_log_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_log_text(value)
    if isinstance(value, BaseException):
        return redact_sensitive_log_text(value)
    if type(value).__module__.startswith(("httpx", "httpcore")):
        return redact_secrets(str(value))
    return value


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from the final log message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_log_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact_log_arg(v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact_log_arg(a) for a in record.args)
        return True


__all__ = [
    "SecretRedactingFilter",
    "redact_graph_id",
    "redact_secrets",
    "redact_sensitive_log_text",
]
