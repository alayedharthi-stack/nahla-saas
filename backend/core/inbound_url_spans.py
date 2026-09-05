"""Structural inbound URL-span projection for keyword haystacks.

Used only to exclude URL spans from contact/store keyword matching.
Does not fetch, expand, resolve, preview, or inspect hostnames as
social networks. Does not replace the raw inbound message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence
from urllib.parse import unquote, urlparse

# Trailing wrappers/punctuation that WhatsApp clients attach around URLs.
_TRAILING_WRAP = ".,:;!?)]}>\"'" + "،؛؟»"

# Scheme / www / scheme-less host+path. Stops before whitespace or Arabic
# letters so adjacent customer questions are not consumed.
_INBOUND_URL_SPAN_RE = re.compile(
    r"(?:"
    r"https?://[^\s<>\"'\u0600-\u06FF]+"
    r"|www\.[^\s<>\"'\u0600-\u06FF]+"
    r"|(?<![A-Za-z0-9._%+@-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}"
    r"(?::\d{2,5})?(?:/[^\s<>\"'\u0600-\u06FF]*)?"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InboundUrlSpan:
    start: int
    end: int
    text: str


def _trim_span_text(raw: str) -> str:
    text = str(raw or "")
    return text.rstrip(_TRAILING_WRAP)


def iter_inbound_url_spans(message: str) -> List[InboundUrlSpan]:
    """Return non-overlapping URL spans in *message* (structural only)."""
    text = str(message or "")
    if not text:
        return []
    spans: List[InboundUrlSpan] = []
    for match in _INBOUND_URL_SPAN_RE.finditer(text):
        trimmed = _trim_span_text(match.group(0))
        if not trimmed:
            continue
        end = match.start() + len(trimmed)
        spans.append(InboundUrlSpan(start=match.start(), end=end, text=trimmed))
    return spans


def extract_inbound_url_spans(message: str) -> List[str]:
    return [span.text for span in iter_inbound_url_spans(message)]


def semantic_text_excluding_url_spans(message: str) -> str:
    """Customer-authored keyword haystack with URL spans removed.

    Raw *message* is not mutated for storage, model context, or audit.
    """
    text = str(message or "")
    if not text:
        return ""
    spans = iter_inbound_url_spans(text)
    if not spans:
        return text.strip()
    parts: List[str] = []
    cursor = 0
    for span in spans:
        parts.append(text[cursor:span.start])
        parts.append(" ")
        cursor = span.end
    parts.append(text[cursor:])
    remainder = "".join(parts)
    remainder = re.sub(r"[ \t]+", " ", remainder)
    remainder = re.sub(r"\n{3,}", "\n\n", remainder)
    remainder = remainder.strip()
    if remainder and re.fullmatch(r"[\s.,:;!?()\[\]{}<>\"'«»،؛؟]+", remainder):
        return ""
    return remainder


def is_url_only_inbound(message: str) -> bool:
    """True when the inbound is one or more URL spans and no other text."""
    text = str(message or "").strip()
    if not text:
        return False
    if not iter_inbound_url_spans(text):
        return False
    return not semantic_text_excluding_url_spans(text)


def _normalize_url_for_compare(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    candidate = raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        if candidate.lower().startswith("www."):
            candidate = "https://" + candidate
        else:
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
    fragment = parsed.fragment or ""
    return f"{host}{path}?{query}#{fragment}"


def url_matches_inbound_span(url: str, inbound_spans: Sequence[str]) -> bool:
    """True when *url* is the same destination as a customer-supplied span."""
    needle = _normalize_url_for_compare(url)
    if not needle:
        return False
    for span in inbound_spans or ():
        other = _normalize_url_for_compare(str(span or ""))
        if other and other == needle:
            return True
    return False


__all__ = [
    "InboundUrlSpan",
    "extract_inbound_url_spans",
    "is_url_only_inbound",
    "iter_inbound_url_spans",
    "semantic_text_excluding_url_spans",
    "url_matches_inbound_span",
]
