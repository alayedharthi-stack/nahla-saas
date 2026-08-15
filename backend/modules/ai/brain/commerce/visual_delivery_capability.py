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
    """Send real product media when a visual ask has imageable catalog evidence."""
    state = getattr(ctx, "state", None)
    cap = collect_visual_delivery_capability(
        state=state,
        facts=getattr(ctx, "facts", None),
        merchant_context=getattr(ctx, "merchant_context", None),
    )
    if not visual_delivery_available(cap):
        return None
    products = list(cap.get("products") or [])
    title = ""
    trusted_title = ""
    try:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            is_deictic_visual_request,
            resolve_trusted_focus_for_deictic,
        )

        trusted = resolve_trusted_focus_for_deictic(
            state,
            str(getattr(ctx, "message", "") or ""),
        )
        trusted_title = str(trusted.title or "").strip()
        trusted_id = str(getattr(trusted, "product_id", "") or "").strip()
        if trusted.reason == "ambiguous_presented":
            import re as _re  # noqa: PLC0415

            _possessive = _re.search(
                r"صور(?:ه|ة)?(?:ته|تها|هم|هن)",
                str(getattr(ctx, "message", "") or ""),
            )
            if _possessive:
                return None
        elif trusted_title or trusted_id:
            matched = None
            if trusted_id:
                for row in products:
                    rid = str(
                        row.get("id") or row.get("product_id") or row.get("external_id") or ""
                    ).strip()
                    if rid and rid == trusted_id:
                        matched = row
                        break
            if matched is None and trusted_title:
                title_hits = [
                    row for row in products
                    if str(row.get("title") or "").strip() == trusted_title
                ]
                if len(title_hits) == 1:
                    matched = title_hits[0]
            if matched is not None:
                title = str(matched.get("title") or trusted_title).strip()
                products = [matched] + [p for p in products if p is not matched]
            elif str(getattr(trusted, "origin", "") or "") in {
                "last_recommended_products",
                "last_presented_products",
                "current_product_focus",
            }:
                # Unique referent exists but is not imageable. Do not send
                # a different SKU's media as if it belonged to the referent.
                return None
            elif trusted_title:
                title = trusted_title
        elif is_deictic_visual_request(str(getattr(ctx, "message", "") or "")):
            import re as _re  # noqa: PLC0415

            _possessive = _re.search(
                r"صور(?:ه|ة)?(?:ته|تها|هم|هن)",
                str(getattr(ctx, "message", "") or ""),
            )
            if _possessive:
                # Possessive image follow-up without presented/focus context
                # must not silently send an unrelated first catalog SKU.
                return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — visual focus probe must not block capability send
        title = ""
    if not title:
        title = str((products[0] or {}).get("title") or "").strip()
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
            "replay_candidates": products,
            "visual_delivery": cap,
        },
        reason="product visual — imageable catalog candidates",
        confidence=0.90,
    )


__all__ = [
    "collect_visual_delivery_capability",
    "imageable_products_from_rows",
    "try_visual_catalog_send_decision",
    "visual_delivery_available",
]
