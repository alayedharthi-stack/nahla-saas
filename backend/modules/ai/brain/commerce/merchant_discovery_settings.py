"""
commerce/merchant_discovery_settings.py
────────────────────────────────────────
Tenant discovery settings — platform defaults with optional merchant overrides.

Persisted under ``TenantSettings.ai_settings["discovery_settings"]`` (Phase 4A).
Read from ``tenant_context`` / ``merchant_context`` dict at brain runtime.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class FeaturedProductConfig:
    product_id: str
    variant_id: str = ""
    priority: int = 0
    label_override: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_id": self.product_id,
            "variant_id": self.variant_id,
            "priority": self.priority,
            "label_override": self.label_override,
        }


@dataclass(frozen=True)
class DiscoveryCollectionConfig:
    id: str
    label: str
    priority: int = 0
    enabled: bool = True
    catalog_match: str = ""
    featured_products: List[FeaturedProductConfig] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "priority": self.priority,
            "enabled": self.enabled,
            "catalog_match": self.catalog_match,
            "featured_products": [fp.to_dict() for fp in self.featured_products],
        }


@dataclass(frozen=True)
class MerchantDiscoverySettings:
    default_mode: str = ""
    initial_product_count: int = 3
    featured_product_ids: List[str] = field(default_factory=list)
    preferred_collections: List[str] = field(default_factory=list)
    collections: List[DiscoveryCollectionConfig] = field(default_factory=list)
    guided_question: str = "وش نوع المنتج اللي تدور عليه؟"
    small_catalog_threshold: int = 5

    @property
    def mode_override(self) -> str:
        """Backward-compatible alias used by Phase 2 tests."""
        return self.default_mode

    def has_merchant_collections(self) -> bool:
        return any(c.enabled for c in self.collections)

    def enabled_collections(self) -> List[DiscoveryCollectionConfig]:
        return sorted(
            [c for c in self.collections if c.enabled],
            key=lambda c: (c.priority, c.label),
        )

    def preferred_collection_labels(self) -> List[str]:
        labels = [c.label for c in self.enabled_collections() if c.label]
        if labels:
            return labels
        return list(self.preferred_collections or [])

    def global_featured_product_ids(self) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        for pid in self.featured_product_ids:
            key = str(pid).strip()
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
        for collection in self.enabled_collections():
            for fp in sorted(collection.featured_products, key=lambda x: x.priority):
                key = str(fp.product_id).strip()
                if key and key not in seen:
                    seen.add(key)
                    ordered.append(key)
        return ordered

    def merchant_priority_map(self) -> Dict[str, float]:
        """Normalized merchant priority scores keyed by product id."""
        priorities: Dict[str, int] = {}
        for idx, pid in enumerate(self.featured_product_ids):
            key = str(pid).strip()
            if key:
                priorities[key] = max(priorities.get(key, 0), 1000 - idx)
        for collection in self.enabled_collections():
            for fp in collection.featured_products:
                key = str(fp.product_id).strip()
                if not key:
                    continue
                priorities[key] = max(priorities.get(key, 0), int(fp.priority or 0))
        if not priorities:
            return {}
        max_p = max(priorities.values()) or 1
        return {pid: round(val / max_p, 6) for pid, val in priorities.items()}

    def match_collection(
        self,
        query: str,
        *,
        labels: Optional[Iterable[str]] = None,
    ) -> Optional[DiscoveryCollectionConfig]:
        q_norm = _norm_token(query)
        if not q_norm:
            return None
        label_norms = {_norm_token(l) for l in (labels or []) if l}
        for collection in self.enabled_collections():
            for candidate in (
                collection.id,
                collection.label,
                collection.catalog_match,
            ):
                cand_norm = _norm_token(candidate)
                if not cand_norm:
                    continue
                if cand_norm == q_norm or cand_norm in q_norm or q_norm in cand_norm:
                    return collection
            for label in label_norms:
                if label and (_norm_token(collection.label) == label or _norm_token(collection.catalog_match) == label):
                    return collection
        return None

    def featured_for_collection(
        self,
        collection: DiscoveryCollectionConfig,
    ) -> List[FeaturedProductConfig]:
        return sorted(collection.featured_products, key=lambda fp: (fp.priority, fp.product_id))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_mode": self.default_mode,
            "initial_product_count": self.initial_product_count,
            "featured_product_ids": list(self.featured_product_ids),
            "preferred_collections": list(self.preferred_collections),
            "collections": [c.to_dict() for c in self.collections],
            "guided_question": self.guided_question,
            "small_catalog_threshold": self.small_catalog_threshold,
        }


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int((os.getenv(name) or str(default)).strip()))
    except (TypeError, ValueError):
        return default


def _norm_token(text: str) -> str:
    return " ".join(str(text or "").strip().split()).lower()


def _parse_featured_products(raw: Any) -> List[FeaturedProductConfig]:
    if not isinstance(raw, list):
        return []
    out: List[FeaturedProductConfig] = []
    for row in raw:
        if isinstance(row, str):
            pid = str(row).strip()
            if pid:
                out.append(FeaturedProductConfig(product_id=pid))
            continue
        if not isinstance(row, dict):
            continue
        pid = str(row.get("product_id") or row.get("id") or "").strip()
        if not pid:
            continue
        out.append(
            FeaturedProductConfig(
                product_id=pid,
                variant_id=str(row.get("variant_id") or "").strip(),
                priority=int(row.get("priority") or 0),
                label_override=str(row.get("label_override") or "").strip(),
            )
        )
    return out


def _parse_collections(raw: Any) -> List[DiscoveryCollectionConfig]:
    if not isinstance(raw, list):
        return []
    out: List[DiscoveryCollectionConfig] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        label = str(row.get("label") or "").strip()
        if not cid or not label:
            continue
        out.append(
            DiscoveryCollectionConfig(
                id=cid,
                label=label,
                priority=int(row.get("priority") or 0),
                enabled=bool(row.get("enabled", True)),
                catalog_match=str(row.get("catalog_match") or row.get("match") or "").strip(),
                featured_products=_parse_featured_products(row.get("featured_products")),
            )
        )
    return out


def parse_merchant_discovery_settings(raw: Mapping[str, Any] | None) -> MerchantDiscoverySettings:
    data = dict(raw or {})
    featured = data.get("featured_product_ids") or data.get("featured_products") or []
    preferred: List[str] = []
    collections = _parse_collections(data.get("collections"))
    if collections:
        preferred = [c.label for c in sorted(collections, key=lambda c: (c.priority, c.label)) if c.enabled]
    else:
        legacy = data.get("preferred_collections") or data.get("collections") or []
        if isinstance(legacy, list):
            preferred = [str(x).strip() for x in legacy if str(x).strip() and isinstance(x, str)]

    default_mode = str(
        data.get("default_mode")
        or data.get("discovery_mode")
        or data.get("mode_override")
        or ""
    ).strip().lower()

    featured_ids: List[str] = []
    if isinstance(featured, list):
        for item in featured:
            if isinstance(item, str) and str(item).strip():
                featured_ids.append(str(item).strip())
            elif isinstance(item, dict):
                pid = str(item.get("product_id") or item.get("id") or "").strip()
                if pid:
                    featured_ids.append(pid)

    return MerchantDiscoverySettings(
        default_mode=default_mode,
        initial_product_count=max(
            1,
            int(data.get("initial_product_count") or _env_int("NAHLA_DISCOVERY_INITIAL_COUNT", 3)),
        ),
        featured_product_ids=featured_ids,
        preferred_collections=preferred,
        collections=collections,
        guided_question=str(
            data.get("guided_question")
            or os.getenv("NAHLA_DISCOVERY_GUIDED_QUESTION")
            or MerchantDiscoverySettings.guided_question
        ).strip(),
        small_catalog_threshold=max(
            1,
            int(data.get("small_catalog_threshold") or _env_int("NAHLA_DISCOVERY_SMALL_CATALOG", 5)),
        ),
    )


def load_merchant_discovery_settings(
    tenant_context: Optional[Any] = None,
) -> MerchantDiscoverySettings:
    ctx: Dict[str, Any] = {}
    if isinstance(tenant_context, dict):
        ctx = tenant_context
    elif tenant_context is not None:
        meta = getattr(tenant_context, "metadata", None)
        if isinstance(meta, dict) and meta.get("discovery_settings"):
            ctx = {"discovery_settings": meta.get("discovery_settings")}
        mc = getattr(tenant_context, "merchant_context", None)
        if isinstance(mc, dict) and mc.get("discovery_settings"):
            ctx = mc

    raw = ctx.get("discovery_settings") if isinstance(ctx.get("discovery_settings"), dict) else {}
    if not raw:
        raw = ctx.get("commerce_discovery_settings") if isinstance(
            ctx.get("commerce_discovery_settings"), dict
        ) else {}
    return parse_merchant_discovery_settings(raw)


__all__ = [
    "DiscoveryCollectionConfig",
    "FeaturedProductConfig",
    "MerchantDiscoverySettings",
    "load_merchant_discovery_settings",
    "parse_merchant_discovery_settings",
]
