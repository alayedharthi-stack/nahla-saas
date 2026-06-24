"""
core/wa_cart_line_items.py
──────────────────────────
PR-3 — Multi-item WhatsApp draft cart normalization, merge, and deltas.

Operational only. Works on ``orders.line_items`` JSONB — does not touch
Salla/Zid sync shapes.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

ITEM_STATUS_CONFIRMED = "confirmed"
ITEM_STATUS_NEEDS_REVIEW = "needs_review"
ITEM_STATUS_CUSTOM_UNMATCHED = "custom_unmatched_item"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_VARIANT_ALIASES = {
    "1kg": "1kg", "1 kg": "1kg", "كilo": "1kg", "كيلو": "1kg",
    "500g": "500g", "500 g": "500g", "نصف كilo": "500g", "نصف كيلو": "500g",
    "250g": "250g", "ربع كilo": "250g", "ربع كيلo": "250g",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_text(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def normalize_variant(raw: Any) -> str:
    text = _norm_text(str(raw or ""))
    if not text:
        return ""
    for key, canonical in _VARIANT_ALIASES.items():
        if _norm_text(key) == text:
            return canonical
    return text


def normalize_line_item(raw: Dict[str, Any], *, source: str = "whatsapp") -> Dict[str, Any]:
    """Normalize a cart/line item dict to a stable Nahla WA shape."""
    if not isinstance(raw, dict):
        return {}
    name = str(
        raw.get("product_name")
        or raw.get("title")
        or raw.get("name")
        or raw.get("display_name")
        or ""
    ).strip()
    variant = normalize_variant(
        raw.get("variant")
        or raw.get("variant_id")
        or raw.get("size")
        or raw.get("variant_name")
        or raw.get("option")
        or ""
    )
    qty_raw = raw.get("quantity") or 1
    try:
        quantity = max(int(qty_raw), 1)
    except (TypeError, ValueError):
        quantity = 1

    product_id = str(
        raw.get("product_id")
        or raw.get("catalog_id")
        or raw.get("id")
        or ""
    ).strip()
    variant_id = str(raw.get("variant_id") or raw.get("external_variant_id") or "").strip()

    match_status = str(raw.get("match_status") or "").strip()
    if not match_status:
        match_status = (
            ITEM_STATUS_NEEDS_REVIEW if product_id else ITEM_STATUS_CUSTOM_UNMATCHED
        )

    item: Dict[str, Any] = {
        "product_name": name or "منتج",
        "title":          name or "منتج",
        "name":           name or "منتج",
        "display_name":   name or "منتج",
        "variant":        variant,
        "quantity":       quantity,
        "source":         str(raw.get("source") or source),
        "match_status":   match_status,
        "last_updated_at": str(raw.get("last_updated_at") or _utcnow_iso()),
    }
    if raw.get("query_hint"):
        item["query_hint"] = str(raw.get("query_hint"))
    if product_id:
        item["product_id"] = product_id
    if variant_id:
        item["variant_id"] = variant_id
    for price_key in ("unit_price", "price"):
        if raw.get(price_key) is not None:
            try:
                item[price_key] = float(
                    str(raw.get(price_key)).replace("ر.س", "").replace(",", "").split()[0]
                )
            except (TypeError, ValueError):
                item[price_key] = raw.get(price_key)
    if raw.get("confidence") is not None:
        item["confidence"] = raw.get("confidence")
    edition = str(raw.get("edition") or raw.get("notes") or "").strip()
    if edition:
        item["edition"] = edition
        item["notes"] = edition
    match_status = str(raw.get("match_status") or "").strip()
    if match_status:
        item["match_status"] = match_status
    query_hint = str(raw.get("query_hint") or "").strip()
    if query_hint:
        item["query_hint"] = query_hint
    return item


def line_item_merge_key(item: Dict[str, Any]) -> str:
    """Stable merge key: product_id+variant_id, else normalized name+variant."""
    pid = str(item.get("product_id") or item.get("catalog_id") or "").strip()
    vid = str(item.get("variant_id") or "").strip()
    variant = normalize_variant(item.get("variant") or "")
    if pid and vid:
        return f"pid:{pid}|vid:{vid}"
    if pid and variant:
        return f"pid:{pid}|var:{variant}"
    name = _norm_text(
        str(item.get("product_name") or item.get("title") or item.get("name") or "")
    )
    return f"name:{name}|var:{variant}"


def merge_line_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge items with the same key by summing quantity."""
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = normalize_line_item(raw)
        if not item.get("product_name") or item.get("product_name") == "منتج":
            if not item.get("product_id"):
                continue
        key = line_item_merge_key(item)
        if key in merged:
            prev = merged[key]
            prev_qty = int(prev.get("quantity") or 1)
            add_qty = int(item.get("quantity") or 1)
            prev["quantity"] = prev_qty + add_qty
            prev["last_updated_at"] = _utcnow_iso()
        else:
            merged[key] = item
            order.append(key)
    return [merged[k] for k in order]


