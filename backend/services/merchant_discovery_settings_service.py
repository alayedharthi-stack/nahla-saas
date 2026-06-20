"""
services/merchant_discovery_settings_service.py
────────────────────────────────────────────────
Persist and validate merchant discovery settings (Phase 4A).

Storage: ``TenantSettings.ai_settings["discovery_settings"]``.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.tenant import get_or_create_settings
from modules.ai.brain.commerce.merchant_discovery_settings import (
    DiscoveryCollectionConfig,
    FeaturedProductConfig,
    MerchantDiscoverySettings,
    parse_merchant_discovery_settings,
)

logger = logging.getLogger("nahla.discovery_settings")

DEFAULT_DISCOVERY_SETTINGS: Dict[str, Any] = {
    "default_mode": "",
    "initial_product_count": 3,
    "featured_product_ids": [],
    "collections": [],
    "guided_question": MerchantDiscoverySettings.guided_question,
    "small_catalog_threshold": 5,
}


def _settings_blob(ts: Any) -> Dict[str, Any]:
    ai = dict(getattr(ts, "ai_settings", None) or {})
    raw = ai.get("discovery_settings")
    return deepcopy(raw) if isinstance(raw, dict) else deepcopy(DEFAULT_DISCOVERY_SETTINGS)


def _write_blob(db: Session, tenant_id: int, blob: Dict[str, Any]) -> Dict[str, Any]:
    ts = get_or_create_settings(db, tenant_id)
    ai = dict(ts.ai_settings or {})
    ai["discovery_settings"] = blob
    ts.ai_settings = ai
    flag_modified(ts, "ai_settings")
    db.add(ts)
    db.commit()
    db.refresh(ts)
    return blob


def _valid_product_ids(db: Session, tenant_id: int, product_ids: List[str]) -> Set[str]:
    if not product_ids:
        return set()
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:
        logger.exception("[DISCOVERY_SETTINGS] Product model import failed")
        return set()

    ints: List[int] = []
    for pid in product_ids:
        try:
            ints.append(int(str(pid).strip()))
        except (TypeError, ValueError):
            continue
    if not ints:
        return set()
    rows = (
        db.query(Product.id)
        .filter(Product.tenant_id == tenant_id, Product.id.in_(ints))
        .all()
    )
    return {str(row[0]) for row in rows}


def _valid_variant_ids(
    db: Session,
    tenant_id: int,
    variant_ids: List[str],
    *,
    product_ids: Optional[Set[str]] = None,
) -> Set[str]:
    if not variant_ids:
        return set()
    try:
        from database.models import ProductVariant  # noqa: PLC0415
    except Exception:
        logger.exception("[DISCOVERY_SETTINGS] ProductVariant model import failed")
        return set()

    ints: List[int] = []
    for vid in variant_ids:
        try:
            ints.append(int(str(vid).strip()))
        except (TypeError, ValueError):
            continue
    if not ints:
        return set()
    q = db.query(ProductVariant.id, ProductVariant.product_id).filter(
        ProductVariant.tenant_id == tenant_id,
        ProductVariant.id.in_(ints),
    )
    rows = q.all()
    valid: Set[str] = set()
    for vid, pid in rows:
        if product_ids is not None and str(pid) not in product_ids:
            continue
        valid.add(str(vid))
    return valid


def sanitize_discovery_settings(
    raw: Dict[str, Any],
    *,
    db: Session,
    tenant_id: int,
) -> Dict[str, Any]:
    """Normalize settings and drop invalid catalog references."""
    parsed = parse_merchant_discovery_settings(raw)
    global_ids = _valid_product_ids(db, tenant_id, list(parsed.featured_product_ids))

    collections_out: List[Dict[str, Any]] = []
    seen_collection_ids: Set[str] = set()
    for collection in parsed.collections:
        if collection.id in seen_collection_ids:
            continue
        seen_collection_ids.add(collection.id)
        featured_out: List[Dict[str, Any]] = []
        coll_product_ids = [fp.product_id for fp in collection.featured_products]
        valid_products = _valid_product_ids(db, tenant_id, coll_product_ids)
        valid_variants = _valid_variant_ids(
            db,
            tenant_id,
            [fp.variant_id for fp in collection.featured_products if fp.variant_id],
            product_ids=valid_products,
        )
        seen_fp: Set[str] = set()
        for fp in sorted(collection.featured_products, key=lambda x: (x.priority, x.product_id)):
            if fp.product_id not in valid_products or fp.product_id in seen_fp:
                continue
            seen_fp.add(fp.product_id)
            row = fp.to_dict()
            if fp.variant_id and fp.variant_id not in valid_variants:
                row["variant_id"] = ""
            featured_out.append(row)
        collections_out.append(
            {
                **collection.to_dict(),
                "featured_products": featured_out,
            }
        )

    ordered_global: List[str] = []
    for pid in parsed.featured_product_ids:
        if pid in global_ids and pid not in ordered_global:
            ordered_global.append(pid)
    blob = {
        "default_mode": parsed.default_mode,
        "initial_product_count": parsed.initial_product_count,
        "featured_product_ids": ordered_global,
        "collections": sorted(collections_out, key=lambda c: (c.get("priority", 0), c.get("label", ""))),
        "guided_question": parsed.guided_question,
        "small_catalog_threshold": parsed.small_catalog_threshold,
    }
    return blob


def get_discovery_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    ts = get_or_create_settings(db, tenant_id)
    return _settings_blob(ts)


def load_settings_for_brain(db: Session, tenant_id: int) -> MerchantDiscoverySettings:
    return parse_merchant_discovery_settings(get_discovery_settings(db, tenant_id))


def save_discovery_settings(
    db: Session,
    tenant_id: int,
    raw: Dict[str, Any],
) -> Dict[str, Any]:
    blob = sanitize_discovery_settings(raw, db=db, tenant_id=tenant_id)
    return _write_blob(db, tenant_id, blob)


def reorder_collections(
    db: Session,
    tenant_id: int,
    collection_ids: List[str],
) -> Dict[str, Any]:
    blob = get_discovery_settings(db, tenant_id)
    collections = list(blob.get("collections") or [])
    by_id = {str(c.get("id")): c for c in collections if isinstance(c, dict) and c.get("id")}
    ordered: List[Dict[str, Any]] = []
    priority = 1
    seen: Set[str] = set()
    for cid in collection_ids:
        key = str(cid).strip()
        if not key or key not in by_id or key in seen:
            continue
        row = dict(by_id[key])
        row["priority"] = priority
        priority += 1
        ordered.append(row)
        seen.add(key)
    for row in collections:
        key = str(row.get("id") or "")
        if key and key not in seen and isinstance(row, dict):
            row = dict(row)
            row["priority"] = priority
            priority += 1
            ordered.append(row)
            seen.add(key)
    blob["collections"] = ordered
    return save_discovery_settings(db, tenant_id, blob)


def set_collection_enabled(
    db: Session,
    tenant_id: int,
    collection_id: str,
    *,
    enabled: bool,
) -> Dict[str, Any]:
    blob = get_discovery_settings(db, tenant_id)
    collections = list(blob.get("collections") or [])
    found = False
    for row in collections:
        if str(row.get("id") or "") == str(collection_id):
            row["enabled"] = bool(enabled)
            found = True
            break
    if not found:
        raise ValueError("collection_not_found")
    blob["collections"] = collections
    return save_discovery_settings(db, tenant_id, blob)


def assign_featured_product(
    db: Session,
    tenant_id: int,
    collection_id: str,
    featured: Dict[str, Any],
) -> Dict[str, Any]:
    blob = get_discovery_settings(db, tenant_id)
    collections = list(blob.get("collections") or [])
    target = None
    for row in collections:
        if str(row.get("id") or "") == str(collection_id):
            target = row
            break
    if target is None:
        raise ValueError("collection_not_found")

    pid = str(featured.get("product_id") or "").strip()
    if not pid:
        raise ValueError("product_id_required")
    valid = _valid_product_ids(db, tenant_id, [pid])
    if pid not in valid:
        raise ValueError("invalid_product_id")

    variant_id = str(featured.get("variant_id") or "").strip()
    if variant_id:
        valid_variants = _valid_variant_ids(db, tenant_id, [variant_id], product_ids=valid)
        if variant_id not in valid_variants:
            variant_id = ""

    featured_rows = list(target.get("featured_products") or [])
    featured_rows = [
        row for row in featured_rows
        if str(row.get("product_id") or "") != pid
    ]
    featured_rows.append(
        {
            "product_id": pid,
            "variant_id": variant_id,
            "priority": int(featured.get("priority") or (len(featured_rows) + 1)),
            "label_override": str(featured.get("label_override") or "").strip(),
        }
    )
    featured_rows.sort(key=lambda r: (int(r.get("priority") or 0), str(r.get("product_id") or "")))
    target["featured_products"] = featured_rows
    blob["collections"] = collections
    return save_discovery_settings(db, tenant_id, blob)


def remove_featured_product(
    db: Session,
    tenant_id: int,
    collection_id: str,
    product_id: str,
) -> Dict[str, Any]:
    blob = get_discovery_settings(db, tenant_id)
    collections = list(blob.get("collections") or [])
    for row in collections:
        if str(row.get("id") or "") != str(collection_id):
            continue
        featured_rows = [
            fp for fp in (row.get("featured_products") or [])
            if str(fp.get("product_id") or "") != str(product_id)
        ]
        row["featured_products"] = featured_rows
        blob["collections"] = collections
        return save_discovery_settings(db, tenant_id, blob)
    raise ValueError("collection_not_found")


__all__ = [
    "DEFAULT_DISCOVERY_SETTINGS",
    "assign_featured_product",
    "get_discovery_settings",
    "load_settings_for_brain",
    "remove_featured_product",
    "reorder_collections",
    "sanitize_discovery_settings",
    "save_discovery_settings",
    "set_collection_enabled",
]
