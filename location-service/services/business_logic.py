from models.models import LocationRequest, LocationResponse
from repositories.data_access import normalize_location


def parse_location(payload: LocationRequest) -> LocationResponse:
    data = normalize_location(payload)
    return LocationResponse(**data)
