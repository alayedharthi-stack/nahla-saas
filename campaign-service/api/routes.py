from fastapi import APIRouter
from models.models import CampaignCreate, CampaignResponse
from services.business_logic import create_campaign as create_campaign_logic, get_performance as get_performance_logic

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

@router.post("/", response_model=CampaignResponse)
async def create_campaign(payload: CampaignCreate):
    return create_campaign_logic(payload)

@router.get("/performance")
async def campaign_performance():
    return get_performance_logic()
