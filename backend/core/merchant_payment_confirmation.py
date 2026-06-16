"""
core/merchant_payment_confirmation.py
──────────────────────────────────────
PR-2B — Merchant manual confirmation of bank-transfer payments.

Operational only: explicit ``payment_confirmed=true`` after merchant verifies
funds in their bank account.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from core.order_payment_policy import (
    ORDER_STATUS_PAYMENT_SUBMITTED,
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_STATUS_PAID,
    PAYMENT_STATUS_PENDING_VERIFICATION,
    infer_payment_method,
    is_payment_explicitly_confirmed,
    is_provider_payment_confirmed,
)
from core.wa_order_lifecycle import (
    STATUS_PENDING_CUSTOMER_INFO,
    has_accepted_delivery_address,
)

CONFIRM_BLOCKED_STATUSES = frozenset({
    "draft",
    "pending_customer_info",
    "cancelled",
    "canceled",
    "completed",
    "complete",
    "abandoned",
    "processing",
    "ready_to_ship",
    "ready_to_process",
    "shipment_created",
    "label_generated",
})

ADDRESS_MISSING_MERCHANT_NOTICE = (
    "تم تأكيد الدفع، لكن لا يمكن تجهيز الطلب أو شحنه قبل إضافة "
    "رابط Google Maps أو رمز العنوان الوطني المختصر."
)


def _order_address_prep(order: Any) -> Dict[str, Any]:
    meta = getattr(order, "extra_metadata", None) or {}
    customer = getattr(order, "customer_info", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(customer, dict):
        customer = {}
    return {
        "short_address_code": str(
            meta.get("short_address_code") or customer.get("short_address_code") or ""
        ).strip(),
        "google_maps_url": str(
            meta.get("google_maps_url") or customer.get("google_maps_url") or ""
        ).strip(),
    }


def order_has_accepted_delivery_address(order: Any) -> bool:
    return has_accepted_delivery_address(_order_address_prep(order))


def can_merchant_confirm_bank_transfer(order: Any) -> Tuple[bool, str]:
    """
    Return ``(allowed, reason)``.

    ``reason`` is ``already_confirmed`` for idempotent re-confirmation.
    """
    meta = getattr(order, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    status = str(getattr(order, "status", "") or "").strip().lower()
    payment_method = infer_payment_method(None, meta)

    if payment_method != PAYMENT_METHOD_BANK_TRANSFER:
        return False, "not_bank_transfer"

    if is_provider_payment_confirmed(meta):
        return False, "provider_confirmed_order"

    if is_payment_explicitly_confirmed(None, meta):
        return True, "already_confirmed"

    if status in CONFIRM_BLOCKED_STATUSES:
        return False, f"status_not_eligible:{status}"

    if status != ORDER_STATUS_PAYMENT_SUBMITTED:
        return False, f"status_not_eligible:{status}"

    payment_status = str(meta.get("payment_status") or "").strip().lower()
    if payment_status and payment_status not in (
        PAYMENT_STATUS_PENDING_VERIFICATION,
        "submitted",
        "pending",
    ):
        return False, f"payment_status_not_eligible:{payment_status}"

    return True, "eligible"


def can_show_confirm_bank_transfer_button(order: Any) -> bool:
    allowed, reason = can_merchant_confirm_bank_transfer(order)
    return allowed and reason == "eligible"


def apply_merchant_payment_confirmation(
    order: Any,
    *,
    verified_by: str,
    verified_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Mutate ``order`` in-place. Returns a result dict for API responses.
    """
    allowed, reason = can_merchant_confirm_bank_transfer(order)
    if not allowed:
        raise ValueError(reason)

    now_iso = verified_at or datetime.now(timezone.utc).isoformat()
    meta = dict(getattr(order, "extra_metadata", None) or {})
    prev_status = str(getattr(order, "status", "") or "")

    if reason == "already_confirmed":
        return {
            "idempotent":          True,
            "order_id":            getattr(order, "id", None),
            "status":              prev_status,
            "payment_confirmed":   True,
            "payment_status":      meta.get("payment_status"),
            "address_accepted":    order_has_accepted_delivery_address(order),
            "merchant_notice":     meta.get("merchant_post_confirm_notice"),
        }

    has_address = order_has_accepted_delivery_address(order)
    target_status = "paid" if has_address else STATUS_PENDING_CUSTOMER_INFO

    meta["payment_confirmed"] = True
    meta["payment_status"] = PAYMENT_STATUS_PAID
    meta["payment_verification_source"] = "merchant_manual"
    meta["payment_verified_by"] = verified_by
    meta["payment_verified_at"] = now_iso
    meta["verified_by_staff"] = True
    meta["payment_verification_status"] = "confirmed"
    meta["counts_in_revenue"] = True

    if has_address:
        meta.pop("merchant_post_confirm_notice", None)
        missing = [m for m in (meta.get("missing_fields") or []) if m != "delivery_address"]
        meta["missing_fields"] = missing
        meta["delivery_address_status"] = "accepted"
    else:
        missing = list(meta.get("missing_fields") or [])
        if "delivery_address" not in missing:
            missing.append("delivery_address")
        meta["missing_fields"] = missing
        meta["delivery_address_status"] = "required"
        meta["merchant_post_confirm_notice"] = ADDRESS_MISSING_MERCHANT_NOTICE

    payment_timeline = list(meta.get("payment_timeline") or [])
    payment_timeline.append({
        "event":          "payment_confirmed",
        "source":         "merchant_manual",
        "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
        "verified_by":    verified_by,
        "verified_at":    now_iso,
    })
    meta["payment_timeline"] = payment_timeline[-50:]

    status_timeline = list(meta.get("status_timeline") or [])
    status_timeline.append({
        "from":   prev_status or "none",
        "to":     target_status,
        "at":     now_iso,
        "reason": "merchant_manual_payment_confirmation",
    })
    meta["status_timeline"] = status_timeline[-50:]
    meta["status_changed_at"] = now_iso

    order.status = target_status
    order.extra_metadata = meta
    if hasattr(order, "is_abandoned"):
        order.is_abandoned = False

    return {
        "idempotent":        False,
        "order_id":          getattr(order, "id", None),
        "status":            target_status,
        "payment_confirmed": True,
        "payment_status":    PAYMENT_STATUS_PAID,
        "address_accepted":  has_address,
        "merchant_notice":   None if has_address else ADDRESS_MISSING_MERCHANT_NOTICE,
    }


__all__ = [
    "ADDRESS_MISSING_MERCHANT_NOTICE",
    "CONFIRM_BLOCKED_STATUSES",
    "apply_merchant_payment_confirmation",
    "can_merchant_confirm_bank_transfer",
    "can_show_confirm_bank_transfer_button",
    "order_has_accepted_delivery_address",
]
