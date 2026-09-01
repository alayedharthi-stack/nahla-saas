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
from core.meta_catalog_membership import (
    count_memberships_for_catalog,
    first_membership_retailer_id,
    invalidate_meta_catalog_membership,
    membership_authorizes_send,
)

logger = logging.getLogger("nahla.native_catalog")

REASON_SYNTHETIC_RETAILER_ID = "synthetic_retailer_id"
REASON_SKU_ONLY_RETAILER_ID = "sku_only_retailer_id"
REASON_META_CATALOG_UNPUBLISHED = "meta_catalog_unpublished"
REASON_META_CATALOG_UNVERIFIED = "meta_catalog_unverified"
REASON_CATALOG_ID_MISMATCH = "catalog_id_mismatch"
REASON_CATALOG_ID_MISSING = "catalog_id_missing"
REASON_VARIANT_MAPPING_MISSING = "variant_mapping_missing"


@dataclass(frozen=True)
class NativeCatalogProductCapability:
    """Per-referent native catalog send eligibility.

    ``available`` is true only when the exact product/variant has
    authoritative Meta membership evidence for the requested catalog.
    ``external_id`` / copied ``meta_retailer_id`` alone is not enough.
    """

    available: bool
    catalog_id: str = ""
    retailer_id: str = ""
    product_id: Optional[int] = None
    variant_id: Optional[int] = None
    mapping_status: str = "unverified"
    provenance: str = "none"
    reason: str = REASON_META_CATALOG_UNVERIFIED


@dataclass(frozen=True)
class NativeCatalogCapability:
    """Structured outcome of the native-catalog capability gate."""

    eligible: bool
    reason: str
    thumbnail_retailer_id: str = ""
    matchable_product_count: int = 0
    catalog_id: str = ""


@dataclass(frozen=True)
class _CatalogRetailerInventory:
    active_products: int = 0
    trusted_products: int = 0
    meta_confirmed_products: int = 0
    synthetic_only_products: int = 0
    sku_only_products: int = 0


def _resolve_operational_bind(db: Any) -> Any:
    """Return the engine/bind backing *db* without using global SessionLocal."""
    if db is None:
        return None
    getter = getattr(db, "get_bind", None)
    if callable(getter):
        try:
            bind = getter()
            if bind is not None:
                return bind
        except Exception:  # noqa: BLE001  # noqa: silent-ok — bind resolution is best-effort
            pass
    return getattr(db, "bind", None)


def _open_isolated_read_session(db: Any) -> Any:
    bind = _resolve_operational_bind(db)
    if bind is None:
        return None
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    return sessionmaker(bind=bind, autoflush=False, autocommit=False)()


def load_whatsapp_connection(db: Any, tenant_id: int) -> Any:
    """Read committed WhatsApp configuration on a caller-bound owned session.

    This optional capability probe intentionally does not observe uncommitted
    changes on the caller session. Its isolation boundary protects the caller's
    operational transaction from PostgreSQL aborted-transaction cascades.
    """
    if db is None or not tenant_id:
        return None

    read_db = _open_isolated_read_session(db)
    if read_db is None:
        return None

    try:
        from models import WhatsAppConnection  # noqa: PLC0415

        return (
            read_db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == int(tenant_id))
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[NATIVE_CATALOG] connection lookup failed tenant=%s error_type=%s",
            tenant_id,
            type(exc).__name__,
        )
        try:
            read_db.rollback()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — isolated rollback must not poison caller
            pass
        return None
    finally:
        try:
            read_db.close()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — isolated close must not poison caller
            pass


def _is_trusted_meta_retailer_id(retailer_id: str) -> bool:
    """True when *retailer_id* is non-empty and not a Nahla synthetic fallback."""
    rid = str(retailer_id or "").strip()
    if not rid:
        return False
    return not is_synthetic_retailer_id(rid)


def _is_meta_catalog_published(product: Any) -> bool:
    if isinstance(product, dict):
        return bool(product.get("meta_catalog_published_at"))
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


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _variant_retailer_id(variant: Any) -> str:
    if variant is None:
        return ""
    if isinstance(variant, dict):
        return str(variant.get("retailer_id") or "").strip()
    return str(getattr(variant, "retailer_id", "") or "").strip()


