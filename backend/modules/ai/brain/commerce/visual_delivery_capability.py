"""
Bounded visual-delivery capability for product images / cards.

The model may offer photos only when the platform can actually send them.
This module exposes executable capability from catalog evidence — it does
not teach customer phrases or ban image offers.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def _image_url(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(
        row.get("image_url")
        or row.get("image")
        or row.get("product_image_url")
        or row.get("thumbnail_url")
        or ""
    ).strip()


def _title(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("title") or row.get("name") or row.get("display_label") or "").strip()


def _row_id(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("id", "product_id", "external_id", "sku"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return _title(row).lower()


def imageable_products_from_rows(rows: Sequence[Any], *, limit: int = 6) -> List[Dict[str, Any]]:
    cap = max(1, min(int(limit or 6), 8))
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        title = _title(raw)
        url = _image_url(raw)
        if not title or not url:
            continue
        key = _row_id(raw) or title.lower()
        if key in seen:
            continue
        seen.add(key)
        item: Dict[str, Any] = {
            "title": title,
            "image_url": url,
            "in_stock": True,
            "catalog_status": "active",
        }
        pid = raw.get("id") or raw.get("product_id")
        if pid is not None:
            item["id"] = pid
        ext = str(raw.get("external_id") or "").strip()
        if ext:
            item["external_id"] = ext
        if "in_stock" in raw:
            item["in_stock"] = bool(raw.get("in_stock"))
        out.append(item)
        if len(out) >= cap:
            break
    return out


def collect_visual_delivery_capability(
    *,
    catalog_candidates: Optional[Sequence[Any]] = None,
    state: Any = None,
    facts: Any = None,
    merchant_context: Any = None,
) -> Dict[str, Any]:
    rows: List[Any] = []
    for source in (
        list(catalog_candidates or []),
        list(getattr(state, "last_presented_products", None) or []) if state is not None else [],
        list(getattr(state, "last_search_candidates", None) or []) if state is not None else [],
        list(getattr(state, "last_recommended_products", None) or []) if state is not None else [],
        list(getattr(facts, "discovery_products", None) or []) if facts is not None else [],
        list(getattr(facts, "top_products", None) or []) if facts is not None else [],
        list((merchant_context or {}).get("products") or [])
        if isinstance(merchant_context, dict)
        else [],
    ):
        rows.extend(source)
    products = imageable_products_from_rows(rows)
    available = bool(products)
    return {
        "available": available,
        "can_send_images": available,
        "can_send_product_cards": available,
        "product_count": len(products),
        "products": products,
        "source": "catalog_media",
    }


def visual_delivery_available(capability: Any) -> bool:
    if not isinstance(capability, dict):
        return False
    return bool(capability.get("available") and capability.get("products"))


def try_visual_catalog_send_decision(ctx: Any) -> Optional[Any]:
    """Send real product media for the canonical referent when imageable."""
    state = getattr(ctx, "state", None)
    checkout_active = False
    try:
        from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
            is_active_catalog_checkout,
        )

        checkout_active = bool(is_active_catalog_checkout(ctx))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — checkout probe must not block visual capability
        checkout_active = False
    try:
        from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: PLC0415
            canonical_product_referent,
            has_structured_catalog_identity,
            product_focus_identity,
        )
    except Exception:  # noqa: BLE001
        return None

    referent = canonical_product_referent(state, checkout_active=checkout_active)
    if not referent or not has_structured_catalog_identity(referent):
        return None

    cap = collect_visual_delivery_capability(
        state=state,
        facts=getattr(ctx, "facts", None),
        merchant_context=getattr(ctx, "merchant_context", None),
    )
    if not visual_delivery_available(cap):
        return None
    products = list(cap.get("products") or [])
    rid = product_focus_identity(referent)
    matched = None
    for row in products:
        if product_focus_identity(row) == rid:
            matched = row
            break
    if matched is None:
        # Unique referent exists but is not imageable. Do not send another SKU.
        return None
    title = str(matched.get("title") or referent.get("title") or "").strip()
    if not title:
        return None
    from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={
            "query": title,
            "after_search": "product_visual",
            "force_product_card": True,
            "replay_candidates": [matched],
            "visual_delivery": cap,
            "recommended_product": matched,
            "product": matched,
        },
        reason="product visual — canonical referent is imageable",
        confidence=0.90,
    )


__all__ = [
    "collect_visual_delivery_capability",
    "imageable_products_from_rows",
    "try_visual_catalog_send_decision",
    "visual_delivery_available",
]
