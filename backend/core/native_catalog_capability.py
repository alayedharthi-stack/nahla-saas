"""
core/native_catalog_capability.py
─────────────────────────────────
Phase 1 — platform gate for WhatsApp Native Catalog browse entry.

Decides whether a tenant should receive ``interactive.type=catalog_message``
on general browse instead of CatalogNavigator text lists.

Operational only — no I/O beyond the caller's DB session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from core.catalog import (
    effective_retailer_id,
    effective_variant_retailer_id,
    evaluate_tenant_catalog_send_readiness,
    is_catalog_active,
    is_synthetic_retailer_id,
)

logger = logging.getLogger("nahla.native_catalog")

REASON_SYNTHETIC_RETAILER_ID = "synthetic_retailer_id"
REASON_SKU_ONLY_RETAILER_ID = "sku_only_retailer_id"
REASON_META_CATALOG_UNPUBLISHED = "meta_catalog_unpublished"


@dataclass(frozen=True)
class NativeCatalogCapability:
    """Structured outcome of the native-catalog capability gate."""

    eligible: bool
    reason: str
    thumbnail_retailer_id: str = ""
    matchable_product_count: int = 0


@dataclass(frozen=True)
class _CatalogRetailerInventory:
    active_products: int = 0
    trusted_products: int = 0
    meta_confirmed_products: int = 0
    synthetic_only_products: int = 0
    sku_only_products: int = 0


def load_whatsapp_connection(db: Any, tenant_id: int) -> Any:
    """Return the tenant's WhatsAppConnection row, or None."""
    if db is None or not tenant_id:
        return None
    try:
        from models import WhatsAppConnection  # noqa: PLC0415

        return (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == int(tenant_id))
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[NATIVE_CATALOG] connection lookup failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None


def _is_trusted_meta_retailer_id(retailer_id: str) -> bool:
    """True when *retailer_id* is non-empty and not a Nahla synthetic fallback."""
    rid = str(retailer_id or "").strip()
    if not rid:
        return False
    return not is_synthetic_retailer_id(rid)


def _is_meta_catalog_published(product: Any) -> bool:
    return bool(getattr(product, "meta_catalog_published_at", None))


def _trusted_retailer_id(product: Any) -> str:
    """Resolve a Meta-trusted retailer id — variant first, then parent; never SKU-only."""
    variant_rid = effective_variant_retailer_id(product)
    if _is_trusted_meta_retailer_id(variant_rid):
        return variant_rid
    parent_rid = effective_retailer_id(product)
    if _is_trusted_meta_retailer_id(parent_rid):
        return parent_rid
    return ""


def _meta_confirmed_retailer_id(product: Any) -> str:
    """Trusted retailer id with evidence that Nahla published it to Meta catalog."""
    if not _is_meta_catalog_published(product):
        return ""
    return _trusted_retailer_id(product)


def _classify_product_retailer_source(product: Any) -> str:
    """Return ``trusted``, ``synthetic``, ``sku_only``, or ``none``."""
    variant_rid = effective_variant_retailer_id(product)
    if variant_rid:
        if is_synthetic_retailer_id(variant_rid):
            parent_rid = effective_retailer_id(product)
            if _is_trusted_meta_retailer_id(parent_rid):
                return "trusted"
            return "synthetic"
        return "trusted"
    parent_rid = effective_retailer_id(product)
    if parent_rid:
        if is_synthetic_retailer_id(parent_rid):
            return "synthetic"
        return "trusted"
    sku = str(getattr(product, "sku", "") or "").strip()
    if sku:
        return "sku_only"
    return "none"


def _inventory_from_products(products: Any) -> _CatalogRetailerInventory:
    inv = _CatalogRetailerInventory()
    active = trusted = meta_confirmed = synthetic = sku_only = 0
    for row in products or []:
        if not is_catalog_active(row):
            continue
        active += 1
        source = _classify_product_retailer_source(row)
        if source == "trusted":
            trusted += 1
            if _meta_confirmed_retailer_id(row):
                meta_confirmed += 1
        elif source == "synthetic":
            synthetic += 1
        elif source == "sku_only":
            sku_only += 1
    return _CatalogRetailerInventory(
        active_products=active,
        trusted_products=trusted,
        meta_confirmed_products=meta_confirmed,
        synthetic_only_products=synthetic,
        sku_only_products=sku_only,
    )


