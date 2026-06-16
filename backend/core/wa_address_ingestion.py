"""
core/wa_address_ingestion.py
────────────────────────────
Deterministic WhatsApp delivery-address ingestion for Nahla WA orders.

Handles native location pins and accepted map URL text — never payment
receipts or payment claims.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from services.address_resolution import extract_address_signals

_MAPS_HOST_RE = re.compile(
    r"(?:maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.com|google\.com/maps|"
    r"maps\.apple\.com|apple\.com/maps)",
    re.I,
)

_CITY_ONLY_HINT_RE = re.compile(
    r"^(?:ال)?(?:رياض|جده|جدة|دمام|مكه|مكة|المدينه|المدينة|"
    r"الخبر|الطائف|تبوك|بريده|بريدة|ابها|أبها|خميس|نجران|"
    r"riyadh|jeddah|dammam|makkah|madinah|khobar)\s*$",
    re.I,
)


def is_accepted_maps_url(text: str) -> bool:
    """True when ``text`` contains a supported Google/Apple Maps URL."""
    raw = str(text or "").strip()
    if not raw:
        return False
    signals = extract_address_signals(raw)
    if signals.get("google_maps_url"):
        return True
    return bool(_MAPS_HOST_RE.search(raw))


def is_city_only_address_text(text: str) -> bool:
    """City name alone is not an accepted delivery address."""
    raw = str(text or "").strip()
    if not raw or is_accepted_maps_url(raw):
        return False
    norm = re.sub(r"\s+", " ", raw).strip()
    if _CITY_ONLY_HINT_RE.match(norm):
        return True
    signals = extract_address_signals(raw)
    return not signals.get("short_address_code") and not signals.get("google_maps_url")


def build_whatsapp_location_patch(location: Dict[str, Any]) -> Dict[str, Any]:
    lat = location.get("latitude")
    lng = location.get("longitude")
    name = str(location.get("name") or "").strip()
    address = str(location.get("address") or "").strip()
    maps_url = ""
    if lat is not None and lng is not None:
        maps_url = f"https://maps.google.com/?q={lat},{lng}"
    patch: Dict[str, Any] = {
        "delivery_address_status": "accepted",
        "delivery_address_type":   "whatsapp_location",
        "delivery_location_lat":   str(lat) if lat is not None else "",
        "delivery_location_lng":   str(lng) if lng is not None else "",
        "delivery_location_name":  name,
        "delivery_location_address": address,
    }
    if maps_url:
        patch["google_maps_url"] = maps_url
    if lat is not None:
        try:
            patch["latitude"] = float(lat)
        except (TypeError, ValueError):
            pass
    if lng is not None:
        try:
            patch["longitude"] = float(lng)
        except (TypeError, ValueError):
            pass
    if address:
        patch["address_line"] = address
    return patch


def build_maps_url_patch(text: str) -> Dict[str, Any]:
    signals = extract_address_signals(text)
    url = str(signals.get("google_maps_url") or "").strip()
    if not url:
        m = _MAPS_HOST_RE.search(text or "")
        url = m.group(0) if m else ""
    patch: Dict[str, Any] = {
        "delivery_address_status": "accepted",
        "delivery_address_type":   "maps_url",
        "delivery_address_url":    url,
    }
    if url:
        patch["google_maps_url"] = url
    lat = signals.get("latitude")
    lng = signals.get("longitude")
    if lat is not None:
        patch["latitude"] = lat
        patch["delivery_location_lat"] = str(lat)
    if lng is not None:
        patch["longitude"] = lng
        patch["delivery_location_lng"] = str(lng)
    return patch


def compose_address_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    line_items: Optional[list] = None,
) -> str:
    from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

    missing = compute_wa_missing_fields(
        order_prep,
        brain_state=brain_state or {},
        line_items=line_items,
    )
    product_incomplete = "product" in missing
    if not product_incomplete and "delivery_address" not in missing:
        return (
            "وصل الموقع، تم تسجيل العنوان ✅\n"
            "باقي تختار طريقة الدفع: تحويل بنكي أو دفع عند الاستلام إذا متاح."
        )
    return (
        "وصل الموقع، تم تسجيل العنوان ✅\n"
        "باقي تحدد المنتج أو الكمية عشان نكمل الطلب."
    )


def resolve_address_state_patch(
    *,
    inbound_normalized_type: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> Optional[Dict[str, Any]]:
    meta = inbound_metadata or {}
    if inbound_normalized_type == "location":
        loc = meta.get("location") or meta.get("whatsapp_location") or {}
        if not isinstance(loc, dict):
            return None
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            return None
        return build_whatsapp_location_patch(loc)

    text = str(inbound_text or meta.get("text") or "").strip()
    if not text:
        return None
    if is_city_only_address_text(text):
        return None
    if is_accepted_maps_url(text):
        return build_maps_url_patch(text)
    return None


__all__ = [
    "build_maps_url_patch",
    "build_whatsapp_location_patch",
    "compose_address_reply",
    "is_accepted_maps_url",
    "is_city_only_address_text",
    "resolve_address_state_patch",
]