def evaluate_native_catalog_product_capability(
    product: Any,
    *,
    catalog_id: str,
    variant: Any = None,
    membership: Any = None,
    intended_retailer_id: str = "",
    tenant_id: Optional[int] = None,
) -> NativeCatalogProductCapability:
    """Return native-catalog send capability for one canonical referent.

    Fail closed unless a canonical ``MetaCatalogMembership`` fact matches
    tenant + catalog + retailer_id + product (+ exact variant when selected).
    ``meta_catalog_published_at`` and ``external_id`` are not authorization.
    Does not substitute a same-title sibling or a different variant SKU.
    """
    requested = str(catalog_id or "").strip()
    product_id = _optional_int(
        product.get("id") if isinstance(product, dict) else getattr(product, "id", None)
    )
    row_tenant = tenant_id
    if row_tenant is None:
        raw_tid = (
            product.get("tenant_id")
            if isinstance(product, dict)
            else getattr(product, "tenant_id", None)
        )
        row_tenant = _optional_int(raw_tid) or 0
    explicit_variant = variant is not None
    variant_id = None
    if explicit_variant:
        variant_id = _optional_int(
            variant.get("id") if isinstance(variant, dict) else getattr(variant, "id", None)
        )
        rid = _variant_retailer_id(variant)
        if not rid:
            return NativeCatalogProductCapability(
                available=False,
                catalog_id=requested,
                product_id=product_id,
                variant_id=variant_id,
                mapping_status="missing",
                provenance="none",
                reason=REASON_VARIANT_MAPPING_MISSING,
            )
    else:
        rid = str(intended_retailer_id or "").strip() or _trusted_retailer_id(product)
        if not rid:
            rid = str(
                (
                    product.get("external_id")
                    if isinstance(product, dict)
                    else getattr(product, "external_id", "")
                    or ""
                )
            ).strip()

    if not requested:
        return NativeCatalogProductCapability(
            available=False,
            product_id=product_id,
            variant_id=variant_id,
            retailer_id=rid,
            mapping_status="unverified",
            provenance="none",
            reason=REASON_CATALOG_ID_MISSING,
        )

    fact_catalog = str(getattr(membership, "catalog_id", "") or "").strip() if membership is not None else ""
    if fact_catalog and fact_catalog != requested:
        return NativeCatalogProductCapability(
            available=False,
            catalog_id=requested,
            retailer_id=rid,
            product_id=product_id,
            variant_id=variant_id,
            mapping_status="catalog_mismatch",
            provenance="none",
            reason=REASON_CATALOG_ID_MISMATCH,
        )

    if rid and is_synthetic_retailer_id(rid):
        return NativeCatalogProductCapability(
            available=False,
            catalog_id=requested,
            retailer_id=rid,
            product_id=product_id,
            variant_id=variant_id,
            mapping_status="synthetic",
            provenance="none",
            reason=REASON_SYNTHETIC_RETAILER_ID,
        )

    has_variants = bool(
        product.get("has_variants")
        if isinstance(product, dict)
        else getattr(product, "has_variants", False)
    )
    default_variant_id = _optional_int(
        product.get("default_variant_id")
        if isinstance(product, dict)
        else getattr(product, "default_variant_id", None)
    )
    authorized = membership_authorizes_send(
        membership,
        tenant_id=int(row_tenant or 0),
        catalog_id=requested,
        retailer_id=rid,
        product_id=product_id,
        bound_variant_id=variant_id,
        explicit_variant=explicit_variant,
        product_has_variants=has_variants,
        canonical_default_variant_id=default_variant_id,
    )
    if authorized:
        return NativeCatalogProductCapability(
            available=True,
            catalog_id=requested,
            retailer_id=rid,
            product_id=product_id,
            variant_id=variant_id,
            mapping_status="verified",
            provenance=str(getattr(membership, "provenance", "") or "meta_graph_reconcile"),
            reason="ok",
        )

    parent_rid = rid or _trusted_retailer_id(product)
    status = "synthetic" if is_synthetic_retailer_id(parent_rid) else "unverified"
    reason = (
        REASON_SYNTHETIC_RETAILER_ID
        if status == "synthetic"
        else REASON_META_CATALOG_UNVERIFIED
    )
    if not parent_rid:
        status = "missing"
        reason = REASON_META_CATALOG_UNVERIFIED
    if explicit_variant and not _variant_retailer_id(variant):
        status = "missing"
        reason = REASON_VARIANT_MAPPING_MISSING
    return NativeCatalogProductCapability(
        available=False,
        catalog_id=requested,
        retailer_id=parent_rid,
        product_id=product_id,
        variant_id=variant_id,
        mapping_status=status,
        provenance="none",
        reason=reason,
    )


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


