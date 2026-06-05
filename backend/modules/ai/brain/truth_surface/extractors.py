"""
truth_surface/extractors.py
───────────────────────────
Shared fact extraction helpers for Phase 1 inventory and UTS v1 collector.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .contract import OperationalFactKind, TruthSource, TruthSurface

_PRICE_RE = re.compile(
    r"(\d[\d.,]*)\s*(?:ريال|SAR|ر\.?\s*س|rs)",
    re.IGNORECASE,
)


def norm_val(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def product_id(product: Dict[str, Any]) -> Optional[str]:
    if not isinstance(product, dict):
        return None
    for key in ("id", "external_id"):
        val = product.get(key)
        if val is not None and norm_val(val):
            return f"id:{val}"
    title = norm_val(product.get("title"))
    if title:
        return f"title:{title.casefold()}"
    return None


def product_record(
    product: Dict[str, Any],
    *,
    surface: TruthSurface,
    source: TruthSource,
    path: str,
) -> Dict[str, Any]:
    """Normalized product projection for UTS manifest."""
    pid = product_id(product) or path
    return {
        "product_key": pid,
        "title": norm_val(product.get("title")),
        "price": norm_val(product.get("price")),
        "orderable": norm_val(product.get("orderable") or product.get("can_checkout")),
        "product_url": norm_val(product.get("product_url") or product.get("url")),
        "surface": surface.value,
        "source": source.value,
        "path": path,
    }


def bundle_to_dict(bundle: Any) -> Dict[str, Any]:
    from dataclasses import asdict, is_dataclass

    if bundle is None:
        return {}
    if isinstance(bundle, dict):
        return bundle
    if is_dataclass(bundle) and not isinstance(bundle, type):
        return asdict(bundle)
    if hasattr(bundle, "to_dict") and callable(bundle.to_dict):
        return bundle.to_dict()
    return {}


def policy_text(policies: Any) -> str:
    if isinstance(policies, dict):
        parts = []
        for k, v in policies.items():
            if v is not None and norm_val(v):
                parts.append(f"{k}: {norm_val(v)}")
        return "\n".join(parts).strip()
    return norm_val(policies)


def shipping_overlap(a: str, b: str) -> bool:
    """Conservative overlap check for known_facts vs policies dedup."""
    na = norm_val(a).casefold()
    nb = norm_val(b).casefold()
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 20 and na in nb:
        return True
    if len(nb) >= 20 and nb in na:
        return True
    return False


__all__ = [
    "bundle_to_dict",
    "norm_val",
    "policy_text",
    "product_id",
    "product_record",
    "shipping_overlap",
]
