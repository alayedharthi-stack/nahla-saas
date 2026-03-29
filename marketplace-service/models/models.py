from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

class AppCreate(BaseModel):
    developer_id: int
    name: str
    slug: str
    description: Optional[str] = None
    price_sar: int = 0
    billing_model: str = "one_time"
    commission_rate: float = 0.20
    permissions: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    icon_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class AppResponse(AppCreate):
    id: int
    is_published: bool = True
    created_at: datetime
    updated_at: datetime

class AppInstallCreate(BaseModel):
    tenant_id: int
    app_id: int
    permissions: Optional[List[str]] = None
    config: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

class AppInstallResponse(AppInstallCreate):
    id: int
    status: str
    enabled: bool
    installed_at: datetime

class AppPaymentCreate(BaseModel):
    tenant_id: int
    app_id: int
    developer_id: int
    amount_sar: int
    commission_rate: float = 0.20
    gateway: Optional[str] = "moyasar"
    status: str = "pending"
    transaction_reference: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class AppPaymentResponse(AppPaymentCreate):
    id: int
    commission_amount_sar: int
    paid_at: Optional[datetime] = None
    created_at: datetime

class DeveloperCreate(BaseModel):
    username: str
    email: str
    company_name: Optional[str] = None
    website: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class DeveloperResponse(DeveloperCreate):
    id: int
    created_at: datetime
