"""
core/customer_shipping_address_writer.py
────────────────────────────────────────
Persist confirmed customer shipping addresses to ``customer_addresses``.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from core.order_shipping_snapshot import (
    apply_shipping_snapshot_to_customer_info,
    apply_shipping_snapshot_to_metadata,
    build_order_shipping_snapshot,
    shipping_fields_locked,
    shipping_snapshot_confirmed,
)

logger = logging.getLogger("nahla.customer_shipping_address_writer")


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def address_fingerprint(
    *,
    customer_id: int,
    snapshot: Mapping[str, Any],
) -> str:
    parts = [
        str(customer_id),
        _clean_str(snapshot.get("city")).lower(),
        _clean_str(snapshot.get("short_address_code")).upper(),
        _clean_str(snapshot.get("google_maps_url")).lower(),
        _clean_str(snapshot.get("lat")),
        _clean_str(snapshot.get("lng")),
        _clean_str(snapshot.get("address_line")).lower(),
        _clean_str(snapshot.get("district")).lower(),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_to_row_fields(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    short_code = _clean_str(snapshot.get("short_address_code"))
    maps_url = _clean_str(snapshot.get("google_maps_url"))
    lat = _clean_str(snapshot.get("lat"))
    lng = _clean_str(snapshot.get("lng"))
    wa_loc = snapshot.get("whatsapp_location")
    address_text = _clean_str(snapshot.get("address_line")) or _clean_str(snapshot.get("street"))
    row: Dict[str, Any] = {
        "city": _clean_str(snapshot.get("city")) or None,
        "district": _clean_str(snapshot.get("district")) or None,
        "address_text": address_text or None,
        "saudi_national_address": short_code or None,
        "google_maps_link": maps_url or None,
        "lat": lat or None,
        "lng": lng or None,
        "whatsapp_location": dict(wa_loc) if isinstance(wa_loc, dict) and wa_loc else None,
        "raw_address": address_text or None,
        "address_type": "confirmed_shipping",
    }
    return row


def _find_existing_address(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    fingerprint: str,
) -> Any:
    from models import CustomerAddress  # noqa: PLC0415

    rows = (
        db.query(CustomerAddress)
        .filter_by(tenant_id=int(tenant_id), customer_id=int(customer_id))
        .order_by(CustomerAddress.id.desc())
        .limit(50)
        .all()
    )
    for row in rows:
        snap = customer_address_to_snapshot(row)
        if address_fingerprint(customer_id=int(customer_id), snapshot=snap) == fingerprint:
            return row
    return None


def customer_address_to_snapshot(row: Any) -> Dict[str, Any]:
    """Map ``CustomerAddress`` row to unified short-address field names."""
    short_code = _clean_str(getattr(row, "saudi_national_address", None))
    maps_url = _clean_str(getattr(row, "google_maps_link", None))
    return {
        "city": _clean_str(getattr(row, "city", None)),
        "district": _clean_str(getattr(row, "district", None)),
        "address_line": _clean_str(getattr(row, "address_text", None)),
        "short_address_code": short_code,
        "google_maps_url": maps_url,
        "delivery_address_url": maps_url,
        "lat": _clean_str(getattr(row, "lat", None)),
        "lng": _clean_str(getattr(row, "lng", None)),
        "whatsapp_location": dict(getattr(row, "whatsapp_location", None) or {})
        if getattr(row, "whatsapp_location", None)
        else None,
        "source": "customer_addresses",
        "accepted_delivery_address": bool(
            short_code or maps_url or getattr(row, "whatsapp_location", None)
        ),
    }


def persist_customer_shipping_address_if_confirmed(
    db: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    order_id: Optional[int],
    snapshot: Mapping[str, Any],
    extra_metadata: Optional[Mapping[str, Any]] = None,
    order_prep: Optional[Mapping[str, Any]] = None,
    confirmed_reason: str = "",
) -> Tuple[bool, Optional[Any]]:
    """
    Upsert ``customer_addresses`` when shipping evidence is confirmed.

    Returns ``(persisted, row_or_none)``.
    """
    if not customer_id:
        return False, None

    confirmed, inferred_reason = shipping_snapshot_confirmed(
        snapshot,
        extra_metadata=extra_metadata,
        order_prep=order_prep,
    )
    reason = confirmed_reason or inferred_reason
    if not confirmed:
        return False, None

    city = _clean_str(snapshot.get("city"))
    has_evidence = bool(
        _clean_str(snapshot.get("short_address_code"))
        or _clean_str(snapshot.get("google_maps_url"))
        or snapshot.get("whatsapp_location")
        or (_clean_str(snapshot.get("lat")) and _clean_str(snapshot.get("lng")))
    )
    if not city and not has_evidence:
        return False, None

    from models import CustomerAddress  # noqa: PLC0415

    fingerprint = address_fingerprint(customer_id=int(customer_id), snapshot=snapshot)
    existing = _find_existing_address(
        db,
        tenant_id=tenant_id,
        customer_id=int(customer_id),
        fingerprint=fingerprint,
    )
    fields = _snapshot_to_row_fields(snapshot)
    if existing is not None:
        for key, val in fields.items():
            if val not in (None, "", {}):
                setattr(existing, key, val)
        if order_id:
            existing.order_id = int(order_id)
        db.add(existing)
        logger.info(
            "[CUSTOMER_SHIPPING_ADDRESS] updated tenant=%s customer=%s order=%s reason=%s",
            tenant_id,
            customer_id,
            order_id,
            reason,
        )
        return True, existing

    row = CustomerAddress(
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
        order_id=int(order_id) if order_id else None,
        **fields,
    )
    db.add(row)
    logger.info(
        "[CUSTOMER_SHIPPING_ADDRESS] created tenant=%s customer=%s order=%s reason=%s",
        tenant_id,
        customer_id,
        order_id,
        reason,
    )
    return True, row


def sync_order_shipping_layers(
    *,
    order_prep: Dict[str, Any],
    customer_info: Dict[str, Any],
    extra_metadata: Dict[str, Any],
    last_sync_snapshot: Optional[Dict[str, Any]] = None,
    merchant_edit_payload: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Build snapshot and apply to customer_info/metadata unless locked."""
    locked = shipping_fields_locked(extra_metadata, order_prep)
    snapshot = build_order_shipping_snapshot(
        order_prep=order_prep,
        customer_info=customer_info,
        extra_metadata=extra_metadata,
        last_sync_snapshot=last_sync_snapshot,
        merchant_edit_payload=merchant_edit_payload,
    )
    updated_info = apply_shipping_snapshot_to_customer_info(
        customer_info,
        snapshot,
        locked=locked,
    )
    updated_meta = apply_shipping_snapshot_to_metadata(
        extra_metadata,
        snapshot,
        locked=locked,
    )
    return snapshot, updated_info, updated_meta


