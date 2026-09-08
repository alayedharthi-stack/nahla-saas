"""URL resolution and oEmbed target hygiene for untrusted web metadata."""
from __future__ import annotations

from typing import FrozenSet, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

# oEmbed page targets: no query keys by default (TikTok and generic pages).
OEMBED_PAGE_QUERY_ALLOWLIST: FrozenSet[str] = frozenset()

# Non-sensitive oEmbed endpoint parameters that may be preserved.
OEMBED_ENDPOINT_QUERY_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "format",
        "maxwidth",
        "maxheight",
    }
)


def _provider_domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _port_is_invalid(parsed) -> bool:
    try:
        parsed.port
    except ValueError:
        return True
    return False


def _format_netloc(host: str, port: Optional[int], scheme: str) -> str:
    if port is None:
        return host
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return host
    return f"{host}:{port}"


def _public_host_path_only(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{host}{path}"


def _apply_query_allowlist(query: str, allowed_keys: FrozenSet[str]) -> str:
    if not allowed_keys:
        return ""
    allowed = {key.lower() for key in allowed_keys}
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=False):
        if key.lower() in allowed:
            kept.append((key, value))
    return urlencode(kept)


def _sanitize_http_url(
    url: str,
    *,
    allowed_query_keys: FrozenSet[str],
) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""

    if parsed.scheme not in {"http", "https"}:
        return ""
    if _port_is_invalid(parsed):
        return ""

    if parsed.username or parsed.password:
        host = (parsed.hostname or "").lower()
        if not host:
            return ""
        netloc = _format_netloc(host, parsed.port, parsed.scheme)
        parsed = parsed._replace(netloc=netloc)

    host = (parsed.hostname or "").lower()
    if not host:
        return ""

    netloc = _format_netloc(host, parsed.port, parsed.scheme)
    path = parsed.path or "/"
    clean_query = _apply_query_allowlist(parsed.query, allowed_query_keys)
    return urlunparse((parsed.scheme, netloc, path, "", clean_query, ""))


def resolve_http_url(base: str, maybe_relative: str) -> str:
    """Resolve a relative or absolute URL to a safe HTTP(S) URL without credentials."""
    raw = str(maybe_relative or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "data:", "file:", "vbscript:")):
        return ""

    base_raw = str(base or "").strip()
    base_parsed = urlparse(base_raw if "://" in base_raw else f"https://{base_raw}" if base_raw else "")
    if raw.startswith(("http://", "https://")):
        joined = raw
    elif raw.startswith("//"):
        scheme = base_parsed.scheme if base_parsed.scheme in {"http", "https"} else "https"
        joined = f"{scheme}:{raw}"
    elif base_parsed.scheme and base_parsed.netloc:
        joined = urljoin(base_parsed.geturl(), raw)
    else:
        return ""

    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return ""
    if _port_is_invalid(parsed):
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    netloc = _format_netloc(host, parsed.port, parsed.scheme)
    return urlunparse((parsed.scheme, netloc, parsed.path or "/", "", parsed.query, ""))


def same_origin(url_a: str, url_b: str) -> bool:
    left = _provider_domain(url_a)
    right = _provider_domain(url_b)
    return bool(left and right and left == right)


def sanitize_public_metadata_url(url: str) -> str:
    """Fail-closed public URL for provider payload: no credentials, query, or fragment."""
    return _sanitize_http_url(url, allowed_query_keys=OEMBED_PAGE_QUERY_ALLOWLIST)


def sanitize_oembed_target_url(
    *,
    final_url: str,
    canonical_url: str = "",
    allowed_query_keys: Optional[FrozenSet[str]] = None,
) -> str:
    """Build a safe oEmbed page URL with allowlisted query keys only (default: none)."""
    chosen = str(final_url or "").strip()
    canonical = str(canonical_url or "").strip()
    if canonical and same_origin(canonical, chosen):
        chosen = canonical
    allowlist = allowed_query_keys if allowed_query_keys is not None else OEMBED_PAGE_QUERY_ALLOWLIST
    return _sanitize_http_url(chosen, allowed_query_keys=allowlist)


def build_oembed_request_url(
    page_url: str,
    endpoint: str,
    *,
    allowed_page_query_keys: Optional[FrozenSet[str]] = None,
) -> str:
    endpoint = str(endpoint or "").strip()
    allowlist = (
        allowed_page_query_keys
        if allowed_page_query_keys is not None
        else OEMBED_PAGE_QUERY_ALLOWLIST
    )
    safe_page_url = sanitize_oembed_target_url(
        final_url=page_url,
        canonical_url=page_url,
        allowed_query_keys=allowlist,
    )
    if not safe_page_url or not endpoint:
        return ""

    if "{url}" in endpoint:
        return endpoint.replace("{url}", quote(safe_page_url, safe=""))

    if endpoint.startswith(("http://", "https://")):
        endpoint_url = endpoint
    else:
        page_parsed = urlparse(safe_page_url)
        endpoint_url = urljoin(f"{page_parsed.scheme}://{page_parsed.netloc}/", endpoint.lstrip("/"))

    ep_parsed = urlparse(endpoint_url)
    if ep_parsed.scheme not in {"http", "https"} or not ep_parsed.hostname:
        return ""
    if _port_is_invalid(ep_parsed):
        return ""

    allowed_endpoint = {key.lower() for key in OEMBED_ENDPOINT_QUERY_ALLOWLIST}
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(ep_parsed.query, keep_blank_values=True):
        low = key.lower()
        if low == "url":
            continue
        if low in allowed_endpoint:
            kept.append((key, value))
    kept.append(("url", safe_page_url))

    netloc = _format_netloc(ep_parsed.hostname.lower(), ep_parsed.port, ep_parsed.scheme)
    query = urlencode(kept)
    return urlunparse((ep_parsed.scheme, netloc, ep_parsed.path or "/", "", query, ""))
