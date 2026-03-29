import re
from dataclasses import dataclass

@dataclass
class LocationResult:
    lat: float
    lng: float
    address_text: str


def parse_saudi_address(address_text: str) -> LocationResult:
    """Placeholder parser for Saudi National Address format."""
    # TODO: implement Saudi national address normalization
    return LocationResult(lat=0.0, lng=0.0, address_text=address_text)
