"""
catalog/discovery_presenter.py
────────────────────────────────
Evidence-based discovery presentation with merchant-defined structure (Phase 4A).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from ..commerce.merchant_discovery_settings import (
    DiscoveryCollectionConfig,
    FeaturedProductConfig,
    MerchantDiscoverySettings,
)
from .catalog_intelligence import CatalogGroup

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")


def _norm(text: str) -> str:
    t = str(text or "").lower()
    t = _NORM_RE.sub("", t)
    return " ".join(t.split()).strip()


def _format_price(product: Dict[str, Any]) -> str:
    sale = str(product.get("sale_price") or "").strip()
    price = str(product.get("price") or "").strip()
    chosen = sale or price
    if not chosen:
        return "السعر غير محدد"
    if re.match(r"^\s*\d+(\.\d+)?\s*$", chosen):
        return f"{chosen} ريال"
    return chosen


def _display_title(
    product: Dict[str, Any],
    featured: Optional[FeaturedProductConfig] = None,
) -> str:
    override = str(getattr(featured, "label_override", "") or "").strip()
    if override:
        return override
    return str(product.get("title") or "").strip()


def _variant_price_label(product: Dict[str, Any], variant_id: str) -> str:
    if not variant_id:
        return _format_price(product)
    for variant in list(product.get("variants") or []):
        if not isinstance(variant, dict):
            continue
        vid = str(variant.get("id") or variant.get("variant_id") or "").strip()
        if vid == str(variant_id):
            price = str(variant.get("price") or variant.get("sale_price") or product.get("price") or "").strip()
            if price and re.match(r"^\s*\d+(\.\d+)?\s*$", price):
                return f"{price} ريال"
            return price or _format_price(product)
    return _format_price(product)


def compose_merchant_collections(
    collections: Sequence[CatalogGroup | Dict[str, Any]],
    *,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
) -> str:
    rows: List[tuple[int, str, str]] = []
    merchant_by_label = {
        _norm(c.label): c for c in (merchant_settings.enabled_collections() if merchant_settings else [])
    }
    for idx, group in enumerate(collections, start=1):
        if isinstance(group, CatalogGroup):
            label = group.group_name
            group_id = group.group_id
        else:
            label = str(group.get("group_name") or group.get("label") or "").strip()
            group_id = str(group.get("group_id") or group.get("id") or label)
        merchant_row = merchant_by_label.get(_norm(label))
        display = merchant_row.label if merchant_row else label
        priority = merchant_row.priority if merchant_row else idx
        rows.append((priority, group_id, display))
    rows.sort(key=lambda r: (r[0], r[2]))
    lines = ["اختر القسم اللي يناسبك:", ""]
    for i, (_prio, _gid, label) in enumerate(rows, start=1):
        lines.append(f"{i}. {label}")
    return "\n".join(lines)


def compose_collection_products(
    products: Sequence[Dict[str, Any]],
    *,
    collection: Optional[DiscoveryCollectionConfig] = None,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
    collection_label: str = "",
) -> str:
    label = collection_label or (collection.label if collection else "")
    header = f"من {label} المتوفر:" if label else "من المنتجات المتوفر:"
    featured_map: Dict[str, FeaturedProductConfig] = {}
    if collection and merchant_settings:
        for fp in merchant_settings.featured_for_collection(collection):
            featured_map[str(fp.product_id)] = fp
    lines = [header, ""]
    for i, product in enumerate(products, start=1):
        pid = str(product.get("id") or product.get("external_id") or "").strip()
        fp = featured_map.get(pid)
        title = _display_title(product, fp)
        price = _variant_price_label(product, str(getattr(fp, "variant_id", "") or ""))
        lines.append(f"{i}. {title} — {price}")
    lines.append("")
    lines.append("اكتب رقم المنتج أو اسمه ونكمل طلبك.")
    return "\n".join(lines)


def compose_discovery_products(
    products: Sequence[Dict[str, Any]],
    *,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
) -> str:
    if not products:
        return "ما ظهرت لي منتجات متزامنة في الكتالوج الآن."
    featured_map = {}
    if merchant_settings:
        for pid, score in merchant_settings.merchant_priority_map().items():
            featured_map[pid] = score
    lines: List[str] = []
    for i, product in enumerate(products, start=1):
        title = _display_title(product)
        price = _format_price(product)
        lines.append(f"{i}. {title} — {price}")
    return "\n".join(lines)


__all__ = [
    "compose_collection_products",
    "compose_discovery_products",
    "compose_merchant_collections",
]
