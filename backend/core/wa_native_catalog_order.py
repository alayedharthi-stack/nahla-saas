"""
core/wa_native_catalog_order.py
───────────────────────────────
Phase 1 — parse WhatsApp ``type=order`` payloads into Nahla line_items.

Operational only: deterministic matching against catalog retailer ids.
Never invents ``product_id`` when no match exists.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.wa_cart_line_items import (
    ITEM_STATUS_CONFIRMED,
    ITEM_STATUS_NEEDS_REVIEW,
    normalize_line_item,
)

logger = logging.getLogger("nahla.wa_native_catalog_order")


@dataclass(frozen=True)
class NativeCatalogOrderItem:
    product_retailer_id: str
    quantity: int
    item_price: Optional[float]
    currency: str
    name: str = ""


@dataclass
class NativeCatalogOrderPayload:
    catalog_id: str
    customer_note: str
    items: List[NativeCatalogOrderItem] = field(default_factory=list)
    raw_product_items: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RetailerMatchResult:
    matched: bool
    match_field: str = ""
    product_id: Optional[int] = None
    variant_id: Optional[int] = None
    product_title: str = ""
    catalog_price: Optional[float] = None


@dataclass
class NativeOrderLineItemsResult:
    line_items: List[Dict[str, Any]]
    matched_count: int = 0
    unmatched_count: int = 0
    needs_review_count: int = 0
    price_mismatch_count: int = 0


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, *, default: int = 1) -> int:
    try:
        n = int(float(value))
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _extract_item_name(item: Dict[str, Any]) -> str:
    for key in (
        "name",
        "title",
        "product_name",
        "product_title",
        "retailer_name",
        "label",
    ):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for sub_key in ("product", "catalog_item", "item"):
        sub = item.get(sub_key)
        if isinstance(sub, dict):
            for key in ("name", "title", "product_name"):
                val = sub.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def parse_native_catalog_order(
    order_payload: Dict[str, Any],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> NativeCatalogOrderPayload:
    """Parse raw WhatsApp order block (+ optional normalizer metadata)."""
    payload = dict(order_payload or {})
    if metadata:
        if not payload.get("product_items") and metadata.get("product_items"):
            payload["product_items"] = metadata.get("product_items")
        if not payload.get("catalog_id") and metadata.get("catalog_id"):
            payload["catalog_id"] = metadata.get("catalog_id")
        if not payload.get("text") and metadata.get("customer_note"):
            payload["text"] = metadata.get("customer_note")

    raw_items = payload.get("product_items") or []
    if not isinstance(raw_items, list):
        raw_items = []

    items: List[NativeCatalogOrderItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("product_retailer_id") or "").strip()
        if not rid:
            continue
        items.append(
            NativeCatalogOrderItem(
                product_retailer_id=rid,
                quantity=_as_int(raw.get("quantity"), default=1),
                item_price=_as_float(raw.get("item_price")),
                currency=str(raw.get("currency") or "").strip(),
                name=_extract_item_name(raw),
            )
        )

    return NativeCatalogOrderPayload(
        catalog_id=str(payload.get("catalog_id") or "").strip(),
        customer_note=str(payload.get("text") or "").strip(),
        items=items,
        raw_product_items=[dict(x) for x in raw_items if isinstance(x, dict)],
    )


def match_retailer_id(
    db: Any,
    tenant_id: int,
    retailer_id: str,
) -> RetailerMatchResult:
    """Match ``product_retailer_id`` against Nahla catalog identifiers."""
    rid = str(retailer_id or "").strip()
    if not rid or db is None or not tenant_id:
        return RetailerMatchResult(matched=False)

    try:
        from models import Product, ProductVariant  # noqa: PLC0415

        variant = (
            db.query(ProductVariant)
            .join(Product, Product.id == ProductVariant.product_id)
            .filter(
                ProductVariant.tenant_id == int(tenant_id),
                Product.tenant_id == int(tenant_id),
                ProductVariant.retailer_id == rid,
            )
            .first()
        )
        if variant is not None:
            product = getattr(variant, "product", None) or (
                db.query(Product)
                .filter(Product.id == variant.product_id, Product.tenant_id == int(tenant_id))
                .first()
            )
            price = _as_float(getattr(variant, "price", None) or getattr(product, "price", None))
            return RetailerMatchResult(
                matched=True,
                match_field="variant.retailer_id",
                product_id=int(getattr(product, "id", 0) or 0) or None,
                variant_id=int(getattr(variant, "id", 0) or 0) or None,
                product_title=str(getattr(product, "title", "") or ""),
                catalog_price=price,
            )

        product = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id), Product.meta_retailer_id == rid)
            .first()
        )
        if product is not None:
            return _product_match_result(db, tenant_id, product, "product.meta_retailer_id")

        product = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id), Product.external_id == rid)
            .first()
        )
        if product is not None:
            return _product_match_result(db, tenant_id, product, "product.external_id")

        product = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id), Product.sku == rid)
            .first()
        )
        if product is not None:
            return _product_match_result(db, tenant_id, product, "product.sku")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WA_NATIVE_ORDER] match failed tenant=%s retailer_id=%r err=%s",
            tenant_id,
            rid,
            exc,
        )

    return RetailerMatchResult(matched=False)


def _product_match_result(
    db: Any,
    tenant_id: int,
    product: Any,
    match_field: str,
) -> RetailerMatchResult:
    from models import ProductVariant  # noqa: PLC0415

    variant_id = getattr(product, "default_variant_id", None)
    variant = None
    if variant_id:
        variant = (
            db.query(ProductVariant)
            .filter(
                ProductVariant.id == variant_id,
                ProductVariant.tenant_id == int(tenant_id),
            )
            .first()
        )
    price = _as_float(
        getattr(variant, "price", None) if variant is not None else None
    ) or _as_float(getattr(product, "price", None))
    return RetailerMatchResult(
        matched=True,
        match_field=match_field,
        product_id=int(getattr(product, "id", 0) or 0) or None,
        variant_id=int(getattr(variant, "id", 0) or 0) if variant is not None else None,
        product_title=str(getattr(product, "title", "") or ""),
        catalog_price=price,
    )


def _price_mismatch(
    wa_price: Optional[float],
    catalog_price: Optional[float],
) -> bool:
    if wa_price is None or catalog_price is None:
        return False
    return abs(wa_price - catalog_price) > 0.01


def build_line_items_from_payload(
    db: Any,
    tenant_id: int,
    payload: NativeCatalogOrderPayload,
) -> NativeOrderLineItemsResult:
    """Convert parsed native order items into normalized Nahla line_items."""
    line_items: List[Dict[str, Any]] = []
    matched = unmatched = needs_review = price_mismatch = 0

    for item in payload.items or []:
        match = match_retailer_id(db, tenant_id, item.product_retailer_id)
        price_flag = _price_mismatch(item.item_price, match.catalog_price)

        if match.matched and match.product_id:
            matched += 1
            status = ITEM_STATUS_CONFIRMED
            if price_flag:
                needs_review += 1
                status = ITEM_STATUS_NEEDS_REVIEW
                price_mismatch += 1
            raw = {
                "product_id": str(match.product_id),
                "variant_id": str(match.variant_id or ""),
                "product_name": item.name or match.product_title or item.product_retailer_id,
                "title": item.name or match.product_title or item.product_retailer_id,
                "quantity": item.quantity,
                "unit_price": item.item_price,
                "price": item.item_price,
                "currency": item.currency,
                "product_retailer_id": item.product_retailer_id,
                "match_status": status,
                "match_field": match.match_field,
                "source": "whatsapp_native_catalog_order",
                "price_mismatch": price_flag,
            }
        else:
            unmatched += 1
            needs_review += 1
            raw = {
                "product_name": item.name or item.product_retailer_id,
                "title": item.name or item.product_retailer_id,
                "quantity": item.quantity,
                "unit_price": item.item_price,
                "price": item.item_price,
                "currency": item.currency,
                "product_retailer_id": item.product_retailer_id,
                "match_status": ITEM_STATUS_NEEDS_REVIEW,
                "source": "whatsapp_native_catalog_order",
                "price_mismatch": False,
            }
            line_items.append(normalize_line_item(raw, source="whatsapp_native_catalog_order"))
            continue

        line_item = normalize_line_item(raw, source="whatsapp_native_catalog_order")
        if price_flag:
            line_item["price_mismatch"] = True
        line_item["match_field"] = match.match_field
        line_items.append(line_item)

    logger.info(
        "[WA_NATIVE_ORDER] line_items_matched=%d line_items_unmatched=%d "
        "needs_review_count=%d price_mismatch_count=%d tenant=%s items=%d",
        matched,
        unmatched,
        needs_review,
        price_mismatch,
        tenant_id,
        len(line_items),
    )
    return NativeOrderLineItemsResult(
        line_items=line_items,
        matched_count=matched,
        unmatched_count=unmatched,
        needs_review_count=needs_review,
        price_mismatch_count=price_mismatch,
    )


def apply_native_order_to_state(
    *,
    db: Any,
    tenant_id: int,
    state: Any,
    payload: NativeCatalogOrderPayload,
) -> NativeOrderLineItemsResult:
    """Stamp ``order_prep`` / ``cart_items`` from a native catalog order."""
    resolution = build_line_items_from_payload(db, tenant_id, payload)
    if not resolution.line_items:
        return resolution

    prep = getattr(state, "order_prep", None)
    if prep is None:
        from modules.ai.brain.types import OrderPreparationState  # noqa: PLC0415

        prep = OrderPreparationState()
        state.order_prep = prep

    prep.line_items = list(resolution.line_items)
    state.cart_items = list(resolution.line_items)

    first_confirmed = next(
        (
            li
            for li in resolution.line_items
            if str(li.get("match_status") or "") == ITEM_STATUS_CONFIRMED
            and li.get("product_id")
        ),
        resolution.line_items[0],
    )
    if first_confirmed.get("product_id"):
        prep.product_id = str(first_confirmed.get("product_id"))
        prep.quantity = int(first_confirmed.get("quantity") or 1)
    if payload.customer_note and not getattr(prep, "address_line", ""):
        prep.address_line = payload.customer_note

    total = 0.0
    currency = ""
    for item in payload.items or []:
        if item.item_price is not None:
            total += float(item.item_price) * int(item.quantity or 1)
        if item.currency:
            currency = item.currency
    if total > 0:
        prep.catalog_checkout_total = total
        prep.catalog_checkout_currency = currency or "SAR"

    if not getattr(state, "stage", "") or str(getattr(state, "stage", "") or "") in {
        "",
        "discovery",
        "deciding",
    }:
        state.stage = "ordering"
    if not getattr(getattr(state, "order_prep", None), "order_status", ""):
        prep.order_status = "awaiting_address"

    first = next(
        (li for li in resolution.line_items if li.get("product_id")),
        resolution.line_items[0] if resolution.line_items else None,
    )
    state.current_product_focus = {
        "id": (first or {}).get("product_id") or (first or {}).get("product_retailer_id"),
        "external_id": (first or {}).get("product_retailer_id") or "",
        "title": (first or {}).get("product_name") or (first or {}).get("title") or "",
        "price": (first or {}).get("unit_price") or (first or {}).get("price"),
        "currency": (first or {}).get("currency") or "",
        "from_catalog_order": True,
        "from_native_catalog_order": True,
        "line_items_count": len(resolution.line_items),
        "is_multi_item": len(resolution.line_items) > 1,
    }

    logger.info(
        "[WA_NATIVE_ORDER] native_draft_order_created tenant=%s line_items=%d "
        "native_catalog_order_to_checkout=true",
        tenant_id,
        len(resolution.line_items),
    )
    return resolution


__all__ = [
    "NativeCatalogOrderItem",
    "NativeCatalogOrderPayload",
    "NativeOrderLineItemsResult",
    "RetailerMatchResult",
    "apply_native_order_to_state",
    "build_line_items_from_payload",
    "match_retailer_id",
    "parse_native_catalog_order",
]
