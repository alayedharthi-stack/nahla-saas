"""Fail-closed HTTP URL equivalence for Commerce Agent grounding."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import quote, urlsplit

from pydantic import HttpUrl, TypeAdapter, ValidationError


_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PERCENT_ESCAPE_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_CONTROL_OR_SPACE_RE = re.compile(r"[\x00-\x20\x7f]")
_PATH_SAFE = "/:@!$&'()*+,;=-._~%"
_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


def _uppercase_percent_escapes(value: str) -> str:
    return _PERCENT_ESCAPE_RE.sub(lambda match: f"%{match.group(1).upper()}", value)


def canonical_http_url(value: object) -> str | None:
    """Return a comparison-only canonical HTTP(S) URL, or ``None`` if unsafe.

    The function performs no I/O. It preserves path, query, and fragment
    semantics while normalizing only representation details that cannot change
    the addressed resource.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if (
        _CONTROL_OR_SPACE_RE.search(value)
        or _INVALID_PERCENT_RE.search(value)
        or "\\" in value
    ):
        return None
    try:
        validated = _HTTP_URL_ADAPTER.validate_python(value)
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.netloc:
            return None
        canonical_host = str(validated.host or "")
        if not canonical_host or "%" in canonical_host:
            return None
        port = parsed.port
    except (UnicodeError, ValidationError, ValueError):
        return None

    userinfo = ""
    host_port = parsed.netloc.rsplit("@", 1)[-1]
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@"
        userinfo = _uppercase_percent_escapes(userinfo)

    rendered_host = canonical_host
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    if port is not None and not default_port:
        rendered_host = f"{rendered_host}:{port}"
    elif port is None and host_port.endswith(":"):
        return None

    # UTF-8 percent-encode raw Unicode path characters without decoding an
    # existing escape such as %2F into a semantic delimiter.
    try:
        path = quote(parsed.path, safe=_PATH_SAFE, encoding="utf-8", errors="strict")
    except UnicodeError:
        return None
    path = _uppercase_percent_escapes(path)
    if not path:
        path = "/"

    query = _uppercase_percent_escapes(parsed.query)
    fragment = _uppercase_percent_escapes(parsed.fragment)
    before_fragment = value.split("#", 1)[0]
    has_query_delimiter = "?" in before_fragment
    has_fragment_delimiter = "#" in value

    canonical = f"{scheme}://{userinfo}{rendered_host}{path}"
    if has_query_delimiter:
        canonical += f"?{query}"
    if has_fragment_delimiter:
        canonical += f"#{fragment}"
    return canonical


def canonical_http_url_equal(left: object, right: object) -> bool:
    """Compare two HTTP(S) URLs using the shared fail-closed contract."""
    canonical_left = canonical_http_url(left)
    canonical_right = canonical_http_url(right)
    return bool(
        canonical_left is not None
        and canonical_right is not None
        and canonical_left == canonical_right
    )


def url_fingerprint(value: object) -> str | None:
    """Return a non-reversible SHA-256 fingerprint for safe diagnostics."""
    if not isinstance(value, str):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
