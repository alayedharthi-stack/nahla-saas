"""
core/wa_order_line_item_evidence.py
────────────────────────────────────
Deterministic catalog evidence for WhatsApp order line items.

Dashboard/operations only — never mark an item ``confirmed`` without
product_id, catalog name, positive price, and variant when required.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MATCH_STATUS_CONFIRMED = "confirmed"
MATCH_STATUS_NEEDS_REVIEW = "needs_review"
MATCH_STATUS_NEEDS_VARIANT = "needs_variant"
MATCH_STATUS_CUSTOM_UNMATCHED = "custom_unmatched_item"


def parse_unit_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        amt = float(value)
        return amt if amt > 0 else None
    if isinstance(value, dict):
        from core.salla_order_fidelity import extract_salla_money_amount  # noqa: PLC0415

        parsed = extract_salla_money_amount(value)
        if parsed is None:
            return None
        try:
            amt = float(str(parsed).replace(",", ""))
        except (TypeError, ValueError):
            return None
        return amt if amt > 0 else None
    text = str(value).replace("ر.س", "").replace("SAR", "").replace(",", "").strip()
    if not text:
        return None
    try:
        amt = float(text.split()[0])
    except (TypeError, ValueError):
        return None
    return amt if amt > 0 else None


def _product_media(product: Any) -> Dict[str, str]:
    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "image_url": str(meta.get("image_url") or meta.get("thumbnail") or "").strip(),
        "product_url": str(meta.get("product_url") or meta.get("url") or "").strip(),
    }


def resolve_catalog_product(db: Any, tenant_id: int, product_ref: str) -> Optional[Any]:
    from models import Product  # noqa: PLC0415

    ref = str(product_ref or "").strip()
    if not ref:
        return None
    q = db.query(Product).filter(Product.tenant_id == tenant_id)
    if ref.isdigit():
        hit = q.filter(Product.id == int(ref)).first()
        if hit:
            return hit
    return q.filter((Product.external_id == ref) | (Product.sku == ref)).first()


def resolve_catalog_variant(db: Any, product: Any, variant_ref: str) -> Optional[Any]:
    from models import ProductVariant  # noqa: PLC0415

    vid = str(variant_ref or "").strip()
    if not vid:
        return None
    base = db.query(ProductVariant).filter(ProductVariant.product_id == product.id)
    if vid.isdigit():
        hit = base.filter(ProductVariant.id == int(vid)).first()
        if hit:
            return hit
    return base.filter(
        (ProductVariant.salla_variant_id == vid)
        | (ProductVariant.retailer_id == vid)
        | (ProductVariant.sku == vid)
    ).first()


def sellable_variant_count(db: Any, product: Any) -> int:
    from models import ProductVariant  # noqa: PLC0415

    rows = (
        db.query(ProductVariant)
        .filter(ProductVariant.product_id == product.id)
        .all()
    )
    if not rows:
        return 0
    real = [r for r in rows if not getattr(r, "is_default", False)]
    if len(real) >= 2:
        return len(real)
    if len(rows) >= 2:
        return len(rows)
    return max(len(real), len(rows))


def product_requires_variant_selection(db: Any, product: Any) -> bool:
    if bool(getattr(product, "has_variants", False)):
        return True
    return sellable_variant_count(db, product) > 1


def compute_match_status(
    item: Dict[str, Any],
    *,
    product: Any = None,
    variant_row: Any = None,
    requires_variant: bool = False,
) -> str:
    pid = str(item.get("product_id") or "").strip()
    if not pid:
        return MATCH_STATUS_CUSTOM_UNMATCHED

    vid = str(item.get("variant_id") or "").strip()
    variant_text = str(item.get("variant") or item.get("variant_label") or "").strip()

    if requires_variant and not vid and not variant_row:
        return MATCH_STATUS_NEEDS_VARIANT

    price = parse_unit_price(item.get("unit_price") or item.get("price"))
    if price is None:
        return MATCH_STATUS_NEEDS_REVIEW

    if product is None:
        return MATCH_STATUS_NEEDS_REVIEW

    catalog_name = str(getattr(product, "title", "") or "").strip()
    if not catalog_name:
        return MATCH_STATUS_NEEDS_REVIEW

    if requires_variant and not vid and not variant_text:
        return MATCH_STATUS_NEEDS_VARIANT

    return MATCH_STATUS_CONFIRMED


def sanitize_line_item_without_db(item: Dict[str, Any]) -> Dict[str, Any]:
    """Downgrade false ``confirmed`` rows using persisted fields only."""
    from core.wa_cart_line_items import normalize_line_item  # noqa: PLC0415

    row = normalize_line_item(dict(item or {}))
    pid = str(row.get("product_id") or "").strip()
    if not pid:
        row["match_status"] = MATCH_STATUS_CUSTOM_UNMATCHED
        return row

    stored = str(row.get("match_status") or "").strip()
    price = parse_unit_price(row.get("unit_price") or row.get("price"))
    vid = str(row.get("variant_id") or "").strip()
    variant_text = str(row.get("variant") or "").strip()

    if stored == MATCH_STATUS_CONFIRMED:
        if price is None:
            row["match_status"] = MATCH_STATUS_NEEDS_REVIEW
        elif not vid and not variant_text:
            row["match_status"] = MATCH_STATUS_NEEDS_REVIEW
        return row

    if stored in (MATCH_STATUS_NEEDS_VARIANT, MATCH_STATUS_NEEDS_REVIEW, MATCH_STATUS_CUSTOM_UNMATCHED):
        return row

    row["match_status"] = MATCH_STATUS_NEEDS_REVIEW
    return row


def enrich_line_item_with_catalog(
    db: Any,
    tenant_id: int,
    item: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve catalog evidence and return dashboard line-item payload."""
    from core.wa_cart_line_items import normalize_line_item  # noqa: PLC0415

    raw = normalize_line_item(dict(item or {}))
    qty = max(int(raw.get("quantity") or 1), 1)

    free_name = str(
        raw.get("product_name") or raw.get("title") or raw.get("name") or "منتج"
    ).strip()
    pid = str(raw.get("product_id") or "").strip()
    product = resolve_catalog_product(db, tenant_id, pid) if pid else None
    variant_row = None
    requires_variant = False

    if product is not None:
        requires_variant = product_requires_variant_selection(db, product)
        vid = str(raw.get("variant_id") or "").strip()
        if vid:
            variant_row = resolve_catalog_variant(db, product, vid)
        elif not requires_variant and getattr(product, "default_variant_id", None):
            from models import ProductVariant  # noqa: PLC0415

            variant_row = (
                db.query(ProductVariant)
                .filter(ProductVariant.id == product.default_variant_id)
                .first()
            )

        media = _product_media(product)
        if variant_row and getattr(variant_row, "image_url", None):
            media["image_url"] = str(variant_row.image_url).strip() or media["image_url"]

        if variant_row and getattr(variant_row, "price", None):
            vprice = parse_unit_price(variant_row.price)
            if vprice is not None:
                raw["unit_price"] = vprice

        if not raw.get("unit_price") and getattr(product, "price", None):
            pprice = parse_unit_price(product.price)
            if pprice is not None:
                raw["unit_price"] = pprice

        if variant_row:
            vname = str(
                variant_row.option_summary or variant_row.sku or ""
            ).strip()
            if vname:
                raw["variant"] = vname
            raw["variant_id"] = str(
                variant_row.salla_variant_id
                or variant_row.retailer_id
                or variant_row.id
            )

        raw["catalog_product_id"] = product.id
        raw["catalog_product_name"] = product.title
        if media.get("image_url"):
            raw["image_url"] = media["image_url"]
        if media.get("product_url"):
            raw["product_url"] = media["product_url"]

    status = compute_match_status(
        raw,
        product=product,
        variant_row=variant_row,
        requires_variant=requires_variant,
    )
    unit_price = parse_unit_price(raw.get("unit_price"))

    display_name = free_name
    if status == MATCH_STATUS_CONFIRMED and raw.get("catalog_product_name"):
        display_name = str(raw["catalog_product_name"])

    variant_label = str(
        raw.get("variant_label")
        or raw.get("variant")
        or raw.get("size")
        or ""
    ).strip() or None

    return {
        "product_id": pid,
        "catalog_product_id": raw.get("catalog_product_id"),
        "name": display_name,
        "catalog_product_name": raw.get("catalog_product_name"),
        "quantity": qty,
        "variant_id": str(raw.get("variant_id") or "") or None,
        "variant_name": variant_label,
        "variant_label": variant_label,
        "edition": str(raw.get("edition") or raw.get("production") or "").strip() or None,
        "unit_price": unit_price,
        "line_total": round(unit_price * qty, 2) if unit_price is not None else None,
        "image_url": raw.get("image_url") or raw.get("image") or None,
        "product_url": raw.get("product_url") or None,
        "match_status": status,
        "is_catalog_matched": status == MATCH_STATUS_CONFIRMED,
        "query_hint": raw.get("query_hint"),
        "requires_variant_selection": requires_variant,
    }


