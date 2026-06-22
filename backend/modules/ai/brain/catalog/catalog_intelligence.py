"""
catalog/catalog_intelligence.py
───────────────────────────────
Ranking, grouping, and discovery planning over ``CatalogProvider`` output.

The LLM/brain must consume this layer — never invent product ordering.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..commerce.discovery_strategy import DiscoveryMode, DiscoveryStrategyResult
from ..commerce.merchant_discovery_settings import (
    DiscoveryCollectionConfig,
    FeaturedProductConfig,
    MerchantDiscoverySettings,
)
from .catalog_provider import CatalogProvider

logger = logging.getLogger("nahla.brain.catalog.intelligence")

DEFAULT_WEIGHTS = {
    "featured_rank": 0.35,
    "sales_score": 0.25,
    "availability": 0.20,
    "freshness": 0.10,
    "merchant_priority": 0.10,
}


@dataclass(frozen=True)
class CatalogGroup:
    group_id: str
    group_name: str
    browse_rank: int
    product_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "browse_rank": self.browse_rank,
            "product_count": self.product_count,
        }


@dataclass(frozen=True)
class DiscoveryPlan:
    output_kind: str  # products | collections | guided
    products: List[Dict[str, Any]] = field(default_factory=list)
    collections: List[CatalogGroup] = field(default_factory=list)
    guided_question: str = ""
    presentation: str = "discovery_list"
    evidence: Dict[str, Any] = field(default_factory=dict)


def _product_key(product: Dict[str, Any]) -> str:
    return str(
        product.get("external_id")
        or product.get("id")
        or product.get("title")
        or ""
    ).strip()


def _norm_collection_name(name: str) -> str:
    return " ".join(str(name or "").strip().split()).lower()


def compute_discovery_score(
    product: Dict[str, Any],
    *,
    featured_product_ids: Optional[Sequence[str]] = None,
    merchant_priority_map: Optional[Dict[str, float]] = None,
    weights: Optional[Dict[str, float]] = None,
) -> float:
    """Generic discovery score — no category/provider-specific logic."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    meta = product.get("discovery_signals") or {}
    if not isinstance(meta, dict):
        meta = {}

    featured_rank = float(meta.get("featured_rank", product.get("featured_rank", 0)) or 0)
    sales_score = float(meta.get("sales_score", product.get("sales_score", 0)) or 0)
    freshness = float(meta.get("freshness", product.get("freshness", 0)) or 0)

    availability = 0.0
    if product.get("in_stock", True):
        availability = 1.0
        qty = product.get("stock_qty")
        if qty is not None:
            try:
                availability += min(float(qty) / 100.0, 0.5)
            except (TypeError, ValueError):
                pass

    merchant_priority = 0.0
    ext = str(product.get("external_id") or product.get("id") or "").strip()
    pid = str(product.get("id") or "").strip()
    priority_map = dict(merchant_priority_map or {})
    featured_ids = {str(x).strip() for x in (featured_product_ids or []) if str(x).strip()}
    for key in (ext, pid):
        if key and key in priority_map:
            merchant_priority = max(merchant_priority, float(priority_map[key]))
    if merchant_priority <= 0.0:
        if meta.get("is_best_seller"):
            merchant_priority = max(
                merchant_priority,
                float(meta.get("merchant_priority", 1.0) or 1.0),
            )
        elif ext and ext in featured_ids:
            merchant_priority = 1.0
        elif pid in featured_ids:
            merchant_priority = 1.0

    score = (
        featured_rank * w["featured_rank"]
        + sales_score * w["sales_score"]
        + availability * w["availability"]
        + freshness * w["freshness"]
        + merchant_priority * w["merchant_priority"]
    )
    return round(score, 6)


