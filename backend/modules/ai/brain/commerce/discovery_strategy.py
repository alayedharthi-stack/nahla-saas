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


# Phase 2A foundation alias — same shape, architecture-facing name.
DiscoveryPlan = DiscoveryStrategyResult


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


def _resolve_discovery_plan(
    *,
    commerce_objective: str,
    entry_type: str,
    catalog_context: CatalogContextSnapshot,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
) -> DiscoveryPlan:
    settings = merchant_settings or MerchantDiscoverySettings()
    entry = str(entry_type or NO_DISCOVERY).strip().lower()
    objective = str(commerce_objective or COMMERCE_OBJECTIVE_DISCOVERY).strip().lower()
    merchant_mode = str(settings.default_mode or settings.mode_override or "").strip().lower()
    merchant_collections = settings.has_merchant_collections()
    effective_collection_count = max(
        catalog_context.collection_count,
        len(settings.enabled_collections()) if merchant_collections else 0,
    )

    def _merchant_mode() -> Optional[DiscoveryMode]:
        if merchant_mode not in {m.value for m in DiscoveryMode}:
            return None
        return DiscoveryMode(merchant_mode)

    evidence: Dict[str, Any] = {}
    mode: DiscoveryMode

    if entry == GLOBAL_BROWSE:
        if objective == COMMERCE_OBJECTIVE_SELECTION:
            mode = DiscoveryMode.DIRECT_CATALOG
            evidence = {"rule": "selection_direct"}
        else:
            forced = _merchant_mode()
            if forced is not None:
                mode = forced
                evidence = {"rule": "merchant_default_mode", "mode": mode.value}
            elif merchant_collections:
                mode = DiscoveryMode.COLLECTIONS_FIRST
                evidence = {
                    "rule": "global_browse_merchant_collections",
                    "collections": effective_collection_count,
                }
            elif effective_collection_count >= 2:
                mode = DiscoveryMode.COLLECTIONS_FIRST
                evidence = {"rule": "global_browse_collections", "collections": effective_collection_count}
            elif catalog_context.product_count > 0:
                mode = DiscoveryMode.DIRECT_CATALOG
                evidence = {"rule": "global_browse_direct"}
            else:
                mode = DiscoveryMode.GUIDED_DISCOVERY
                evidence = {"rule": "global_browse_no_catalog"}
    elif entry == TOP_PRODUCTS:
        mode = DiscoveryMode.FEATURED_FIRST
        evidence = {"rule": "top_products_featured_first"}
    elif entry == CATEGORY_BROWSE:
        mode = DiscoveryMode.DIRECT_CATALOG
        evidence = {"rule": "category_browse_direct"}
    elif entry == START_ORDER_BARE:
        # Bare order-entry is not catalog-group browsing. COLLECTIONS_FIRST
        # emits the retired collections menu; keep that mode for explicit browse.
        if catalog_context.product_count <= settings.small_catalog_threshold:
            mode = DiscoveryMode.DIRECT_CATALOG
            evidence = {"rule": "start_order_small_catalog"}
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

    featured_ids = settings.global_featured_product_ids()
    result = DiscoveryStrategyResult(
        mode=mode,
        initial_count=max(1, int(settings.initial_product_count or 3)),
        presentation="discovery_list",
        guided_question=guided_question,
        preferred_collections=list(settings.preferred_collection_labels()),
        featured_product_ids=featured_ids,
        evidence={
            **evidence,
            "entry_type": entry,
            "commerce_objective": objective,
            "product_count": catalog_context.product_count,
            "collection_count": effective_collection_count,
            "merchant_default_mode": merchant_mode or "-",
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


def resolve_discovery_strategy_for_ctx(
    ctx: Any,
    settings: Optional[MerchantDiscoverySettings] = None,
) -> DiscoveryPlan:
    """Resolve discovery plan from a brain context (Phase 2A ctx API)."""
    from ..discovery.entry import (
        _load_merchant_discovery_settings,
        resolve_discovery_entry,
    )
    from .commerce_objective import get_commerce_objective

    merchant_settings = settings or _load_merchant_discovery_settings(ctx)
    entry = resolve_discovery_entry(ctx)
    entry_type = entry.entry_type if entry.matched else NO_DISCOVERY
    objective = get_commerce_objective(getattr(ctx, "state", None)) or COMMERCE_OBJECTIVE_DISCOVERY
    facts = getattr(ctx, "facts", None)
    catalog_context = build_catalog_context_snapshot(
        facts=facts,
        collection_count=0,
        has_featured=bool(merchant_settings.global_featured_product_ids()),
    )
    return _resolve_discovery_plan(
        commerce_objective=objective,
        entry_type=entry_type,
        catalog_context=catalog_context,
        merchant_settings=merchant_settings,
    )


def resolve_discovery_strategy(
    ctx: Any = None,
    settings: Optional[MerchantDiscoverySettings] = None,
    *,
    commerce_objective: str = "",
    entry_type: str = "",
    catalog_context: Optional[CatalogContextSnapshot] = None,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
) -> DiscoveryPlan:
    """
    Dual API discovery resolver (Phase 2A).

    - ``resolve_discovery_strategy(ctx, settings=...)`` — context-first.
    - ``resolve_discovery_strategy(commerce_objective=..., entry_type=..., catalog_context=...)`` — legacy kwargs.
    """
    if ctx is not None and hasattr(ctx, "state"):
        return resolve_discovery_strategy_for_ctx(ctx, settings=settings or merchant_settings)
    if catalog_context is None:
        catalog_context = CatalogContextSnapshot()
    return _resolve_discovery_plan(
        commerce_objective=commerce_objective,
        entry_type=entry_type,
        catalog_context=catalog_context,
        merchant_settings=merchant_settings or settings,
    )


def strategy_to_decision_args(
    strategy: DiscoveryStrategyResult,
    *,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
) -> Dict[str, Any]:
    args = {
        "discovery_mode": strategy.mode.value,
        "discovery_initial_count": strategy.initial_count,
        "discovery_presentation": strategy.presentation,
        "discovery_guided_question": strategy.guided_question,
        "discovery_preferred_collections": list(strategy.preferred_collections),
        "discovery_featured_product_ids": list(strategy.featured_product_ids),
        "discovery_strategy_evidence": dict(strategy.evidence),
    }
    if merchant_settings is not None:
        args["discovery_settings"] = merchant_settings.to_dict()
    return args


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
    "DiscoveryPlan",
    "DiscoveryStrategyResult",
    "build_catalog_context_snapshot",
    "resolve_discovery_strategy",
    "resolve_discovery_strategy_for_ctx",
    "strategy_from_decision_args",
    "strategy_to_decision_args",
]
