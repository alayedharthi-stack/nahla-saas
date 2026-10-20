"""
Canonical Shopify shop identity: exactly one ``<label>.myshopify.com``.

Shopify's recommended check is ``^[a-zA-Z0-9][a-zA-Z0-9\\-]*\\.myshopify\\.com$``
anchored at both ends. This module applies it with ``fullmatch`` (``$`` alone
would accept a trailing newline) to ASCII input only, then tightens it:

  * no scheme, path, query, fragment, port, credentials or whitespace — the
    value is a bare host name, never a URL;
  * exactly one label before ``myshopify.com`` (no ``a.b.myshopify.com``);
  * DNS label rules: at most 63 characters, no trailing hyphen;
  * no IDNA ``xn--`` label and no non-ASCII character, so look-alike Unicode
    (full-width dots, homoglyphs) is refused rather than normalised.

The canonical form is lower case. Every outbound Shopify URL is built from a
value this function returned, so no caller-supplied host can be reached
(no SSRF through the shop parameter).
"""
from __future__ import annotations

import re
from typing import Any, Optional

SHOP_SUFFIX = ".myshopify.com"
_MAX_LABEL = 63
_SHOP_RE = re.compile(r"[a-z0-9][a-z0-9\-]*\.myshopify\.com")


def canonical_shop_domain(raw: Any) -> Optional[str]:
    """The canonical lower-case shop domain, or None when *raw* is not one."""
    if not isinstance(raw, str):
        return None
    if not raw or len(raw) > _MAX_LABEL + len(SHOP_SUFFIX) or not raw.isascii():
        return None
    value = raw.lower()
    if not _SHOP_RE.fullmatch(value):
        return None
    label = value[: -len(SHOP_SUFFIX)]
    if len(label) > _MAX_LABEL or label.endswith("-") or label.startswith("xn--"):
        return None
    return value


def is_canonical_shop_domain(raw: Any) -> bool:
    """True only when *raw* already is the canonical form (no case folding)."""
    return isinstance(raw, str) and canonical_shop_domain(raw) == raw
