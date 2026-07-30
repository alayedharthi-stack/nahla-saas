"""
core/order_shipment_service.py
──────────────────────────────
Internal shipment + label placeholder operations (no external carrier).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from core.order_payment_policy import (
    ORDER_STATUS_LABEL_GENERATED,
    ORDER_STATUS_SHIPMENT_CREATED,
)
from core.order_shipping_policy import (
    SHIPMENT_STATUS_CREATED,
    SHIPMENT_STATUS_LABEL_GENERATED,
    ShippingGateResult,
    build_order_address_prep,
    can_create_shipment,
    can_generate_label,
    order_has_accepted_address,
)
from core.wa_order_dashboard import build_delivery_location_display


def resolve_tenant_cod_enabled(db: Any, tenant_id: int) -> bool:
    """Read COD toggle from tenant settings; default True when unset."""
    try:
        from models import TenantSettings  # noqa: PLC0415
    except ImportError:
        return True

    row = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    if not row:
        return True

    ai = getattr(row, "ai_settings", None) or {}
    if not isinstance(ai, dict):
        ai = {}
    store = getattr(row, "store_settings", None) or {}
    if not isinstance(store, dict):
        store = {}

    for container in (ai, store):
        val = container.get("cash_on_delivery_enabled")
        if val is None:
            val = container.get("cod_enabled")
        if val is not None:
            return bool(val)
    return True


def _order_meta(order: Any) -> Dict[str, Any]:
    meta = getattr(order, "extra_metadata", None) or {}
    return meta if isinstance(meta, dict) else {}


def _append_status_timeline(order: Any, event: str, *, note: str = "") -> None:
    meta = _order_meta(order)
    timeline = list(meta.get("status_timeline") or [])
    timeline.append({
        "event": event,
        "at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    })
    meta["status_timeline"] = timeline
    order.extra_metadata = meta


def _append_shipment_timeline(order: Any, event: str, *, shipment_id: int) -> None:
    meta = _order_meta(order)
    timeline = list(meta.get("shipment_timeline") or [])
    timeline.append({
        "event": event,
        "shipment_id": shipment_id,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    meta["shipment_timeline"] = timeline
    order.extra_metadata = meta


def _address_snapshot(order: Any) -> Dict[str, Any]:
    prep = build_order_address_prep(order)
    display = build_delivery_location_display(order)
    address_type = "unknown"
    address_url = (
        str(prep.get("google_maps_url") or prep.get("delivery_address_url") or "").strip()
        or None
    )
    if prep.get("short_address_code"):
        address_type = "short_address_code"
    elif address_url:
        address_type = "maps_url"
    elif prep.get("latitude") is not None and prep.get("longitude") is not None:
        address_type = "location_pin"

    address_text = None
    if isinstance(display, dict):
        address_text = str(display.get("summary") or display.get("label") or "").strip() or None

    customer = getattr(order, "customer_info", None) or {}
    if not isinstance(customer, dict):
        customer = {}

    return {
        "address_type": address_type,
        "address_text": address_text,
        "address_url": address_url,
        "latitude": str(prep.get("latitude") or prep.get("delivery_location_lat") or "") or None,
        "longitude": str(prep.get("longitude") or prep.get("delivery_location_lng") or "") or None,
        "recipient_name": (
            str(getattr(order, "customer_name", "") or customer.get("name") or "").strip()
            or None
        ),
        "recipient_phone": (
            str(customer.get("phone") or customer.get("mobile") or "").strip() or None
        ),
    }


def get_order_shipment(db: Any, tenant_id: int, order_id: int) -> Any:
    from models import OrderShipment  # noqa: PLC0415

    return (
        db.query(OrderShipment)
        .filter(
            OrderShipment.tenant_id == tenant_id,
            OrderShipment.order_id == order_id,
        )
        .order_by(OrderShipment.id.desc())
        .first()
    )


def serialise_shipment(shipment: Any) -> Dict[str, Any]:
    meta = getattr(shipment, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    created = getattr(shipment, "created_at", None)
    updated = getattr(shipment, "updated_at", None)
    return {
        "id": shipment.id,
        "order_id": shipment.order_id,
        "provider": shipment.provider,
        "status": shipment.status,
        "status_label_ar": _shipment_status_label_ar(shipment.status),
        "tracking_number": shipment.tracking_number,
        "label_url": shipment.label_url,
        "label_pdf_path": shipment.label_pdf_path,
        "recipient_name": shipment.recipient_name,
        "recipient_phone": shipment.recipient_phone,
        "address_type": shipment.address_type,
        "address_text": shipment.address_text,
        "address_url": shipment.address_url,
        "latitude": shipment.latitude,
        "longitude": shipment.longitude,
        "cod_amount": shipment.cod_amount,
        "created_at": created.isoformat() if created else None,
        "updated_at": updated.isoformat() if updated else None,
        "label_placeholder": bool(meta.get("label_placeholder")),
        "extra_metadata": meta,
    }


def _shipment_status_label_ar(status: str) -> str:
    norm = str(status or "").strip().lower()
    if norm == SHIPMENT_STATUS_CREATED:
        return "تم إنشاء الشحنة"
    if norm == SHIPMENT_STATUS_LABEL_GENERATED:
        return "تم توليد البوليصة"
    return norm or "—"


def evaluate_create_shipment(
    order: Any,
    *,
    cod_enabled: bool,
    existing_shipment: Any = None,
) -> ShippingGateResult:
    return can_create_shipment(
        order,
        cod_enabled=cod_enabled,
        has_existing_shipment=existing_shipment is not None,
    )


def create_order_shipment(
    db: Any,
    *,
    tenant_id: int,
    order: Any,
    verified_by: str,
) -> Tuple[Any, Dict[str, Any]]:
    """
    Create internal shipment row + stamp order status.

    Raises ``ValueError`` with reason_key when blocked.
    """
    from core.acceptance_execution_context import deny_external_egress  # noqa: PLC0415

    deny_external_egress(
        egress_kind="shipping",
        operation="create_order_shipment",
        tenant_id=tenant_id,
    )

    from models import OrderShipment  # noqa: PLC0415

    cod_enabled = resolve_tenant_cod_enabled(db, tenant_id)
    existing = get_order_shipment(db, tenant_id, order.id)
    gate = evaluate_create_shipment(order, cod_enabled=cod_enabled, existing_shipment=existing)
    if not gate.allowed:
        raise ValueError(gate.reason_key or "shipment_blocked")

    if not order_has_accepted_address(order):
        raise ValueError("address_missing")

    snap = _address_snapshot(order)
    meta = _order_meta(order)
    cod_amount = None
    if str(meta.get("payment_method") or "").strip().lower() == "cash_on_delivery":
        cod_amount = str(getattr(order, "total", "") or "").strip() or None

    shipment = OrderShipment(
        tenant_id=tenant_id,
        order_id=order.id,
        provider="internal",
        status=SHIPMENT_STATUS_CREATED,
        recipient_name=snap["recipient_name"],
        recipient_phone=snap["recipient_phone"],
        address_type=snap["address_type"],
        address_text=snap["address_text"],
        address_url=snap["address_url"],
        latitude=snap["latitude"],
        longitude=snap["longitude"],
        cod_amount=cod_amount,
        extra_metadata={
            "created_by": verified_by,
            "placeholder_carrier": True,
        },
    )
    db.add(shipment)
    db.flush()

    order.status = ORDER_STATUS_SHIPMENT_CREATED
    _append_status_timeline(order, "shipment_created", note=f"shipment_id={shipment.id}")
    _append_shipment_timeline(order, "shipment_created", shipment_id=shipment.id)

    meta = _order_meta(order)
    meta["latest_shipment_id"] = shipment.id
    order.extra_metadata = meta

    return shipment, serialise_shipment(shipment)


def generate_shipment_label(
    db: Any,
    *,
    tenant_id: int,
    order: Any,
    shipment: Any,
    verified_by: str,
) -> Dict[str, Any]:
    """
    Placeholder label generation — stores metadata only (no external PDF).
    """
    from core.acceptance_execution_context import deny_external_egress  # noqa: PLC0415

    deny_external_egress(
        egress_kind="shipping",
        operation="generate_shipment_label",
        tenant_id=tenant_id,
    )

    cod_enabled = resolve_tenant_cod_enabled(db, tenant_id)
    gate = can_generate_label(order, shipment, cod_enabled=cod_enabled)
    if not gate.allowed:
        raise ValueError(gate.reason_key or "label_blocked")

    label_ref = f"nahla-label-{tenant_id}-{order.id}-{shipment.id}"
    shipment.status = SHIPMENT_STATUS_LABEL_GENERATED
    shipment.label_url = f"/orders/{order.id}/shipments/{shipment.id}/label"
    meta = getattr(shipment, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({
        "label_placeholder": True,
        "label_generated_by": verified_by,
        "label_generated_at": datetime.now(timezone.utc).isoformat(),
        "label_reference": label_ref,
    })
    shipment.extra_metadata = meta

    order.status = ORDER_STATUS_LABEL_GENERATED
    _append_status_timeline(order, "label_generated", note=f"shipment_id={shipment.id}")
    _append_shipment_timeline(order, "label_generated", shipment_id=shipment.id)

    db.flush()
    return serialise_shipment(shipment)


__all__ = [
    "create_order_shipment",
    "evaluate_create_shipment",
    "generate_shipment_label",
    "get_order_shipment",
    "resolve_tenant_cod_enabled",
    "serialise_shipment",
]
