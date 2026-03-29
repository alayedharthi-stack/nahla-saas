from pydantic import BaseModel
from typing import Optional, Dict, Any

class LocationRequest(BaseModel):
    tenant_id: int
    raw_address: Optional[str] = None
    google_maps_link: Optional[str] = None
    apple_maps_link: Optional[str] = None
    whatsapp_location: Optional[Dict[str, Any]] = None

class LocationResponse(BaseModel):
    lat: float
    lng: float
    address_text: str
