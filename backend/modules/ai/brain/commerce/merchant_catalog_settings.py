"""
commerce/merchant_catalog_settings.py
───────────────────────────────────────
Tenant catalog intelligence settings — platform-wide defaults with overrides.

Persisted under ``TenantSettings.store_settings["catalog_intelligence"]`` (Phase 1).
Read-only parser for future brain runtime; no pipeline wiring in Phase 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


ALLOWED_BEST_SELLER_MODES = frozenset({"manual", "auto", "hybrid"})
ALLOWED_RELATION_TYPES = frozenset({"alternative", "related", "upsell", "cross_sell"})


@dataclass(frozen=True)
class MerchantCatalogSettings:
    best_seller_mode: str = "manual"
    max_relations_per_product: int = 8
    default_group_slug: str = ""
    small_catalog_threshold: int = 5
    scoring_weights: Dict[str, float] = field(default_factory=lambda: {
        "featured_rank": 0.35,
        "sales_score": 0.25,
        "availability": 0.20,
        "freshness": 0.10,
        "merchant_priority": 0.10,
    })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "best_seller_mode": self.best_seller_mode,
            "max_relations_per_product": self.max_relations_per_product,
            "default_group_slug": self.default_group_slug,
            "small_catalog_threshold": self.small_catalog_threshold,
            "scoring_weights": dict(self.scoring_weights or {}),
        }


def parse_merchant_catalog_settings(raw: Any) -> MerchantCatalogSettings:
    if not isinstance(raw, Mapping):
        return MerchantCatalogSettings()

    mode = str(raw.get("best_seller_mode") or "manual").strip().lower()
    if mode not in ALLOWED_BEST_SELLER_MODES:
        mode = "manual"

    try:
        max_rel = int(raw.get("max_relations_per_product") or 8)
    except (TypeError, ValueError):
        max_rel = 8
    max_rel = max(1, min(max_rel, 50))

    try:
        threshold = int(raw.get("small_catalog_threshold") or 5)
    except (TypeError, ValueError):
        threshold = 5
    threshold = max(1, min(threshold, 500))

    weights_raw = raw.get("scoring_weights")
    weights: Dict[str, float] = {}
    if isinstance(weights_raw, Mapping):
        for key, val in weights_raw.items():
            try:
                weights[str(key)] = float(val)
            except (TypeError, ValueError):
                continue

    return MerchantCatalogSettings(
        best_seller_mode=mode,
        max_relations_per_product=max_rel,
        default_group_slug=str(raw.get("default_group_slug") or "").strip(),
        small_catalog_threshold=threshold,
        scoring_weights=weights or MerchantCatalogSettings().scoring_weights,
    )


def normalize_relation_type(value: str) -> Optional[str]:
    norm = str(value or "").strip().lower()
    if norm in ALLOWED_RELATION_TYPES:
        return norm
    return None


__all__ = [
    "ALLOWED_BEST_SELLER_MODES",
    "ALLOWED_RELATION_TYPES",
    "MerchantCatalogSettings",
    "normalize_relation_type",
    "parse_merchant_catalog_settings",
]
