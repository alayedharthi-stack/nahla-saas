"""
core/wa_order_lifecycle.py
──────────────────────────
Deterministic WhatsApp order status + completeness for Nahla-native orders.

Operational only — no LLM, no KB. Used by ``services.nahla_order_bridge`` to
decide ``Order.status`` and ``extra_metadata.missing_fields`` from persisted
brain ``order_prep`` state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Canonical WhatsApp order statuses (platform-wide constants).
STATUS_DRAFT = "draft"
STATUS_PENDING_CUSTOMER_INFO = "pending_customer_info"
STATUS_PENDING_PAYMENT = "pending_payment"
STATUS_PAYMENT_SUBMITTED = "payment_submitted"
STATUS_PAID = "paid"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_ABANDONED = "abandoned"

ADDRESS_REQUIRED_TYPE = "google_maps_url_or_short_national_address"

_CUSTOMER_INFO_FIELDS = (
    "customer_first_name",
    "customer_last_name",
    "city",
)


def has_payment_submission(order_prep: Dict[str, Any]) -> bool:
    """True when customer submitted payment evidence or a text claim."""
    return bool(
        order_prep.get("payment_receipt_received")
        or order_prep.get("payment_submission_received")
        or order_prep.get("payment_claim_submitted")
    )


def is_payment_verified(order_prep: Dict[str, Any]) -> bool:
    """Explicit verification only — never inferred from receipt/claim alone."""
    return bool(
        order_prep.get("payment_confirmed")
        or order_prep.get("verified_by_staff")
        or order_prep.get("payment_verified")
    )


def _prep_str(order_prep: Dict[str, Any], key: str) -> str:
    return str(order_prep.get(key) or "").strip()


def has_accepted_delivery_address(order_prep: Dict[str, Any]) -> bool:
    """True for Google/Apple Maps URL, national short code, or WA location pin."""
    if _prep_str(order_prep, "short_address_code"):
        return True
    if _prep_str(order_prep, "google_maps_url"):
        return True
    if _prep_str(order_prep, "delivery_address_url"):
        return True
    lat = order_prep.get("latitude") or order_prep.get("delivery_location_lat")
    lng = order_prep.get("longitude") or order_prep.get("delivery_location_lng")
    if lat is not None and lng is not None:
        try:
            float(lat)
            float(lng)
            return True
        except (TypeError, ValueError):
            pass
    if _prep_str(order_prep, "delivery_address_status") == "accepted":
        return bool(
            _prep_str(order_prep, "delivery_location_lat")
            and _prep_str(order_prep, "delivery_location_lng")
            or _prep_str(order_prep, "google_maps_url")
            or _prep_str(order_prep, "delivery_address_url")
        )
    pending = order_prep.get("pending_delivery_location")
    if isinstance(pending, dict):
        pending_status = str(pending.get("delivery_address_status") or "").strip().lower()
        if pending_status == "accepted":
            if str(pending.get("google_maps_url") or pending.get("delivery_address_url") or "").strip():
                return True
            lat = pending.get("latitude") or pending.get("delivery_location_lat")
            lng = pending.get("longitude") or pending.get("delivery_location_lng")
            if lat is not None and lng is not None:
                try:
                    float(lat)
                    float(lng)
                    return True
                except (TypeError, ValueError):
                    pass
    return False


def sync_funnel_status_after_accepted_delivery(
    order_prep: Dict[str, Any],
) -> Optional[str]:
    """Return the funnel marker after delivery is accepted.

    ``order_status=awaiting_address`` is descriptive leftover once maps/short
    code/pin evidence exists. Canonical missing-fields owns what to collect
    next; this only clears the stale address funnel label.
    """
    if not has_accepted_delivery_address(order_prep):
        return None
    status = str(order_prep.get("order_status") or "").strip()
    if status != "awaiting_address":
        return None
    return "awaiting_payment"


def _has_cart_items(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    if line_items:
        return True
    for container in (order_prep, brain_state):
        if not isinstance(container, dict):
            continue
        for key in ("line_items", "cart_items", "items"):
            raw = container.get(key)
            if isinstance(raw, list) and raw:
                return True
    return False


def _has_product(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    *,
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    if _has_cart_items(order_prep, brain_state, line_items):
        return True
    if _prep_str(order_prep, "product_id"):
        return True
    focus = brain_state.get("current_product_focus") or {}
    if isinstance(focus, dict) and (focus.get("id") or focus.get("title")):
        return True
    return False


def _has_any_customer_info(order_prep: Dict[str, Any]) -> bool:
    return any(_prep_str(order_prep, key) for key in _CUSTOMER_INFO_FIELDS)


def compute_wa_missing_fields(
    order_prep: Dict[str, Any],
    *,
    brain_state: Optional[Dict[str, Any]] = None,
    whatsapp_phone: Optional[str] = None,
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Fields still required before ``pending_payment``. Phone is never listed."""
    missing: List[str] = []
    bs = brain_state or {}

    if not _has_product(order_prep, bs, line_items=line_items):
        missing.append("product")

    if not _prep_str(order_prep, "customer_first_name"):
        missing.append("customer_first_name")
    if not _prep_str(order_prep, "customer_last_name"):
        missing.append("customer_last_name")
    if not _prep_str(order_prep, "city"):
        missing.append("city")

    if not has_accepted_delivery_address(order_prep):
        missing.append("delivery_address")

    # WhatsApp conversation already carries the customer phone — never ask.
    _ = whatsapp_phone
    return missing


