"""Text sanitization helpers for URL enrichment."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

TITLE_MAX = 180
DESCRIPTION_MAX = 400
AUTHOR_MAX = 80
IMAGE_URL_MAX = 500
READABLE_EXCERPT_MAX = 400


def sanitize_text(value: Any, limit: int) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def provider_domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def absolute_url(base: str, maybe_relative: str) -> str:
    raw = str(maybe_relative or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    try:
        parsed = urlparse(base)
        if raw.startswith("//"):
            return f"{parsed.scheme}:{raw}"
        if raw.startswith("/"):
            return f"{parsed.scheme}://{parsed.netloc}{raw}"
    except Exception:
        return ""
    return ""


def normalize_compare_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    candidate = raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
    except Exception:
        return raw.lower().rstrip("/")
    host = (parsed.hostname or "").lower()
    path = unquote(parsed.path or "")
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    query = parsed.query or ""
    return f"{host}{path}?{query}"
