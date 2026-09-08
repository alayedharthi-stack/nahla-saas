"""oEmbed discovery and safe response parsing."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, Optional

from backend.services.safe_http_fetch import SafeHttpResult

from .quality import merge_preferring_richer
from .text import AUTHOR_MAX, DESCRIPTION_MAX, IMAGE_URL_MAX, TITLE_MAX, sanitize_text
from .types import EnrichmentDraft
from .url_safety import build_oembed_request_url

FetchFn = Callable[..., Awaitable[SafeHttpResult]]

_BLOCKED_FETCH_ERRORS = frozenset(
    {
        "ip_blocked",
        "host_blocked",
        "scheme_blocked",
        "credentials_blocked",
    }
)


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


def map_fetch_error_to_oembed_status(error_class: str) -> str:
    if str(error_class or "").strip().lower() in _BLOCKED_FETCH_ERRORS:
        return "blocked"
    return "failed"


async def fetch_oembed(
    *,
    page_url: str,
    endpoint: str,
    fetch: FetchFn,
    deadline: Optional[float] = None,
) -> tuple[EnrichmentDraft, str]:
    """Fetch and parse oEmbed JSON via the safe fetch path only."""
    draft = EnrichmentDraft()
    oembed_url = build_oembed_request_url(page_url, endpoint)
    if not oembed_url:
        return draft, "failed"

    if deadline is not None:
        try:
            got = fetch(oembed_url, deadline=deadline)
        except TypeError:
            got = fetch(oembed_url)
    else:
        got = fetch(oembed_url)
    result = await got if hasattr(got, "__await__") else got

    if not result.ok or not result.body:
        return draft, map_fetch_error_to_oembed_status(getattr(result, "error_class", ""))

    try:
        payload = json.loads(result.body.decode("utf-8", errors="ignore"))
    except Exception:
        return draft, "invalid_json"

    if not isinstance(payload, dict):
        return draft, "invalid_json"

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
