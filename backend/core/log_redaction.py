"""Redact credentials from log output — fail closed.

Why this exists
===============
Outbound Meta Graph calls carry credentials in the query string (``client_secret``,
``fb_exchange_token``, ``access_token``, ``input_token``, ``appsecret_proof``) and
``httpx`` logs every request as ``HTTP Request: <method> <url> "<status>"`` at
INFO level. Inbound requests can carry ``hub.verify_token`` (webhook verification)
and OAuth ``code`` values, which uvicorn's access log prints with the full query
string. ``httpx`` exceptions embed the request URL in ``str(exc)``.

The previous filter only scrubbed *string* log arguments; ``httpx`` passes the URL
as an ``httpx.URL`` object, so the credential-bearing URL was formatted into the
final line untouched. This module therefore redacts the **formatted** message
(``record.getMessage()``), the rendered exception text and the stack text, and
never lets an unformattable record through unredacted.

Public API (stable):
  * ``redact_secrets(text)``     — scrub URLs, ``key=value`` / ``"key": "value"``
                                   fragments, Bearer/Authorization/Cookie values
                                   and Meta-style ``EAA…`` tokens from free text.
  * ``redact_value(value)``      — recursive redaction for mappings / sequences
                                   (dict keys are matched case-insensitively).
  * ``redact_exception(exc)``    — ``"ExcType: <redacted message>"`` for logging.
  * ``SecretRedactingFilter``    — logging filter, safe on any handler or logger.
  * ``install_log_redaction()``  — idempotent installation on the root handlers
                                   and on the ``httpx`` / ``httpcore`` / uvicorn
                                   loggers (uvicorn's access logger does not
                                   propagate to the root handlers).

Policy: the marker is the fixed string ``REDACTED``; no prefix, suffix, length,
hash or fingerprint of a secret is ever emitted.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "REDACTED"
REDACTED_RECORD = "[REDACTED_LOG_RECORD]"
REDACTED_URL = "[REDACTED_URL]"

# Exact key names (case-insensitive) whose values are always secrets.
_EXACT_SENSITIVE_KEYS = frozenset({
    "access_token",
    "token",
    "refresh_token",
    "fb_exchange_token",
    "id_token",
    "input_token",
    "verify_token",
    "hub.verify_token",
    "client_secret",
    "app_secret",
    "appsecret_proof",
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "d360-api-key",
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "secret",
    "password",
    "passwd",
    "client_id_secret",
    "code",
    "state",
})
# Any key *containing* one of these fragments is treated as a secret
# (``x-api-key``, ``app_secret``, ``proxy-authorization`` …).
_SENSITIVE_KEY_FRAGMENTS = ("secret", "password", "passwd", "api_key", "apikey", "api-key",
                            "authorization", "cookie")
# ``token`` is a secret only when it is the whole key or its last word
# (``fb_exchange_token``, ``hub.verify_token``, ``input_token``): usage
# counters (``input_tokens``) and status fields (``token_status``,
# ``token_type``, ``access_token_set``) are safe diagnostics.
_TOKEN_KEY_RE = re.compile(r"(?:^|[_.\-])token$")


def is_sensitive_key(key: Any) -> bool:
    k = str(key or "").strip().lower()
    if not k:
        return False
    if k in _EXACT_SENSITIVE_KEYS:
        return True
    if _TOKEN_KEY_RE.search(k):
        return True
    return any(fragment in k for fragment in _SENSITIVE_KEY_FRAGMENTS)


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_BEARER = re.compile(r"(Bearer\s+)[^\s\"',;]+", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"((?:Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*[:=]\s*)(?!Bearer\b)([^\s\"',;]+)",
    re.IGNORECASE,
)
# ``key=value``, ``key: value``, ``"key": "value"`` fragments in free text (JSON,
# query strings, bare request paths such as uvicorn's access log).
_KV = re.compile(
    r"((?<![A-Za-z0-9_.\-])"
    r"(?:[A-Za-z0-9_.\-]*token"
    r"|[A-Za-z0-9_.\-]*(?:secret|password|passwd|api[_\-]?key|authorization|cookie)[A-Za-z0-9_.\-]*"
    r"|code|state|appsecret_proof)"
    r"[\"']?\s*[=:]\s*[\"']?)(?!(?:Bearer|Basic|Digest)\b)([^\s\"'&,;]+)",
    re.IGNORECASE,
)
# Meta Graph user / page / system-user tokens start with ``EAA``.
_META_TOKEN = re.compile(r"\bEAA[A-Za-z0-9]{20,}")


def _redact_query(query: str) -> str:
    if not query:
        return query
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        pairs.append((key, REDACTED if is_sensitive_key(key) else value))
    return urlencode(pairs)


def _redact_one_url(raw: str) -> str:
    """Redact a single URL; unparseable input collapses to ``[REDACTED_URL]``."""
    try:
        parts = urlsplit(raw)
        netloc = parts.netloc
        if "@" in netloc:  # user:password@host — never keep credentials
            netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"
        fragment = _redact_query(parts.fragment) if "=" in parts.fragment else parts.fragment
        return urlunsplit((parts.scheme, netloc, parts.path, _redact_query(parts.query), fragment))
    except Exception:  # noqa: BLE001 — fail closed: drop the whole URL
        return REDACTED_URL


def _redact_urls(text: str) -> str:
    return _URL_RE.sub(lambda m: _redact_one_url(m.group(0)), text)


def redact_secrets(text: Any) -> str:
    """Remove credentials from free text. Never raises; fails closed."""
    if text is None:
        return ""
    try:
        out = str(text)
        if not out:
            return out
        out = _redact_urls(out)
        out = _BEARER.sub(r"\1" + REDACTED, out)
        out = _AUTH_HEADER.sub(r"\1" + REDACTED, out)
        out = _KV.sub(r"\1" + REDACTED, out)
        out = _META_TOKEN.sub(REDACTED, out)
        return out
    except Exception:  # noqa: BLE001 — a failing redactor must not leak the input
        return REDACTED_RECORD


def redact_value(value: Any) -> Any:
    """Recursively redact strings, mappings and sequences (dict keys case-insensitive).

    Objects that are neither primitives nor containers (``httpx.URL``, exceptions,
    dataclasses …) are rendered with ``str()`` and redacted as text — the
    previous behaviour passed them through untouched, which is how credential
    URLs reached the log line.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if is_sensitive_key(k) else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = [redact_value(item) for item in value]
        return seq if isinstance(value, list) else type(value)(seq)
    return redact_secrets(str(value))