def apply_confirmed_shipping_for_order(
    db: Any,
    *,
    tenant_id: int,
    order: Any,
    customer: Any,
    order_prep: Dict[str, Any],
    extra_metadata: Optional[Dict[str, Any]] = None,
    merchant_edit_payload: Optional[Dict[str, Any]] = None,
    confirmed_reason: str = "",
) -> Tuple[Dict[str, Any], bool]:
    """
    Full path: snapshot → order layers → optional ``customer_addresses`` persist.

    Returns ``(snapshot, customer_address_persisted)``.
    """
    meta = dict(extra_metadata or getattr(order, "extra_metadata", None) or {})
    info = dict(getattr(order, "customer_info", None) or {})
    sync = dict(meta.get("last_sync_snapshot") or {})
    locked = shipping_fields_locked(meta, order_prep)

    snapshot = build_order_shipping_snapshot(
        order_prep=order_prep,
        customer_info=info,
        extra_metadata=meta,
        last_sync_snapshot=sync,
        merchant_edit_payload=merchant_edit_payload,
    )
    if not locked:
        order.customer_info = apply_shipping_snapshot_to_customer_info(info, snapshot, locked=False)
        order.extra_metadata = apply_shipping_snapshot_to_metadata(meta, snapshot, locked=False)
    else:
        order.extra_metadata = meta

    customer_id = getattr(customer, "id", None) if customer is not None else None
    persisted, _ = persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant_id,
        customer_id=customer_id,
        order_id=getattr(order, "id", None),
        snapshot=snapshot,
        extra_metadata=order.extra_metadata,
        order_prep=order_prep,
        confirmed_reason=confirmed_reason,
    )
    if persisted:
        meta_persisted = dict(order.extra_metadata or {})
        meta_persisted["customer_address_persisted"] = True
        meta_persisted["customer_address_persisted_at"] = meta_persisted.get("last_updated_at")
        order.extra_metadata = meta_persisted
    return snapshot, persisted


__all__ = [
    "address_fingerprint",
    "apply_confirmed_shipping_for_order",
    "customer_address_to_snapshot",
    "persist_customer_shipping_address_if_confirmed",
    "sync_order_shipping_layers",
]
