"""
services/address_resolution.py
──────────────────────────────
Resolve Saudi national short address codes and map coordinates into structured
address fields for checkout preparation.

Primary runtime mode:
  - If `SPL_NATIONAL_ADDRESS_API_KEY` is configured, use SPL National Address API.
Fallback mode:
  - Extract short code / map URL / coordinates from user text and keep them in
    the checkout state so the brain can continue collecting any missing fields.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import os
import re
from typing import Any, Dict, Optional

import httpx

_SPL_API_KEY = os.environ.get("SPL_NATIONAL_ADDRESS_API_KEY", "").strip()
_SPL_BASE = os.environ.get(
    "SPL_NATIONAL_ADDRESS_BASE_URL",
    "https://apina.address.gov.sa/NationalAddress/v3.1",
).rstrip("/")

_SHORT_CODE_RE = re.compile(r"\b([A-Za-z]{4}\d{4})\b")
_MAPS_URL_RE = re.compile(r"(https?://(?:www\.)?(?:maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.com|google\.com/maps|g\.page)[^\s]+)", re.IGNORECASE)
_AT_COORDS_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
_PAIR_COORDS_RE = re.compile(r"\b(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\b")
_BANG_COORDS_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


@dataclass
class ResolvedNationalAddress:
    city: str = ""
    district: str = ""
    street: str = ""
    postal_code: str = ""
    building_number: str = ""
    additional_number: str = ""
    short_address_code: str = ""
    google_maps_url: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    resolution_source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def extract_address_signals(text: str) -> Dict[str, Any]:
    raw = text or ""
    short_match = _SHORT_CODE_RE.search(raw)
    map_match = _MAPS_URL_RE.search(raw)
    lat, lng = _extract_coords(raw)
    return {
        "short_address_code": short_match.group(1).upper() if short_match else "",
        "google_maps_url": map_match.group(1) if map_match else "",
        "latitude": lat,
        "longitude": lng,
    }


async def resolve_short_address(
    short_code: str,
    *,
    city: str = "",
    timeout_seconds: float = 12.0,
) -> Optional[ResolvedNationalAddress]:
    if not short_code or not _SPL_API_KEY:
        return None

    query = " ".join(part for part in [short_code.strip(), city.strip()] if part).strip()
    if not query:
        return None

    url = f"{_SPL_BASE}/address/address-free-text"
    params = {
        "language": "A",
        "format": "JSON",
        "page": 1,
        "addressstring": query,
        "api_key": _SPL_API_KEY,
    }
    payload = await _fetch_json(url, params=params, timeout_seconds=timeout_seconds)
    if not payload:
        return None

    resolved = _normalize_address_candidate(payload)
    if not resolved:
        return None
    resolved.short_address_code = short_code.upper()
    resolved.resolution_source = "spl_short_code"
    return resolved


async def resolve_coordinates(
    lat: float,
    lng: float,
    *,
    timeout_seconds: float = 12.0,
) -> Optional[ResolvedNationalAddress]:
    if lat is None or lng is None or not _SPL_API_KEY:
        return None

    url = f"{_SPL_BASE}/address/address-geocode"
    params = {
        "language": "A",
        "format": "JSON",
        "encode": "utf8",
        "lat": lat,
        "long": lng,
        "api_key": _SPL_API_KEY,
    }
    payload = await _fetch_json(url, params=params, timeout_seconds=timeout_seconds)
    if not payload:
        return None

    resolved = _normalize_address_candidate(payload)
    if not resolved:
        resolved = ResolvedNationalAddress()
    resolved.latitude = lat
    resolved.longitude = lng
    resolved.resolution_source = "spl_geocode"
    return resolved


def spl_resolution_available() -> bool:
    return bool(_SPL_API_KEY)


async def _fetch_json(
    url: str,
    *,
    params: Dict[str, Any],
    timeout_seconds: float,
) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_coords(text: str) -> tuple[Optional[float], Optional[float]]:
    for regex in (_AT_COORDS_RE, _BANG_COORDS_RE, _PAIR_COORDS_RE):
        match = regex.search(text or "")
        if not match:
            continue
        try:
            return float(match.group(1)), float(match.group(2))
        except Exception:
            continue
    return None, None


def _normalize_address_candidate(payload: Dict[str, Any]) -> Optional[ResolvedNationalAddress]:
    best = _best_candidate(payload)
    if not best:
        return None

    normalized: Dict[str, Any] = {}
    for key, value in best.items():
        if value in (None, "", [], {}):
            continue
        slot = _map_key(key)
        if not slot:
            continue
        normalized[slot] = value

    if not normalized:
        return None

    return ResolvedNationalAddress(
        city=str(normalized.get("city", "") or ""),
        district=str(normalized.get("district", "") or ""),
        street=str(normalized.get("street", "") or ""),
        postal_code=str(normalized.get("postal_code", "") or ""),
        building_number=str(normalized.get("building_number", "") or ""),
        additional_number=str(normalized.get("additional_number", "") or ""),
        latitude=_to_float(normalized.get("latitude")),
        longitude=_to_float(normalized.get("longitude")),
    )


def _best_candidate(payload: Any) -> Optional[Dict[str, Any]]:
    candidates: list[tuple[int, Dict[str, Any]]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            score = 0
            for key in node.keys():
                if _map_key(key):
                    score += 1
            if score >= 2:
                candidates.append((score, node))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _map_key(key: str) -> Optional[str]:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    key_map = {
        "city": "city",
        "cityname": "city",
        "district": "district",
        "districtname": "district",
        "street": "street",
        "streetname": "street",
        "postcode": "postal_code",
        "postalcode": "postal_code",
        "zipcode": "postal_code",
        "zip": "postal_code",
        "buildingnumber": "building_number",
        "additionalnumber": "additional_number",
        "lat": "latitude",
        "latitude": "latitude",
        "long": "longitude",
        "longitude": "longitude",
        "lng": "longitude",
    }
    return key_map.get(normalized)


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None
