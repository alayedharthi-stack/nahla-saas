from pydantic import BaseModel
from typing import Optional, Dict, Any

class AutomationRuleCreate(BaseModel):
    tenant_id: int
    name: str
    trigger_type: str
    trigger_config: Optional[Dict[str, Any]] = None
    action_config: Optional[Dict[str, Any]] = None
    is_active: bool = True

class AutomationRuleResponse(AutomationRuleCreate):
    id: int