def resolve_wa_order_status(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    *,
    whatsapp_phone: Optional[str] = None,
    payment_verified: bool = False,
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[str], List[str], str]:
    """
    Return ``(status, missing_fields, delivery_address_status)``.

    ``status`` is ``None`` when no product is selected (no order row yet).
    """
    if not _has_product(order_prep, brain_state, line_items=line_items):
        if has_payment_submission(order_prep):
            missing = compute_wa_missing_fields(
                order_prep,
                brain_state=brain_state,
                whatsapp_phone=whatsapp_phone,
                line_items=line_items,
            )
            addr_status = (
                "accepted" if has_accepted_delivery_address(order_prep) else "required"
            )
            if is_payment_verified(order_prep) and payment_verified:
                if not has_accepted_delivery_address(order_prep):
                    return STATUS_PAYMENT_SUBMITTED, missing, addr_status
                return STATUS_PAID, missing, addr_status
            return STATUS_PAYMENT_SUBMITTED, missing, addr_status
        return None, [], "none"

    missing = compute_wa_missing_fields(
        order_prep,
        brain_state=brain_state,
        whatsapp_phone=whatsapp_phone,
        line_items=line_items,
    )
    addr_status = "accepted" if has_accepted_delivery_address(order_prep) else "required"

    if has_payment_submission(order_prep):
        if is_payment_verified(order_prep) and payment_verified:
            if not has_accepted_delivery_address(order_prep):
                return STATUS_PAYMENT_SUBMITTED, missing, addr_status
            return STATUS_PAID, missing, addr_status
        return STATUS_PAYMENT_SUBMITTED, missing, addr_status

    if not missing:
        return STATUS_PENDING_PAYMENT, missing, addr_status

    if _has_product(order_prep, brain_state, line_items=line_items) and not _has_any_customer_info(order_prep):
        if "delivery_address" not in missing or not has_accepted_delivery_address(order_prep):
            return STATUS_DRAFT, missing, addr_status

    return STATUS_PENDING_CUSTOMER_INFO, missing, addr_status


def is_wa_automation_payment_eligible(
    status: str,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Only ``pending_payment`` Nahla WA orders may enter unpaid-order automations."""
    meta = extra_metadata or {}
    if meta.get("created_via") != "nahla_order_bridge":
        return True
    norm = str(status or "").strip().lower()
    return norm == STATUS_PENDING_PAYMENT


__all__ = [
    "ADDRESS_REQUIRED_TYPE",
    "STATUS_ABANDONED",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_DRAFT",
    "STATUS_PAID",
    "STATUS_PAYMENT_SUBMITTED",
    "STATUS_PENDING_CUSTOMER_INFO",
    "STATUS_PENDING_PAYMENT",
    "STATUS_PROCESSING",
    "compute_wa_missing_fields",
    "has_accepted_delivery_address",
    "sync_funnel_status_after_accepted_delivery",
    "has_payment_submission",
    "is_payment_verified",
    "is_wa_automation_payment_eligible",
    "resolve_wa_order_status",
]