def _scan_catalog_retailer_inventory(db: Any, tenant_id: int) -> _CatalogRetailerInventory:
    """Summarise active catalog rows by trusted / meta-confirmed retailer ids."""
    if db is None or not tenant_id:
        return _CatalogRetailerInventory()
    try:
        from models import Product  # noqa: PLC0415

        return _inventory_from_products(
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id))
            .order_by(Product.id.asc())
            .limit(200)
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[NATIVE_CATALOG] inventory scan failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return _CatalogRetailerInventory()


def _ineligibility_reason_from_inventory(inv: _CatalogRetailerInventory) -> str:
    if inv.meta_confirmed_products > 0:
        return "ok"
    if inv.trusted_products > 0:
        return REASON_META_CATALOG_UNPUBLISHED
    if inv.active_products <= 0:
        return "no_matchable_products"
    if inv.synthetic_only_products > 0 and inv.sku_only_products <= 0:
        return REASON_SYNTHETIC_RETAILER_ID
    if inv.sku_only_products > 0 and inv.synthetic_only_products <= 0:
        return REASON_SKU_ONLY_RETAILER_ID
    if inv.synthetic_only_products > 0:
        return REASON_SYNTHETIC_RETAILER_ID
    return "no_retailer_id"


def _iter_meta_confirmed_variant_retailer_ids(
    db: Any,
    tenant_id: int,
    *,
    limit: int = 50,
) -> Iterator[str]:
    if db is None or not tenant_id:
        return
    try:
        from models import Product, ProductVariant  # noqa: PLC0415

        q = (
            db.query(ProductVariant)
            .join(Product, Product.id == ProductVariant.product_id)
            .filter(
                ProductVariant.tenant_id == int(tenant_id),
                Product.tenant_id == int(tenant_id),
                ProductVariant.retailer_id.isnot(None),
                ProductVariant.retailer_id != "",
                ~ProductVariant.retailer_id.like("nahla_p_%"),
                Product.meta_catalog_published_at.isnot(None),
            )
            .order_by(ProductVariant.id.asc())
            .limit(limit)
        )
        for variant_row in q.all():
            parent = getattr(variant_row, "product", None)
            if parent is not None and not is_catalog_active(parent):
                continue
            rid = str(getattr(variant_row, "retailer_id", "") or "").strip()
            if _is_trusted_meta_retailer_id(rid):
                yield rid
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — variant scan is best-effort
        logger.debug(
            "[NATIVE_CATALOG] variant scan failed tenant=%s err=%s",
            tenant_id,
            exc,
        )


def count_matchable_catalog_products(db: Any, tenant_id: int) -> int:
    """Count active catalog products with a Meta-confirmed retailer id."""
    inv = _scan_catalog_retailer_inventory(db, tenant_id)
    return inv.meta_confirmed_products


