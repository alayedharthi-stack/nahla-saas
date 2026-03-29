from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime

class CouponCreate(BaseModel):
    tenant_id: int
    code: str
    description: Optional[str] = None
    discount_type: Optional[str] = None
    discount_value: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None

class CouponRuleCreate(BaseModel):
    rule_type: str
    rule_config: Optional[Dict[str, Any]] = None

class CouponResponse(CouponCreate):
    id: int
