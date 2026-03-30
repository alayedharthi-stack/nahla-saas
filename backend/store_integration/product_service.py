"""
ProductService
──────────────
Fetch real product data through the store adapter.
Falls back to Nahla DB products when no adapter is configured.
"""
from __future__ import annotations
import logging, os, sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from store_integration.registry import get_adapter
from store_integration.models import NormalizedProduct

logger = logging.getLogger("nahla.store_integration.product")


async def fetch_products(tenant_id: int) -> List[NormalizedProduct]:
    """
    Fetch products from the real store. Returns [] on failure.
    Caller must handle empty list gracefully.
    """
    adapter = get_adapter(tenant_id)
    if not adapter:
        logger.debug(f"No store adapter for tenant {tenant_id} — caller should use DB fallback")
        return []
    try:
        products = await adapter.get_products()
        logger.info(f"[ProductService] tenant={tenant_id} fetched {len(products)} products from {adapter.platform}")
        return products
    except Exception as exc:
        logger.error(f"[ProductService] tenant={tenant_id} fetch failed: {exc}")
        return []


async def fetch_product(tenant_id: int, product_id: str) -> Optional[NormalizedProduct]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return None
    try:
        return await adapter.get_product(product_id)
    except Exception as exc:
        logger.error(f"[ProductService] tenant={tenant_id} get_product failed: {exc}")
        return None


def normalize_db_product(p) -> Dict[str, Any]:
    """Convert a DB Product row to the same shape as NormalizedProduct for consistency."""
    price = None
    try:
        raw = str(p.price or "").replace("ر.س", "").replace(",", "").strip()
        if raw:
            price = float(raw)
    except ValueError:
        pass
    return {
        "id": str(p.id),
        "title": p.title,
        "price": price,
        "currency": "SAR",
        "sku": p.sku or "",
        "in_stock": True,
        "description": (p.description or "")[:200],
        "tags": p.recommendation_tags or [],
        "variants": [],
    }
