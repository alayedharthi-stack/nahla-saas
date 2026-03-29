from pydantic import BaseModel
from typing import Optional, Dict, Any

class ProductCreate(BaseModel):
    tenant_id: int
    external_id: Optional[str] = None
    sku: Optional[str] = None
    title: str
    description: Optional[str] = None
    price: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class ProductResponse(ProductCreate):
    id: int

class ProductSyncResponse(BaseModel):
    status: str
    synced: int
    message: str
