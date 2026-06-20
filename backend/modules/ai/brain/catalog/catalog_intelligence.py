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
from typing import Any, Dict, List, Optional, Sequence

from ..commerce.discovery_strategy import DiscoveryMode, DiscoveryStrategyResult
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
    featured_ids = {str(x).strip() for x in (featured_product_ids or []) if str(x).strip()}
    if ext and ext in featured_ids:
        merchant_priority = 1.0
    elif str(product.get("id") or "").strip() in featured_ids:
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

    def list_collections(self, *, limit: int = 20) -> List[CatalogGroup]:
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
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        groups: List[CatalogGroup] = []
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
    ) -> List[Dict[str, Any]]:
        featured_ids = list(strategy.featured_product_ids or [])
        scored: List[Dict[str, Any]] = []
        for product in list(products or []):
            row = dict(product)
            score = compute_discovery_score(row, featured_product_ids=featured_ids)
            row["discovery_score"] = score
            scored.append(row)
        scored.sort(
            key=lambda p: (
                -float(p.get("discovery_score") or 0.0),
                str(p.get("title") or ""),
            ),
        )
        if strategy.mode == DiscoveryMode.FEATURED_FIRST and featured_ids:
            featured: List[Dict[str, Any]] = []
            rest: List[Dict[str, Any]] = []
            id_set = set(featured_ids)
            for row in scored:
                ext = str(row.get("external_id") or row.get("id") or "").strip()
                if ext in id_set:
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
    ) -> DiscoveryPlan:
        mode = strategy.mode
        query_s = str(query or "").strip()
        src = str(source or "").strip().lower()

        if mode == DiscoveryMode.GUIDED_DISCOVERY:
            return DiscoveryPlan(
                output_kind="guided",
                guided_question=strategy.guided_question or "وش نوع المنتج اللي تدور عليه؟",
                presentation=strategy.presentation,
                evidence={"mode": mode.value, "source": src},
            )

        if mode == DiscoveryMode.COLLECTIONS_FIRST and not query_s:
            collections = self.list_collections(limit=10)
            pref = [str(x).strip() for x in (preferred_collections or strategy.preferred_collections or []) if x]
            if pref:
                pref_norm = {_norm_collection_name(x): x for x in pref}
                ordered: List[CatalogGroup] = []
                seen = set()
                for name in pref:
                    key = _norm_collection_name(name)
                    for group in collections:
                        if _norm_collection_name(group.group_name) == key:
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
                evidence={"mode": mode.value, "source": src, "collection_count": len(collections)},
            )

        if query_s:
            raw = self._provider.search_products(query_s, limit=max(12, strategy.initial_count * 4))
        else:
            raw = self._provider.get_top_products(limit=max(12, strategy.initial_count * 4))

        ranked = self.rank_products(raw, strategy=strategy)
        return DiscoveryPlan(
            output_kind="products",
            products=ranked[: max(1, strategy.initial_count)],
            presentation=strategy.presentation,
            evidence={
                "mode": mode.value,
                "source": src,
                "query": query_s,
                "ranked_count": len(ranked),
            },
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
        row["discovery_signals"] = {
            "featured_rank": float(meta.get("featured_rank", meta.get("merchant_priority", 0)) or 0),
            "sales_score": float(meta.get("sales_score", meta.get("stats_converted", 0)) or 0),
            "freshness": freshness,
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
