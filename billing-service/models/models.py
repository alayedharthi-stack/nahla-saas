from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

class PlanLimits(BaseModel):
    message_limit: int
    ai_features: str
    campaign_limit: int
    additional_limits: Optional[Dict[str, Any]] = None

class PlanCreate(BaseModel):
    tenant_id: Optional[int] = None
    slug: str
    name: str
    description: Optional[str] = None
    price_sar: int
    billing_cycle: str = "monthly"
    is_enterprise: bool = False
    branding_locked: bool = False
    branding_text: str = "🐝 Powered by Nahla"
    metadata: Optional[Dict[str, Any]] = None

class PlanResponse(PlanCreate):
    id: int
    limits: PlanLimits

class SubscriptionCreate(BaseModel):
    tenant_id: int
    plan_id: int
    trial_days: Optional[int] = 0
    auto_renew: bool = True
    metadata: Optional[Dict[str, Any]] = None

class SubscriptionResponse(SubscriptionCreate):
    id: int
    status: str
    started_at: datetime
    ends_at: Optional[datetime] = None
    trial_ends_at: Optional[datetime] = None

class PaymentCreate(BaseModel):
    tenant_id: int
    subscription_id: Optional[int] = None
    amount_sar: int
    gateway: str = "moyasar"
    transaction_reference: Optional[str] = None
    status: str = "pending"
    metadata: Optional[Dict[str, Any]] = None

class PaymentResponse(PaymentCreate):
    id: int
    paid_at: Optional[datetime] = None

class InvoiceLineItem(BaseModel):
    description: str
    amount_sar: int
    quantity: int = 1
    metadata: Optional[Dict[str, Any]] = None

class InvoiceCreate(BaseModel):
    tenant_id: int
    subscription_id: Optional[int] = None
    amount_due_sar: int
    due_date: Optional[datetime] = None
    line_items: List[InvoiceLineItem]
    metadata: Optional[Dict[str, Any]] = None

class InvoiceResponse(InvoiceCreate):
    id: int
    status: str
    issued_date: datetime
    amount_paid_sar: int = 0
