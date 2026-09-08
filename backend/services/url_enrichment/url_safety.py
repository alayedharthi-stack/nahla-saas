"""URL resolution and oEmbed target hygiene for untrusted web metadata."""
from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

_BLOCKED_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "auth",
        "signature",
        "sig",
        "session",
        "sessionid",
        "key",
        "secret",
        "password",
        "code",
    }
)
_TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "mc_eid", "ref")


def _provider_domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _strip_sensitive_query(query: str) -> str:
    if not query:
        return ""
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=False):
        low = key.lower()
        if low in _BLOCKED_QUERY_KEYS:
            continue
        if any(low.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
            continue
        kept.append((key, value))
    return urlencode(kept)


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
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    if port and not (
        (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return urlunparse((parsed.scheme, netloc, parsed.path or "/", parsed.params, parsed.query, ""))


def same_origin(url_a: str, url_b: str) -> bool:
    left = _provider_domain(url_a)
    right = _provider_domain(url_b)
    return bool(left and right and left == right)


def sanitize_oembed_target_url(*, final_url: str, canonical_url: str = "") -> str:
    """Build a safe oEmbed page URL: no fragment, no sensitive/tracking query."""
    chosen = str(final_url or "").strip()
    canonical = str(canonical_url or "").strip()
    if canonical and same_origin(canonical, chosen):
        chosen = canonical
    parsed = urlparse(chosen)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    clean_query = _strip_sensitive_query(parsed.query)
    clean = parsed._replace(fragment="", query=clean_query)
    return urlunparse(clean)


def build_oembed_request_url(page_url: str, endpoint: str) -> str:
    endpoint = str(endpoint or "").strip()
    safe_page_url = sanitize_oembed_target_url(final_url=page_url, canonical_url=page_url)
    if not safe_page_url:
        return ""
    if "{url}" in endpoint:
        return endpoint.replace("{url}", quote(safe_page_url, safe=""))
    if "url=" in endpoint:
        return endpoint
    joiner = "&" if "?" in endpoint else "?"
    return f"{endpoint}{joiner}url={quote(safe_page_url, safe='')}"
