"""OrderFlowV2 state helpers — pending vs active checkout separation."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.wa_order_lifecycle import compute_wa_missing_fields, has_accepted_delivery_address


def prep_dict(order_prep: Any) -> Dict[str, Any]:
    if isinstance(order_prep, dict):
        return dict(order_prep)
    if order_prep is None:
        return {}
    out: Dict[str, Any] = {}
    for key in (
        "order_flow_v2_active",
        "order_flow_v2_pending",
        "order_flow_v2_trusted_price",
        "order_flow_v2_catalog_total",
        "order_flow_v2_currency",
        "order_flow_v2_last_field",
        "order_flow_v2_contract",
        "order_flow_v2_address_refused",
        "order_flow_v2_payment_rejected",
        "order_flow_v2_payment_rejection_reason",
        "order_flow_v2_available_payment_methods",
        "customer_first_name",
        "customer_last_name",
        "city",
        "short_address_code",
        "google_maps_url",
        "delivery_address_url",
        "address_line",
        "payment_method",
        "line_items",
        "cart_items",
        "product_id",
        "quantity",
        "catalog_line_items_authoritative",
        "catalog_order_extraction_incomplete",
        "catalog_skus",
        "catalog_order_line_count",
        "catalog_total_quantity",
        "catalog_checkout_total",
        "catalog_checkout_currency",
        "checkout_channel",
        "order_total",
        "total",
        "order_status",
        "payment_status",
        "payment_confirmed",
        "payment_verified",
        "verified_by_staff",
        "receipt_verified_by_merchant",
        "receipt_status",
        "payment_receipt_received",
        "payment_receipt_metadata",
        "payment_verification_status",
        "requested_bank",
        "payment_bank",
        "missing_fields",
    ):
        if hasattr(order_prep, key):
            val = getattr(order_prep, key, None)
            if val not in (None, ""):
                out[key] = val
    return out


def line_items_from_state(order_prep: Dict[str, Any], brain_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    for container in (order_prep, brain_state):
        raw = container.get("line_items") or container.get("cart_items") or []
        if isinstance(raw, list) and raw:
            return [dict(x) for x in raw if isinstance(x, dict)]
    return []


def pending_order_exists(order_prep: Dict[str, Any], brain_state: Dict[str, Any]) -> bool:
    """Incomplete order data exists but checkout is not active now."""
    if order_prep.get("order_flow_v2_active"):
        return False
    items = line_items_from_state(order_prep, brain_state)
    if items:
        return True
    if str(order_prep.get("product_id") or "").strip():
        return True
    focus = brain_state.get("current_product_focus") or {}
    if isinstance(focus, dict) and (focus.get("id") or focus.get("title")):
        return True
    missing = compute_wa_missing_fields(order_prep, brain_state=brain_state, line_items=items or None)
    return bool(items) or len(missing) < 5


def checkout_active_now(order_prep: Dict[str, Any]) -> bool:
    return bool(order_prep.get("order_flow_v2_active"))


def trusted_catalog_price(order_prep: Dict[str, Any], brain_state: Dict[str, Any]) -> bool:
    if order_prep.get("order_flow_v2_trusted_price"):
        return True
    for item in line_items_from_state(order_prep, brain_state):
        if item.get("catalog_price") is not None or item.get("item_price") is not None:
            return True
        if str(item.get("price_source") or "").strip().lower() in {"catalog", "whatsapp_catalog"}:
            return True
    total = order_prep.get("order_flow_v2_catalog_total") or order_prep.get("order_total") or order_prep.get("total")
    return total not in (None, "", 0)


def activate_checkout_patch() -> Dict[str, Any]:
    return {
        "order_flow_v2_active": True,
        "order_flow_v2_pending": False,
        "order_status": "collecting_customer_info",
    }


def deactivate_checkout_patch() -> Dict[str, Any]:
    return {
        "order_flow_v2_active": False,
    }


def mark_pending_patch() -> Dict[str, Any]:
    return {
        "order_flow_v2_pending": True,
        "order_flow_v2_active": False,
    }


def has_complete_customer_identity(order_prep: Dict[str, Any]) -> bool:
    return bool(
        str(order_prep.get("customer_first_name") or "").strip()
        and str(order_prep.get("customer_last_name") or "").strip()
    )


def has_payment_method(order_prep: Dict[str, Any]) -> bool:
    return bool(str(order_prep.get("payment_method") or "").strip())
