"""Metadata usefulness assessment for URL enrichment."""
from __future__ import annotations

import re

from .text import provider_domain, sanitize_text
from .types import EnrichmentDraft

_THIN_TITLE_PATTERNS = (
    re.compile(r"^tiktok\s*-\s*make your day$", re.I),
    re.compile(r"^tiktok$", re.I),
    re.compile(r"^youtube$", re.I),
    re.compile(r"^facebook$", re.I),
    re.compile(r"^instagram$", re.I),
    re.compile(r"^twitter$", re.I),
    re.compile(r"^x$", re.I),
    re.compile(r"^home$", re.I),
    re.compile(r"^welcome$", re.I),
    re.compile(r"^about$", re.I),
    re.compile(r"^app$", re.I),
)
_PLATFORM_AUTHOR_PATTERNS = (
    re.compile(r"^tiktok$", re.I),
    re.compile(r"^youtube$", re.I),
    re.compile(r"^facebook$", re.I),
    re.compile(r"^instagram$", re.I),
    re.compile(r"^twitter$", re.I),
    re.compile(r"^x$", re.I),
)


def _is_thin_title(title: str) -> bool:
    cleaned = sanitize_text(title, 240).strip()
    if not cleaned:
        return True
    return any(pattern.fullmatch(cleaned) for pattern in _THIN_TITLE_PATTERNS)


def _is_platform_only_author(author: str, canonical_domain: str) -> bool:
    cleaned = sanitize_text(author, 120).strip()
    if not cleaned:
        return True
    if any(pattern.fullmatch(cleaned) for pattern in _PLATFORM_AUTHOR_PATTERNS):
        return True
    domain = (canonical_domain or "").lower().strip()
    if domain and cleaned.lower() == domain:
        return True
    root = domain[4:] if domain.startswith("www.") else domain
    if root and cleaned.lower() == root.split(".")[0]:
        return True
    return False


def assess_draft_quality(draft: EnrichmentDraft) -> tuple[str, bool]:
    title = sanitize_text(draft.page_title, 240)
    description = sanitize_text(draft.safe_description, 500)
    excerpt = sanitize_text(draft.readable_excerpt, 500)
    author = sanitize_text(draft.author_or_channel, 120)
    body_text = description or excerpt

    if not title and not body_text and not author:
        return "empty", False

    title_is_thin = _is_thin_title(title)
    domain_only = bool(title) and title.lower() == (draft.canonical_domain or "").lower()
    has_real_title = bool(title) and not title_is_thin and not domain_only
    has_substance = bool(body_text)

    if has_substance or has_real_title:
        return "useful", True

    if title or author:
        return "thin", False

    return "empty", False


def merge_preferring_richer(
    draft: EnrichmentDraft,
    *,
    page_title: str = "",
    safe_description: str = "",
    author_or_channel: str = "",
    content_kind: str = "",
    published_at: str = "",
    canonical_url: str = "",
    preview_image: str = "",
    readable_excerpt: str = "",
    source: str = "",
) -> None:
    if page_title and (not draft.page_title or _is_thin_title(draft.page_title)):
        draft.page_title = page_title
    if safe_description and not draft.safe_description:
        draft.safe_description = safe_description
    if author_or_channel and not draft.author_or_channel:
        draft.author_or_channel = author_or_channel
    if content_kind and not draft.content_kind:
        draft.content_kind = content_kind
    if published_at and not draft.published_at:
        draft.published_at = published_at
    if canonical_url:
        draft.canonical_url = canonical_url
        draft.canonical_domain = provider_domain(canonical_url)
    if preview_image and not draft.preview_image:
        draft.preview_image = preview_image
    if readable_excerpt and not draft.readable_excerpt:
        draft.readable_excerpt = readable_excerpt
    if source:
        if draft.enrichment_source and source not in draft.enrichment_source:
            draft.enrichment_source = f"{draft.enrichment_source}+{source}"
        elif not draft.enrichment_source:
            draft.enrichment_source = source
        if source not in draft.sources_used:
            draft.sources_used.append(source)
