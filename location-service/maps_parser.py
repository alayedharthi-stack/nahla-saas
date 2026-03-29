import re
from dataclasses import dataclass

from address_parser import LocationResult


def parse_map_link(url: str) -> LocationResult:
    """Parse Google Maps or Apple Maps links into normalized coordinates."""
    if "google.com/maps" in url or "goo.gl/maps" in url:
        # Extract latitude/longitude from Google Maps URL if present
        match = re.search(r"@([0-9.+-]+),([0-9.+-]+)", url)
        if match:
            lat = float(match.group(1))
            lng = float(match.group(2))
            return LocationResult(lat=lat, lng=lng, address_text=url)
    if "apple.com/maps" in url:
        # Extract latitude/longitude from Apple Maps URL if present
        match = re.search(r"ll=([0-9.+-]+),([0-9.+-]+)", url)
        if match:
            lat = float(match.group(1))
            lng = float(match.group(2))
            return LocationResult(lat=lat, lng=lng, address_text=url)
    return LocationResult(lat=0.0, lng=0.0, address_text=url)


def parse_whatsapp_location(payload: dict) -> LocationResult:
    """Parse WhatsApp location message payload into normalized coordinates."""
    location = payload.get("location", {})
    lat = float(location.get("latitude", 0.0))
    lng = float(location.get("longitude", 0.0))
    name = location.get("name") or location.get("address") or "WhatsApp Location"
    return LocationResult(lat=lat, lng=lng, address_text=name)
