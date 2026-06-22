"""
catalog/collections_pagination.py
──────────────────────────────────
Collections-page pagination + button mapping for CatalogNavigator groups view.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .product_pick import has_active_group_products_context

COLLECTIONS_BUTTON_PAGE_SIZE = 2

BUTTON_MORE_COLLECTIONS = "nav_more_collections"
BUTTON_START_COLLECTIONS = "nav_collections_start"
BUTTON_MORE_PRODUCTS = "nav_more_products"
BUTTON_BACK_GROUPS = "nav_back_groups"


def has_active_collections_browse_context(state: Any) -> bool:
    if has_active_group_products_context(state):
        return False
    source = str(getattr(state, "catalog_navigation_source", "") or "").strip()
    if source != "groups":
        return False
    return bool(
        list(getattr(state, "last_presented_collections", None) or [])
        or list(getattr(state, "collections_pool", None) or [])
    )


def get_collections_pool(state: Any) -> List[Dict[str, Any]]:
    pool = list(getattr(state, "collections_pool", None) or [])
    if pool:
        return [dict(row) if isinstance(row, dict) else {"label": str(row)} for row in pool]
    return [
        dict(row) if isinstance(row, dict) else {"label": str(row)}
        for row in list(getattr(state, "last_presented_collections", None) or [])
    ]


def normalize_collections_page(
    collections: Sequence[Dict[str, Any]],
    *,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    page: List[Dict[str, Any]] = []
    for index, row in enumerate(list(collections or []), start=1):
        item = dict(row or {})
        item["list_index"] = index
        item["pool_index"] = int(offset) + index
        page.append(item)
    return page


def build_collection_quick_buttons(
    page_collections: Sequence[Dict[str, Any]],
    *,
    collections_next_available: bool,
    collections_at_end: bool = False,
) -> List[Dict[str, Any]]:
    """Build up to 3 WhatsApp quick-reply buttons for the current collections page."""
    from core.product_button_label import compact_whatsapp_product_button_title  # noqa: PLC0415

    rows = list(page_collections or [])
    buttons: List[Dict[str, Any]] = []
    for index, row in enumerate(rows[:COLLECTIONS_BUTTON_PAGE_SIZE], start=1):
        label = str(
            row.get("group_name")
            or row.get("label")
            or row.get("name")
            or ""
        ).strip()
        title = compact_whatsapp_product_button_title(label) if label else str(index)
        buttons.append({
            "type": "reply",
            "reply": {"id": f"coll_{index}", "title": title or str(index)},
        })

    if collections_next_available and len(buttons) < 3:
        buttons.append({
            "type": "reply",
            "reply": {"id": BUTTON_MORE_COLLECTIONS, "title": "المزيد"},
        })
    elif collections_at_end and len(buttons) < 3:
        buttons.append({
            "type": "reply",
            "reply": {"id": BUTTON_START_COLLECTIONS, "title": "البداية"},
        })
    return buttons[:3]


__all__ = [
    "BUTTON_BACK_GROUPS",
    "BUTTON_MORE_COLLECTIONS",
    "BUTTON_MORE_PRODUCTS",
    "BUTTON_START_COLLECTIONS",
    "COLLECTIONS_BUTTON_PAGE_SIZE",
    "build_collection_quick_buttons",
    "get_collections_pool",
    "has_active_collections_browse_context",
    "normalize_collections_page",
]
