"""Standard HTML metadata extraction (title, OG, Twitter, canonical)."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, Optional

from .text import (
    AUTHOR_MAX,
    DESCRIPTION_MAX,
    IMAGE_URL_MAX,
    TITLE_MAX,
    absolute_url,
    provider_domain,
    sanitize_text,
)
from .types import EnrichmentDraft


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.meta: Dict[str, str] = {}
        self.canonical_url = ""
        self.oembed_endpoint = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): v for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "link":
            rel = (attr_map.get("rel") or "").lower()
            href = attr_map.get("href") or ""
            if "canonical" in rel and href:
                self.canonical_url = href
            if "alternate" in rel and "oembed" in (attr_map.get("type") or "").lower():
                self.oembed_endpoint = href
            return
        if tag != "meta":
            return
        name = (attr_map.get("name") or attr_map.get("property") or "").lower()
        content = attr_map.get("content") or ""
        if not name or not content:
            return
        self.meta[name] = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def parse_html_metadata(html: str, base_url: str) -> tuple[EnrichmentDraft, Optional[str]]:
    """Return draft fields and optional declared oEmbed endpoint."""
    if not html:
        return EnrichmentDraft(), None

    parser = _MetadataParser()
    try:
        parser.feed(html)
    except Exception:
        return EnrichmentDraft(), None

    meta = parser.meta
    title = sanitize_text(
        meta.get("og:title") or meta.get("twitter:title") or parser.title,
        TITLE_MAX,
    )
    description = sanitize_text(
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description"),
        DESCRIPTION_MAX,
    )
    author = sanitize_text(
        meta.get("author")
        or meta.get("article:author")
        or meta.get("og:site_name")
        or meta.get("twitter:creator"),
        AUTHOR_MAX,
    )
    canonical = absolute_url(base_url, parser.canonical_url or meta.get("og:url") or "")
    preview = absolute_url(
        base_url,
        meta.get("og:image") or meta.get("twitter:image") or meta.get("twitter:image:src") or "",
    )
    preview = sanitize_text(preview, IMAGE_URL_MAX)
    published = sanitize_text(meta.get("article:published_time") or meta.get("og:updated_time"), 80)

    content_kind = ""
    og_type = (meta.get("og:type") or "").lower()
    if og_type:
        content_kind = og_type

    draft = EnrichmentDraft(
        page_title=title,
        safe_description=description,
        author_or_channel=author,
        content_kind=content_kind,
        published_at=published,
        canonical_url=canonical or base_url,
        canonical_domain=provider_domain(canonical or base_url),
        preview_image=preview,
        enrichment_source="html_metadata",
        sources_used=["html_metadata"],
    )
    oembed_endpoint = absolute_url(base_url, parser.oembed_endpoint or "")
    return draft, oembed_endpoint or None


def has_useful_standard_metadata(draft: EnrichmentDraft) -> bool:
    return bool(
        sanitize_text(draft.safe_description, 40)
        or sanitize_text(draft.author_or_channel, 20)
        or (
            sanitize_text(draft.page_title, 40)
            and not re.match(r"^(tiktok|youtube|facebook|instagram|twitter|x)\b", draft.page_title, re.I)
        )
    )
