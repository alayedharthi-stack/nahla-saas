"""
Bounded catalog evidence for LLM reasoning.

Existence, stock, and checkout eligibility stay distinct. Discovery and
recommendation turns need real tenant titles even when a SKU is not
currently checkout-eligible. Checkout paths keep using orderable-only lists.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

_DEFAULT_LIMIT = 8


def _title_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(
        row.get("title")
        or row.get("name")
        or row.get("display_label")
        or ""
    ).strip()


def _row_id(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("id", "product_id", "external_id", "sku"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return _title_of(row).lower()


def _can_checkout(row: Any) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
    if "can_checkout" in row:
        return bool(row.get("can_checkout"))
    if "orderable" in row:
        return bool(row.get("orderable"))
    ext = str(row.get("external_id") or "").strip()
    if ext:
        return True
    return None


def _normalize_candidate(row: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    title = _title_of(row)
    if not title:
        return None
    item: Dict[str, Any] = {"title": title}
    pid = row.get("id") or row.get("product_id")
    if pid is not None:
        item["id"] = pid
    ext = str(row.get("external_id") or "").strip()
    if ext:
        item["external_id"] = ext
    price = row.get("price")
    if price not in (None, ""):
        item["price"] = price
    if "in_stock" in row:
        item["in_stock"] = bool(row.get("in_stock"))
    checkout = _can_checkout(row)
    if checkout is not None:
        item["can_checkout"] = checkout
    category = str(row.get("category") or row.get("category_name") or "").strip()
    if category:
        item["category"] = category
    image_url = str(
        row.get("image_url")
        or row.get("image")
        or row.get("product_image_url")
        or row.get("thumbnail_url")
        or ""
    ).strip()
    if image_url:
        item["image_url"] = image_url
    return item


def _extend_unique(
    dest: List[Dict[str, Any]],
    rows: Sequence[Any],
    *,
    seen: set[str],
    limit: int,
) -> None:
    for raw in rows or []:
        if len(dest) >= limit:
            return
        item = _normalize_candidate(raw)
        if not item:
            continue
        key = _row_id(item) or item["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        dest.append(item)


def collect_catalog_reasoning_candidates(
    *,
    facts: Any = None,
    merchant_context: Any = None,
    state: Any = None,
    limit: int = _DEFAULT_LIMIT,
) -> List[Dict[str, Any]]:
    """Return a bounded, tenant-scoped catalog evidence set for compose.

    Preference order:
    1. facts.discovery_products (existence-capable active catalog)
    2. facts.top_products (synced/orderable subset)
    3. merchant_context.products
    4. state.last_search_candidates / last_recommended_products
    """
    cap = max(1, min(int(limit or _DEFAULT_LIMIT), 12))
    ctx = merchant_context if isinstance(merchant_context, dict) else {}
    cached = ctx.get("_catalog_reasoning_candidates")
    if isinstance(cached, list) and cached:
        return list(cached)[:cap]
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    if facts is not None:
        _extend_unique(
            out,
            list(getattr(facts, "discovery_products", None) or []),
            seen=seen,
            limit=cap,
        )
        _extend_unique(
            out,
            list(getattr(facts, "top_products", None) or []),
            seen=seen,
            limit=cap,
        )

    ctx = merchant_context if isinstance(merchant_context, dict) else {}
    _extend_unique(out, list(ctx.get("products") or []), seen=seen, limit=cap)

    if state is not None:
        _extend_unique(
            out,
            list(getattr(state, "last_search_candidates", None) or []),
            seen=seen,
            limit=cap,
        )
        _extend_unique(
            out,
            list(getattr(state, "last_recommended_products", None) or []),
            seen=seen,
            limit=cap,
        )

    if isinstance(merchant_context, dict) and out:
        merchant_context["_catalog_reasoning_candidates"] = list(out)
    return out


def catalog_reasoning_titles(
    *,
    facts: Any = None,
    merchant_context: Any = None,
    state: Any = None,
    limit: int = _DEFAULT_LIMIT,
) -> List[str]:
    return [
        str(item.get("title") or "").strip()
        for item in collect_catalog_reasoning_candidates(
            facts=facts,
            merchant_context=merchant_context,
            state=state,
            limit=limit,
        )
        if str(item.get("title") or "").strip()
    ]


__all__ = [
    "catalog_reasoning_titles",
    "collect_catalog_reasoning_candidates",
]
