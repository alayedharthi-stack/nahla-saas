from models.models import LocationRequest
from address_parser import parse_saudi_address
from maps_parser import parse_map_link, parse_whatsapp_location


def normalize_location(payload: LocationRequest) -> dict:
    if payload.whatsapp_location:
        result = parse_whatsapp_location(payload.whatsapp_location)
        return result.__dict__
    if payload.google_maps_link:
        result = parse_map_link(payload.google_maps_link)
        return result.__dict__
    if payload.apple_maps_link:
        result = parse_map_link(payload.apple_maps_link)
        return result.__dict__
    if payload.raw_address:
        result = parse_saudi_address(payload.raw_address)
        return result.__dict__
    return {"lat": 0.0, "lng": 0.0, "address_text": ""}
