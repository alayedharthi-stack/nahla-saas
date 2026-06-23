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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.catalog.provider")


@dataclass(frozen=True)
class GroupProductsFetchResult:
    products: List[Dict[str, Any]]
    product_source: str
    group_db_id: Optional[int] = None
    group_slug: str = ""
    group_name: str = ""
    membership_count: int = 0
    orderable_count: int = 0
    products_returned: int = 0
    empty_reason: str = ""


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

    def get_collection_products_by_id(
        self,
        group_id: int,
        *,
        limit: int = 12,
        allow_search_fallback: bool = False,
        group_slug: str = "",
        group_name: str = "",
    ) -> GroupProductsFetchResult:
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
        from .catalog_browse_scope_resolver import load_merchant_catalog_groups  # noqa: PLC0415
        from .catalog_intelligence import CatalogIntelligence  # noqa: PLC0415

        groups = load_merchant_catalog_groups(self._db, self._tenant_id)
        intel = CatalogIntelligence(self)
        return [g.to_dict() for g in intel.list_collections(limit=limit, merchant_catalog_groups=groups)]

    def get_collection_products(
        self,
        collection_name: str,
        *,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        from .catalog_browse_scope_resolver import (  # noqa: PLC0415
            hydrate_group_products,
            load_merchant_catalog_groups,
            match_group_by_collection_name,
            resolve_browse_scope,
        )

        name = str(collection_name or "").strip()
        if not name:
            return []

        groups = load_merchant_catalog_groups(self._db, self._tenant_id)
        group = match_group_by_collection_name(groups, name)
        if group is not None:
            resolution = resolve_browse_scope(
                self._db,
                self._tenant_id,
                name,
                name,
                active_group_slug=str(group.get("slug") or ""),
            )
            if resolution.matched and resolution.product_ids:
                hydrated = hydrate_group_products(
                    self._builder,
                    resolution.product_ids,
                    limit=limit,
                )
                if hydrated:
                    return hydrated

        return self.search_products(name, limit=limit)

    def get_collection_products_by_id(
        self,
        group_id: int,
        *,
        limit: int = 12,
        allow_search_fallback: bool = False,
        group_slug: str = "",
        group_name: str = "",
    ) -> GroupProductsFetchResult:
        from .catalog_browse_scope_resolver import (  # noqa: PLC0415
            group_by_db_id,
            hydrate_group_products,
            load_merchant_catalog_groups,
            read_group_membership_ids,
        )

        try:
            db_id = int(group_id)
        except (TypeError, ValueError):
            result = GroupProductsFetchResult(
                products=[],
                product_source="scoped_empty",
                group_db_id=None,
                group_slug=str(group_slug or ""),
                group_name=str(group_name or ""),
                empty_reason="invalid_group_id",
            )
            self._log_group_products_fetch(result)
            return result

        groups = load_merchant_catalog_groups(self._db, self._tenant_id)
        group = group_by_db_id(groups, db_id)
        slug = str(group_slug or (group or {}).get("slug") or "").strip()
        label = str(group_name or (group or {}).get("label") or slug or "").strip()

        if group is None:
            result = GroupProductsFetchResult(
                products=[],
                product_source="scoped_empty",
                group_db_id=db_id,
                group_slug=slug,
                group_name=label,
                empty_reason="group_not_found",
            )
            self._log_group_products_fetch(result)
            return result

        product_ids = read_group_membership_ids(self._db, self._tenant_id, db_id)
        membership_count = len(product_ids)
        if not product_ids:
            result = GroupProductsFetchResult(
                products=[],
                product_source="scoped_empty",
                group_db_id=db_id,
                group_slug=slug or str(group.get("slug") or ""),
                group_name=label or str(group.get("label") or ""),
                membership_count=0,
                orderable_count=0,
                empty_reason="no_group_items",
            )
            self._log_group_products_fetch(result)
            return result

        hydrated = hydrate_group_products(
            self._builder,
            product_ids,
            limit=limit,
        )
        orderable_count = len(hydrated or [])
        if hydrated:
            result = GroupProductsFetchResult(
                products=list(hydrated),
                product_source="group_items",
                group_db_id=db_id,
                group_slug=slug or str(group.get("slug") or ""),
                group_name=label or str(group.get("label") or ""),
                membership_count=membership_count,
                orderable_count=orderable_count,
                products_returned=orderable_count,
            )
            self._log_group_products_fetch(result)
            return result

        if allow_search_fallback:
            fallback_query = label or slug
            searched = self.search_products(fallback_query, limit=limit) if fallback_query else []
            result = GroupProductsFetchResult(
                products=list(searched or []),
                product_source="blocked_search_fallback" if searched else "scoped_empty",
                group_db_id=db_id,
                group_slug=slug or str(group.get("slug") or ""),
                group_name=label or str(group.get("label") or ""),
                membership_count=membership_count,
                orderable_count=0,
                products_returned=len(searched or []),
                empty_reason="no_orderable_members" if not searched else "search_fallback_used",
            )
            self._log_group_products_fetch(result)
            return result

        result = GroupProductsFetchResult(
            products=[],
            product_source="scoped_empty",
            group_db_id=db_id,
            group_slug=slug or str(group.get("slug") or ""),
            group_name=label or str(group.get("label") or ""),
            membership_count=membership_count,
            orderable_count=0,
            empty_reason="no_orderable_members",
        )
        self._log_group_products_fetch(result)
        return result

    def _log_group_products_fetch(self, result: GroupProductsFetchResult) -> None:
        logger.info(
            "[CATALOG_PROVIDER] group_products tenant=%s product_source=%s "
            "group_db_id=%s group_slug=%r group_name=%r membership_count=%s "
            "orderable_count=%s products_returned=%s empty_reason=%r",
            self._tenant_id,
            result.product_source,
            result.group_db_id,
            result.group_slug,
            result.group_name,
            result.membership_count,
            result.orderable_count,
            result.products_returned,
            result.empty_reason,
        )


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
    "GroupProductsFetchResult",
    "LocalCatalogProvider",
    "ManualCatalogProvider",
    "MetaCatalogProvider",
    "SallaCatalogProvider",
    "ShopifyCatalogProvider",
    "WooCommerceCatalogProvider",
    "get_catalog_provider",
]
