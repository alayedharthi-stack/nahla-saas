"""
core/store_display.py
─────────────────────
Customer-facing store name formatting. Internal DB values (e.g. Tenant.name
like «متجر … (turky.ayed)») stay unchanged; only strings shown to shoppers /
injected into LLM prompts are sanitized.

Heuristic: strip trailing parenthetical segments when the inner text looks like
a platform username or slug (ASCII, ``user.name``, ``snake_case``, ``kebab``,
or a long lowercase alphanumeric token). Parentheticals with Arabic, spaces,
or mixed-case short labels are kept.
"""
from __future__ import annotations

import re
from typing import Optional

# Trailing `` (slug) `` where inner is ASCII slug-like (no spaces).
_TRAILING_SLUG_PAREN = re.compile(r"\s*\(\s*([a-zA-Z0-9._-]+)\s*\)\s*$")


def _is_technical_slug_fragment(inner: str) -> bool:
    if not inner or not inner.isascii():
        return False
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", inner):
        return False
    if inner.isdigit():
        return False
    if "." in inner or "_" in inner or "-" in inner:
        return True
    # Long generic slug token (e.g. standalone subdomain username)
    if inner.islower() and len(inner) >= 6:
        return True
    return False


def clean_store_name(name: Optional[str]) -> str:
    """
    Return the commercial display name without trailing ``(username/slug)``.

    Safe for empty / non-string input. Does not mutate storage.
    """
    if name is None or not isinstance(name, str):
        return ""
    s = name.strip()
    while s:
        m = _TRAILING_SLUG_PAREN.search(s)
        if not m:
            break
        inner = m.group(1).strip()
        if not _is_technical_slug_fragment(inner):
            break
        s = s[: m.start()].strip()
    return s


# Alias for call sites that prefer merchant-language naming.
display_store_name = clean_store_name
