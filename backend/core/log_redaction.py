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
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

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
                            "authorization", "cookie",
                            # Encryption keys (``WA_TOKEN_ENC_KEY``, ``TOTP_ENC_KEY``,
                            # ``wa_key``-style locals are caught by value below).
                            "enc_key", "encryption_key", "fernet_key", "signing_key", "private_key")
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
# One flat key token (no alternation of unbounded runs), starting only at a
# token boundary, then a callback that decides sensitivity: linear in the
# input length. The rule is exactly the former alternation's: a key that ends
# with ``token``; contains a secret/password/api-key/authorization/cookie or
# encryption-key fragment; or is ``code``, ``state`` or ``appsecret_proof``.
_KV_KEY = re.compile(
    r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]+)([\"']?\s*[=:]\s*[\"']?)",
    re.IGNORECASE,
)
_KV_VALUE = re.compile(r"(?!(?:Bearer|Basic|Digest)\b)[^\s\"'&,;]+", re.IGNORECASE)
_KV_KEY_FRAGMENTS = (
    "secret", "password", "passwd", "apikey", "api_key", "api-key", "authorization", "cookie",
    "enc_key", "encryption_key", "fernet_key", "signing_key", "private_key",
)
_KV_EXACT_KEYS = frozenset({"code", "state", "appsecret_proof"})


def _kv_key_is_sensitive(key: str) -> bool:
    k = key.lower()
    return k.endswith("token") or k in _KV_EXACT_KEYS or any(f in k for f in _KV_KEY_FRAGMENTS)


def _redact_kv(text: str) -> str:
    """Redact the value of every sensitive ``key=value`` / ``key: value`` fragment.

    A forward scan: each key token is matched once at a token boundary (flat
    character class, no alternation of unbounded runs), so the cost is linear
    in the input. Only a sensitive key consumes its value; a non-sensitive key
    consumes just itself and its separator, so it can never swallow a
    following sensitive pair. The sensitivity rule is the former
    alternation's: a key ending with ``token``; containing a
    secret/password/api-key/authorization/cookie or encryption-key fragment;
    or exactly ``code``, ``state`` or ``appsecret_proof``.
    """
    out = []
    pos = 0
    while True:
        m = _KV_KEY.search(text, pos)
        if m is None:
            break
        if _kv_key_is_sensitive(m.group(1)):
            value = _KV_VALUE.match(text, m.end())
            if value is not None:
                out.append(text[pos:m.end()])
                out.append(REDACTED)
                pos = value.end()
                continue
        out.append(text[pos:m.end()])
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


# Meta Graph user / page / system-user tokens start with ``EAA``.
_META_TOKEN = re.compile(r"\bEAA[A-Za-z0-9]{20,}")
# A Fernet key (32 bytes, url-safe base64 with one ``=`` pad) wherever it
# appears, e.g. as the value of a local named ``key``. Over-matching another
# 32-byte base64 value only redacts more; it never reveals anything.
_FERNET_KEY = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{43}=(?![A-Za-z0-9_\-=])")


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


def redact_raw_query(query: str) -> str:
    """Redact a raw (still percent-encoded) query string by its *decoded* keys.

    ``co%64e=…`` / ``STATE=…`` / ``access%5Ftoken=…`` are the same parameters a
    web framework decodes, so they are matched after decoding while every
    other pair keeps its original bytes. A key that cannot be decoded makes
    the whole query ``REDACTED`` (fail closed).
    """
    if not query:
        return query
    pairs = []
    for part in query.split("&"):
        key, sep, _value = part.partition("=")
        try:
            decoded = unquote_plus(key, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return REDACTED
        if sep and is_sensitive_key(decoded.strip()):
            pairs.append(f"{key}={REDACTED}")
        else:
            pairs.append(part)
    return "&".join(pairs)


# A bare request target (``/path?query``) as uvicorn's access log and raw
# diagnostics print it — no scheme or host, so ``_URL_RE`` does not see it.
# Starts only at a token boundary (start, whitespace, quote, bracket, ``=``,
# ``,`` or ``;``), so each token is scanned once: linear on any input.
_BARE_TARGET = re.compile(r"(?<![^\s\"'(\[=,;])(/[^\s\"'<>?#]*)\?([^\s\"'<>#]*)")


def _redact_bare_targets(text: str) -> str:
    return _BARE_TARGET.sub(lambda m: f"{m.group(1)}?{redact_raw_query(m.group(2))}", text)


# ``key=value`` fragments whose key carries percent-encoding (``co%64e=…``,
# ``access%5Ftoken=…``) anywhere in free text — bare query strings, breadcrumb
# fields, request ``query_string``. Plain keys are handled by ``_KV``.
#
# One flat character class for the key (no nested or alternating
# quantifiers) and a start only at a token boundary, so a scan is linear in
# the input length even for adversarial ``%ab%ab…`` runs.
_ENCODED_KEY_CHARS = "A-Za-z0-9_.\\-+%"
_ENCODED_KEY = re.compile(rf"(?<![{_ENCODED_KEY_CHARS}])([{_ENCODED_KEY_CHARS}]+)=")
_ENCODED_VALUE = re.compile(r"[^\s\"'&,;]*")
_MALFORMED_PCT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _encoded_key_redacts(key: str) -> bool:
    if "%" not in key and "+" not in key:
        return False  # plain keys are _redact_kv's job
    if _MALFORMED_PCT.search(key):
        return True  # malformed escape: fail closed
    try:
        decoded = unquote_plus(key, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return True  # undecodable key: fail closed
    return is_sensitive_key(decoded.strip())


def _redact_encoded_kv(text: str) -> str:
    """Forward scan; only a key that redacts consumes its value (linear)."""
    out = []
    pos = 0
    while True:
        m = _ENCODED_KEY.search(text, pos)
        if m is None:
            break
        if _encoded_key_redacts(m.group(1)):
            value = _ENCODED_VALUE.match(text, m.end())
            out.append(text[pos:m.end()])
            out.append(REDACTED)
            pos = value.end() if value is not None else m.end()
            continue
        out.append(text[pos:m.end()])
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def redact_secrets(text: Any) -> str:
    """Remove credentials from free text. Never raises; fails closed."""
    if text is None:
        return ""
    try:
        out = str(text)
        if not out:
            return out
        out = _redact_urls(out)
        out = _redact_bare_targets(out)
        out = _redact_encoded_kv(out)
        out = _BEARER.sub(r"\1" + REDACTED, out)
        out = _AUTH_HEADER.sub(r"\1" + REDACTED, out)
        out = _redact_kv(out)
        out = _META_TOKEN.sub(REDACTED, out)
        out = _FERNET_KEY.sub(REDACTED, out)
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


def redacted_query_preview(query: Any, *, limit: int = 80) -> str:
    """Diagnostic preview of a raw query string: redacted first, then truncated.

    Truncating first could leave a credential value that no longer follows its
    key; redacting the whole string first never does.
    """
    if isinstance(query, (bytes, bytearray)):
        text = bytes(query).decode("latin-1")
    else:
        text = repr(query) if not isinstance(query, str) else query
    return redact_secrets(redact_raw_query(text))[: max(0, int(limit))]


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
    "redact_raw_query",
    "redact_value",
    "redacted_query_preview",
]
