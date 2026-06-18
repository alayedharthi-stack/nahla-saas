"""
core/wa_order_editor.py
─────────────────────────
P1 — Merchant dashboard editing for Nahla WhatsApp draft orders.

Operational only: validates catalog evidence, recomputes totals/status,
records audit events, and protects paid orders from destructive edits.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.wa_order_editor")

MATCH_STATUS_CONFIRMED = "confirmed"
MATCH_STATUS_NEEDS_REVIEW = "needs_review"
MATCH_STATUS_NEEDS_VARIANT = "needs_variant"
MATCH_STATUS_CUSTOM_UNMATCHED = "custom_unmatched_item"

EDITABLE_STATUSES = frozenset({
    "draft",
    "pending_customer_info",
    "pending_payment",
})
DELETABLE_STATUSES = frozenset({
    "draft",
    "pending_customer_info",
})
CANCELABLE_STATUSES = frozenset({
    "draft",
    "pending_customer_info",
    "pending_payment",
    "payment_submitted",
})
PAID_IMMUTABLE_STATUSES = frozenset({
    "paid",
    "processing",
    "completed",
    "delivered",
    "shipped",
})

SHIPPING_PROVIDERS = frozenset({"manual", "oto", "beez"})


class OrderEditError(ValueError):
    """Raised when a merchant edit violates order safety rules."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _raw_status(order: Any) -> str:
    return str(getattr(order, "status", "") or "").strip().lower()


