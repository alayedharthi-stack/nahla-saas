"""
services/catalog_intelligence_service.py
────────────────────────────────────────
Catalog Intelligence Phase 1 — relational CRUD + read-only helpers.

Storage:
  • ``ProductGroup`` / ``ProductGroupItem`` / ``ProductRelation`` / ``ProductRanking``
  • ``TenantSettings.store_settings["catalog_intelligence"]`` for merchant config

No AI runtime wiring in Phase 1.
"""
from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.tenant import get_or_create_settings
from modules.ai.brain.commerce.merchant_catalog_settings import (
    MerchantCatalogSettings,
    normalize_relation_type,
    parse_merchant_catalog_settings,
)

logger = logging.getLogger("nahla.catalog_intelligence")

DEFAULT_CATALOG_SETTINGS: Dict[str, Any] = MerchantCatalogSettings().to_dict()

_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


def normalize_group_slug(value: str) -> str:
    slug = str(value or "").strip().lower()
    slug = slug.replace("_", "-").replace(" ", "-")
    slug = _SLUG_RE.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:64]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _product_ids_for_tenant(db: Session, tenant_id: int, product_ids: Sequence[int]) -> Set[int]:
    if not product_ids:
        return set()
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:
        logger.exception("[CATALOG_INTELLIGENCE] Product model import failed")
        return set()
    rows = (
        db.query(Product.id)
        .filter(Product.tenant_id == tenant_id, Product.id.in_(list(product_ids)))
        .all()
    )
    return {int(row[0]) for row in rows}


def _variant_ids_for_tenant(
    db: Session,
    tenant_id: int,
    variant_ids: Sequence[int],
    *,
    product_id: Optional[int] = None,
) -> Set[int]:
    if not variant_ids:
        return set()
    try:
        from database.models import ProductVariant  # noqa: PLC0415
    except Exception:
        logger.exception("[CATALOG_INTELLIGENCE] ProductVariant model import failed")
        return set()
    q = db.query(ProductVariant.id).filter(
        ProductVariant.tenant_id == tenant_id,
        ProductVariant.id.in_(list(variant_ids)),
    )
    if product_id is not None:
        q = q.filter(ProductVariant.product_id == product_id)
    return {int(row[0]) for row in q.all()}


def _get_group(db: Session, tenant_id: int, group_id: int):
    from database.models import ProductGroup  # noqa: PLC0415

    return (
        db.query(ProductGroup)
        .filter(
            ProductGroup.id == group_id,
            ProductGroup.tenant_id == tenant_id,
            ProductGroup.deleted_at.is_(None),
        )
        .first()
    )


def _serialize_group_item(item) -> Dict[str, Any]:
    return {
        "id": item.id,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "priority": item.priority,
        "label_override": item.label_override or "",
        "product_title": getattr(getattr(item, "product", None), "title", "") or "",
    }


def _serialize_group(group, *, include_items: bool = False) -> Dict[str, Any]:
    payload = {
        "id": group.id,
        "slug": group.slug,
        "label": group.label,
        "description": group.description or "",
        "catalog_match": group.catalog_match or "",
        "priority": group.priority,
        "is_active": bool(group.is_active),
        "source": group.source or "manual",
        "metadata_json": dict(group.metadata_json or {}),
        "product_count": len(getattr(group, "items", []) or []),
    }
    if include_items:
        items = sorted(group.items or [], key=lambda i: (i.priority, i.id))
        payload["items"] = [_serialize_group_item(i) for i in items]
    return payload


def _serialize_relation(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "source_product_id": row.source_product_id,
        "target_product_id": row.target_product_id,
        "relation_type": row.relation_type,
        "priority": row.priority,
        "source": row.source or "manual",
        "target_product_title": getattr(getattr(row, "target_product", None), "title", "") or "",
    }