class CatalogIntelligence:
    def __init__(self, provider: CatalogProvider) -> None:
        self._provider = provider

    def list_collections(
        self,
        *,
        limit: int = 20,
        merchant_settings: Optional[MerchantDiscoverySettings] = None,
        merchant_catalog_groups: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> List[CatalogGroup]:
        db_groups = [
            g for g in (merchant_catalog_groups or [])
            if isinstance(g, Mapping) and g.get("is_active", True)
        ]
        if db_groups:
            ranked: List[CatalogGroup] = []
            for group in sorted(
                db_groups,
                key=lambda g: (int(g.get("priority") or 100), str(g.get("label") or "")),
            ):
                ranked.append(
                    CatalogGroup(
                        group_id=str(group.get("slug") or group.get("id") or ""),
                        group_name=str(group.get("label") or group.get("slug") or ""),
                        browse_rank=int(group.get("priority") or 100),
                        product_count=int(group.get("product_count") or 0),
                    )
                )
            return ranked[: max(1, limit)]

        products = self._provider.get_top_products(limit=max(limit * 8, 40))
        counts: Dict[str, int] = {}
        labels: Dict[str, str] = {}
        for product in products:
            raw = str(product.get("category") or "").strip()
            if not raw:
                continue
            key = _norm_collection_name(raw)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            labels[key] = raw

        groups: List[CatalogGroup] = []
        if merchant_settings and merchant_settings.has_merchant_collections():
            seen_keys: set[str] = set()
            for cfg in merchant_settings.enabled_collections():
                match_key = _norm_collection_name(cfg.catalog_match or cfg.label)
                count = 0
                display_name = cfg.label
                for key, cat_count in counts.items():
                    if key == match_key or match_key in key or key in match_key:
                        count += cat_count
                        if not cfg.catalog_match:
                            display_name = labels.get(key, cfg.label)
                groups.append(
                    CatalogGroup(
                        group_id=cfg.id,
                        group_name=display_name,
                        browse_rank=cfg.priority,
                        product_count=count,
                    )
                )
                seen_keys.add(match_key)
            for idx, (key, count) in enumerate(
                sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])),
                start=len(groups) + 1,
            ):
                if key in seen_keys:
                    continue
                groups.append(
                    CatalogGroup(
                        group_id=key,
                        group_name=labels.get(key, key),
                        browse_rank=idx,
                        product_count=count,
                    )
                )
            groups.sort(key=lambda g: (g.browse_rank, g.group_name))
            return groups[: max(1, limit)]

        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        for idx, (key, count) in enumerate(ranked[: max(1, limit)], start=1):
            groups.append(
                CatalogGroup(
                    group_id=key,
                    group_name=labels.get(key, key),
                    browse_rank=idx,
                    product_count=count,
                )
            )
        return groups

    def rank_products(
        self,
        products: List[Dict[str, Any]],
        *,
        strategy: DiscoveryStrategyResult,
        merchant_settings: Optional[MerchantDiscoverySettings] = None,
        collection: Optional[DiscoveryCollectionConfig] = None,
    ) -> List[Dict[str, Any]]:
        settings = merchant_settings or MerchantDiscoverySettings()
        featured_ids = list(strategy.featured_product_ids or settings.global_featured_product_ids())
        priority_map = settings.merchant_priority_map()
        if collection:
            for fp in settings.featured_for_collection(collection):
                key = str(fp.product_id).strip()
                if not key:
                    continue
                norm_priority = max(0.1, 1.0 - (max(0, int(fp.priority) - 1) * 0.05))
                priority_map[key] = max(priority_map.get(key, 0.0), norm_priority)

        scored: List[Dict[str, Any]] = []
        for product in list(products or []):
            row = dict(product)
            score = compute_discovery_score(
                row,
                featured_product_ids=featured_ids,
                merchant_priority_map=priority_map,
            )
            row["discovery_score"] = score
            scored.append(row)
        scored.sort(
            key=lambda p: (
                -float(p.get("discovery_score") or 0.0),
                str(p.get("title") or ""),
            ),
        )

        if collection and collection.featured_products:
            featured_rows: List[Dict[str, Any]] = []
            rest: List[Dict[str, Any]] = []
            order = [str(fp.product_id) for fp in settings.featured_for_collection(collection)]
            order_index = {pid: idx for idx, pid in enumerate(order)}
            for row in scored:
                pid = str(row.get("id") or row.get("external_id") or "").strip()
                if pid in order_index:
                    featured_rows.append(row)
                else:
                    rest.append(row)
            featured_rows.sort(key=lambda r: order_index.get(str(r.get("id") or r.get("external_id") or ""), 999))
            scored = featured_rows + rest
        elif strategy.mode == DiscoveryMode.FEATURED_FIRST and featured_ids:
            featured: List[Dict[str, Any]] = []
            rest = []
            id_set = set(featured_ids)
            for row in scored:
                ext = str(row.get("external_id") or row.get("id") or "").strip()
                if ext in id_set or str(row.get("id") or "") in id_set:
                    featured.append(row)
                else:
                    rest.append(row)
            scored = featured + rest
        return scored[: max(1, strategy.initial_count * 4)]

    def build_discovery_plan(
        self,
        *,
        strategy: DiscoveryStrategyResult,
        query: str = "",
        source: str = "",
        preferred_collections: Optional[Sequence[str]] = None,
        merchant_settings: Optional[MerchantDiscoverySettings] = None,
        merchant_catalog_groups: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> DiscoveryPlan:
        mode = strategy.mode
        query_s = str(query or "").strip()
        src = str(source or "").strip().lower()
        settings = merchant_settings or MerchantDiscoverySettings()

        if mode == DiscoveryMode.GUIDED_DISCOVERY:
            return DiscoveryPlan(
                output_kind="guided",
                guided_question=strategy.guided_question or settings.guided_question or "وش نوع المنتج اللي تدور عليه؟",
                presentation=strategy.presentation,
                evidence={"mode": mode.value, "source": src},
            )

        matched_collection = settings.match_collection(query_s) if query_s else None

        if mode == DiscoveryMode.COLLECTIONS_FIRST and not query_s:
            collections = self.list_collections(
                limit=10,
                merchant_settings=settings,
                merchant_catalog_groups=merchant_catalog_groups,
            )
            pref = [str(x).strip() for x in (preferred_collections or settings.preferred_collection_labels()) if x]
            if pref:
                pref_norm = {_norm_collection_name(x): x for x in pref}
                ordered: List[CatalogGroup] = []
                seen = set()
                for name in pref:
                    key = _norm_collection_name(name)
                    for group in collections:
                        if (
                            _norm_collection_name(group.group_name) == key
                            or group.group_id == key
                            or _norm_collection_name(group.group_id) == key
                        ):
                            ordered.append(group)
                            seen.add(group.group_id)
                            break
                for group in collections:
                    if group.group_id not in seen:
                        ordered.append(group)
                collections = ordered
            return DiscoveryPlan(
                output_kind="collections",
                collections=collections[: max(1, strategy.initial_count + 2)],
                presentation=strategy.presentation,
                evidence={
                    "mode": mode.value,
                    "source": src,
                    "collection_count": len(collections),
                    "merchant_collections": bool(settings.has_merchant_collections()),
                },
            )

        search_query = query_s
        if matched_collection and not search_query:
            search_query = matched_collection.catalog_match or matched_collection.label

        group_match = None
        if merchant_catalog_groups and query_s:
            from .catalog_browse_scope_resolver import match_catalog_group  # noqa: PLC0415

            group_match = match_catalog_group(
                merchant_catalog_groups,
                message=query_s,
                query=query_s,
            )

        if group_match and group_match.group_slug:
            raw = self._provider.get_collection_products(
                group_match.group_label or group_match.group_slug,
                limit=max(12, strategy.initial_count * 4),
            )
        elif search_query:
            raw = self._provider.search_products(search_query, limit=max(12, strategy.initial_count * 4))
        else:
            raw = self._provider.get_top_products(limit=max(12, strategy.initial_count * 4))

        ranked = self.rank_products(
            raw,
            strategy=strategy,
            merchant_settings=settings,
            collection=matched_collection,
        )
        evidence = {
            "mode": mode.value,
            "source": src,
            "query": query_s,
            "ranked_count": len(ranked),
            "collection_id": matched_collection.id if matched_collection else "",
        }
        if group_match and group_match.group_slug:
            evidence["catalog_group_slug"] = group_match.group_slug
            evidence["catalog_group_id"] = group_match.group_id
        return DiscoveryPlan(
            output_kind="products",
            products=ranked[: max(1, strategy.initial_count)],
            presentation=strategy.presentation,
            evidence=evidence,
        )


def attach_discovery_signals_from_db(
    products: List[Dict[str, Any]],
    *,
    db: Any,
    tenant_id: int,
) -> List[Dict[str, Any]]:
    """Best-effort enrichment from Product.metadata for ranking signals."""
    if not products or db is None:
        return products
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return products

    ids = [p.get("id") for p in products if p.get("id") is not None]
    if not ids:
        return products

    rows = (
        db.query(Product)
        .filter(Product.tenant_id == tenant_id, Product.id.in_(ids))
        .all()
    )
    by_id = {row.id: row for row in rows}
    rankings_by_id: Dict[int, Any] = {}
    try:
        from database.models import ProductRanking  # noqa: PLC0415

        ranking_rows = (
            db.query(ProductRanking)
            .filter(ProductRanking.tenant_id == tenant_id, ProductRanking.product_id.in_(ids))
            .all()
        )
        rankings_by_id = {int(r.product_id): r for r in ranking_rows}
    except Exception:  # noqa: BLE001
        rankings_by_id = {}
    now = time.time()
    enriched: List[Dict[str, Any]] = []
    for product in products:
        row = dict(product)
        db_row = by_id.get(row.get("id"))
        meta = getattr(db_row, "extra_metadata", None) if db_row is not None else {}
        if not isinstance(meta, dict):
            meta = {}
        updated_at = getattr(db_row, "updated_at", None) if db_row is not None else None
        freshness = 0.0
        if updated_at is not None:
            try:
                freshness = max(0.0, 1.0 - min((now - updated_at.timestamp()) / (86400 * 30), 1.0))
            except Exception:  # noqa: BLE001
                freshness = 0.0
        ranking = rankings_by_id.get(int(row.get("id") or 0))
        is_best_seller = bool(getattr(ranking, "is_best_seller", False)) if ranking else False
        merchant_priority = float(getattr(ranking, "merchant_priority", 0) or 0) if ranking else 0.0
        row["discovery_signals"] = {
            "featured_rank": float(meta.get("featured_rank", meta.get("merchant_priority", 0)) or 0),
            "sales_score": float(
                meta.get("sales_score", meta.get("stats_converted", 0))
                or (getattr(ranking, "sales_score", 0) if ranking else 0)
                or 0,
            ),
            "freshness": freshness,
            "is_best_seller": is_best_seller,
            "merchant_priority": merchant_priority,
        }
        enriched.append(row)
    return enriched


__all__ = [
    "CatalogGroup",
    "CatalogIntelligence",
    "DiscoveryPlan",
    "compute_discovery_score",
    "attach_discovery_signals_from_db",
]
