"""
core/order_shipping_policy.py
─────────────────────────────
Platform-wide shipping / label eligibility for Nahla orders.

Operational gates only — no carrier integration in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.order_payment_policy import (
    ORDER_STATUS_COD_PENDING,
    ORDER_STATUS_PAYMENT_SUBMITTED,
    ORDER_STATUS_READY_TO_PROCESS,
    ORDER_STATUS_READY_TO_SHIP,
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_CASH_ON_DELIVERY,
    PAYMENT_STATUS_COD_PENDING,
    can_create_shipment as payment_allows_shipment,
    infer_payment_method,
    is_payment_explicitly_confirmed,
    is_provider_payment_confirmed,
)
from core.wa_order_dashboard import _address_prep
from core.wa_order_lifecycle import has_accepted_delivery_address

SHIPMENT_STATUS_CREATED = "shipment_created"
SHIPMENT_STATUS_LABEL_GENERATED = "label_generated"

SHIPPING_BLOCKED_STATUSES = frozenset({
    "draft",
    "pending_customer_info",
    "pending_payment",
    ORDER_STATUS_PAYMENT_SUBMITTED,
    "cancelled",
    "canceled",
    "completed",
    "complete",
    "abandoned",
})

SHIPPING_ELIGIBLE_ORDER_STATUSES = frozenset({
    "paid",
    ORDER_STATUS_COD_PENDING,
    ORDER_STATUS_READY_TO_PROCESS,
    ORDER_STATUS_READY_TO_SHIP,
})

MSG_BANK_TRANSFER_NOT_CONFIRMED = (
    "لا يمكن إنشاء الشحنة قبل تأكيد التحويل البنكي."
)
MSG_ADDRESS_MISSING = (
    "لا يمكن إنشاء الشحنة قبل إضافة الموقع أو الرمز الوطني."
)
MSG_ORDER_STATUS_BLOCKED = "لا يمكن إنشاء شحنة لهذا الطلب في حالته الحالية."
MSG_COD_DISABLED = "دفع عند الاستلام غير مفعّل لهذا المتجر."
MSG_SHIPMENT_EXISTS = "توجد شحنة مسجّلة لهذا الطلب بالفعل."
MSG_NO_SHIPMENT = "لا توجد شحنة لهذا الطلب."
MSG_LABEL_ALREADY_GENERATED = "تم توليد البوليصة مسبقاً."
MSG_LABEL_NOT_READY = "لا يمكن توليد البوليصة قبل إنشاء الشحنة."


@dataclass(frozen=True)
class ShippingGateResult:
    allowed: bool
    reason_key: Optional[str] = None
    message_ar: Optional[str] = None


def build_order_address_prep(order: Any) -> Dict[str, Any]:
    """Address evidence prep shared with WA lifecycle helpers."""
    return _address_prep(order)


def order_has_accepted_address(order: Any) -> bool:
    return has_accepted_delivery_address(build_order_address_prep(order))


def shipping_block_reason(
    order: Any,
    *,
    cod_enabled: bool = True,
    has_existing_shipment: bool = False,
) -> Optional[str]:
    """Human-readable Arabic block reason, or ``None`` if allowed."""
    result = can_create_shipment(
        order,
        cod_enabled=cod_enabled,
        has_existing_shipment=has_existing_shipment,
    )
    return result.message_ar if not result.allowed else None


def _payment_and_address_gate(
    order: Any,
    *,
    cod_enabled: bool,
    fulfillment_phase: bool = False,
) -> ShippingGateResult:
    """Payment + address checks shared by create-shipment and generate-label."""
    status = str(getattr(order, "status", "") or "").strip().lower()
    meta = getattr(order, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    payment_method = infer_payment_method(None, meta)
    if payment_method == PAYMENT_METHOD_CASH_ON_DELIVERY:
        if not cod_enabled:
            return ShippingGateResult(
                allowed=False,
                reason_key="cod_disabled",
                message_ar=MSG_COD_DISABLED,
            )
        payment_status = str(meta.get("payment_status") or "").strip().lower()
        if payment_status and payment_status != PAYMENT_STATUS_COD_PENDING:
            return ShippingGateResult(
                allowed=False,
                reason_key="cod_payment_status",
                message_ar=MSG_ORDER_STATUS_BLOCKED,
            )

    if fulfillment_phase:
        confirmed = (
            is_payment_explicitly_confirmed(None, meta)
            or is_provider_payment_confirmed(meta)
        )
        if payment_method == PAYMENT_METHOD_BANK_TRANSFER:
            if not confirmed:
                return ShippingGateResult(
                    allowed=False,
                    reason_key="bank_transfer_not_confirmed",
                    message_ar=MSG_BANK_TRANSFER_NOT_CONFIRMED,
                )
        elif payment_method == PAYMENT_METHOD_CASH_ON_DELIVERY:
            pass
        elif not confirmed:
            return ShippingGateResult(
                allowed=False,
                reason_key="bank_transfer_not_confirmed",
                message_ar=MSG_BANK_TRANSFER_NOT_CONFIRMED,
            )
    elif not payment_allows_shipment(
        order_status=status,
        meta=meta,
        cod_enabled=cod_enabled,
    ):
        if payment_method == PAYMENT_METHOD_CASH_ON_DELIVERY:
            return ShippingGateResult(
                allowed=False,
                reason_key="cod_not_eligible",
                message_ar=MSG_ORDER_STATUS_BLOCKED,
            )
        return ShippingGateResult(
            allowed=False,
            reason_key="bank_transfer_not_confirmed",
            message_ar=MSG_BANK_TRANSFER_NOT_CONFIRMED,
        )

    if not order_has_accepted_address(order):
        return ShippingGateResult(
            allowed=False,
            reason_key="address_missing",
            message_ar=MSG_ADDRESS_MISSING,
        )

    return ShippingGateResult(allowed=True)


def can_create_shipment(
    order: Any,
    *,
    cod_enabled: bool = True,
    has_existing_shipment: bool = False,
) -> ShippingGateResult:
    """
    Full shipment-creation gate: payment, address, lifecycle, duplicates.
    """
    if has_existing_shipment:
        return ShippingGateResult(
            allowed=False,
            reason_key="shipment_exists",
            message_ar=MSG_SHIPMENT_EXISTS,
        )

    status = str(getattr(order, "status", "") or "").strip().lower()
    if status in SHIPPING_BLOCKED_STATUSES:
        if status == ORDER_STATUS_PAYMENT_SUBMITTED:
            return ShippingGateResult(
                allowed=False,
                reason_key="payment_submitted",
                message_ar=MSG_ORDER_STATUS_BLOCKED,
            )
        if status == "pending_customer_info":
            return ShippingGateResult(
                allowed=False,
                reason_key="pending_customer_info",
                message_ar=MSG_ADDRESS_MISSING,
            )
        return ShippingGateResult(
            allowed=False,
            reason_key="order_status_blocked",
            message_ar=MSG_ORDER_STATUS_BLOCKED,
        )

    if status in ("shipment_created", "label_generated", "shipped", "delivered", "processing"):
        return ShippingGateResult(
            allowed=False,
            reason_key="shipment_exists" if has_existing_shipment else "order_status_blocked",
            message_ar=MSG_SHIPMENT_EXISTS if has_existing_shipment else MSG_ORDER_STATUS_BLOCKED,
        )

    if status not in SHIPPING_ELIGIBLE_ORDER_STATUSES:
        return ShippingGateResult(
            allowed=False,
            reason_key="order_status_blocked",
            message_ar=MSG_ORDER_STATUS_BLOCKED,
        )

    return _payment_and_address_gate(order, cod_enabled=cod_enabled)


def can_generate_label(
    order: Any,
    shipment: Any,
    *,
    cod_enabled: bool = True,
) -> ShippingGateResult:
    """Label generation gate — requires an existing internal shipment row."""
    if shipment is None:
        return ShippingGateResult(
            allowed=False,
            reason_key="no_shipment",
            message_ar=MSG_NO_SHIPMENT,
        )

    ship_status = str(getattr(shipment, "status", "") or "").strip().lower()
    if ship_status == SHIPMENT_STATUS_LABEL_GENERATED:
        return ShippingGateResult(
            allowed=False,
            reason_key="label_already_generated",
            message_ar=MSG_LABEL_ALREADY_GENERATED,
        )
    if ship_status != SHIPMENT_STATUS_CREATED:
        return ShippingGateResult(
            allowed=False,
            reason_key="label_not_ready",
            message_ar=MSG_LABEL_NOT_READY,
        )

    status = str(getattr(order, "status", "") or "").strip().lower()
    if status in SHIPPING_BLOCKED_STATUSES:
        if status == ORDER_STATUS_PAYMENT_SUBMITTED:
            return ShippingGateResult(
                allowed=False,
                reason_key="payment_submitted",
                message_ar=MSG_ORDER_STATUS_BLOCKED,
            )
        return ShippingGateResult(
            allowed=False,
            reason_key="order_status_blocked",
            message_ar=MSG_ORDER_STATUS_BLOCKED,
        )
    if status in ("cancelled", "canceled", "completed", "complete", "abandoned"):
        return ShippingGateResult(
            allowed=False,
            reason_key="order_status_blocked",
            message_ar=MSG_ORDER_STATUS_BLOCKED,
        )

    return _payment_and_address_gate(
        order,
        cod_enabled=cod_enabled,
        fulfillment_phase=True,
    )


__all__ = [
    "MSG_ADDRESS_MISSING",
    "MSG_BANK_TRANSFER_NOT_CONFIRMED",
    "SHIPMENT_STATUS_CREATED",
    "SHIPMENT_STATUS_LABEL_GENERATED",
    "ShippingGateResult",
    "build_order_address_prep",
    "can_create_shipment",
    "can_generate_label",
    "order_has_accepted_address",
    "shipping_block_reason",
]
