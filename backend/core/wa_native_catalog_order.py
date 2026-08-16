"""
core/wa_native_catalog_order.py
───────────────────────────────
Phase 1 — parse WhatsApp ``type=order`` payloads into Nahla line_items.

Operational only: deterministic matching against catalog retailer ids.
Never invents ``product_id`` when no match exists.
"""
from __future__ import annotations

import logging
import re
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
    text_line_count: Optional[int] = None
    total_quantity: Optional[int] = None
    total_price: Optional[float] = None
    currency: str = ""
    text_extracted: bool = False


@dataclass
class RetailerMatchResult:
    matched: bool
    match_field: str = ""
    product_id: Optional[int] = None
    variant_id: Optional[int] = None
    product_title: str = ""
    catalog_price: Optional[float] = None
    store_external_id: str = ""


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


_TEXT_LINE_COUNT_RE = re.compile(
    r"عدد\s*(?:أسطر|اسطر|سطور|الأسطر|الاسطر|المنتجات|الأصناف|الاصناف)(?:\s*الطلب)?\s*[:：]\s*(\d+)",
    re.I | re.UNICODE,
)
_TEXT_TOTAL_QTY_RE = re.compile(
    r"(?:إجمالي|اجمالي)\s*الكمية\s*[:：]\s*(\d+)",
    re.I | re.UNICODE,
)
_TEXT_TOTAL_RE = re.compile(
    r"(?:الإجمالي|الاجمالي|إجمالي\s*الطلب|اجمالي\s*الطلب)\s*[:：]\s*([0-9]+(?:[\.,][0-9]+)?)\s*([A-Z]{3}|ر\.?س|ريال)?",
    re.I | re.UNICODE,
)
_TEXT_SKU_RE = re.compile(
    r"(?:رمز\s*المنتج\s*\(?\s*SKU\s*\)?|SKU)\s*[:：]\s*([A-Za-z0-9_.-]+)",
    re.I | re.UNICODE,
)


def extract_catalog_order_text_facts(text: str) -> Dict[str, Any]:
    """Extract operational catalog-order facts from WhatsApp fallback text."""
    raw = str(text or "")
    facts: Dict[str, Any] = {}
    line_match = _TEXT_LINE_COUNT_RE.search(raw)
    if line_match:
        facts["catalog_order_line_count"] = _as_int(line_match.group(1), default=0)
    qty_match = _TEXT_TOTAL_QTY_RE.search(raw)
    if qty_match:
        facts["total_quantity"] = _as_int(qty_match.group(1), default=0)
    total_match = _TEXT_TOTAL_RE.search(raw)
    if total_match:
        facts["catalog_total"] = _as_float(str(total_match.group(1)).replace(",", "."))
        currency = str(total_match.group(2) or "").strip()
        if currency:
            facts["catalog_currency"] = "SAR" if currency in {"ر.س", "رس", "ريال"} else currency
    skus = [m.group(1).strip() for m in _TEXT_SKU_RE.finditer(raw) if m.group(1).strip()]
    if skus:
        facts["catalog_skus"] = skus
    return facts


