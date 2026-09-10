"""
SSRF-safe HTTPS fetch for order_confirmation header images only.

Uses pinned-IP transport from ``services.safe_http_fetch`` (no DNS rebinding).
External merchant URLs are allowed only through that hardened path; platform R2
URLs are the preferred trusted source when no merchant customization exists.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Tuple
from urllib.parse import urlparse

from services.safe_http_fetch import (
    MAX_HEADER_IMAGE_BYTES,
    fetch_https_image_bytes_async,
    redact_url_for_log,
)

logger = logging.getLogger("nahla.commerce_lifecycle.order_confirmation_header_fetch")

FETCH_TIMEOUT_S = 30.0
_RAILWAY_INTERNAL_SUFFIXES = (".railway.internal",)
_ALLOWED_MIME = frozenset({"image/jpeg", "image/png"})


class HeaderImageFetchError(ValueError):
    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code


def _is_railway_internal_host(host: str) -> bool:
    name = str(host or "").strip().lower().rstrip(".")
    return any(name.endswith(suffix) for suffix in _RAILWAY_INTERNAL_SUFFIXES)


def _railway_host_blocker(host: str) -> bool:
    return _is_railway_internal_host(host)


def _map_fetch_error(error_class: str) -> str:
    mapping = {
        "oversized": "body_too_large",
        "missing_content_type": "missing_content_type",
        "html_response_blocked": "html_response_blocked",
        "content_type_blocked": "content_type_blocked",
        "scheme_blocked": "scheme_blocked",
        "host_blocked": "host_blocked",
        "ip_blocked": "ip_blocked",
        "destination_blocked": "destination_blocked",
        "redirect_missing": "redirect_missing",
        "too_many_redirects": "redirect_limit",
        "redirect_loop": "redirect_limit",
        "http_error": "http_error",
        "timeout": "fetch_failed",
        "transport_error": "fetch_failed",
        "dns_error": "fetch_failed",
        "network_error": "fetch_failed",
        "tls_error": "fetch_failed",
    }
    return mapping.get(str(error_class or "").strip(), "fetch_failed")


def _detect_image_mime(body: bytes, content_type: str) -> str:
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        raise HeaderImageFetchError("missing_content_type")
    if ct in {"text/html", "application/xhtml+xml"}:
        raise HeaderImageFetchError("html_response_blocked")
    if ct not in _ALLOWED_MIME:
        raise HeaderImageFetchError("content_type_blocked")
    if len(body) < 4:
        raise HeaderImageFetchError("body_too_small")
    if body[:3] == b"\xff\xd8\xff":
        if ct != "image/jpeg":
            raise HeaderImageFetchError("content_sniff_mismatch")
        return "image/jpeg"
    if body[:4] == b"\x89PNG":
        if ct != "image/png":
            raise HeaderImageFetchError("content_sniff_mismatch")
        return "image/png"
    raise HeaderImageFetchError("unsupported_image_format")


async def fetch_header_image_bytes_secure(
    url: str,
    *,
    timeout: float = FETCH_TIMEOUT_S,
    max_bytes: int = MAX_HEADER_IMAGE_BYTES,
    transport: object = None,
    resolver: object = None,
) -> Tuple[bytes, str]:
    """
    Fetch a header image over HTTPS with pinned-IP SSRF controls and size limits.

    Returns ``(body, mime_type)`` where mime is ``image/jpeg`` or ``image/png``.
    """
    raw = str(url or "").strip()
    try:
        railway_host = urlparse(raw).hostname or ""
    except Exception:
        railway_host = ""
    if _is_railway_internal_host(railway_host):
        raise HeaderImageFetchError("host_blocked")
    try:
        result = await fetch_https_image_bytes_async(
            raw,
            transport=transport,
            resolver=resolver,
            max_bytes=max_bytes,
            deadline=None,
            host_blocker=_railway_host_blocker,
        )
    except Exception:
        logger.exception(
            "[order_confirmation_header_fetch] failed url=%s",
            redact_url_for_log(raw),
        )
        raise HeaderImageFetchError("fetch_failed")
    if not result.ok:
        code = _map_fetch_error(result.error_class)
        logger.info(
            "[order_confirmation_header_fetch] blocked url=%s code=%s",
            redact_url_for_log(result.final_url or raw),
            code,
        )
        raise HeaderImageFetchError(code)
    try:
        mime = _detect_image_mime(result.body or b"", result.content_type or "")
    except HeaderImageFetchError:
        logger.info(
            "[order_confirmation_header_fetch] sniff blocked url=%s",
            redact_url_for_log(result.final_url or raw),
        )
        raise
    logger.info(
        "[order_confirmation_header_fetch] ok url=%s bytes=%d mime=%s",
        redact_url_for_log(result.final_url or raw),
        len(result.body or b""),
        mime,
    )
    return result.body or b"", mime


def fetch_header_image_bytes(url: str, *, timeout: float = FETCH_TIMEOUT_S) -> Tuple[bytes, str]:
    """Sync wrapper for tests and non-async callers."""
    return asyncio.run(fetch_header_image_bytes_secure(url, timeout=timeout))


__all__ = [
    "HeaderImageFetchError",
    "MAX_HEADER_IMAGE_BYTES",
    "fetch_header_image_bytes",
    "fetch_header_image_bytes_secure",
]