def _match_item(item: Dict[str, Any], match: Dict[str, Any]) -> bool:
    if not match:
        return False
    if match.get("merge_key"):
        return line_item_merge_key(item) == str(match["merge_key"])
    pid = str(match.get("product_id") or "").strip()
    if pid and str(item.get("product_id") or "") != pid:
        return False
    variant = normalize_variant(match.get("variant") or "")
    if variant and normalize_variant(item.get("variant") or "") != variant:
        return False
    contains = str(match.get("product_name_contains") or match.get("name_contains") or "").strip()
    if contains:
        needle = _norm_text(contains)
        hay = _norm_text(str(item.get("product_name") or item.get("title") or ""))
        if needle not in hay:
            return False
    if pid or variant or contains:
        return True
    return False


def apply_cart_delta(
    items: List[Dict[str, Any]],
    delta: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Apply one cart mutation. Returns ``(new_items, timeline_events)``.
    """
    events: List[Dict[str, Any]] = []
    cart = merge_line_items(list(items or []))
    op = str(delta.get("op") or delta.get("action") or "").strip().lower()
    now = _utcnow_iso()

    if op in ("add", "set"):
        incoming = normalize_line_item(delta.get("item") or delta)
        cart = merge_line_items(cart + [incoming])
        events.append({
            "event":        "line_item_added",
            "source":       "whatsapp",
            "at":           now,
            "product_name": incoming.get("product_name"),
            "variant":      incoming.get("variant"),
            "quantity":     incoming.get("quantity"),
        })
        return cart, events

    if op in ("update_quantity", "set_quantity"):
        match = delta.get("match") or {}
        qty = delta.get("quantity")
        try:
            new_qty = max(int(qty), 0)
        except (TypeError, ValueError):
            return cart, events
        updated: List[Dict[str, Any]] = []
        for item in cart:
            if _match_item(item, match):
                old_qty = int(item.get("quantity") or 1)
                if new_qty <= 0:
                    events.append({
                        "event":        "line_item_removed",
                        "source":       "whatsapp",
                        "at":           now,
                        "product_name": item.get("product_name"),
                        "variant":      item.get("variant"),
                    })
                    continue
                item = dict(item)
                item["quantity"] = new_qty
                item["last_updated_at"] = now
                events.append({
                    "event":        "line_item_quantity_updated",
                    "source":       "whatsapp",
                    "at":           now,
                    "product_name": item.get("product_name"),
                    "variant":      item.get("variant"),
                    "old_quantity": old_qty,
                    "new_quantity": new_qty,
                })
                updated.append(item)
            else:
                updated.append(item)
        return updated, events

    if op in ("update_variant", "set_variant"):
        match = delta.get("match") or {}
        new_variant = normalize_variant(delta.get("variant") or delta.get("new_variant") or "")
        if not new_variant:
            return cart, events
        rebuilt: List[Dict[str, Any]] = []
        for item in cart:
            if _match_item(item, match):
                old_variant = item.get("variant")
                item = dict(item)
                item["variant"] = new_variant
                item["last_updated_at"] = now
                events.append({
                    "event":        "line_item_variant_updated",
                    "source":       "whatsapp",
                    "at":           now,
                    "product_name": item.get("product_name"),
                    "old_variant":  old_variant,
                    "new_variant":  new_variant,
                })
                rebuilt.append(item)
            else:
                rebuilt.append(item)
        return merge_line_items(rebuilt), events

    if op in ("remove", "delete"):
        match = delta.get("match") or delta
        kept: List[Dict[str, Any]] = []
        for item in cart:
            if _match_item(item, match):
                events.append({
                    "event":        "line_item_removed",
                    "source":       "whatsapp",
                    "at":           now,
                    "product_name": item.get("product_name"),
                    "variant":      item.get("variant"),
                })
            else:
                kept.append(item)
        return kept, events

    return cart, events


def apply_focus_item(
    items: List[Dict[str, Any]],
    focus_item: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Apply the current product focus without double-counting on resync.

    Same merge key → set quantity (not sum). New key → add.
    """
    events: List[Dict[str, Any]] = []
    cart = merge_line_items(list(items or []))
    focus = normalize_line_item(focus_item)
    key = line_item_merge_key(focus)
    now = _utcnow_iso()
    for idx, item in enumerate(cart):
        if line_item_merge_key(item) != key:
            continue
        old_qty = int(item.get("quantity") or 1)
        new_qty = int(focus.get("quantity") or 1)
        updated = dict(item)
        updated.update({k: v for k, v in focus.items() if v not in ("", None)})
        updated["quantity"] = new_qty
        updated["last_updated_at"] = now
        cart[idx] = normalize_line_item(updated)
        if old_qty != new_qty:
            events.append({
                "event":        "line_item_quantity_updated",
                "source":       "whatsapp",
                "at":           now,
                "product_name": updated.get("product_name"),
                "variant":      updated.get("variant"),
                "old_quantity": old_qty,
                "new_quantity": new_qty,
            })
        return cart, events
    return apply_cart_delta(cart, {"op": "add", "item": focus})


def _collect_item_lists(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    existing_meta: Optional[Dict[str, Any]] = None,
    existing_line_items: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    brain_cart = brain_state.get("cart_items") if isinstance(brain_state.get("cart_items"), list) else None
    if brain_cart:
        return list(brain_cart)

    collected: List[Dict[str, Any]] = []
    if existing_line_items:
        collected.extend(existing_line_items)
    for container in (existing_meta or {}, order_prep):
        if not isinstance(container, dict):
            continue
        for key in ("line_items", "cart_items", "items"):
            raw = container.get(key)
            if isinstance(raw, list):
                collected.extend(raw)
    return collected


def _item_from_focus(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    focus = brain_state.get("current_product_focus") or {}
    if not isinstance(focus, dict):
        focus = {}
    product_id = str(order_prep.get("product_id") or focus.get("id") or focus.get("external_id") or "").strip()
    name = str(
        order_prep.get("product_name")
        or order_prep.get("product_title")
        or focus.get("title")
        or focus.get("name")
        or ""
    ).strip()
    if not product_id and not name:
        return None
    variant = (
        order_prep.get("variant")
        or order_prep.get("size")
        or focus.get("variant")
        or focus.get("size")
        or ""
    )
    qty_raw = order_prep.get("quantity") or focus.get("quantity") or 1
    try:
        quantity = max(int(qty_raw), 1)
    except (TypeError, ValueError):
        quantity = 1
    raw: Dict[str, Any] = {
        "product_name": name,
        "product_id":   product_id,
        "variant":      variant,
        "quantity":     quantity,
        "source":       "whatsapp",
    }
    price = focus.get("price") or order_prep.get("price") or order_prep.get("total_price")
    if price is not None:
        raw["unit_price"] = price
        raw["price"] = price
    return normalize_line_item(raw)


def line_items_fingerprint(items: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for item in merge_line_items(items):
        parts.append(
            f"{line_item_merge_key(item)}:{int(item.get('quantity') or 1)}"
        )
    return "|".join(sorted(parts))


def build_line_items_from_order_prep(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    existing_meta: Optional[Dict[str, Any]] = None,
    existing_line_items: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], str, List[Dict[str, Any]]]:
    """
    Build merged ``line_items`` for a WhatsApp draft order.

    Returns ``(items, primary_title, timeline_events)``.
    """
    events: List[Dict[str, Any]] = []

    if order_prep.get("catalog_line_items_authoritative"):
        cart = merge_line_items(
            list(order_prep.get("line_items") or [])
            or list(brain_state.get("cart_items") or [])
        )
        primary = ""
        for item in cart:
            name = str(item.get("product_name") or item.get("title") or "").strip()
            if name and name != "منتج":
                primary = name
                break
        if len(cart) > 1 and primary:
            primary = f"{len(cart)} منتجات"
        return cart, primary, events

    cart = merge_line_items(_collect_item_lists(
        order_prep, brain_state, existing_meta, existing_line_items,
    ))

    for delta in order_prep.get("cart_deltas") or []:
        if isinstance(delta, dict):
            cart, delta_events = apply_cart_delta(cart, delta)
            events.extend(delta_events)

    focus_item = _item_from_focus(order_prep, brain_state)
    if focus_item is not None:
        cart, focus_events = apply_focus_item(cart, focus_item)
        events.extend(focus_events)

    primary = ""
    for item in cart:
        name = str(item.get("product_name") or item.get("title") or "").strip()
        if name and name != "منتج":
            primary = name
            break
    if not primary and focus_item:
        primary = str(focus_item.get("product_name") or "")

    return cart, primary, events


def format_cart_summary_ar(items: List[Dict[str, Any]]) -> str:
    """Human-readable Arabic cart summary for outbound replies."""
    merged = merge_line_items(items)
    if not merged:
        return ""
    lines = ["طلبك الحالي:"]
    for idx, item in enumerate(merged, start=1):
        name = item.get("product_name") or item.get("title") or "منتج"
        variant = item.get("variant") or ""
        qty = int(item.get("quantity") or 1)
        detail = name
        if variant:
            detail += f" — {variant}"
        lines.append(f"{idx}. {detail} — الكمية {qty}")
    return "\n".join(lines)


def cart_total_amount(items: List[Dict[str, Any]]) -> Optional[float]:
    total = 0.0
    found = False
    for item in merge_line_items(items):
        unit_raw = item.get("unit_price") or item.get("price")
        if unit_raw is None:
            continue
        try:
            unit = float(str(unit_raw).replace("ر.س", "").replace(",", "").split()[0])
        except (TypeError, ValueError):
            continue
        qty = int(item.get("quantity") or 1)
        total += unit * qty
        found = True
    return round(total, 2) if found else None


__all__ = [
    "ITEM_STATUS_CONFIRMED",
    "ITEM_STATUS_CUSTOM_UNMATCHED",
    "ITEM_STATUS_NEEDS_REVIEW",
    "apply_cart_delta",
    "apply_focus_item",
    "build_line_items_from_order_prep",
    "cart_total_amount",
    "format_cart_summary_ar",
    "line_item_merge_key",
    "line_items_fingerprint",
    "merge_line_items",
    "normalize_line_item",
    "normalize_variant",
]