def count_matchable_catalog_products(
    db: Any,
    tenant_id: int,
    catalog_id: str = "",
) -> int:
    """Count canonical Meta memberships for the connected catalog."""
    cid = str(catalog_id or "").strip()
    if not cid:
        conn = load_whatsapp_connection(db, tenant_id)
        cid = str(getattr(conn, "meta_catalog_id", "") or "").strip() if conn is not None else ""
    if not cid:
        return 0
    return count_memberships_for_catalog(db, tenant_id=int(tenant_id), catalog_id=cid)


def pick_thumbnail_retailer_id(
    db: Any,
    tenant_id: int,
    catalog_id: str = "",
) -> str:
    """First canonical membership retailer id for catalog_message thumbnail."""
    cid = str(catalog_id or "").strip()
    if not cid:
        conn = load_whatsapp_connection(db, tenant_id)
        cid = str(getattr(conn, "meta_catalog_id", "") or "").strip() if conn is not None else ""
    if not cid:
        return ""
    return first_membership_retailer_id(db, tenant_id=int(tenant_id), catalog_id=cid)


def invalidate_meta_catalog_publish_for_retailer_id(
    db: Any,
    tenant_id: int,
    retailer_id: str,
    *,
    catalog_id: str = "",
) -> int:
    """Invalidate exact catalog membership after Meta products-not-found.

    Requires catalog_id. Does not clear sibling SKUs or other catalogs.
    Dashboard stamp is derived inside the membership owner.
    """
    cid = str(catalog_id or "").strip()
    if not cid:
        return 0
    return invalidate_meta_catalog_membership(
        db,
        tenant_id=int(tenant_id),
        catalog_id=cid,
        retailer_id=str(retailer_id or "").strip(),
    )


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
    matchable = count_matchable_catalog_products(db, tenant_id, catalog_id)
    if matchable <= 0:
        reason = _ineligibility_reason_from_inventory(inventory)
        if reason == "ok":
            reason = REASON_META_CATALOG_UNPUBLISHED
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=%s "
            "active=%d trusted=%d meta_confirmed=%d synthetic=%d sku_only=%d",
            tenant_id,
            reason,
            inventory.active_products,
            inventory.trusted_products,
            matchable,
            inventory.synthetic_only_products,
            inventory.sku_only_products,
        )
        return NativeCatalogCapability(eligible=False, reason=reason)

    thumbnail = pick_thumbnail_retailer_id(db, tenant_id, catalog_id)
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
        catalog_id=catalog_id,
    )


def whatsapp_native_order_ready(db: Any, tenant_id: int) -> bool:
    """True when native WhatsApp catalog order capability is eligible.

    Same evidence ``MerchantCapabilities.has_whatsapp_catalog`` uses.
    Fail closed when db/tenant is missing or the probe errors.
    """
    if db is None or not tenant_id:
        return False
    try:
        return bool(evaluate_native_catalog_capability(db, int(tenant_id)).eligible)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — capability probe must fail closed
        return False


__all__ = [
    "NativeCatalogCapability",
    "NativeCatalogProductCapability",
    "REASON_CATALOG_ID_MISMATCH",
    "REASON_CATALOG_ID_MISSING",
    "REASON_META_CATALOG_UNPUBLISHED",
    "REASON_META_CATALOG_UNVERIFIED",
    "REASON_SKU_ONLY_RETAILER_ID",
    "REASON_SYNTHETIC_RETAILER_ID",
    "REASON_VARIANT_MAPPING_MISSING",
    "_CatalogRetailerInventory",
    "_classify_product_retailer_source",
    "_ineligibility_reason_from_inventory",
    "_inventory_from_products",
    "_meta_confirmed_retailer_id",
    "_scan_catalog_retailer_inventory",
    "count_matchable_catalog_products",
    "evaluate_native_catalog_capability",
    "whatsapp_native_order_ready",
    "evaluate_native_catalog_product_capability",
    "invalidate_meta_catalog_publish_for_retailer_id",
    "load_whatsapp_connection",
    "pick_thumbnail_retailer_id",
]