def enrich_order_line_items_for_dashboard(
    db: Any,
    tenant_id: int,
    line_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [
        enrich_line_item_with_catalog(db, tenant_id, item)
        for item in (line_items or [])
        if isinstance(item, dict)
    ]


def line_item_blocks_confirm(item: Dict[str, Any]) -> bool:
    status = str(item.get("match_status") or "").strip()
    if status != MATCH_STATUS_CONFIRMED:
        return True
    if not str(item.get("product_id") or "").strip():
        return True
    if parse_unit_price(item.get("unit_price")) is None:
        return True
    return False


def order_line_items_block_confirm(items: List[Dict[str, Any]]) -> List[str]:
    blockers: List[str] = []
    if not items:
        blockers.append("product")
    for idx, item in enumerate(items or []):
        status = str(item.get("match_status") or "").strip()
        if status == MATCH_STATUS_CUSTOM_UNMATCHED or not item.get("product_id"):
            blockers.append("catalog_review_required")
        elif status == MATCH_STATUS_NEEDS_VARIANT:
            blockers.append("catalog_needs_variant")
        elif status == MATCH_STATUS_NEEDS_REVIEW:
            blockers.append("catalog_review_required")
        elif status != MATCH_STATUS_CONFIRMED:
            blockers.append("catalog_review_required")
        elif parse_unit_price(item.get("unit_price")) is None:
            blockers.append("catalog_price_missing")
        _ = idx
    return sorted(set(blockers))


__all__ = [
    "MATCH_STATUS_CONFIRMED",
    "MATCH_STATUS_CUSTOM_UNMATCHED",
    "MATCH_STATUS_NEEDS_REVIEW",
    "MATCH_STATUS_NEEDS_VARIANT",
    "compute_match_status",
    "enrich_line_item_with_catalog",
    "enrich_order_line_items_for_dashboard",
    "line_item_blocks_confirm",
    "order_line_items_block_confirm",
    "parse_unit_price",
    "product_requires_variant_selection",
    "resolve_catalog_product",
    "resolve_catalog_variant",
    "sanitize_line_item_without_db",
    "sellable_variant_count",
]
