"""Redact secrets from log messages (Graph tokens, OAuth codes, Bearer headers)."""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_QUERY_KEYS = frozenset({
    "access_token",
    "token",
    "code",
    "state",
    "appsecret_proof",
    "client_secret",
    "authorization",
    "input_token",
})
_SENSITIVE_DICT_KEYS = _SENSITIVE_QUERY_KEYS | frozenset({
    "refresh_token",
    "id_token",
    "client_id_secret",
})
_BEARER = re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"(Authorization\s*[:=]\s*)(?!Bearer\b)([^\s\"']+)",
    re.IGNORECASE,
)
_KV = re.compile(
    r"((?:access_token|token|code|state|appsecret_proof|client_secret)"
    r"\s*[=:]\s*|authorization\s*=\s*)([^\s\"'&,;]+)",
    re.IGNORECASE,
)


def _redact_query(query: str) -> str:
    if not query:
        return query
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if str(key).lower() in _SENSITIVE_QUERY_KEYS:
            pairs.append((key, "REDACTED"))
        else:
            pairs.append((key, value))
    return urlencode(pairs)


def _redact_urls(text: str) -> str:
    out = text
    for match in re.finditer(r"https?://[^\s\"']+", text):
        raw = match.group(0)
        parts = urlsplit(raw)
        if not parts.query:
            continue
        redacted = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, _redact_query(parts.query), parts.fragment)
        )
        out = out.replace(raw, redacted)
    return out


def redact_secrets(text: str) -> str:
    """Remove OAuth/Graph secrets from URLs, headers, and key=value fragments."""
    if not text:
        return text
    out = _redact_urls(str(text))
    out = _BEARER.sub(r"\1REDACTED", out)
    out = _AUTH_HEADER.sub(r"\1REDACTED", out)
    out = _KV.sub(r"\1REDACTED", out)
    return out


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {
            k: ("REDACTED" if str(k).lower() in _SENSITIVE_DICT_KEYS else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        seq = [redact_value(item) for item in value]
        return type(value)(seq) if not isinstance(value, list) else seq
    return value


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from the final log message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            record.args = redact_value(record.args)
        return True


__all__ = ["SecretRedactingFilter", "redact_secrets", "redact_value"]
