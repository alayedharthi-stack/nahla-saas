"""
commerce/merchant_discovery_settings.py
────────────────────────────────────────
Tenant discovery settings — defaults until dashboard Phase 3.

Read from ``tenant_context["discovery_settings"]`` when present; otherwise
platform defaults apply for every merchant category.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class MerchantDiscoverySettings:
    mode_override: str = ""
    initial_product_count: int = 3
    featured_product_ids: List[str] = field(default_factory=list)
    preferred_collections: List[str] = field(default_factory=list)
    guided_question: str = "وش نوع المنتج اللي تدور عليه؟"
    small_catalog_threshold: int = 5


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int((os.getenv(name) or str(default)).strip()))
    except (TypeError, ValueError):
        return default


def load_merchant_discovery_settings(
    tenant_context: Optional[Dict[str, Any]] = None,
) -> MerchantDiscoverySettings:
    ctx = tenant_context if isinstance(tenant_context, dict) else {}
    raw = ctx.get("discovery_settings") if isinstance(ctx.get("discovery_settings"), dict) else {}
    if not raw:
        raw = ctx.get("commerce_discovery_settings") if isinstance(
            ctx.get("commerce_discovery_settings"), dict
        ) else {}

    featured = raw.get("featured_product_ids") or raw.get("featured_products") or []
    preferred = raw.get("preferred_collections") or raw.get("collections") or []
    return MerchantDiscoverySettings(
        mode_override=str(raw.get("discovery_mode") or raw.get("mode_override") or "").strip().lower(),
        initial_product_count=max(
            1,
            int(raw.get("initial_product_count") or _env_int("NAHLA_DISCOVERY_INITIAL_COUNT", 3)),
        ),
        featured_product_ids=[str(x).strip() for x in featured if str(x).strip()],
        preferred_collections=[str(x).strip() for x in preferred if str(x).strip()],
        guided_question=str(
            raw.get("guided_question")
            or os.getenv("NAHLA_DISCOVERY_GUIDED_QUESTION")
            or MerchantDiscoverySettings.guided_question
        ).strip(),
        small_catalog_threshold=max(
            1,
            int(raw.get("small_catalog_threshold") or _env_int("NAHLA_DISCOVERY_SMALL_CATALOG", 5)),
        ),
    )


__all__ = ["MerchantDiscoverySettings", "load_merchant_discovery_settings"]