def _serialize_ranking(row) -> Dict[str, Any]:
    return {
        "product_id": row.product_id,
        "is_best_seller": bool(row.is_best_seller),
        "sales_rank": row.sales_rank,
        "sales_score": row.sales_score,
        "merchant_priority": row.merchant_priority,
        "stats_source": row.stats_source or "manual",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


# ── Settings (JSONB) ───────────────────────────────────────────────────────────

def get_catalog_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    ts = get_or_create_settings(db, tenant_id)
    store = dict(getattr(ts, "store_settings", None) or {})
    raw = store.get("catalog_intelligence")
    if isinstance(raw, dict):
        return parse_merchant_catalog_settings(raw).to_dict()
    return deepcopy(DEFAULT_CATALOG_SETTINGS)


def save_catalog_settings(db: Session, tenant_id: int, raw: Dict[str, Any]) -> Dict[str, Any]:
    parsed = parse_merchant_catalog_settings(raw).to_dict()
    ts = get_or_create_settings(db, tenant_id)
    store = dict(ts.store_settings or {})
    store["catalog_intelligence"] = parsed
    ts.store_settings = store
    flag_modified(ts, "store_settings")
    db.add(ts)
    db.commit()
    db.refresh(ts)
    return parsed


# ── Groups CRUD ──────────────────────────────────────────────────────────────

def list_product_groups(
    db: Session,
    tenant_id: int,
    *,
    include_inactive: bool = False,
) -> List[Dict[str, Any]]:
    from database.models import ProductGroup  # noqa: PLC0415

    q = db.query(ProductGroup).filter(
        ProductGroup.tenant_id == tenant_id,
        ProductGroup.deleted_at.is_(None),
    )
    if not include_inactive:
        q = q.filter(ProductGroup.is_active.is_(True))
    rows = q.order_by(ProductGroup.priority.asc(), ProductGroup.label.asc()).all()
    return [_serialize_group(row) for row in rows]


def get_product_group(db: Session, tenant_id: int, group_id: int) -> Optional[Dict[str, Any]]:
    group = _get_group(db, tenant_id, group_id)
    if group is None:
        return None
    return _serialize_group(group, include_items=True)


def create_product_group(db: Session, tenant_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    from database.models import ProductGroup  # noqa: PLC0415

    slug = normalize_group_slug(payload.get("slug") or payload.get("label") or "")
    if not slug:
        raise ValueError("group_slug_required")
    label = str(payload.get("label") or slug).strip()
    if not label:
        raise ValueError("group_label_required")

    exists = (
        db.query(ProductGroup.id)
        .filter(ProductGroup.tenant_id == tenant_id, ProductGroup.slug == slug, ProductGroup.deleted_at.is_(None))
        .first()
    )
    if exists:
        raise ValueError("group_slug_exists")

    row = ProductGroup(
        tenant_id=tenant_id,
        slug=slug,
        label=label,
        description=str(payload.get("description") or "").strip() or None,
        catalog_match=str(payload.get("catalog_match") or "").strip() or None,
        priority=int(payload.get("priority") or 100),
        is_active=bool(payload.get("is_active", True)),
        source=str(payload.get("source") or "manual"),
        metadata_json=dict(payload.get("metadata_json") or {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_group(row)


def update_product_group(
    db: Session,
    tenant_id: int,
    group_id: int,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    group = _get_group(db, tenant_id, group_id)
    if group is None:
        return None

    if "label" in payload:
        label = str(payload.get("label") or "").strip()
        if label:
            group.label = label
    if "description" in payload:
        group.description = str(payload.get("description") or "").strip() or None
    if "catalog_match" in payload:
        group.catalog_match = str(payload.get("catalog_match") or "").strip() or None
    if "priority" in payload:
        group.priority = int(payload.get("priority") or group.priority)
    if "is_active" in payload:
        group.is_active = bool(payload.get("is_active"))
    if "metadata_json" in payload and isinstance(payload.get("metadata_json"), dict):
        group.metadata_json = dict(payload.get("metadata_json") or {})
    if "slug" in payload:
        slug = normalize_group_slug(payload.get("slug") or "")
        if slug and slug != group.slug:
            from database.models import ProductGroup  # noqa: PLC0415

            clash = (
                db.query(ProductGroup.id)
                .filter(
                    ProductGroup.tenant_id == tenant_id,
                    ProductGroup.slug == slug,
                    ProductGroup.id != group.id,
                    ProductGroup.deleted_at.is_(None),
                )
                .first()
            )
            if clash:
                raise ValueError("group_slug_exists")
            group.slug = slug

    group.updated_at = _now()
    db.add(group)
    db.commit()
    db.refresh(group)
    return _serialize_group(group, include_items=True)


def delete_product_group(db: Session, tenant_id: int, group_id: int) -> bool:
    group = _get_group(db, tenant_id, group_id)
    if group is None:
        return False
    group.deleted_at = _now()
    group.is_active = False
    group.updated_at = _now()
    db.add(group)
    db.commit()
    return True


def reorder_product_groups(db: Session, tenant_id: int, group_ids: Sequence[int]) -> List[Dict[str, Any]]:
    from database.models import ProductGroup  # noqa: PLC0415

    ids = [int(x) for x in group_ids if str(x).strip()]
    if not ids:
        return list_product_groups(db, tenant_id, include_inactive=True)

    rows = (
        db.query(ProductGroup)
        .filter(
            ProductGroup.tenant_id == tenant_id,
            ProductGroup.id.in_(ids),
            ProductGroup.deleted_at.is_(None),
        )
        .all()
    )
    by_id = {row.id: row for row in rows}
    for idx, gid in enumerate(ids, start=1):
        row = by_id.get(gid)
        if row is not None:
            row.priority = idx
            row.updated_at = _now()
            db.add(row)
    db.commit()
    return list_product_groups(db, tenant_id, include_inactive=True)


# ── Group items ────────────────────────────────────────────────────────────────

def list_group_items(db: Session, tenant_id: int, group_id: int) -> Optional[List[Dict[str, Any]]]:
    group = _get_group(db, tenant_id, group_id)
    if group is None:
        return None
    items = sorted(group.items or [], key=lambda i: (i.priority, i.id))
    return [_serialize_group_item(i) for i in items]


def add_group_item(
    db: Session,
    tenant_id: int,
    group_id: int,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    from database.models import ProductGroupItem  # noqa: PLC0415

    group = _get_group(db, tenant_id, group_id)
    if group is None:
        return None

    try:
        product_id = int(payload.get("product_id"))
    except (TypeError, ValueError):
        raise ValueError("product_id_required") from None

    if product_id not in _product_ids_for_tenant(db, tenant_id, [product_id]):
        raise ValueError("product_not_found")

    variant_id = payload.get("variant_id")
    variant_int: Optional[int] = None
    if variant_id not in (None, "", 0, "0"):
        try:
            variant_int = int(variant_id)
        except (TypeError, ValueError):
            raise ValueError("variant_not_found") from None
        if variant_int not in _variant_ids_for_tenant(db, tenant_id, [variant_int], product_id=product_id):
            raise ValueError("variant_not_found")

    existing = (
        db.query(ProductGroupItem)
        .filter(ProductGroupItem.group_id == group.id, ProductGroupItem.product_id == product_id)
        .first()
    )
    if existing:
        raise ValueError("group_item_exists")

    row = ProductGroupItem(
        group_id=group.id,
        product_id=product_id,
        variant_id=variant_int,
        priority=int(payload.get("priority") or 0),
        label_override=str(payload.get("label_override") or "").strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_group_item(row)


def update_group_item(
    db: Session,
    tenant_id: int,
    group_id: int,
    item_id: int,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    from database.models import ProductGroupItem  # noqa: PLC0415

    group = _get_group(db, tenant_id, group_id)
    if group is None:
        return None

    row = (
        db.query(ProductGroupItem)
        .filter(ProductGroupItem.id == item_id, ProductGroupItem.group_id == group.id)
        .first()
    )
    if row is None:
        return None

    if "priority" in payload:
        row.priority = int(payload.get("priority") or row.priority)
    if "label_override" in payload:
        row.label_override = str(payload.get("label_override") or "").strip()
    if "variant_id" in payload:
        variant_id = payload.get("variant_id")
        if variant_id in (None, "", 0, "0"):
            row.variant_id = None
        else:
            try:
                variant_int = int(variant_id)
            except (TypeError, ValueError):
                raise ValueError("variant_not_found") from None
            if variant_int not in _variant_ids_for_tenant(
                db, tenant_id, [variant_int], product_id=row.product_id,
            ):
                raise ValueError("variant_not_found")
            row.variant_id = variant_int

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_group_item(row)


def delete_group_item(db: Session, tenant_id: int, group_id: int, item_id: int) -> bool:
    from database.models import ProductGroupItem  # noqa: PLC0415

    group = _get_group(db, tenant_id, group_id)
    if group is None:
        return False
    row = (
        db.query(ProductGroupItem)
        .filter(ProductGroupItem.id == item_id, ProductGroupItem.group_id == group.id)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


# ── Relations ─────────────────────────────────────────────────────────────────

def list_product_relations(
    db: Session,
    tenant_id: int,
    product_id: int,
    *,
    relation_type: str = "",
) -> Optional[List[Dict[str, Any]]]:
    from database.models import ProductRelation  # noqa: PLC0415

    if product_id not in _product_ids_for_tenant(db, tenant_id, [product_id]):
        return None

    q = db.query(ProductRelation).filter(
        ProductRelation.tenant_id == tenant_id,
        ProductRelation.source_product_id == product_id,
    )
    norm_type = normalize_relation_type(relation_type)
    if norm_type:
        q = q.filter(ProductRelation.relation_type == norm_type)
    rows = q.order_by(ProductRelation.priority.asc(), ProductRelation.id.asc()).all()
    return [_serialize_relation(r) for r in rows]


def create_product_relation(
    db: Session,
    tenant_id: int,
    product_id: int,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    from database.models import ProductRelation  # noqa: PLC0415

    if product_id not in _product_ids_for_tenant(db, tenant_id, [product_id]):
        return None

    try:
        target_product_id = int(payload.get("target_product_id"))
    except (TypeError, ValueError):
        raise ValueError("target_product_id_required") from None

    if target_product_id == product_id:
        raise ValueError("relation_self_reference")

    if target_product_id not in _product_ids_for_tenant(db, tenant_id, [target_product_id]):
        raise ValueError("target_product_not_found")

    relation_type = normalize_relation_type(str(payload.get("relation_type") or ""))
    if not relation_type:
        raise ValueError("relation_type_invalid")

    settings = parse_merchant_catalog_settings(get_catalog_settings(db, tenant_id))
    existing_count = (
        db.query(ProductRelation.id)
        .filter(
            ProductRelation.tenant_id == tenant_id,
            ProductRelation.source_product_id == product_id,
        )
        .count()
    )
    if existing_count >= settings.max_relations_per_product:
        raise ValueError("relation_limit_reached")

    row = ProductRelation(
        tenant_id=tenant_id,
        source_product_id=product_id,
        target_product_id=target_product_id,
        relation_type=relation_type,
        priority=int(payload.get("priority") or 0),
        source=str(payload.get("source") or "manual"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_relation(row)


def delete_product_relation(
    db: Session,
    tenant_id: int,
    product_id: int,
    relation_id: int,
) -> bool:
    from database.models import ProductRelation  # noqa: PLC0415

    row = (
        db.query(ProductRelation)
        .filter(
            ProductRelation.id == relation_id,
            ProductRelation.tenant_id == tenant_id,
            ProductRelation.source_product_id == product_id,
        )
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


# ── Rankings ──────────────────────────────────────────────────────────────────

def get_product_ranking(db: Session, tenant_id: int, product_id: int) -> Optional[Dict[str, Any]]:
    from database.models import ProductRanking  # noqa: PLC0415

    if product_id not in _product_ids_for_tenant(db, tenant_id, [product_id]):
        return None
    row = (
        db.query(ProductRanking)
        .filter(ProductRanking.tenant_id == tenant_id, ProductRanking.product_id == product_id)
        .first()
    )
    if row is None:
        return {
            "product_id": product_id,
            "is_best_seller": False,
            "sales_rank": None,
            "sales_score": None,
            "merchant_priority": 0,
            "stats_source": "manual",
            "updated_at": "",
        }
    return _serialize_ranking(row)


def save_product_ranking(
    db: Session,
    tenant_id: int,
    product_id: int,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    from database.models import ProductRanking  # noqa: PLC0415

    if product_id not in _product_ids_for_tenant(db, tenant_id, [product_id]):
        return None

    row = (
        db.query(ProductRanking)
        .filter(ProductRanking.tenant_id == tenant_id, ProductRanking.product_id == product_id)
        .first()
    )
    if row is None:
        row = ProductRanking(tenant_id=tenant_id, product_id=product_id)
    row.is_best_seller = bool(payload.get("is_best_seller", row.is_best_seller))
    if "sales_rank" in payload:
        val = payload.get("sales_rank")
        row.sales_rank = int(val) if val is not None and str(val).strip() else None
    if "sales_score" in payload:
        val = payload.get("sales_score")
        row.sales_score = float(val) if val is not None and str(val).strip() else None
    if "merchant_priority" in payload:
        row.merchant_priority = int(payload.get("merchant_priority") or 0)
    if "stats_source" in payload:
        row.stats_source = str(payload.get("stats_source") or "manual")
    row.updated_at = _now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_ranking(row)


# ── Read-only helpers (future brain runtime; no wiring in Phase 1) ─────────────

def read_catalog_groups(
    db: Session,
    tenant_id: int,
    *,
    active_only: bool = True,
) -> List[Dict[str, Any]]:
    return list_product_groups(db, tenant_id, include_inactive=not active_only)


def read_group_products(db: Session, tenant_id: int, group_id: int) -> Optional[List[Dict[str, Any]]]:
    return list_group_items(db, tenant_id, group_id)


def read_product_relations(
    db: Session,
    tenant_id: int,
    product_id: int,
    *,
    relation_type: str = "",
) -> Optional[List[Dict[str, Any]]]:
    return list_product_relations(db, tenant_id, product_id, relation_type=relation_type)


def read_best_sellers(
    db: Session,
    tenant_id: int,
    *,
    group_id: Optional[int] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    from database.models import ProductRanking  # noqa: PLC0415

    cap = max(1, min(int(limit or 10), 50))
    product_ids: Optional[Set[int]] = None
    if group_id is not None:
        group = _get_group(db, tenant_id, group_id)
        if group is None:
            return []
        product_ids = {int(i.product_id) for i in (group.items or [])}

    q = db.query(ProductRanking).filter(
        ProductRanking.tenant_id == tenant_id,
        ProductRanking.is_best_seller.is_(True),
    )
    if product_ids is not None:
        if not product_ids:
            return []
        q = q.filter(ProductRanking.product_id.in_(sorted(product_ids)))

    rows = q.order_by(
        ProductRanking.merchant_priority.desc(),
        ProductRanking.sales_rank.asc(),
        ProductRanking.sales_score.desc(),
    ).limit(cap).all()
    return [_serialize_ranking(r) for r in rows]


__all__ = [
    "add_group_item",
    "create_product_group",
    "create_product_relation",
    "delete_group_item",
    "delete_product_group",
    "delete_product_relation",
    "get_catalog_settings",
    "get_product_group",
    "get_product_ranking",
    "list_group_items",
    "list_product_groups",
    "list_product_relations",
    "normalize_group_slug",
    "read_best_sellers",
    "read_catalog_groups",
    "read_group_products",
    "read_product_relations",
    "reorder_product_groups",
    "save_catalog_settings",
    "save_product_ranking",
    "update_group_item",
    "update_product_group",
]
