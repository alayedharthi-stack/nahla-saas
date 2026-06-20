"""Unified product discovery entry routing (Phase 1)."""

from .entry import (
    CATEGORY_BROWSE,
    GLOBAL_BROWSE,
    NO_DISCOVERY,
    PRODUCT_SPECIFIC,
    SHOW_MORE,
    START_ORDER_BARE,
    TOP_PRODUCTS,
    DiscoveryEntryDecision,
    extract_order_product_query,
    resolve_discovery_entry,
    route_discovery_entry,
)

__all__ = [
    "CATEGORY_BROWSE",
    "GLOBAL_BROWSE",
    "NO_DISCOVERY",
    "PRODUCT_SPECIFIC",
    "SHOW_MORE",
    "START_ORDER_BARE",
    "TOP_PRODUCTS",
    "DiscoveryEntryDecision",
    "extract_order_product_query",
    "resolve_discovery_entry",
    "route_discovery_entry",
]
