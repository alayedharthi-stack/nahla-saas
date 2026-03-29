from fastapi import APIRouter, HTTPException
from models.models import (
    PlanCreate,
    PlanResponse,
    SubscriptionCreate,
    SubscriptionResponse,
    PaymentCreate,
    PaymentResponse,
    InvoiceCreate,
    InvoiceResponse,
)
from services.business_logic import (
    create_plan,
    list_plans,
    get_plan,
    create_subscription,
    get_subscription,
    record_payment,
    create_invoice,
    get_invoice,
    resolve_plan_limits,
)

router = APIRouter(prefix="/billing", tags=["billing"])

@router.post("/plans", response_model=PlanResponse)
async def create_billing_plan(payload: PlanCreate):
    return create_plan(payload)

@router.get("/plans", response_model=list[PlanResponse])
async def get_billing_plans():
    return list_plans()

@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_billing_plan(plan_id: int):
    plan = get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan

@router.post("/subscriptions", response_model=SubscriptionResponse)
async def create_billing_subscription(payload: SubscriptionCreate):
    return create_subscription(payload)

@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionResponse)
async def get_billing_subscription(subscription_id: int):
    subscription = get_subscription(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return subscription

@router.post("/payments", response_model=PaymentResponse)
async def process_payment(payload: PaymentCreate):
    return record_payment(payload)

@router.post("/invoices", response_model=InvoiceResponse)
async def generate_invoice(payload: InvoiceCreate):
    return create_invoice(payload)

@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
async def get_billing_invoice(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice

@router.get("/plans/{plan_id}/limits")
async def get_billing_plan_limits(plan_id: int):
    plan = get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return resolve_plan_limits(plan)
