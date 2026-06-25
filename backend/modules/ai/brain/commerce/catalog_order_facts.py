"""
Operational catalog-order facts for compose — no customer-facing prose.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _prep_dict(state: Any) -> Dict[str, Any]:
    prep = getattr(state, "order_prep", None) if state is not None else None
    if prep is None:
        return {}
    if isinstance(prep, dict):
        return dict(prep)
    if hasattr(prep, "to_dict"):
        try:
            return dict(prep.to_dict())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _line_items_from_state(state: Any) -> List[Dict[str, Any]]:
    if state is None:
        return []
    prep = _prep_dict(state)
    items = list(prep.get("line_items") or [])
    if items:
        return [dict(x) for x in items if isinstance(x, dict)]
    cart = list(getattr(state, "cart_items", None) or [])
    return [dict(x) for x in cart if isinstance(x, dict)]


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _total_quantity_from_items(items: List[Dict[str, Any]]) -> int:
    total = 0
    for row in items or []:
        if not isinstance(row, dict):
            continue
        try:
            total += max(1, int(float(row.get("quantity") or 1)))
        except (TypeError, ValueError):
            total += 1
    return total


def _item_fact(row: Dict[str, Any]) -> Dict[str, Any]:
    name = (
        str(row.get("product_name") or row.get("title") or row.get("name") or "")
        .strip()
        or str(row.get("product_retailer_id") or "منتج")
    )
    qty = int(float(row.get("quantity") or 1))
    unit = _as_float(
        row.get("unit_price") or row.get("price") or row.get("item_price")
    )
    return {
        "name": name,
        "quantity": qty,
        "unit_price": unit,
        "product_id": str(row.get("product_id") or "") or None,
        "product_retailer_id": str(row.get("product_retailer_id") or "") or None,
    }


def build_catalog_order_compose_facts(
    *,
    state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Facts for LLM when the current inbound is a WhatsApp catalog order."""
    meta = dict(inbound_metadata or {})
    source = str(meta.get("source_type") or "").strip().lower()
    items_raw = meta.get("product_items")
    if source not in {"catalog_order", "order"} and not items_raw:
        return None
    if not isinstance(items_raw, list) or not items_raw:
        return None

    line_items = _line_items_from_state(state)
    item_facts = [_item_fact(row) for row in line_items] if line_items else []
    if not item_facts:
        for raw in items_raw:
            if isinstance(raw, dict):
                item_facts.append(_item_fact(raw))

    total = _as_float(meta.get("total_price"))
    if total is None:
        prep = _prep_dict(state)
        total = _as_float(
            prep.get("catalog_checkout_total")
            or prep.get("order_total")
            or prep.get("order_flow_v2_catalog_total")
        )
    currency = str(meta.get("currency") or _prep_dict(state).get("catalog_checkout_currency") or "SAR")

    count = len(item_facts) or int(meta.get("line_items_count") or len(items_raw) or 0)
    total_quantity = _total_quantity_from_items(
        [dict(x) for x in items_raw if isinstance(x, dict)]
    )
    if total_quantity <= 0 and item_facts:
        total_quantity = sum(int(x.get("quantity") or 0) for x in item_facts)
    return {
        "has_catalog_order": True,
        "line_items_count": count,
        "total_quantity": total_quantity,
        "line_items": item_facts,
        "is_multi_item": count > 1,
        "total_amount": total,
        "currency": currency,
        "source": "whatsapp_catalog_order",
    }


__all__ = ["build_catalog_order_compose_facts"]
