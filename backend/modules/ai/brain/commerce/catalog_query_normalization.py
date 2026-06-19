"""
catalog_query_normalization.py
──────────────────────────────
Cross-lingual product query extraction and catalog search expansion.

Platform-wide — reuses honey type rules for English↔Arabic matching
(e.g. ``Sidr honey`` → ``سدر`` / ``عسل سدر``).
"""
from __future__ import annotations

import re
from typing import List

_EN_ORDER_PRODUCT_RE = re.compile(
    r"(?is)"
    r"(?:"
    r"(?:i\s+(?:want|would\s+like|'d\s+like)\s+(?:to\s+)?"
    r"(?:order|buy|get|purchase))\s+"
    r"|(?:can|could)\s+i\s+(?:order|buy|get)\s+"
    r"|(?:order|buy|get|purchase)\s+"
    r"|(?:looking\s+for|do\s+you\s+have|show\s+me)\s+"
    r")"
    r"(.{2,60})"
)

_TRAILING_NOISE_RE = re.compile(
    r"\s*(?:please|thanks|thank\s+you|plz|\.|,|!|\?)\s*$",
    re.IGNORECASE,
)

_LATIN_HINT_RE = re.compile(r"[a-zA-Z]")


def extract_english_order_product_query(message: str) -> str:
    """Extract product phrase from English commerce messages."""
    raw = (message or "").strip()
    if not raw or not _LATIN_HINT_RE.search(raw):
        return ""
    match = _EN_ORDER_PRODUCT_RE.search(raw)
    if not match:
        return ""
    product = _TRAILING_NOISE_RE.sub("", (match.group(1) or "").strip()).strip()
    return product if len(product) >= 2 else ""


def expand_honey_type_search_queries(query: str) -> List[str]:
    """Add Arabic catalog variants when an English/Arabic honey type is named."""
    from .honey_browse_strategy import customer_specified_honey_type  # noqa: PLC0415

    q = (query or "").strip()
    if not q:
        return []
    label = customer_specified_honey_type("", q)
    if not label:
        return []
    variants = [label, f"عسل {label}", f"عسل ال{label}", f"ال{label}"]
    seen: set[str] = set()
    out: List[str] = []
    for v in variants:
        key = v.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def expand_catalog_search_queries(query: str) -> List[str]:
    """Deterministic retry queries for catalog search (platform-wide)."""
    return expand_honey_type_search_queries(query)


__all__ = [
    "expand_catalog_search_queries",
    "expand_honey_type_search_queries",
    "extract_english_order_product_query",
]
