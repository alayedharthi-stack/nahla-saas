"""
catalog/catalog_provider.py
───────────────────────────
Provider-agnostic catalog access for discovery intelligence.

All synced catalog rows live in the Nahla ``products`` table. Provider
classes are factory aliases over ``LocalCatalogProvider`` — the AI never
branches on catalog source.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.catalog.provider")


class CatalogProvider(ABC):
    @abstractmethod
    def search_products(self, query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_top_products(self, *, limit: int = 12) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def list_collections(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_collection_products(
        self,
        collection_name: str,
        *,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class LocalCatalogProvider(CatalogProvider):
    """Reads the Nahla local products hub only."""

    def __init__(self, db: Any, tenant_id: int) -> None:
        from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

        self._db = db
        self._tenant_id = tenant_id
        self._builder = CatalogContextBuilder(db, tenant_id)

    def search_products(self, query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        q = str(query or "").strip()
        if not q:
            return self.get_top_products(limit=limit)
        return list(self._builder.search_products(q, limit=limit) or [])

    def get_top_products(self, *, limit: int = 12) -> List[Dict[str, Any]]:
        return list(self._builder.get_top_products(limit=limit) or [])

    def list_collections(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        from .catalog_intelligence import CatalogIntelligence  # noqa: PLC0415

        intel = CatalogIntelligence(self)
        return [g.to_dict() for g in intel.list_collections(limit=limit)]

    def get_collection_products(
        self,
        collection_name: str,
        *,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        name = str(collection_name or "").strip()
        if not name:
            return []
        return self.search_products(name, limit=limit)


class MetaCatalogProvider(LocalCatalogProvider):
    """Alias — Meta-synced rows are read from the local hub."""


class SallaCatalogProvider(LocalCatalogProvider):
    """Alias — Salla-synced rows are read from the local hub."""


class ShopifyCatalogProvider(LocalCatalogProvider):
    """Alias — future Shopify sync still reads the local hub."""


class WooCommerceCatalogProvider(LocalCatalogProvider):
    """Alias — future WooCommerce sync still reads the local hub."""


class ManualCatalogProvider(LocalCatalogProvider):
    """Alias — manually entered rows read the local hub."""


def get_catalog_provider(
    db: Any,
    tenant_id: int,
    *,
    integration_platform: str = "",
) -> CatalogProvider:
    """Return the tenant catalog provider. Source is telemetry-only."""
    platform = str(integration_platform or "").strip().lower()
    mapping = {
        "meta": MetaCatalogProvider,
        "salla": SallaCatalogProvider,
        "shopify": ShopifyCatalogProvider,
        "woocommerce": WooCommerceCatalogProvider,
        "woo": WooCommerceCatalogProvider,
        "manual": ManualCatalogProvider,
    }
    cls = mapping.get(platform, LocalCatalogProvider)
    provider = cls(db, tenant_id)
    logger.debug(
        "[CATALOG_PROVIDER] tenant=%s platform=%s provider=%s",
        tenant_id,
        platform or "local",
        cls.__name__,
    )
    return provider


__all__ = [
    "CatalogProvider",
    "LocalCatalogProvider",
    "ManualCatalogProvider",
    "MetaCatalogProvider",
    "SallaCatalogProvider",
    "ShopifyCatalogProvider",
    "WooCommerceCatalogProvider",
    "get_catalog_provider",
]