def pick_thumbnail_retailer_id(db: Any, tenant_id: int) -> str:
    """First Meta-confirmed retailer id for catalog_message thumbnail."""
    if db is None or not tenant_id:
        return ""
    try:
        from models import Product  # noqa: PLC0415

        for rid in _iter_meta_confirmed_variant_retailer_ids(db, tenant_id, limit=50):
            return rid

        for row in (
            db.query(Product)
            .filter(
                Product.tenant_id == int(tenant_id),
                Product.meta_catalog_published_at.isnot(None),
            )
            .order_by(Product.id.asc())
            .limit(100)
            .all()
        ):
            if not is_catalog_active(row):
                continue
            rid = _meta_confirmed_retailer_id(row)
            if rid:
                return rid
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — thumbnail pick is best-effort
        logger.debug(
            "[NATIVE_CATALOG] thumbnail pick failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return ""


def invalidate_meta_catalog_publish_for_retailer_id(
    db: Any,
    tenant_id: int,
    retailer_id: str,
) -> int:
    """Clear publish stamps when Meta rejects a catalog send for *retailer_id*."""
    rid = str(retailer_id or "").strip()
    if db is None or not tenant_id or not rid:
        return 0
    cleared = 0
    try:
        from models import Product  # noqa: PLC0415

        rows = (
            db.query(Product)
            .filter(
                Product.tenant_id == int(tenant_id),
                Product.meta_catalog_published_at.isnot(None),
            )
            .all()
        )
        for row in rows:
            if _trusted_retailer_id(row) != rid and effective_retailer_id(row) != rid:
                continue
            row.meta_catalog_published_at = None
            cleared += 1
        if cleared:
            db.flush()
            logger.info(
                "[NATIVE_CATALOG] publish_stamp_cleared tenant=%s retailer_id=%s count=%d",
                tenant_id,
                rid,
                cleared,
            )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — publish stamp clear is best-effort
        logger.debug(
            "[NATIVE_CATALOG] publish_stamp_clear_failed tenant=%s retailer_id=%s err=%s",
            tenant_id,
            rid,
            exc,
        )
    return cleared


def evaluate_native_catalog_capability(
    db: Any,
    tenant_id: int,
    *,
    connection: Any = None,
) -> NativeCatalogCapability:
    """Return whether native catalog browse entry is allowed for *tenant_id*."""
    conn = connection if connection is not None else load_whatsapp_connection(db, tenant_id)
    if conn is None:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=connection_missing",
            tenant_id,
        )
        return NativeCatalogCapability(eligible=False, reason="connection_missing")

    send_ready = evaluate_tenant_catalog_send_readiness(conn)
    if not send_ready.ready:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=%s",
            tenant_id,
            send_ready.reason,
        )
        return NativeCatalogCapability(eligible=False, reason=send_ready.reason)

    if not bool(getattr(conn, "catalog_enabled", False)):
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=catalog_disabled",
            tenant_id,
        )
        return NativeCatalogCapability(eligible=False, reason="catalog_disabled")

    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    if not catalog_id:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=catalog_id_missing",
            tenant_id,
        )
        return NativeCatalogCapability(eligible=False, reason="catalog_id_missing")

    inventory = _scan_catalog_retailer_inventory(db, tenant_id)
    matchable = inventory.meta_confirmed_products
    if matchable <= 0:
        reason = _ineligibility_reason_from_inventory(inventory)
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=%s "
            "active=%d trusted=%d meta_confirmed=%d synthetic=%d sku_only=%d",
            tenant_id,
            reason,
            inventory.active_products,
            inventory.trusted_products,
            inventory.meta_confirmed_products,
            inventory.synthetic_only_products,
            inventory.sku_only_products,
        )
        return NativeCatalogCapability(eligible=False, reason=reason)

    thumbnail = pick_thumbnail_retailer_id(db, tenant_id)
    if not thumbnail:
        reason = _ineligibility_reason_from_inventory(inventory)
        if reason == "ok":
            reason = REASON_META_CATALOG_UNPUBLISHED
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=%s",
            tenant_id,
            reason,
        )
        return NativeCatalogCapability(eligible=False, reason=reason)

    return NativeCatalogCapability(
        eligible=True,
        reason="ok",
        thumbnail_retailer_id=thumbnail,
        matchable_product_count=matchable,
    )


__all__ = [
    "NativeCatalogCapability",
    "REASON_META_CATALOG_UNPUBLISHED",
    "REASON_SKU_ONLY_RETAILER_ID",
    "REASON_SYNTHETIC_RETAILER_ID",
    "_CatalogRetailerInventory",
    "_classify_product_retailer_source",
    "_ineligibility_reason_from_inventory",
    "_inventory_from_products",
    "_meta_confirmed_retailer_id",
    "_scan_catalog_retailer_inventory",
    "count_matchable_catalog_products",
    "evaluate_native_catalog_capability",
    "invalidate_meta_catalog_publish_for_retailer_id",
    "load_whatsapp_connection",
    "pick_thumbnail_retailer_id",
]
