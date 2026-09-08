"""Safe JSON-LD structured data extraction."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

from .quality import merge_preferring_richer
from .text import AUTHOR_MAX, DESCRIPTION_MAX, IMAGE_URL_MAX, TITLE_MAX, absolute_url, sanitize_text
from .types import EnrichmentDraft

_SUPPORTED_TYPES = {
    "article",
    "newsarticle",
    "blogposting",
    "videoobject",
    "product",
}


def _iter_json_ld_objects(payload: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(payload, dict):
        if "@graph" in payload and isinstance(payload["@graph"], list):
            for item in payload["@graph"]:
                if isinstance(item, dict):
                    yield item
        else:
            yield payload
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item


def _type_names(node: Dict[str, Any]) -> List[str]:
    raw = node.get("@type")
    if isinstance(raw, list):
        return [str(x).lower() for x in raw]
    if raw:
        return [str(raw).lower()]
    return []


def _author_name(value: Any) -> str:
    if isinstance(value, str):
        return sanitize_text(value, AUTHOR_MAX)
    if isinstance(value, dict):
        return sanitize_text(value.get("name"), AUTHOR_MAX)
    if isinstance(value, list):
        for item in value:
            name = _author_name(item)
            if name:
                return name
    return ""


def _image_url(value: Any, base_url: str) -> str:
    if isinstance(value, str):
        return sanitize_text(absolute_url(base_url, value), IMAGE_URL_MAX)
    if isinstance(value, dict):
        return sanitize_text(absolute_url(base_url, value.get("url") or ""), IMAGE_URL_MAX)
    if isinstance(value, list) and value:
        return _image_url(value[0], base_url)
    return ""


def _extract_from_node(node: Dict[str, Any], base_url: str) -> Optional[Dict[str, str]]:
    types = _type_names(node)
    if not any(t in _SUPPORTED_TYPES for t in types):
        return None
    title = sanitize_text(node.get("headline") or node.get("name"), TITLE_MAX)
    description = sanitize_text(node.get("description"), DESCRIPTION_MAX)
    author = _author_name(node.get("author"))
    published = sanitize_text(node.get("datePublished") or node.get("dateCreated"), 80)
    preview = _image_url(node.get("image") or node.get("thumbnailUrl"), base_url)
    content_kind = next((t for t in types if t in _SUPPORTED_TYPES), "")
    if not any([title, description, author, preview]):
        return None
    return {
        "page_title": title,
        "safe_description": description,
        "author_or_channel": author,
        "content_kind": content_kind,
        "published_at": published,
        "preview_image": preview,
    }


def extract_json_ld_metadata(html: str, base_url: str) -> EnrichmentDraft:
    draft = EnrichmentDraft()
    if not html:
        return draft

    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    )
    if not scripts:
        return draft

    for raw_script in scripts:
        try:
            payload = json.loads(raw_script)
        except Exception:
            continue
        for node in _iter_json_ld_objects(payload):
            extracted = _extract_from_node(node, base_url)
            if not extracted:
                continue
            merge_preferring_richer(
                draft,
                page_title=extracted.get("page_title", ""),
                safe_description=extracted.get("safe_description", ""),
                author_or_channel=extracted.get("author_or_channel", ""),
                content_kind=extracted.get("content_kind", ""),
                published_at=extracted.get("published_at", ""),
                preview_image=extracted.get("preview_image", ""),
                source="json_ld",
            )
            if draft.page_title or draft.safe_description:
                return draft
    return draft
