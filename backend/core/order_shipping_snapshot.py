"""
core/order_shipping_snapshot.py
───────────────────────────────
Unified shipping snapshot from order_prep, Order row, and sync metadata.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from core.wa_order_lifecycle import has_accepted_delivery_address

SOURCE_CUSTOMER_MESSAGE = "customer_message"
SOURCE_WHATSAPP_LOCATION = "whatsapp_location"
SOURCE_MERCHANT_EDIT = "merchant_edit"
SOURCE_CUSTOMER_CONFIRMED_PREVIOUS = "customer_confirmed_previous_address"


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _pick(mapping: Optional[Mapping[str, Any]], key: str) -> str:
    if not isinstance(mapping, dict):
        return ""
    return _clean_str(mapping.get(key))


def _pick_first(*sources: Optional[Mapping[str, Any]], key: str) -> str:
    for src in sources:
        val = _pick(src, key)
        if val:
            return val
    return ""


def _normalize_short_code(*sources: Optional[Mapping[str, Any]]) -> str:
    for key in ("short_address_code", "national_short_address"):
        for src in sources:
            val = _pick(src, key)
            if val:
                return val.upper()
    return ""


def _normalize_maps_url(*sources: Optional[Mapping[str, Any]]) -> str:
    for key in ("google_maps_url", "delivery_address_url", "google_maps_link"):
        for src in sources:
            val = _pick(src, key)
            if val:
                return val
    return ""


def _normalize_lat_lng(
    *sources: Optional[Mapping[str, Any]],
) -> tuple[str, str]:
    lat = _pick_first(*sources, key="lat") or _pick_first(*sources, key="latitude")
    lng = _pick_first(*sources, key="lng") or _pick_first(*sources, key="longitude")
    if not lat:
        lat = _pick_first(*sources, key="delivery_location_lat")
    if not lng:
        lng = _pick_first(*sources, key="delivery_location_lng")
    return lat, lng


def _whatsapp_location(*sources: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw = src.get("whatsapp_location") or src.get("delivery_location")
        if isinstance(raw, dict) and raw:
            return dict(raw)
    return None


def shipping_fields_locked(
    extra_metadata: Optional[Mapping[str, Any]],
    order_prep: Optional[Mapping[str, Any]] = None,
) -> bool:
    meta = dict(extra_metadata or {})
    prep = dict(order_prep or {})
    if bool(meta.get("merchant_edit_locked")):
        return True
    if bool(meta.get("merchant_shipping_locked")):
        return True
    return bool(prep.get("merchant_shipping_locked"))


def build_order_shipping_snapshot(
    *,
    order_prep: Optional[Dict[str, Any]] = None,
    customer_info: Optional[Dict[str, Any]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    last_sync_snapshot: Optional[Dict[str, Any]] = None,
    merchant_edit_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge shipping fields from runtime prep and persisted order layers."""
    prep = dict(order_prep or {})
    info = dict(customer_info or {})
    meta = dict(extra_metadata or {})
    sync = dict(last_sync_snapshot or {})
    edit = dict(merchant_edit_payload or {})

    sources = (edit, prep, info, meta, sync)
    short_code = _normalize_short_code(*sources)
    maps_url = _normalize_maps_url(*sources)
    lat, lng = _normalize_lat_lng(*sources)
    wa_loc = _whatsapp_location(*sources)

    city = _pick_first(edit, prep, info, meta, sync, key="city")
    district = _pick_first(edit, prep, info, meta, sync, key="district")
    street = _pick_first(edit, prep, info, meta, sync, key="street")
    address_line = (
        _pick_first(edit, prep, info, meta, sync, key="address_line")
        or _pick_first(edit, prep, info, meta, sync, key="address")
    )
    delivery_notes = _pick_first(edit, prep, info, meta, sync, key="delivery_notes")

    source = _pick_first(edit, prep, meta, key="shipping_source") or SOURCE_CUSTOMER_MESSAGE
    if bool(meta.get("merchant_edited_at")) or edit:
        source = SOURCE_MERCHANT_EDIT
    elif bool(prep.get("customer_confirmed_previous_address")) or bool(
        meta.get("customer_confirmed_previous_address")
    ):
        source = SOURCE_CUSTOMER_CONFIRMED_PREVIOUS
    elif wa_loc or (lat and lng):
        source = SOURCE_WHATSAPP_LOCATION

    prep_like = {
        "city": city,
        "district": district,
        "street": street,
        "address_line": address_line,
        "short_address_code": short_code,
        "google_maps_url": maps_url,
        "delivery_address_url": maps_url,
        "latitude": lat or None,
        "longitude": lng or None,
        "delivery_location_lat": lat or None,
        "delivery_location_lng": lng or None,
        "delivery_address_status": prep.get("delivery_address_status")
        or meta.get("delivery_address_status"),
        "customer_confirmed_previous_address": bool(
            prep.get("customer_confirmed_previous_address")
            or meta.get("customer_confirmed_previous_address")
        ),
    }
    accepted = has_accepted_delivery_address(prep_like)

    delivery_location: Optional[Dict[str, Any]] = None
    if wa_loc:
        delivery_location = dict(wa_loc)
    elif lat and lng:
        delivery_location = {"lat": lat, "lng": lng}

    return {
        "city": city,
        "district": district,
        "street": street,
        "address_line": address_line,
        "short_address_code": short_code,
        "google_maps_url": maps_url,
        "delivery_address_url": maps_url,
        "whatsapp_location": wa_loc,
        "lat": lat,
        "lng": lng,
        "delivery_location": delivery_location,
        "delivery_notes": delivery_notes,
        "source": source,
        "accepted_delivery_address": accepted,
    }


