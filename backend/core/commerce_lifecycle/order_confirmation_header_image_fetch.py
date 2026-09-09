"""
SSRF-safe HTTPS fetch for order_confirmation header images only.

Used before Meta template submit. Reuses shared SSRF validators from
``services.safe_http_fetch`` with stricter HTTPS-only policy.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from services.safe_http_fetch import (
    _idna_hostname,
    _is_blocked_hostname,
    _parse_and_validate_url,
    default_resolver,
    hostname_is_ip_trick,
    is_blocked_ip,
    redact_url_for_log,
    validate_destination,
)

logger = logging.getLogger("nahla.commerce_lifecycle.order_confirmation_header_fetch")

MAX_HEADER_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_REDIRECTS = 3
FETCH_TIMEOUT_S = 30.0
_ALLOWED_MIME = frozenset({"image/jpeg", "image/png"})
_RAILWAY_INTERNAL_SUFFIXES = (".railway.internal",)


class HeaderImageFetchError(ValueError):
    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code


def _is_railway_internal_host(host: str) -> bool:
    name = _idna_hostname(host)
    return any(name.endswith(suffix) for suffix in _RAILWAY_INTERNAL_SUFFIXES)


def _validate_https_image_url(url: str) -> str:
    raw = str(url or "").strip()
    parsed, err = _parse_and_validate_url(raw)
    if parsed is None:
        raise HeaderImageFetchError(err or "invalid_url")
    if str(parsed.scheme or "").lower() != "https":
        raise HeaderImageFetchError("scheme_blocked")
    host = _idna_hostname(parsed.hostname or "")
    if _is_railway_internal_host(host):
        raise HeaderImageFetchError("host_blocked")
    return raw


def _validate_resolved_destination(url: str) -> None:
    _validate_https_image_url(url)
    req, err = validate_destination(url, resolver=default_resolver)
    if req is None:
        raise HeaderImageFetchError(err or "destination_blocked")


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
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[bytes, str]:
    """
    Fetch a header image over HTTPS with SSRF controls and size limits.

    Returns ``(body, mime_type)`` where mime is ``image/jpeg`` or ``image/png``.
    """
    current = _validate_https_image_url(url)
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            verify=ssl.create_default_context(),
        )
    try:
        for hop in range(MAX_REDIRECTS + 1):
            _validate_resolved_destination(current)
            resp = await client.get(current)
            if resp.status_code in {301, 302, 303, 307, 308}:
                location = str(resp.headers.get("location") or "").strip()
                if not location:
                    raise HeaderImageFetchError("redirect_missing")
                if hop >= MAX_REDIRECTS:
                    raise HeaderImageFetchError("redirect_limit")
                current = urljoin(current, location)
                continue
            if resp.status_code < 200 or resp.status_code >= 300:
                raise HeaderImageFetchError("http_error")
            content_type = str(resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise HeaderImageFetchError("body_too_large")
                chunks.append(chunk)
            body = b"".join(chunks)
            mime = _detect_image_mime(body, content_type)
            logger.info(
                "[order_confirmation_header_fetch] ok url=%s bytes=%d mime=%s",
                redact_url_for_log(current),
                len(body),
                mime,
            )
            return body, mime
        raise HeaderImageFetchError("redirect_limit")
    except HeaderImageFetchError:
        raise
    except Exception:
        logger.exception(
            "[order_confirmation_header_fetch] failed url=%s",
            redact_url_for_log(current),
        )
        raise HeaderImageFetchError("fetch_failed")
    finally:
        if owns_client and client is not None:
            await client.aclose()


def fetch_header_image_bytes(url: str, *, timeout: float = FETCH_TIMEOUT_S) -> Tuple[bytes, str]:
    """Sync wrapper for tests and non-async callers."""
    return asyncio.run(fetch_header_image_bytes_secure(url, timeout=timeout))


__all__ = [
    "HeaderImageFetchError",
    "MAX_HEADER_IMAGE_BYTES",
    "fetch_header_image_bytes",
    "fetch_header_image_bytes_secure",
]
