from pydantic import BaseModel
from typing import Dict, Any, List, Optional

class CampaignCreate(BaseModel):
    tenant_id: int
    name: str
    trigger: Dict[str, Any]
    actions: List[Dict[str, Any]]
    is_active: bool = True

class CampaignResponse(CampaignCreate):
    id: int