def redact_exception(exc: BaseException) -> str:
    """``"ExcType: message"`` with credentials removed — safe for ``%s`` logging."""
    return f"{type(exc).__name__}: {redact_secrets(str(exc))}"


_EXC_FORMATTER = logging.Formatter()


class SecretRedactingFilter(logging.Filter):
    """Redact the message, arguments, exception text and stack text of a record.

    Arguments are redacted *in place* so the record keeps its structure:

    * a tuple stays a tuple with the same item count, every item redacted
      recursively (``redact_value``: numbers and booleans are preserved,
      ``httpx.URL`` and other objects become redacted strings);
    * a mapping stays a mapping so ``%(name)s`` formatting keeps working.

    Formatters that unpack ``record.args`` themselves — uvicorn's
    ``AccessFormatter`` needs its five access-log arguments — therefore keep
    working.  Collapsing ``args`` to ``()`` (the previous behaviour) made every
    uvicorn access record raise ``ValueError: not enough values to unpack``.

    After redaction the record is formatted once to prove it still renders.
    Fail closed: if formatting genuinely fails, or the *literal* message text
    itself carries a secret that in-place argument redaction cannot reach, the
    record is replaced by its redacted pre-formatted text (or by
    ``[REDACTED_LOG_RECORD]``) with empty arguments.  The filter always returns
    ``True`` so records are never dropped silently.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if isinstance(args, Mapping):
                record.args = redact_value(dict(args))
            elif isinstance(args, tuple):
                record.args = tuple(redact_value(item) for item in args)
            elif args is not None:
                record.args = (redact_value(args),)
            formatted = record.getMessage()  # proves the mutated record still formats
            redacted = redact_secrets(formatted)
            if redacted != formatted:
                # Secret in the literal message text (not in args): fall back
                # to the pre-formatted redacted string.  ``getMessage`` skips
                # ``%`` formatting when args is empty, so this stays renderable.
                record.msg = redacted
                record.args = ()
        except Exception:  # noqa: BLE001
            record.msg = REDACTED_RECORD
            record.args = ()
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = redact_secrets(_EXC_FORMATTER.formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = redact_secrets(record.exc_text)
        except Exception:  # noqa: BLE001
            record.exc_text = REDACTED_RECORD
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


# Loggers that own their own handlers (uvicorn) or emit request URLs (httpx).
DEFAULT_REDACTED_LOGGERS = (
    "httpx",
    "httpcore",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)

_SHARED_FILTER = SecretRedactingFilter()


def _add_once(target: Any, filt: logging.Filter) -> None:
    if not any(isinstance(existing, SecretRedactingFilter) for existing in target.filters):
        target.addFilter(filt)


def install_log_redaction(
    logger_names: Iterable[str] = DEFAULT_REDACTED_LOGGERS,
    *,
    root: Optional[logging.Logger] = None,
) -> SecretRedactingFilter:
    """Attach the redacting filter to every root handler and to ``logger_names``.

    Idempotent. Handler-level installation covers every logger that propagates
    to the root; logger-level installation covers loggers with private,
    non-propagating handlers (uvicorn's access log) and ``httpx``.
    """
    root_logger = root if root is not None else logging.getLogger()
    for handler in list(root_logger.handlers):
        _add_once(handler, _SHARED_FILTER)
    for name in logger_names:
        _add_once(logging.getLogger(name), _SHARED_FILTER)
    return _SHARED_FILTER


__all__ = [
    "DEFAULT_REDACTED_LOGGERS",
    "REDACTED",
    "SecretRedactingFilter",
    "install_log_redaction",
    "is_sensitive_key",
    "redact_exception",
    "redact_secrets",
    "redact_value",
]