def _meta(order: Any) -> Dict[str, Any]:
    raw = getattr(order, "extra_metadata", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _customer_info(order: Any) -> Dict[str, Any]:
    raw = getattr(order, "customer_info", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def is_wa_whatsapp_order(order: Any) -> bool:
    source = str(getattr(order, "source", "") or "").strip().lower()
    meta = _meta(order)
    if source == "whatsapp":
        return True
    ext = str(getattr(order, "external_id", "") or "")
    return ext.startswith("nahla-wa-")


def is_order_editable(order: Any) -> bool:
    if not is_wa_whatsapp_order(order):
        return False
    if _raw_status(order) in PAID_IMMUTABLE_STATUSES:
        return False
    if bool(_meta(order).get("payment_confirmed")):
        return False
    return _raw_status(order) in EDITABLE_STATUSES


def can_delete_draft_order(order: Any) -> bool:
    if not is_wa_whatsapp_order(order):
        return False
    if _raw_status(order) in PAID_IMMUTABLE_STATUSES:
        return False
    if bool(_meta(order).get("payment_confirmed")):
        return False
    return _raw_status(order) in DELETABLE_STATUSES


def can_cancel_order(order: Any) -> bool:
    if not is_wa_whatsapp_order(order):
        return False
    if _raw_status(order) in PAID_IMMUTABLE_STATUSES:
        return False
    if bool(_meta(order).get("payment_confirmed")):
        return False
    return _raw_status(order) in CANCELABLE_STATUSES


def assert_editable(order: Any) -> None:
    if not is_order_editable(order):
        raise OrderEditError("order_not_editable")


def _append_audit(
    meta: Dict[str, Any],
    *,
    action: str,
    actor: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    timeline = list(meta.get("merchant_audit_log") or [])
    timeline.append({
        "action": action,
        "actor":  actor,
        "at":     _utcnow_iso(),
        "detail": detail or {},
    })
    meta["merchant_audit_log"] = timeline[-100:]


def _stamp_merchant_edit(meta: Dict[str, Any], *, actor: str, action: str) -> None:
    now = _utcnow_iso()
    meta["merchant_edited_at"] = now
    meta["merchant_edited_by"] = actor
    meta["merchant_edit_locked"] = True
    _append_audit(meta, action=action, actor=actor)


def _split_display_name(name: str) -> Tuple[str, str]:
    parts = [p for p in str(name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _resolve_display_name(first: str, last: str, fallback: str = "") -> str:
    first = str(first or "").strip()
    last = str(last or "").strip()
    if first and last:
        return f"{first} {last}"
    if first:
        return first
    if last:
        return last
    return str(fallback or "").strip() or "—"


def _order_prep_from_order(order: Any) -> Dict[str, Any]:
    meta = _meta(order)
    info = _customer_info(order)
    first = str(meta.get("customer_first_name") or info.get("first_name") or "").strip()
    last = str(meta.get("customer_last_name") or info.get("last_name") or "").strip()
    if not first and not last:
        first, last = _split_display_name(str(getattr(order, "customer_name", "") or ""))
    return {
        "customer_first_name": first,
        "customer_last_name":  last,
        "city":                str(info.get("city") or meta.get("city") or "").strip(),
        "short_address_code":  str(
            meta.get("short_address_code")
            or info.get("short_address_code")
            or ""
        ).strip(),
        "google_maps_url":     str(
            meta.get("google_maps_url")
            or info.get("google_maps_url")
            or meta.get("delivery_address_url")
            or ""
        ).strip(),
        "address_line":        str(
            info.get("address")
            or info.get("street")
            or meta.get("address_line")
            or ""
        ).strip(),
        "district":            str(info.get("district") or meta.get("district") or "").strip(),
        "delivery_notes":      str(meta.get("delivery_notes") or info.get("delivery_notes") or "").strip(),
    }


def _refresh_order_state(order: Any) -> None:
    from core.wa_cart_line_items import cart_total_amount, merge_line_items  # noqa: PLC0415
    from core.wa_order_line_item_evidence import sanitize_line_item_without_db  # noqa: PLC0415
    from core.wa_order_lifecycle import resolve_wa_order_status  # noqa: PLC0415

    meta = _meta(order)
    info = _customer_info(order)
    line_items = [
        sanitize_line_item_without_db(item)
        for item in merge_line_items(list(getattr(order, "line_items", None) or []))
    ]
    order.line_items = line_items

    prep = _order_prep_from_order(order)
    phone = str(info.get("phone") or info.get("mobile") or "").strip()
    status, missing_fields, delivery_address_status = resolve_wa_order_status(
        prep,
        {},
        whatsapp_phone=phone or None,
        payment_verified=bool(meta.get("payment_confirmed")),
        line_items=line_items,
    )
    if status:
        prev = _raw_status(order)
        if prev != status:
            timeline = list(meta.get("status_timeline") or [])
            timeline.append({
                "from": prev or "none",
                "to":   status,
                "at":   _utcnow_iso(),
                "reason": "merchant_edit",
            })
            meta["status_timeline"] = timeline[-50:]
        order.status = status

    meta["missing_fields"] = missing_fields
    meta["delivery_address_status"] = delivery_address_status

    total = cart_total_amount(line_items)
    if total is not None:
        order.total = f"{total:.2f} ر.س"
        meta["amount_value"] = total
        meta["amount_source"] = "line_items"
    elif not line_items:
        meta["needs_amount_review"] = True

    order.extra_metadata = meta


def _validate_line_item(item: Dict[str, Any]) -> Dict[str, Any]:
    from core.wa_order_line_item_evidence import sanitize_line_item_without_db  # noqa: PLC0415

    row = sanitize_line_item_without_db(dict(item or {}))
    status = str(row.get("match_status") or "").strip()
    pid = str(row.get("product_id") or "").strip()

    if status == MATCH_STATUS_CONFIRMED:
        if not pid:
            raise OrderEditError("confirmed_item_requires_product_id")
        from core.wa_order_line_item_evidence import parse_unit_price  # noqa: PLC0415

        if parse_unit_price(row.get("unit_price")) is None:
            raise OrderEditError("confirmed_item_requires_price")
        vid = str(row.get("variant_id") or "").strip()
        if not vid and not str(row.get("variant") or "").strip():
            raise OrderEditError("confirmed_item_requires_variant")
    return row


def _lookup_catalog_line_item(
    db: Any,
    tenant_id: int,
    *,
    product_id: Any,
    variant_id: Optional[Any] = None,
    quantity: int = 1,
) -> Dict[str, Any]:
    from core.wa_order_line_item_evidence import (  # noqa: PLC0415
        MATCH_STATUS_CONFIRMED,
        enrich_line_item_with_catalog,
        product_requires_variant_selection,
        resolve_catalog_product,
        resolve_catalog_variant,
    )

    ref = str(product_id or "").strip()
    if not ref:
        raise OrderEditError("product_id_required")

    product = resolve_catalog_product(db, tenant_id, ref)
    if product is None:
        raise OrderEditError("catalog_product_not_found")

    requires_variant = product_requires_variant_selection(db, product)
    vid = str(variant_id or "").strip()
    if requires_variant and not vid:
        raise OrderEditError("catalog_variant_required")

    variant_row = resolve_catalog_variant(db, product, vid) if vid else None
    if requires_variant and vid and variant_row is None:
        raise OrderEditError("catalog_variant_not_found")

    ext_id = str(product.external_id or product.id)
    item: Dict[str, Any] = {
        "product_id":   ext_id,
        "catalog_product_id": product.id,
        "product_name": product.title,
        "title":        product.title,
        "quantity":     max(int(quantity or 1), 1),
        "source":       "merchant_dashboard",
    }
    if vid:
        item["variant_id"] = vid
    if variant_row:
        item["variant_id"] = str(
            variant_row.salla_variant_id
            or variant_row.retailer_id
            or variant_row.id
        )
        item["variant"] = str(variant_row.option_summary or variant_row.sku or "").strip()
        if variant_row.price:
            item["unit_price"] = variant_row.price
        if variant_row.image_url:
            item["image_url"] = variant_row.image_url
    elif product.price:
        item["unit_price"] = product.price

    media_meta = getattr(product, "extra_metadata", None) or {}
    if isinstance(media_meta, dict):
        if media_meta.get("image_url") and not item.get("image_url"):
            item["image_url"] = media_meta.get("image_url")
        if media_meta.get("product_url"):
            item["product_url"] = media_meta.get("product_url")

    enriched = enrich_line_item_with_catalog(db, tenant_id, item)
    if enriched["match_status"] != MATCH_STATUS_CONFIRMED:
        raise OrderEditError(f"catalog_evidence_incomplete:{enriched['match_status']}")

    return {
        **item,
        "match_status": MATCH_STATUS_CONFIRMED,
        "unit_price": enriched.get("unit_price"),
        "catalog_product_name": enriched.get("catalog_product_name"),
        "product_url": enriched.get("product_url"),
        "image_url": enriched.get("image_url"),
        "variant": enriched.get("variant_label") or item.get("variant"),
    }


def update_order_customer(
    order: Any,
    *,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    internal_note: Optional[str] = None,
    actor: str = "merchant",
) -> None:
    assert_editable(order)
    meta = _meta(order)
    info = _customer_info(order)

    if first_name is not None:
        meta["customer_first_name"] = str(first_name).strip()
        info["first_name"] = meta["customer_first_name"]
    if last_name is not None:
        meta["customer_last_name"] = str(last_name).strip()
        info["last_name"] = meta["customer_last_name"]
    if phone is not None:
        cleaned = str(phone).strip()
        info["phone"] = cleaned
        info["mobile"] = cleaned
    if internal_note is not None:
        meta["internal_note"] = str(internal_note).strip()

    from core.customer_display import is_valid_customer_display_name  # noqa: PLC0415

    display = _resolve_display_name(
        meta.get("customer_first_name", ""),
        meta.get("customer_last_name", ""),
        fallback=str(getattr(order, "customer_name", "") or ""),
    )
    if is_valid_customer_display_name(display):
        order.customer_name = display
        meta["customer_name"] = display
        info["name"] = display
        meta["customer_name_source"] = "merchant_order_edit"
    else:
        phone_fb = str(info.get("phone") or info.get("mobile") or "").strip()
        order.customer_name = phone_fb or "—"
        meta.pop("customer_name", None)
        info.pop("name", None)

    order.customer_info = info
    order.extra_metadata = meta
    _stamp_merchant_edit(meta, actor=actor, action="update_customer")
    order.extra_metadata = meta
    _refresh_order_state(order)


def update_order_address(
    order: Any,
    *,
    city: Optional[str] = None,
    district: Optional[str] = None,
    street: Optional[str] = None,
    address: Optional[str] = None,
    short_address_code: Optional[str] = None,
    google_maps_url: Optional[str] = None,
    delivery_notes: Optional[str] = None,
    actor: str = "merchant",
) -> None:
    assert_editable(order)
    meta = _meta(order)
    info = _customer_info(order)

    if city is not None:
        info["city"] = str(city).strip()
        meta["city"] = info["city"]
    if district is not None:
        info["district"] = str(district).strip()
        meta["district"] = info["district"]
    if street is not None:
        info["street"] = str(street).strip()
    if address is not None:
        info["address"] = str(address).strip()
        meta["address_line"] = info["address"]
    if short_address_code is not None:
        code = str(short_address_code).strip().upper()
        meta["short_address_code"] = code
        info["short_address_code"] = code
        meta["national_short_address"] = code
    if google_maps_url is not None:
        url = str(google_maps_url).strip()
        meta["google_maps_url"] = url
        meta["delivery_address_url"] = url
        info["google_maps_url"] = url
    if delivery_notes is not None:
        meta["delivery_notes"] = str(delivery_notes).strip()
        info["delivery_notes"] = meta["delivery_notes"]

    order.customer_info = info
    _stamp_merchant_edit(meta, actor=actor, action="update_address")
    order.extra_metadata = meta
    _refresh_order_state(order)


def update_order_shipping_meta(
    order: Any,
    *,
    shipping_provider: Optional[str] = None,
    shipping_cost: Optional[float] = None,
    tracking_number: Optional[str] = None,
    shipping_status: Optional[str] = None,
    delivery_notes: Optional[str] = None,
    actor: str = "merchant",
) -> None:
    assert_editable(order)
    meta = _meta(order)

    if shipping_provider is not None:
        provider = str(shipping_provider).strip().lower()
        if provider and provider not in SHIPPING_PROVIDERS:
            raise OrderEditError("invalid_shipping_provider")
        meta["shipping_provider"] = provider or "manual"
    if shipping_cost is not None:
        meta["shipping_cost"] = float(shipping_cost)
    if tracking_number is not None:
        meta["tracking_number"] = str(tracking_number).strip()
    if shipping_status is not None:
        meta["shipping_status"] = str(shipping_status).strip()
    if delivery_notes is not None:
        meta["delivery_notes"] = str(delivery_notes).strip()

    _stamp_merchant_edit(meta, actor=actor, action="update_shipping_meta")
    order.extra_metadata = meta


def set_order_line_items(
    order: Any,
    line_items: List[Dict[str, Any]],
    *,
    actor: str = "merchant",
) -> None:
    assert_editable(order)
    validated = [_validate_line_item(dict(item)) for item in (line_items or [])]
    order.line_items = validated
    meta = _meta(order)
    _stamp_merchant_edit(meta, actor=actor, action="set_line_items")
    order.extra_metadata = meta
    _refresh_order_state(order)


def add_order_line_item(
    order: Any,
    item: Dict[str, Any],
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    actor: str = "merchant",
) -> None:
    assert_editable(order)
    incoming = dict(item or {})
    if incoming.get("product_id") and db is not None and tenant_id:
        incoming = _lookup_catalog_line_item(
            db,
            tenant_id,
            product_id=incoming.get("product_id"),
            variant_id=incoming.get("variant_id"),
            quantity=incoming.get("quantity") or 1,
        )
    else:
        incoming = _validate_line_item(incoming)

    items = list(getattr(order, "line_items", None) or [])
    items.append(incoming)
    set_order_line_items(order, items, actor=actor)


def update_order_line_item(
    order: Any,
    index: int,
    patch: Dict[str, Any],
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    actor: str = "merchant",
) -> None:
    assert_editable(order)
    items = list(getattr(order, "line_items", None) or [])
    if index < 0 or index >= len(items):
        raise OrderEditError("line_item_not_found")

    current = dict(items[index])
    if patch.get("product_id") and db is not None and tenant_id:
        merged = _lookup_catalog_line_item(
            db,
            tenant_id,
            product_id=patch.get("product_id"),
            variant_id=patch.get("variant_id") or current.get("variant_id"),
            quantity=patch.get("quantity") or current.get("quantity") or 1,
        )
    else:
        merged = {**current, **patch}
        if patch.get("match_status") == MATCH_STATUS_CONFIRMED:
            raise OrderEditError("cannot_force_confirmed_without_catalog")
        merged = _validate_line_item(merged)

    items[index] = merged
    set_order_line_items(order, items, actor=actor)


def delete_order_line_item(
    order: Any,
    index: int,
    *,
    actor: str = "merchant",
) -> None:
    assert_editable(order)
    items = list(getattr(order, "line_items", None) or [])
    if index < 0 or index >= len(items):
        raise OrderEditError("line_item_not_found")
    items.pop(index)
    set_order_line_items(order, items, actor=actor)


def confirm_order_ready(
    order: Any,
    *,
    actor: str = "merchant",
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> None:
    """Move toward pending_payment when minimum data is present."""
    assert_editable(order)
    meta = _meta(order)
    prep = _order_prep_from_order(order)
    missing = list(meta.get("missing_fields") or [])

    from core.wa_order_line_item_evidence import (  # noqa: PLC0415
        enrich_order_line_items_for_dashboard,
        order_line_items_block_confirm,
        sanitize_line_item_without_db,
    )

    raw_items = list(getattr(order, "line_items", None) or [])
    if db is not None and tenant_id is not None:
        eval_items = enrich_order_line_items_for_dashboard(db, tenant_id, raw_items)
    else:
        eval_items = [
            sanitize_line_item_without_db(dict(item or {}))
            for item in raw_items
        ]

    blockers = []
    if not prep.get("customer_first_name"):
        blockers.append("customer_first_name")
    if not prep.get("customer_last_name"):
        blockers.append("customer_last_name")
    blockers.extend(order_line_items_block_confirm(eval_items))

    if blockers or missing:
        raise OrderEditError(f"order_incomplete:{','.join(sorted(set(blockers + missing)))}")

    order.status = "pending_payment"
    _stamp_merchant_edit(meta, actor=actor, action="confirm_ready")
    order.extra_metadata = meta
    _refresh_order_state(order)


def cancel_order(order: Any, *, actor: str = "merchant", reason: str = "") -> None:
    if not can_cancel_order(order):
        raise OrderEditError("order_not_cancellable")
    meta = _meta(order)
    prev = _raw_status(order)
    order.status = "cancelled"
    meta["cancelled_at"] = _utcnow_iso()
    meta["cancel_reason"] = str(reason or "").strip()
    _append_audit(meta, action="cancel", actor=actor, detail={"from": prev, "reason": reason})
    order.extra_metadata = meta


def delete_draft_order(order: Any, *, actor: str = "merchant") -> None:
    if not can_delete_draft_order(order):
        raise OrderEditError("order_not_deletable")


def log_draft_delete_audit(order: Any, *, actor: str) -> None:
    meta = _meta(order)
    _append_audit(
        meta,
        action="delete_draft",
        actor=actor,
        detail={"order_id": getattr(order, "id", None)},
    )
    order.extra_metadata = meta


def order_edit_capabilities(
    order: Any,
    *,
    enriched_line_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    from core.wa_order_line_item_evidence import order_line_items_block_confirm  # noqa: PLC0415

    meta = _meta(order)
    prep = _order_prep_from_order(order)
    items = enriched_line_items if enriched_line_items is not None else list(
        getattr(order, "line_items", None) or []
    )
    catalog_blockers = order_line_items_block_confirm(items)
    missing = list(meta.get("missing_fields") or [])
    confirm_blockers = sorted(set(missing + catalog_blockers))
    return {
        "is_editable":          is_order_editable(order),
        "can_delete_draft":     can_delete_draft_order(order),
        "can_cancel":           can_cancel_order(order),
        "can_confirm_ready":    is_order_editable(order) and not confirm_blockers,
        "confirm_blockers":     confirm_blockers,
        "missing_fields":       missing,
        "needs_amount_review":  bool(meta.get("needs_amount_review")),
        "merchant_edited_at":   meta.get("merchant_edited_at"),
        "customer_first_name":  prep.get("customer_first_name"),
        "customer_last_name":   prep.get("customer_last_name"),
        "internal_note":        meta.get("internal_note"),
        "shipping_provider":    meta.get("shipping_provider") or "manual",
        "shipping_cost":        meta.get("shipping_cost"),
        "tracking_number":      meta.get("tracking_number"),
        "shipping_status":      meta.get("shipping_status"),
        "delivery_notes":       meta.get("delivery_notes") or prep.get("delivery_notes"),
        "national_short_address": meta.get("national_short_address") or meta.get("short_address_code"),
    }


__all__ = [
    "MATCH_STATUS_CONFIRMED",
    "MATCH_STATUS_CUSTOM_UNMATCHED",
    "MATCH_STATUS_NEEDS_REVIEW",
    "MATCH_STATUS_NEEDS_VARIANT",
    "OrderEditError",
    "add_order_line_item",
    "cancel_order",
    "can_cancel_order",
    "can_delete_draft_order",
    "confirm_order_ready",
    "delete_draft_order",
    "delete_order_line_item",
    "is_order_editable",
    "log_draft_delete_audit",
    "order_edit_capabilities",
    "set_order_line_items",
    "update_order_address",
    "update_order_customer",
    "update_order_line_item",
    "update_order_shipping_meta",
]