def apply_shipping_snapshot_to_customer_info(
    customer_info: Optional[Dict[str, Any]],
    snapshot: Mapping[str, Any],
    *,
    locked: bool,
) -> Dict[str, Any]:
    """Copy operational shipping fields into ``Order.customer_info`` when unlocked."""
    if locked:
        return dict(customer_info or {})
    info = dict(customer_info or {})
    mapping = {
        "city": snapshot.get("city"),
        "district": snapshot.get("district"),
        "street": snapshot.get("street"),
        "address": snapshot.get("address_line"),
        "short_address_code": snapshot.get("short_address_code"),
        "google_maps_url": snapshot.get("google_maps_url"),
        "delivery_notes": snapshot.get("delivery_notes"),
    }
    for key, val in mapping.items():
        if _clean_str(val):
            info[key] = _clean_str(val)
    if snapshot.get("delivery_location"):
        info["delivery_location"] = dict(snapshot["delivery_location"])
    if snapshot.get("whatsapp_location"):
        info["whatsapp_location"] = dict(snapshot["whatsapp_location"])
    lat = _clean_str(snapshot.get("lat"))
    lng = _clean_str(snapshot.get("lng"))
    if lat:
        info["latitude"] = lat
    if lng:
        info["longitude"] = lng
    return info


def apply_shipping_snapshot_to_metadata(
    extra_metadata: Optional[Dict[str, Any]],
    snapshot: Mapping[str, Any],
    *,
    locked: bool,
) -> Dict[str, Any]:
    """Mirror shipping snapshot onto order metadata for dashboard/API reads."""
    if locked:
        return dict(extra_metadata or {})
    meta = dict(extra_metadata or {})
    if _clean_str(snapshot.get("city")):
        meta["city"] = snapshot["city"]
    if _clean_str(snapshot.get("district")):
        meta["district"] = snapshot["district"]
    if _clean_str(snapshot.get("address_line")):
        meta["address_line"] = snapshot["address_line"]
    code = _clean_str(snapshot.get("short_address_code"))
    if code:
        meta["short_address_code"] = code
        meta["national_short_address"] = code
    maps_url = _clean_str(snapshot.get("google_maps_url"))
    if maps_url:
        meta["google_maps_url"] = maps_url
        meta["delivery_address_url"] = maps_url
    if _clean_str(snapshot.get("delivery_notes")):
        meta["delivery_notes"] = snapshot["delivery_notes"]
    if snapshot.get("whatsapp_location"):
        meta["whatsapp_location"] = dict(snapshot["whatsapp_location"])
    lat = _clean_str(snapshot.get("lat"))
    lng = _clean_str(snapshot.get("lng"))
    if lat:
        meta["latitude"] = lat
    if lng:
        meta["longitude"] = lng
    if bool(snapshot.get("accepted_delivery_address")):
        meta["delivery_address_status"] = "accepted"
    if _clean_str(snapshot.get("source")):
        meta["shipping_source"] = snapshot["source"]
    meta["shipping_snapshot"] = dict(snapshot)
    return meta


def shipping_snapshot_confirmed(
    snapshot: Mapping[str, Any],
    *,
    extra_metadata: Optional[Mapping[str, Any]] = None,
    order_prep: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, str]:
    """
    True when there is evidence the address is confirmed for persistence.

    Does NOT treat bare city or unconfirmed known_previous as confirmed.
    """
    meta = dict(extra_metadata or {})
    prep = dict(order_prep or {})

    if shipping_fields_locked(meta, prep):
        if bool(snapshot.get("accepted_delivery_address")) or _has_address_evidence(snapshot):
            return True, "merchant_edit"

    if bool(prep.get("customer_confirmed_previous_address")) or bool(
        meta.get("customer_confirmed_previous_address")
    ):
        if _has_address_evidence(snapshot):
            return True, "customer_confirmed_previous_address"

    if bool(snapshot.get("accepted_delivery_address")):
        if _clean_str(snapshot.get("short_address_code")):
            return True, "short_address_code"
        if _clean_str(snapshot.get("google_maps_url")):
            return True, "google_maps_url"
        if snapshot.get("whatsapp_location") or (
            _clean_str(snapshot.get("lat")) and _clean_str(snapshot.get("lng"))
        ):
            return True, "whatsapp_location"

    return False, ""


def _has_address_evidence(snapshot: Mapping[str, Any]) -> bool:
    if _clean_str(snapshot.get("short_address_code")):
        return True
    if _clean_str(snapshot.get("google_maps_url")):
        return True
    if snapshot.get("whatsapp_location"):
        return True
    if _clean_str(snapshot.get("lat")) and _clean_str(snapshot.get("lng")):
        return True
    return False


__all__ = [
    "SOURCE_CUSTOMER_CONFIRMED_PREVIOUS",
    "SOURCE_CUSTOMER_MESSAGE",
    "SOURCE_MERCHANT_EDIT",
    "SOURCE_WHATSAPP_LOCATION",
    "apply_shipping_snapshot_to_customer_info",
    "apply_shipping_snapshot_to_metadata",
    "build_order_shipping_snapshot",
    "shipping_fields_locked",
    "shipping_snapshot_confirmed",
]
