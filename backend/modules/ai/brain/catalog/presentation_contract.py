"""
catalog/presentation_contract.py
────────────────────────────────
Discovery presentation contract — catalog evidence required.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Sequence

logger = logging.getLogger("nahla.brain.catalog.presentation_contract")

_FORBIDDEN_GENERIC_CLAIMS = (
    re.compile(r"عندنا\s+أ?نواع\s+مميز", re.UNICODE | re.IGNORECASE),
    re.compile(r"لدينا\s+خيارات\s+رائ", re.UNICODE | re.IGNORECASE),
    re.compile(r"منتجات\s+متنوع", re.UNICODE | re.IGNORECASE),
    re.compile(r"we\s+have\s+great\s+options", re.IGNORECASE),
    re.compile(r"amazing\s+products", re.IGNORECASE),
)


def product_has_presentation_evidence(product: Dict[str, Any]) -> bool:
    title = str(product.get("title") or "").strip()
    if len(title) < 2:
        return False
    if product.get("price") is not None or product.get("sale_price") is not None:
        return True
    if str(product.get("image_url") or "").strip():
        return True
    if str(product.get("description") or "").strip():
        return True
    if str(product.get("product_url") or product.get("url") or "").strip():
        return True
    return False


def validate_discovery_products(products: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only products with real catalog evidence for discovery output."""
    valid: List[Dict[str, Any]] = []
    for product in products or []:
        if product_has_presentation_evidence(product):
            valid.append(dict(product))
        else:
            logger.info(
                "[PRESENTATION_CONTRACT] dropped_product missing_evidence title=%r",
                (product or {}).get("title"),
            )
    return valid


def discovery_has_catalog_evidence(
    *,
    products: Sequence[Dict[str, Any]] | None = None,
    collections: Sequence[Dict[str, Any]] | None = None,
) -> bool:
    if collections:
        return any(str(c.get("group_name") or "").strip() for c in collections)
    validated = validate_discovery_products(list(products or []))
    return bool(validated)


def reply_contains_ungrounded_discovery_claim(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    return any(pattern.search(body) for pattern in _FORBIDDEN_GENERIC_CLAIMS)


__all__ = [
    "discovery_has_catalog_evidence",
    "product_has_presentation_evidence",
    "reply_contains_ungrounded_discovery_claim",
    "validate_discovery_products",
]
