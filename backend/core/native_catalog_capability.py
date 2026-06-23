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
from typing import Any, Optional

from core.catalog import (
    effective_retailer_id,
    effective_variant_retailer_id,
    evaluate_tenant_catalog_send_readiness,
    is_catalog_active,
)

logger = logging.getLogger("nahla.native_catalog")


@dataclass(frozen=True)
class NativeCatalogCapability:
    """Structured outcome of the native-catalog capability gate."""

    eligible: bool
    reason: str
    thumbnail_retailer_id: str = ""
    matchable_product_count: int = 0


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


def _product_has_matchable_retailer_id(product: Any) -> bool:
    rid = effective_variant_retailer_id(product) or effective_retailer_id(product)
    if rid:
        return True
    sku = getattr(product, "sku", None)
    if sku and str(sku).strip():
        return True
    return False


def count_matchable_catalog_products(db: Any, tenant_id: int) -> int:
    """Count active catalog products with a resolvable Meta retailer id."""
    if db is None or not tenant_id:
        return 0
    try:
        from models import Product, ProductVariant  # noqa: PLC0415

        variant_hit = (
            db.query(ProductVariant.id)
            .join(Product, Product.id == ProductVariant.product_id)
            .filter(
                ProductVariant.tenant_id == int(tenant_id),
                Product.tenant_id == int(tenant_id),
                ProductVariant.retailer_id.isnot(None),
                ProductVariant.retailer_id != "",
            )
            .limit(1)
            .first()
        )
        if variant_hit is not None:
            return 1

        q = db.query(Product).filter(Product.tenant_id == int(tenant_id))
        count = 0
        for row in q.limit(200).all():
            if not is_catalog_active(row):
                continue
            if _product_has_matchable_retailer_id(row):
                count += 1
        return count
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[NATIVE_CATALOG] matchable count failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return 0


def pick_thumbnail_retailer_id(db: Any, tenant_id: int) -> str:
    """First resolvable retailer id for catalog_message thumbnail."""
    if db is None or not tenant_id:
        return ""
    try:
        from models import Product, ProductVariant  # noqa: PLC0415

        variant_row = (
            db.query(ProductVariant)
            .join(Product, Product.id == ProductVariant.product_id)
            .filter(
                ProductVariant.tenant_id == int(tenant_id),
                Product.tenant_id == int(tenant_id),
                ProductVariant.retailer_id.isnot(None),
                ProductVariant.retailer_id != "",
            )
            .order_by(ProductVariant.id.asc())
            .first()
        )
        if variant_row is not None:
            parent = getattr(variant_row, "product", None)
            if parent is None or is_catalog_active(parent):
                rid = str(getattr(variant_row, "retailer_id", "") or "").strip()
                if rid:
                    return rid

        for row in (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id))
            .order_by(Product.id.asc())
            .limit(100)
            .all()
        ):
            if not is_catalog_active(row):
                continue
            rid = effective_variant_retailer_id(row) or effective_retailer_id(row)
            if rid:
                return rid
            sku = str(getattr(row, "sku", "") or "").strip()
            if sku:
                return sku
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — thumbnail pick is best-effort
        logger.debug(
            "[NATIVE_CATALOG] thumbnail pick failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return ""


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

    matchable = count_matchable_catalog_products(db, tenant_id)
    if matchable <= 0:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=no_matchable_products",
            tenant_id,
        )
        return NativeCatalogCapability(eligible=False, reason="no_matchable_products")

    thumbnail = pick_thumbnail_retailer_id(db, tenant_id)
    if not thumbnail:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=no_retailer_id",
            tenant_id,
        )
        return NativeCatalogCapability(eligible=False, reason="no_retailer_id")

    return NativeCatalogCapability(
        eligible=True,
        reason="ok",
        thumbnail_retailer_id=thumbnail,
        matchable_product_count=matchable,
    )


__all__ = [
    "NativeCatalogCapability",
    "count_matchable_catalog_products",
    "evaluate_native_catalog_capability",
    "load_whatsapp_connection",
    "pick_thumbnail_retailer_id",
]
