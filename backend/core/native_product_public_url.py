"""
core/native_product_public_url.py
─────────────────────────────────
Canonical public HTTPS URLs for Nahla-native catalog products.

Used when merchants have no external store product page (PR 1).
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

from core.catalog import canonical_retailer_id

# Matches Meta retailer_id constraints and Nahla synthetic ids (nahla_p_123).
PUBLIC_RETAILER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

_PUBLIC_PATH_PREFIX = "/public/catalog/items/"


def public_api_base_url() -> str:
    """HTTPS base for unauthenticated native product pages."""
    raw = (
        os.environ.get("NAHLA_PUBLIC_API_BASE_URL")
        or os.environ.get("BACKEND_URL")
        or "https://api.nahlah.ai"
    ).strip().rstrip("/")
    if raw.startswith("http://"):
        raw = "https://" + raw[len("http://") :]
    return raw


def is_valid_public_retailer_id(retailer_id: str) -> bool:
    rid = (retailer_id or "").strip()
    if not rid or "/" in rid or "\\" in rid or ".." in rid:
        return False
    return bool(PUBLIC_RETAILER_ID_RE.match(rid))


def is_valid_https_product_url(url: str) -> bool:
    """True for merchant/external product URLs safe to persist and send to Meta."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    if not parsed.netloc:
        return False
    host = parsed.netloc.lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return False
    return True


def build_native_product_public_url(retailer_id: str) -> Optional[str]:
    """Build the canonical Nahla public product URL for *retailer_id*."""
    rid = (retailer_id or "").strip()
    if not is_valid_public_retailer_id(rid):
        return None
    return f"{public_api_base_url()}{_PUBLIC_PATH_PREFIX}{rid}"


def resolve_product_public_url(
    product: Any,
    *,
    merchant_product_url: Optional[str] = None,
) -> Optional[str]:
    """Resolve product URL with approved fallback priority.

    1. Merchant-provided HTTPS URL (create/patch payload).
    2. Existing stored HTTPS URL on the product row.
    3. Nahla public native product URL from canonical retailer_id.
    """
    explicit = (merchant_product_url or "").strip()
    if explicit:
        if is_valid_https_product_url(explicit):
            return explicit
        # Invalid explicit URL — fall through to stored/native fallback.

    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    stored = (meta.get("product_url") or meta.get("url") or "").strip()
    if stored and is_valid_https_product_url(stored):
        return stored

    rid = canonical_retailer_id(product, fallback_to_synthetic=True)
    return build_native_product_public_url(rid)


def resolve_meta_export_product_url(parent: Any, variant: Any) -> Optional[str]:
    """Defense-in-depth URL for Meta payload building."""
    parent_meta = getattr(parent, "extra_metadata", None) or {}
    if not isinstance(parent_meta, dict):
        parent_meta = {}
    stored = (parent_meta.get("product_url") or parent_meta.get("url") or "").strip()
    if stored and is_valid_https_product_url(stored):
        return stored
    rid = (
        (getattr(variant, "retailer_id", None) or "").strip()
        or canonical_retailer_id(parent, fallback_to_synthetic=True)
    )
    return build_native_product_public_url(rid)


__all__ = [
    "PUBLIC_RETAILER_ID_RE",
    "build_native_product_public_url",
    "is_valid_https_product_url",
    "is_valid_public_retailer_id",
    "public_api_base_url",
    "resolve_meta_export_product_url",
    "resolve_product_public_url",
]
