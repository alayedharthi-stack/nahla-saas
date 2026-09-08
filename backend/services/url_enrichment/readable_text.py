"""Bounded readable-text fallback extraction."""
from __future__ import annotations

import re

from .text import READABLE_EXCERPT_MAX, sanitize_text

_BLOCK_TAGS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|iframe|svg)[^>]*>.*?</\1>",
    flags=re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def extract_readable_excerpt(html: str) -> str:
    if not html:
        return ""
    cleaned = _BLOCK_TAGS.sub(" ", html)
    # Prefer body content when present.
    body_match = re.search(r"<body[^>]*>(.*)</body>", cleaned, flags=re.I | re.S)
    if body_match:
        cleaned = body_match.group(1)
    text = _TAG_RE.sub(" ", cleaned)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    # Drop very short navigation-like crumbs.
    if len(text) < 40:
        return ""
    return sanitize_text(text, READABLE_EXCERPT_MAX)
