"""
Catalog message body policy — no fixed marketing intro before native catalog sends.

Meta requires a non-empty interactive body; callers supply contextual copy or
we fall back to a minimal neutral placeholder — never a canned catalog phrase.
"""
from __future__ import annotations

import re

MAX_CATALOG_BODY_LEN = 1024

# Legacy hardcoded intro removed from production paths — kept for detection only.
FORBIDDEN_CATALOG_INTRO_MARKERS = (
    "تفضّل، اختر من الكتالوج",
    "تفضل، اختر من الكتالوج",
    "تفضّل المنتج",
    "تفضل المنتج",
)

_FIXED_POINTER_RE = re.compile(r"👇")


def is_forbidden_catalog_intro(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    return any(m in body for m in FORBIDDEN_CATALOG_INTRO_MARKERS)


def resolve_catalog_body_text(
    body_text: str = "",
    *,
    context_reply: str = "",
) -> str:
    """Pick catalog interactive body text without injecting fixed marketing copy."""
    for candidate in (body_text, context_reply):
        c = str(candidate or "").strip()
        if not c:
            continue
        if is_forbidden_catalog_intro(c):
            continue
        if len(c) > MAX_CATALOG_BODY_LEN:
            return c[: MAX_CATALOG_BODY_LEN - 1] + "…"
        return c
    # Meta requires non-empty body — neutral placeholder, not a scripted intro.
    return "."


def has_fixed_catalog_pointer(text: str) -> bool:
    return bool(_FIXED_POINTER_RE.search(str(text or "")))


__all__ = [
    "FORBIDDEN_CATALOG_INTRO_MARKERS",
    "has_fixed_catalog_pointer",
    "is_forbidden_catalog_intro",
    "resolve_catalog_body_text",
]
