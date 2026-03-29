from pydantic import BaseModel
from typing import Optional, Dict, Any, List

class OrderCreate(BaseModel):
    tenant_id: int
    external_id: Optional[str] = None
    total: Optional[str] = None
    customer_info: Optional[Dict[str, Any]] = None
    line_items: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None

class OrderResponse(OrderCreate):
    id: int
    status: str
    checkout_url: Optional[str] = None
    is_abandoned: bool = False

class CheckoutResponse(BaseModel):
    checkout_url: str
