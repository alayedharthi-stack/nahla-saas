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
from bisect import bisect_right
from typing import Any, Iterable, Mapping, Optional, Tuple
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
# Auth-scheme markers. Each marker's credential is the token right after it
# (``Bearer <cred>``; ``Authorization: <cred>`` unless that is itself a
# ``Bearer`` marker), after an optional opening quote. Found with a zero-width
# lookahead so every marker start in the unchanged text is seen, including
# overlapping ones (``Proxy-Authorization`` / ``Authorization``). See
# ``_auth_marker_spans``.
_BEARER_MARKER = re.compile(r"(?=(Bearer\s+[\"']?))", re.IGNORECASE)
_AUTH_HEADER_MARKER = re.compile(
    r"(?=((?:Authorization|Set-Cookie|Cookie)\s*[:=]\s*[\"']?))", re.IGNORECASE,
)
# A header-style credential continues across ``,`` / ``;`` separators
# (``Authorization: k=,<cred>``, ``Cookie: a=1; b=2``, Digest parameters).
# Each repetition must consume a separator, so matching stays linear.
_AUTH_VALUE = re.compile(r"[^\s\"',;]+(?:[,;]\s*[^\s\"',;]+)*")
_BEARER_WORD = re.compile(r"Bearer\b", re.IGNORECASE)
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


# A credential value that holds a URL extends over the whole URL (and any
# value characters right after it): ``;`` and ``,`` inside a URL query do not
# end it. The URL pass would otherwise normalize them away later.
_VALUE_URL = re.compile(r"https?://", re.IGNORECASE)
_VALUE_URL_BODY = re.compile(r"[^\s\"'<>]*")
_VALUE_TAIL = re.compile(r"[^\s\"',;]*")


def _extend_over_url(text: str, scan_start: int, end: int) -> int:
    # The last URL in the token reaches furthest: an earlier one's body stops
    # at the first ``<`` / ``>`` before it, or ends where the last one does.
    url = None
    for url in _VALUE_URL.finditer(text, scan_start, end):
        pass
    if url is None:
        return end
    url_end = _VALUE_URL_BODY.match(text, url.end()).end()
    if url_end <= end:
        return end
    return _VALUE_TAIL.match(text, url_end).end()


def _kv_spans(text: str) -> list:
    """Value spans of every sensitive ``key=value`` / ``key: value`` fragment.

    A forward scan: each key token is matched once at a token boundary (flat
    character class, no alternation of unbounded runs), so the cost is linear
    in the input. Every key token is examined, including one inside another
    key's value, so a value can never swallow a following sensitive pair.
    The sensitivity rule is the former
    alternation's: a key ending with ``token``; containing a
    secret/password/api-key/authorization/cookie or encryption-key fragment;
    or exactly ``code``, ``state`` or ``appsecret_proof``.
    """
    spans = []
    pos = 0
    covered = 0  # end of the last value span
    while True:
        m = _KV_KEY.search(text, pos)
        if m is None:
            return spans
        # Keys inside a redacted value are still examined (``Cookie: secret=
        # <cred>``); a value starting inside the last span ends no later than
        # it, so it is not rescanned and the pass stays linear.
        if m.end() >= covered and _kv_key_is_sensitive(m.group(1)):
            value = _KV_VALUE.match(text, m.end())
            if value is not None:
                covered = _extend_over_url(text, m.start(), value.end())
                spans.append((value.start(), covered))
        pos = m.end()


def _replace_spans(text: str, spans: list) -> str:
    """Replace the union of ``spans`` (found on the unchanged ``text``) with REDACTED."""
    if not spans:
        return text
    merged = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out = []
    pos = 0
    for start, end in merged:
        out.append(text[pos:start])
        out.append(REDACTED)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _redact_kv(text: str) -> str:
    """Redact the value of every sensitive ``key=value`` / ``key: value`` fragment."""
    return _replace_spans(text, _kv_spans(text))


# Meta Graph user / page / system-user tokens start with ``EAA``.
_META_TOKEN = re.compile(r"\bEAA[A-Za-z0-9]{20,}")
# A Fernet key (32 bytes, url-safe base64 with one ``=`` pad) wherever it
# appears, e.g. as the value of a local named ``key``. Over-matching another
# 32-byte base64 value only redacts more; it never reveals anything.
_FERNET_KEY = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{43}=(?![A-Za-z0-9_\-=])")


def _auth_value_end(text: str, value_start: int) -> int:
    """End of the credential token at ``value_start`` (``value_start`` if none).

    A token holding a URL extends over the whole URL (and any token
    characters right after it): ``;`` and ``,`` inside a URL query do not end
    the credential.
    """
    value = _AUTH_VALUE.match(text, value_start)
    if value is None:
        return value_start
    return _extend_over_url(text, value_start, value.end())


