"""oEmbed discovery and safe response parsing."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote

from backend.services.safe_http_fetch import SafeHttpResult, validate_destination

from .quality import merge_preferring_richer
from .text import AUTHOR_MAX, DESCRIPTION_MAX, IMAGE_URL_MAX, TITLE_MAX, sanitize_text
from .types import EnrichmentDraft

FetchFn = Callable[[str], Awaitable[SafeHttpResult]]


def build_oembed_url(page_url: str, endpoint: str) -> str:
    endpoint = str(endpoint or "").strip()
    if "{url}" in endpoint:
        return endpoint.replace("{url}", quote(page_url, safe=""))
    if "url=" in endpoint:
        return endpoint
    joiner = "&" if "?" in endpoint else "?"
    return f"{endpoint}{joiner}url={quote(page_url, safe='')}"


def parse_oembed_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    """Extract safe text fields; ignore html and other executable content."""
    return {
        "page_title": sanitize_text(payload.get("title"), TITLE_MAX),
        "safe_description": sanitize_text(payload.get("description"), DESCRIPTION_MAX),
        "author_or_channel": sanitize_text(
            payload.get("author_name") or payload.get("provider_name"),
            AUTHOR_MAX,
        ),
        "preview_image": sanitize_text(
            payload.get("thumbnail_url") or payload.get("thumbnail"),
            IMAGE_URL_MAX,
        ),
    }


async def fetch_oembed(
    *,
    page_url: str,
    endpoint: str,
    fetch: FetchFn,
) -> tuple[EnrichmentDraft, str]:
    """Fetch and parse oEmbed JSON. Returns draft and status."""
    draft = EnrichmentDraft()
    oembed_url = build_oembed_url(page_url, endpoint)
    validated, err = validate_destination(oembed_url)
    if validated is None:
        status = "blocked" if err in {"ip_blocked", "host_blocked"} else "failed"
        return draft, status
    result = await fetch(oembed_url)
    if not result.ok or not result.body:
        return draft, "failed"

    try:
        payload = json.loads(result.body.decode("utf-8", errors="ignore"))
    except Exception:
        return draft, "invalid_json"

    if not isinstance(payload, dict):
        return draft, "invalid_json"

    # Explicitly ignore html field even if present.
    extracted = parse_oembed_payload(payload)
    merge_preferring_richer(
        draft,
        page_title=extracted.get("page_title", ""),
        safe_description=extracted.get("safe_description", ""),
        author_or_channel=extracted.get("author_or_channel", ""),
        preview_image=extracted.get("preview_image", ""),
        source="oembed",
    )
    if any(extracted.values()):
        return draft, "ok"
    return draft, "empty"
