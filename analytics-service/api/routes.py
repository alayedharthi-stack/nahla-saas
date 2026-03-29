from fastapi import APIRouter
from models.models import MetricsResponse, CampaignPerformanceResponse, ConversationAnalyticsResponse
from services.business_logic import get_store_metrics, get_campaign_performance, get_conversation_analytics

router = APIRouter(prefix="/analytics", tags=["analytics"])

@router.get("/metrics", response_model=MetricsResponse)
async def store_metrics():
    return get_store_metrics()

@router.get("/campaigns", response_model=CampaignPerformanceResponse)
async def campaign_performance():
    return get_campaign_performance()

@router.get("/conversations", response_model=ConversationAnalyticsResponse)
async def conversation_analytics():
    return get_conversation_analytics()
