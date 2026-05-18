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

Supported map URL formats (capture + coordinate extraction):
  - Google Maps full:  maps.google.com, google.com/maps (coords in URL)
  - Google Maps short: maps.app.goo.gl, goo.gl/maps     (requires redirect expansion)
  - Google Places:     g.page
  - Apple Maps:        maps.apple.com  (?q=lat,lng or ?ll=lat,lng)
  - Waze:              waze.com/ul      (?ll=lat,lng)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
import os
import re
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("nahla-backend")

# SPL config — read from env directly (same as before) so this module stays
# usable without importing from core.config (avoids circular import risk).
# The canonical values are also published in core/config.py for logging/docs.
_SPL_API_KEY = os.environ.get("SPL_NATIONAL_ADDRESS_API_KEY", "").strip()
_SPL_BASE = os.environ.get(
    "SPL_NATIONAL_ADDRESS_BASE_URL",
    "https://apina.address.gov.sa/NationalAddress/v3.1",
).rstrip("/")

_SHORT_CODE_RE = re.compile(r"\b([A-Za-z]{4}\d{4})\b")

# Recognise all major map platforms used in Saudi Arabia.
# Google short links (maps.app.goo.gl, goo.gl/maps) need redirect-expansion
# to recover coordinates; Apple Maps and Waze embed them in query params.
_MAPS_URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:"
    r"maps\.app\.goo\.gl"       # Google Maps new short link (most common in SA)
    r"|goo\.gl/maps"            # Google Maps old short link
    r"|maps\.google\.com"       # Google Maps full domain
    r"|google\.com/maps"        # Google Maps via google.com
    r"|g\.page"                 # Google Places short link
    r"|maps\.apple\.com"        # Apple Maps (iOS users share these)
    r"|waze\.com/ul"            # Waze navigation links
    r")[^\s]*)",
    re.IGNORECASE,
)

_AT_COORDS_RE    = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
_PAIR_COORDS_RE  = re.compile(r"\b(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\b")
_BANG_COORDS_RE  = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
# Coordinate query params we accept across providers:
#   * Google           ── ?q=lat,lng / ?center=lat,lng / ?sll=lat,lng / ?daddr=lat,lng
#   * Apple Maps       ── ?ll=lat,lng / ?q=lat,lng / ?coordinate=lat,lng (newer iOS)
#   * Waze             ── ?ll=lat,lng
# ``coordinate`` and ``sll`` are added for Apple's newer share format
# and Google's "search location" param respectively — both observed in
# real customer shares but absent from the original regex.
_QUERY_COORDS_RE = re.compile(
    r"[?&](?:q|ll|daddr|center|sll|coordinate)=(-?\d+\.\d+),(-?\d+\.\d+)"
)


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
    """Extract any address-y signals from a free-form customer message.

    Returns a dict with:
      * ``short_address_code`` — Saudi national 4-letter+4-digit code
      * ``google_maps_url``    — a clickable map URL the merchant can
        open in any browser. When the customer shared a non-Google
        URL (Apple Maps / Waze / shortened Google) AND we managed to
        pull coordinates out of it, we synthesise a canonical
        ``https://maps.google.com/?q=lat,lng`` so staff and dashboards
        always see a Google-clickable link without having to install
        the original app. Source URL is preserved when no coords were
        recovered (so merchant can still open it in Apple/Waze).
      * ``latitude`` / ``longitude`` — first valid pair found

    The synthesised Google URL is the "internal Google Maps lookup"
    requested in the merchant brief: NO new dependency, NO reverse
    geocode network call — we just rebuild a stable URL from the
    coords we already extracted.
    """
    raw = text or ""
    short_match = _SHORT_CODE_RE.search(raw)
    map_match = _MAPS_URL_RE.search(raw)
    lat, lng = _extract_coords(raw)

    source_url = map_match.group(1) if map_match else ""

    # Synthesise a Google Maps URL whenever we have real coords AND
    # the source URL is missing or non-Google. Keeps the rest of the
    # pipeline (template rendering, merchant dashboard preview, audit
    # logs) on a single canonical URL shape.
    google_maps_url = source_url
    if lat is not None and lng is not None:
        if not source_url or "google.com" not in source_url.lower():
            google_maps_url = f"https://maps.google.com/?q={lat},{lng}"

    return {
        "short_address_code": short_match.group(1).upper() if short_match else "",
        "google_maps_url": google_maps_url,
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


# ── Google Maps / shortened-URL helpers ──────────────────────────────────────

_SHORTENED_URL_HOSTS = frozenset({
    "maps.app.goo.gl",
    "goo.gl",
    "g.page",
})


def _is_shortened_maps_url(url: str) -> bool:
    """Return True when the URL is a known short-link that hides coordinates."""
    try:
        from urllib.parse import urlparse  # noqa: PLC0415
        host = urlparse(url).hostname or ""
        return host.lower() in _SHORTENED_URL_HOSTS
    except Exception:
        return False


async def expand_maps_url(url: str, timeout_seconds: float = 8.0) -> str:
    """Follow HTTP redirects on a shortened map URL and return the final URL.

    Uses a HEAD request to avoid downloading the full page HTML. Returns the
    original URL unchanged if expansion fails (network error, timeout, etc.)
    so callers can treat this as a best-effort, non-fatal enrichment step.

    Typical use-case: `maps.app.goo.gl/xyz` → `maps.google.com/maps/place/.../@lat,lng,...`
    after which `_extract_coords()` can pull out the coordinates.
    """
    if not url or not _is_shortened_maps_url(url):
        return url
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            max_redirects=6,
        ) as client:
            resp = await client.head(url)
            expanded = str(resp.url)
            if expanded and expanded != url:
                logger.info(
                    "[AddressResolution] expanded short maps URL | %s -> %s",
                    url[:60], expanded[:80],
                )
            return expanded or url
    except Exception as exc:
        logger.debug(
            "[AddressResolution] expand_maps_url failed (non-fatal) | url=%s err=%s",
            url[:60], exc,
        )
        return url


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
    """Extract the first lat/lng pair found in text or a URL.

    Priority order (most-specific to least):
      1. @lat,lng        — Google Maps full URL viewport anchor
      2. !3dlat!4dlng   — Google Maps embed/data format
      3. ?q= / ?ll= / ?daddr= / ?center= — Apple Maps, Waze, Google query params
      4. bare lat,lng   — loose coordinate pair anywhere in text
    """
    for regex in (_AT_COORDS_RE, _BANG_COORDS_RE, _QUERY_COORDS_RE, _PAIR_COORDS_RE):
        match = regex.search(text or "")
        if not match:
            continue
        try:
            lat, lng = float(match.group(1)), float(match.group(2))
            # Basic sanity check: valid latitude -90..90, longitude -180..180
            if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
                return lat, lng
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
