"""Redact secrets from log messages (Graph tokens, Bearer headers, query params)."""
from __future__ import annotations

import logging
import re
import hashlib
from typing import Any, Optional

_ACCESS_TOKEN_QS = re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE)
_BEARER = re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Remove access tokens from URLs and Authorization bearer values."""
    if not text:
        return text
    out = _ACCESS_TOKEN_QS.sub(r"\1REDACTED", text)
    return _BEARER.sub(r"\1REDACTED", out)


def redact_graph_id(value: Optional[str]) -> str:
    """Hash a Graph WABA/phone identifier for logs and audit persistence."""
    token = str(value or "").strip()
    if not token:
        return "∅"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"#{digest}"


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from the final log message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


__all__ = ["SecretRedactingFilter", "redact_graph_id", "redact_secrets"]
