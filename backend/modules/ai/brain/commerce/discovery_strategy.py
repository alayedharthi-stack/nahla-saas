"""
commerce/discovery_strategy.py
──────────────────────────────
Platform-wide discovery strategy resolver (Phase 2).

Maps commerce objective + discovery entry + catalog context + merchant
settings to a deterministic ``DiscoveryMode`` plan.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..discovery.entry import (
    CATEGORY_BROWSE,
    GLOBAL_BROWSE,
    NO_DISCOVERY,
    PRODUCT_SPECIFIC,
    SHOW_MORE,
    START_ORDER_BARE,
    TOP_PRODUCTS,
)
from .commerce_objective import (
    COMMERCE_OBJECTIVE_DISCOVERY,
    COMMERCE_OBJECTIVE_SELECTION,
)
from .merchant_discovery_settings import MerchantDiscoverySettings

logger = logging.getLogger("nahla.brain.discovery_strategy")


class DiscoveryMode(str, Enum):
    FEATURED_FIRST = "featured_first"
    COLLECTIONS_FIRST = "collections_first"
    DIRECT_CATALOG = "direct_catalog"
    GUIDED_DISCOVERY = "guided_discovery"


@dataclass(frozen=True)
class CatalogContextSnapshot:
    product_count: int = 0
    orderable_count: int = 0
    collection_count: int = 0
    has_featured: bool = False


@dataclass(frozen=True)
class DiscoveryStrategyResult:
    mode: DiscoveryMode
    initial_count: int = 3
    presentation: str = "discovery_list"
    guided_question: str = ""
    preferred_collections: List[str] = field(default_factory=list)
    featured_product_ids: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)


def build_catalog_context_snapshot(
    *,
    facts: Any,
    collection_count: int = 0,
    has_featured: bool = False,
) -> CatalogContextSnapshot:
    product_count = int(getattr(facts, "product_count", 0) or 0)
    orderable_count = int(getattr(facts, "in_stock_count", 0) or product_count or 0)
    if not product_count and getattr(facts, "has_products", False):
        tops = list(getattr(facts, "top_products", None) or [])
        product_count = max(product_count, len(tops))
        orderable_count = max(orderable_count, len(tops))
    return CatalogContextSnapshot(
        product_count=product_count,
        orderable_count=orderable_count,
        collection_count=max(0, int(collection_count or 0)),
        has_featured=bool(has_featured),
    )


def resolve_discovery_strategy(
    *,
    commerce_objective: str,
    entry_type: str,
    catalog_context: CatalogContextSnapshot,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
) -> DiscoveryStrategyResult:
    settings = merchant_settings or MerchantDiscoverySettings()
    entry = str(entry_type or NO_DISCOVERY).strip().lower()
    objective = str(commerce_objective or COMMERCE_OBJECTIVE_DISCOVERY).strip().lower()

    if settings.mode_override:
        mode = DiscoveryMode(settings.mode_override)
        evidence = {"rule": "merchant_override", "mode": mode.value}
    elif entry == GLOBAL_BROWSE:
        if catalog_context.collection_count >= 2:
            mode = DiscoveryMode.COLLECTIONS_FIRST
            evidence = {"rule": "global_browse_collections", "collections": catalog_context.collection_count}
        elif catalog_context.product_count > 20:
            mode = DiscoveryMode.GUIDED_DISCOVERY
            evidence = {"rule": "global_browse_large_catalog"}
        else:
            mode = DiscoveryMode.DIRECT_CATALOG
            evidence = {"rule": "global_browse_direct"}
    elif entry == TOP_PRODUCTS:
        mode = DiscoveryMode.FEATURED_FIRST
        evidence = {"rule": "top_products_featured_first"}
    elif entry == CATEGORY_BROWSE:
        mode = DiscoveryMode.DIRECT_CATALOG
        evidence = {"rule": "category_browse_direct"}
    elif entry == START_ORDER_BARE:
        if catalog_context.product_count <= settings.small_catalog_threshold:
            mode = DiscoveryMode.DIRECT_CATALOG
            evidence = {"rule": "start_order_small_catalog"}
        elif catalog_context.collection_count >= 2:
            mode = DiscoveryMode.COLLECTIONS_FIRST
            evidence = {"rule": "start_order_collections_first"}
        else:
            mode = DiscoveryMode.FEATURED_FIRST
            evidence = {"rule": "start_order_featured_first"}
    elif entry == PRODUCT_SPECIFIC:
        mode = DiscoveryMode.DIRECT_CATALOG
        evidence = {"rule": "product_specific_direct"}
    elif entry == SHOW_MORE:
        mode = DiscoveryMode.DIRECT_CATALOG
        evidence = {"rule": "show_more_continue"}
    elif objective == COMMERCE_OBJECTIVE_SELECTION:
        mode = DiscoveryMode.DIRECT_CATALOG
        evidence = {"rule": "selection_direct"}
    else:
        mode = DiscoveryMode.GUIDED_DISCOVERY
        evidence = {"rule": "ambiguous_guided"}

    guided_question = ""
    if mode == DiscoveryMode.GUIDED_DISCOVERY:
        guided_question = str(settings.guided_question or "").strip()

    result = DiscoveryStrategyResult(
        mode=mode,
        initial_count=max(1, int(settings.initial_product_count or 3)),
        presentation="discovery_list",
        guided_question=guided_question,
        preferred_collections=list(settings.preferred_collections or []),
        featured_product_ids=list(settings.featured_product_ids or []),
        evidence={
            **evidence,
            "entry_type": entry,
            "commerce_objective": objective,
            "product_count": catalog_context.product_count,
            "collection_count": catalog_context.collection_count,
        },
    )
    logger.info(
        "[DISCOVERY_STRATEGY] mode=%s entry=%s objective=%s products=%d collections=%d rule=%s",
        result.mode.value,
        entry,
        objective,
        catalog_context.product_count,
        catalog_context.collection_count,
        evidence.get("rule", "-"),
    )
    return result


def strategy_to_decision_args(strategy: DiscoveryStrategyResult) -> Dict[str, Any]:
    return {
        "discovery_mode": strategy.mode.value,
        "discovery_initial_count": strategy.initial_count,
        "discovery_presentation": strategy.presentation,
        "discovery_guided_question": strategy.guided_question,
        "discovery_preferred_collections": list(strategy.preferred_collections),
        "discovery_featured_product_ids": list(strategy.featured_product_ids),
        "discovery_strategy_evidence": dict(strategy.evidence),
    }


def strategy_from_decision_args(args: Dict[str, Any]) -> DiscoveryStrategyResult:
    mode_raw = str(args.get("discovery_mode") or DiscoveryMode.DIRECT_CATALOG.value).strip().lower()
    try:
        mode = DiscoveryMode(mode_raw)
    except ValueError:
        mode = DiscoveryMode.DIRECT_CATALOG
    return DiscoveryStrategyResult(
        mode=mode,
        initial_count=max(1, int(args.get("discovery_initial_count") or 3)),
        presentation=str(args.get("discovery_presentation") or "discovery_list"),
        guided_question=str(args.get("discovery_guided_question") or "").strip(),
        preferred_collections=list(args.get("discovery_preferred_collections") or []),
        featured_product_ids=list(args.get("discovery_featured_product_ids") or []),
        evidence=dict(args.get("discovery_strategy_evidence") or {}),
    )


__all__ = [
    "CatalogContextSnapshot",
    "DiscoveryMode",
    "DiscoveryStrategyResult",
    "build_catalog_context_snapshot",
    "resolve_discovery_strategy",
    "strategy_from_decision_args",
    "strategy_to_decision_args",
]
