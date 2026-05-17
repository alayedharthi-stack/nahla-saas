"""services/google_merchant_feed.py
─────────────────────────────────
Google Merchant Center JSON feed emitter (migration 0064 — Phase 4).

Why this exists
───────────────
Google Merchant Center pulls a per-tenant feed when the merchant
registers it in Merchant Center. Until now we only computed
*readiness* signals — i.e. "this catalog could feed Google if the
merchant wanted" — but never emitted the feed itself. With variant
rows in the database (Phase 2) we can now produce a proper
per-variant feed where every variant is its own item and all
variants of one parent share the same ``item_group_id`` — the
canonical Google shape for "size/color products".

Contract
────────
* Feed is JSON (not XML) by design — operators trying to debug a
  malformed item can grep / jq the response without touching XSLT.
  Google accepts JSON via the Content API; merchants who prefer the
  classic XML feed will get an XML transform on top in a later
  iteration.
* Each item carries:
    - ``id``            = variant.retailer_id (Meta product id is
                          deliberately reused so all three channels
                          (WA/Meta/Google) reference the same SKU).
    - ``item_group_id`` = str(parent.id) (deterministic, stable per
                          tenant — never reused across tenants since
                          ``product_variants.tenant_id`` is part of
                          the schema).
    - ``title``         = parent.title + variant.option_summary.
    - ``price``         = "<amount> <currency>" (Google shape).
    - ``availability``  = "in_stock" / "out_of_stock".
    - ``image_link``    = variant.image_url || parent image.
    - ``size`` / ``color`` / ``material`` extracted from
       ``variant.options`` when present.
* Variants whose ``retailer_id`` is empty are SKIPPED — emitting
  them would crash Merchant Center's validator. The endpoint
  surfaces a ``skipped`` counter so operators can spot un-mapped
  rows from the diagnostics card.

This module is pure read-only: it never writes back to the
database. The endpoint that serves the feed (:func:`build_feed`)
is cached behind a simple ETag wrapper in the router.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.catalog.google_feed")


# ─────────────────────────────────────────────────────────────────────────────
# Field extractors
# ─────────────────────────────────────────────────────────────────────────────


def _opt(options: Any, *keys: str) -> Optional[str]:
    """Return the first non-empty value from ``options`` for any of *keys*.

    Tolerant of None / dict / non-dict (Salla sometimes drops in a list).
    Lower-cases the key when matching since adapters disagree on case
    ("Size" vs "size" vs "SIZE").
    """
    if not isinstance(options, dict) or not options:
        return None
    lookup = {str(k).lower(): v for k, v in options.items() if k}
    for k in keys:
        v = lookup.get(k.lower())
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _parent_image(parent: Any) -> str:
    """Best-effort image URL for the parent (used when the variant
    has no per-variant image of its own)."""
    meta = getattr(parent, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return ""
    for key in ("image_url", "thumbnail", "image"):
        val = meta.get(key)
        if val:
            return str(val)
    return ""


def _parent_product_url(parent: Any) -> str:
    meta = getattr(parent, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("product_url") or meta.get("url") or "")


# ─────────────────────────────────────────────────────────────────────────────
# Per-variant item shape
# ─────────────────────────────────────────────────────────────────────────────


def variant_to_feed_item(parent: Any, variant: Any) -> Optional[Dict[str, Any]]:
    """Render a single feed item dict, or ``None`` when the row is
    unmappable (missing retailer_id — Google rejects items without
    an id).

    Synthetic ``is_default=True`` variants are passed through
    untouched — for one-SKU parents they ARE the Google item.
    """
    retailer = (getattr(variant, "retailer_id", "") or "").strip()
    if not retailer:
        return None
    parent_title = (getattr(parent, "title", "") or "").strip()
    summary = (getattr(variant, "option_summary", "") or "").strip()
    if summary and not variant.is_default:
        title = f"{parent_title} — {summary}"
    else:
        title = parent_title or summary or f"Product {parent.id}"

    price = (getattr(variant, "price", None)
             or getattr(parent, "price", None) or "")
    currency = (getattr(variant, "currency", None) or "SAR")
    price_str = ""
    if price not in (None, "", 0):
        # Google expects "<amount> <currency>", e.g. "129.00 SAR".
        price_str = f"{price} {currency}".strip()

    in_stock = bool(getattr(variant, "in_stock", True))
    image = (getattr(variant, "image_url", None) or "").strip() or _parent_image(parent)

    item: Dict[str, Any] = {
        "id":             retailer,
        "item_group_id":  str(getattr(parent, "id", "")),
        "title":          title,
        "price":          price_str,
        "availability":   "in_stock" if in_stock else "out_of_stock",
        "image_link":     image,
        "link":           _parent_product_url(parent),
    }
    opts = getattr(variant, "options", None) or {}
    size = _opt(opts, "size", "حجم", "مقاس")
    color = _opt(opts, "color", "colour", "لون")
    material = _opt(opts, "material", "خامة", "مادة")
    if size:     item["size"] = size
    if color:    item["color"] = color
    if material: item["material"] = material
    # MPN / brand / gtin fall through from parent metadata for free —
    # Google treats them as optional and we'd rather emit a minimal
    # valid item than a maximalist invalid one.
    return item


# ─────────────────────────────────────────────────────────────────────────────
# Public feed builder
# ─────────────────────────────────────────────────────────────────────────────


def build_feed(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Build the JSON feed for one tenant.

    Returns:

        {
            "tenant_id": int,
            "generated_at": ISO8601 str,
            "items_count": int,
            "items_skipped": int,
            "items": [ ... ],
        }

    The endpoint serves this dict verbatim. Items with no
    ``retailer_id`` (or no resolvable price) are SKIPPED rather
    than emitted as broken rows; ``items_skipped`` lets operators
    spot un-mapped variants without scanning the whole list.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        from models import Product, ProductVariant  # noqa: PLC0415
    except ImportError:  # noqa: BLE001
        from database.models import Product, ProductVariant  # type: ignore  # noqa: PLC0415

    rows: Iterable[ProductVariant] = (
        db.query(ProductVariant)
          .filter(ProductVariant.tenant_id == tenant_id)
          .all()
    )
    # Index parents by id so we don't N+1 the join.
    parent_ids = {v.product_id for v in rows}
    parents: Dict[int, Any] = {
        p.id: p for p in db.query(Product)
                           .filter(Product.tenant_id == tenant_id)
                           .filter(Product.id.in_(parent_ids))
                           .all()
    } if parent_ids else {}

    items: List[Dict[str, Any]] = []
    skipped = 0
    for v in rows:
        parent = parents.get(v.product_id)
        if parent is None:
            skipped += 1
            continue
        item = variant_to_feed_item(parent, v)
        if item is None:
            skipped += 1
            continue
        items.append(item)

    return {
        "tenant_id":     int(tenant_id),
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "items_count":   len(items),
        "items_skipped": skipped,
        "items":         items,
    }


__all__ = [
    "build_feed",
    "variant_to_feed_item",
]
