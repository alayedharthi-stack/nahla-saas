from fastapi import APIRouter, HTTPException
from models.models import LocationRequest, LocationResponse
from services.business_logic import parse_location

router = APIRouter(prefix="/location", tags=["location"])

@router.post("/parse", response_model=LocationResponse)
async def parse_location_endpoint(payload: LocationRequest):
    result = parse_location(payload)
    if not result:
        raise HTTPException(status_code=400, detail="Unable to parse location")
    return result