def _auth_marker_spans(text: str) -> list:
    """``Bearer <credential>`` and ``Authorization: <credential>`` style values.

    Every marker start in the unchanged text redacts the token after it, even
    when that marker sits inside another marker's token (``Bearer Bearer
    <cred>``, ``Bearer x=Authorization: <cred>``, ``Cookie: Cookie: <cred>``):
    a non-overlapping substitution would consume the inner marker as the outer
    credential and leave ``<cred>``. Markers are visited in text order; a
    marker whose value would start inside an already-redacted token is covered
    by it (its value ends no later), so every character is scanned a bounded
    number of times and the pass is linear.
    """
    lowered = text.lower()
    if "bearer" not in lowered and "authorization" not in lowered and "cookie" not in lowered:
        return []
    markers = [(m.start(), m.end(1), False) for m in _BEARER_MARKER.finditer(text)]
    markers += [(m.start(), m.end(1), True) for m in _AUTH_HEADER_MARKER.finditer(text)]
    markers.sort()
    spans = []
    pos = 0  # end of the last redacted token (or 0)
    for _start, value_start, is_header in markers:
        if value_start < pos:
            continue  # inside a redacted token, which already covers this value
        if is_header and _BEARER_WORD.match(text, value_start):
            continue  # ``Authorization: Bearer <cred>`` — the Bearer marker's job
        end = _auth_value_end(text, value_start)
        if end == value_start:
            continue
        spans.append((value_start, end))
        pos = end
    return spans


def _redact_markers_and_keys(text: str, *, outside_urls: bool = False) -> str:
    """Auth-marker and plain-key values, found on the unchanged text, replaced as one union.

    With ``outside_urls`` only values that start outside an absolute URL are
    replaced: inside a URL the URL pass runs first (it parses the query and
    fragment) and this pass then runs again over the normalized text, in the
    pre-PR order, so a value pass can never break a URL's structure first.
    """
    spans = _auth_marker_spans(text) + _kv_spans(text)
    if outside_urls and spans:
        urls = [(m.start(), m.end()) for m in _URL_RE.finditer(text)]
        if urls:
            starts = [start for start, _end in urls]
            kept = []
            for span in spans:
                i = bisect_right(starts, span[0]) - 1
                if i >= 0 and urls[i][0] < span[0] < urls[i][1]:
                    continue  # the value starts inside a URL (a URL value itself is kept)
                kept.append(span)
            spans = kept
    return _replace_spans(text, spans)


# A query key a URL parser can legitimately print back: plain name characters.
_PLAIN_QUERY_KEY = re.compile(r"[A-Za-z0-9_.\-\[\]]*")


def _redact_query_pairs(query: str) -> Tuple[str, bool]:
    """(redacted query, whether any pair was redacted).

    An earlier pass may already have redacted inside this query: a pair whose
    key holds ``REDACTED`` (or a sensitive key with separator characters) is
    dropped whole together with everything after it, and a value holding
    ``REDACTED`` is replaced whole.
    """
    if not query:
        return query, False
    pairs = []
    redacted = False
    for key, value in parse_qsl(query, keep_blank_values=True):
        sensitive = is_sensitive_key(key)
        if REDACTED in key or (sensitive and not _PLAIN_QUERY_KEY.fullmatch(key)):
            # Credential text may be inside what parses as the key, and a
            # header-style credential can run on across ``&``: nothing after
            # this pair is kept.
            pairs.append((REDACTED, ""))
            return urlencode(pairs), True
        elif sensitive or REDACTED in value:
            pairs.append((key, REDACTED))
            redacted = True
        else:
            pairs.append((key, value))
    return urlencode(pairs), redacted


def _redact_query(query: str) -> str:
    return _redact_query_pairs(query)[0]


def _redact_one_url(raw: str) -> str:
    """Redact a single URL; unparseable input collapses to ``[REDACTED_URL]``."""
    try:
        parts = urlsplit(raw)
        netloc = parts.netloc
        if "@" in netloc:  # user:password@host — never keep credentials
            netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"
        query, query_redacted = _redact_query_pairs(parts.query)
        if query_redacted and parts.fragment:
            # A redacted query value may run on past ``#`` (a credential holding
            # ``#``); the fragment is not kept next to it.
            fragment = REDACTED
        elif "=" in parts.fragment or REDACTED in parts.fragment:
            fragment = _redact_query(parts.fragment)
        else:
            fragment = parts.fragment
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
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
    # Auth markers and plain keys first, as in ``redact_secrets``: replacing a
    # sensitive pair up to ``&`` must not consume a ``Bearer`` marker or key
    # whose credential runs past it.
    query = _redact_markers_and_keys(query)
    pairs = []
    for part in query.split("&"):
        key, sep, value = part.partition("=")
        try:
            decoded = unquote_plus(key, errors="strict").strip()
        except (UnicodeDecodeError, ValueError):
            return REDACTED
        sensitive = is_sensitive_key(decoded)
        if REDACTED in key or (sensitive and not _PLAIN_QUERY_KEY.fullmatch(decoded)):
            # Same rule as the URL query: credential text may be inside the
            # key, and a header-style credential can run on across ``&``.
            pairs.append(REDACTED)
            break
        if sep and (sensitive or REDACTED in value):
            pairs.append(f"{key}={REDACTED}")
        else:
            pairs.append(part)
    return "&".join(pairs)