def _product_items_from_text(text: str) -> List[Dict[str, Any]]:
    facts = extract_catalog_order_text_facts(text)
    skus = list(facts.get("catalog_skus") or [])
    if not skus:
        return []
    total_qty = int(facts.get("total_quantity") or 0)
    total = _as_float(facts.get("catalog_total"))
    currency = str(facts.get("catalog_currency") or "").strip()
    items: List[Dict[str, Any]] = []
    for sku in skus:
        qty = total_qty if len(skus) == 1 and total_qty > 0 else 1
        item_price = (total / qty) if total is not None and qty > 0 and len(skus) == 1 else None
        items.append({
            "product_retailer_id": sku,
            "quantity": qty,
            "item_price": item_price,
            "currency": currency,
        })
    return items


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
    source_text = ""
    if metadata:
        if not payload.get("product_items") and metadata.get("product_items"):
            payload["product_items"] = metadata.get("product_items")
        if not payload.get("catalog_id") and metadata.get("catalog_id"):
            payload["catalog_id"] = metadata.get("catalog_id")
        if not payload.get("text") and metadata.get("customer_note"):
            payload["text"] = metadata.get("customer_note")
        for key in ("_catalog_order_message", "inbound_text", "message", "text", "caption", "body"):
            val = metadata.get(key)
            if isinstance(val, str) and val.strip():
                source_text = val
                break

    if not payload.get("product_items") and source_text:
        text_items = _product_items_from_text(source_text)
        if text_items:
            payload["product_items"] = text_items

    raw_items = payload.get("product_items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    text_facts = extract_catalog_order_text_facts(source_text)

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
        text_line_count=text_facts.get("catalog_order_line_count"),
        total_quantity=text_facts.get("total_quantity"),
        total_price=text_facts.get("catalog_total"),
        currency=str(text_facts.get("catalog_currency") or "").strip(),
        text_extracted=bool(source_text and text_facts),
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
                store_external_id=str(getattr(product, "external_id", "") or "").strip(),
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
        store_external_id=str(getattr(product, "external_id", "") or "").strip(),
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
                "from_native_catalog_order": True,
            }
            store_ext = str(match.store_external_id or "").strip()
            if store_ext:
                raw["external_id"] = store_ext
                raw["salla_product_id"] = store_ext
                raw["store_external_id"] = store_ext
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
    prep.catalog_line_items_authoritative = True
    if not str(getattr(prep, "checkout_channel", "") or "").strip():
        prep.checkout_channel = "whatsapp_catalog"

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
    total_qty = sum(int(li.get("quantity") or 1) for li in resolution.line_items)
    if total_qty > 0:
        prep.quantity = total_qty
    elif first_confirmed.get("product_id"):
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
        "product_retailer_id": (first or {}).get("product_retailer_id") or "",
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
    try:
        from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: PLC0415
            stamp_structured_presented_products,
        )

        stamp_structured_presented_products(
            state,
            list(resolution.line_items),
            provenance="catalog_order_selected",
            customer_selected=True,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — referent stamp must not block catalog order
        logger.debug("[WA_NATIVE_ORDER] presented_referent_stamp_skipped", exc_info=True)
    return resolution


def persist_structured_catalog_order_referent(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    conversation: Any = None,
) -> bool:
    """Stamp native catalog-order identity onto persisted brain state.

    Used when the inbound is persist-only (empty customer note) so Brain
    never runs — the structured referent must still survive to the next turn.
    """
    meta = dict(inbound_metadata or {})
    if meta.get("source_type") != "catalog_order" and not (
        meta.get("product_items") or meta.get("order")
    ):
        return False
    if db is None or not tenant_id or not (phone or conversation is not None):
        return False
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from modules.ai.brain.types import MerchantConversationState  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        conv = conversation
        bs: Dict[str, Any] = {}
        if conv is None:
            conv, bs = _load_brain_state(
                db, tenant_id=int(tenant_id), phone=str(phone or ""),
            )
        else:
            extra = dict(getattr(conv, "extra_metadata", None) or {})
            raw_bs = extra.get("brain_state") or {}
            bs = dict(raw_bs) if isinstance(raw_bs, dict) else {}
        if conv is None:
            return False
        payload = parse_native_catalog_order(
            {
                "catalog_id": meta.get("catalog_id"),
                "text": meta.get("customer_note"),
                "product_items": meta.get("product_items") or [],
            },
            metadata=meta,
        )
        if not payload.items:
            return False
        state = MerchantConversationState.from_dict(bs if isinstance(bs, dict) else {})
        apply_native_order_to_state(
            db=db,
            tenant_id=int(tenant_id),
            state=state,
            payload=payload,
        )
        updated = dict(bs or {})
        updated["last_presented_products"] = list(
            getattr(state, "last_presented_products", None) or []
        )
        updated["current_product_focus"] = getattr(state, "current_product_focus", None)
        updated["cart_items"] = list(getattr(state, "cart_items", None) or [])
        updated["stage"] = getattr(state, "stage", None) or updated.get("stage")
        prep = getattr(state, "order_prep", None)
        if prep is not None:
            updated["order_prep"] = prep.to_dict() if hasattr(prep, "to_dict") else dict(prep)
        extra_meta = dict(getattr(conv, "extra_metadata", None) or {})
        extra_meta["brain_state"] = updated
        conv.extra_metadata = extra_meta
        flag_modified(conv, "extra_metadata")
        db.add(conv)
        db.flush()
        logger.info(
            "[WA_NATIVE_ORDER] persist_only_referent_stamped tenant=%s "
            "presented=%d focus=%r",
            tenant_id,
            len(updated.get("last_presented_products") or []),
            (updated.get("current_product_focus") or {}).get("title")
            if isinstance(updated.get("current_product_focus"), dict)
            else None,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "[WA_NATIVE_ORDER] persist_only_referent_stamp_failed tenant=%s",
            tenant_id,
        )
        return False


__all__ = [
    "NativeCatalogOrderItem",
    "NativeCatalogOrderPayload",
    "NativeOrderLineItemsResult",
    "RetailerMatchResult",
    "apply_native_order_to_state",
    "build_line_items_from_payload",
    "extract_catalog_order_text_facts",
    "match_retailer_id",
    "parse_native_catalog_order",
    "persist_structured_catalog_order_referent",
]
