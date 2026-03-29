from pydantic import BaseModel
from typing import Dict, Any

class MetricsResponse(BaseModel):
    revenue: int
    orders: int
    customers: int

class CampaignPerformanceResponse(BaseModel):
    campaigns: list[Dict[str, Any]]

class ConversationAnalyticsResponse(BaseModel):
    open_conversations: int
    human_handoffs: int