# A bare request target (``/path?query``) as uvicorn's access log and raw
# diagnostics print it — no scheme or host, so ``_URL_RE`` does not see it.
#
# Same matches as the regex ``(?<![^\s"'(\[=,;])(/[^\s"'<>?#]*)\?([^\s"'<>#]*)``
# but found by one forward pass: a regex restarts its path scan at every
# boundary character inside a path (``/=/=/…``), which is quadratic.
_TARGET_STOP = frozenset("\"'<>#")  # also any whitespace
_TARGET_START_BOUNDARY = frozenset("\"'([=,;")  # also any whitespace or start of text


def _redact_bare_targets(text: str) -> str:
    if "/" not in text or "?" not in text:
        return text
    out = []
    n = len(text)
    i = 0
    while i < n:
        # A segment: a maximal run of characters a path or query may contain.
        if text[i].isspace() or text[i] in _TARGET_STOP:
            j = i
            while j < n and (text[j].isspace() or text[j] in _TARGET_STOP):
                j += 1
            out.append(text[i:j])
            i = j
            continue
        j = i
        last_q = -1
        while j < n and not (text[j].isspace() or text[j] in _TARGET_STOP):
            if text[j] == "?":
                last_q = j
            j += 1
        # Leftmost valid start: a "/" at a token boundary with a "?" after it.
        start = -1
        if last_q > i:
            for k in range(i, last_q):
                if text[k] == "/":
                    prev = text[k - 1] if k > 0 else ""
                    if prev == "" or prev.isspace() or prev in _TARGET_START_BOUNDARY:
                        start = k
                        break
        if start < 0:
            out.append(text[i:j])
        else:
            q = text.index("?", start)
            out.append(text[i:q + 1])
            out.append(redact_raw_query(text[q + 1:j]))
        i = j
    return "".join(out)


# ``key=value`` fragments whose key carries percent-encoding (``co%64e=…``,
# ``access%5Ftoken=…``) anywhere in free text — bare query strings, breadcrumb
# fields, request ``query_string``. Plain keys are handled by ``_KV``.
#
# One flat character class for the key (no nested or alternating
# quantifiers) and a start only at a token boundary, so a scan is linear in
# the input length even for adversarial ``%ab%ab…`` runs.
_ENCODED_KEY_CHARS = "A-Za-z0-9_.\\-+%"
_ENCODED_KEY = re.compile(rf"(?<![{_ENCODED_KEY_CHARS}])([{_ENCODED_KEY_CHARS}]+)=[\"']?")
# Same value rule as ``_KV_VALUE``: an auth-scheme word is left for the
# header passes instead of being consumed ahead of its credential.
_ENCODED_VALUE = re.compile(r"(?!(?:Bearer|Basic|Digest)\b)[^\s\"'&,;]+", re.IGNORECASE)
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
            if value is not None:
                out.append(text[pos:m.end()])
                out.append(REDACTED)
                pos = value.end()
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
        # Auth-scheme markers and plain keys first, both found on the
        # unchanged text and replaced as one union of spans: a pass that
        # rewrote the text earlier (URL, request-target, encoded-key, or one
        # of these two) could consume a ``Bearer`` / ``Authorization:``
        # marker or a ``secret=`` key and leave the credential after it.
        # Values that start inside an absolute URL are left to the URL pass
        # and the second marker/key pass, in the pre-PR order. The
        # request-target and encoded-key passes then only redact more.
        out = _redact_markers_and_keys(out, outside_urls=True)
        out = _redact_urls(out)
        # URL normalization can turn ``?code#…`` into ``?code=…`` or
        # ``Authorization:k=,…`` into ``Authorization=…&…``; the marker and key
        # passes run again over the normalized text, as the pre-PR pipeline
        # (URLs first, then markers and keys) did.
        out = _redact_markers_and_keys(out)
        out = _redact_bare_targets(out)
        out = _redact_encoded_kv(out)
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
