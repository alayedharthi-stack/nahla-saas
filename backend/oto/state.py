"""Apply provider-confirmed shipping evidence without inventing fulfillment."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.hostname else None


def apply_oto_status(order: Any, shipment: Any, payload: Mapping[str, Any], *,
                     event_type: str = "orderStatus") -> bool:
    """Return False for stale/duplicate events; never mark shipped on API ack."""
    stamp = payload.get("timestamp")
    try:
        observed = datetime.fromtimestamp(int(stamp) / 1000, timezone.utc) if stamp else datetime.now(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return False
    prior = shipment.source_event_at
    if prior is not None and prior.tzinfo is None:
        prior = prior.replace(tzinfo=timezone.utc)
    if prior is not None and observed <= prior:
        return False
    if event_type == "shipmentError":
        status = "oto_shipment_error"
    else:
        raw = str(payload.get("status") or "").strip()
        if not raw:
            return False
        status = raw
    shipment.status = status
    shipment.tracking_data_source = "oto"
    shipment.source_event_at = observed
    shipment.last_verified_at = datetime.now(timezone.utc)
    shipment.latest_event = {"status": status, "at": observed.isoformat(), "source": "oto"}
    number = payload.get("trackingNumber") or payload.get("dcTrackingNumber")
    if number:
        shipment.tracking_number = str(number)[:120]
    external_id = payload.get("shipmentId") or payload.get("shipmentNumber")
    if external_id:
        shipment.external_shipment_id = str(external_id)[:120]
    if payload.get("deliveryCompany"):
        shipment.carrier = str(payload["deliveryCompany"])[:120]
    if safe_url(payload.get("trackingUrl")):
        shipment.tracking_url = safe_url(payload.get("trackingUrl"))
    if safe_url(payload.get("printAWBURL")):
        shipment.label_url = safe_url(payload.get("printAWBURL"))
    if event_type == "orderStatus":
        key = status.lower()
        if key == "delivered":
            order.status = "delivered"
        elif key == "returned":
            order.status = "returned"
        elif key in {"pickedup", "outfordelivery", "shipped"} and order.status not in {"delivered", "returned"}:
            order.status = "shipped"
    return True
