from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from models.models import (
    InvoiceCreate,
    InvoiceLineItem,
    InvoiceResponse,
    PaymentCreate,
    PaymentResponse,
    PlanCreate,
    PlanResponse,
    PlanLimits,
    SubscriptionCreate,
    SubscriptionResponse,
)
from repositories.data_access import (
    create_invoice_record,
    create_payment_record,
    create_plan_record,
    create_subscription_record,
    get_invoice_record,
    get_plan_record,
    get_subscription_record,
    list_plan_records,
)

DEFAULT_PLANS = [
    PlanResponse(
        id=1,
        tenant_id=None,
        slug="starter",
        name="Starter",
        description="Small merchant plan with basic AI and campaign support.",
        price_sar=499,
        billing_cycle="monthly",
        is_enterprise=False,
        branding_locked=True,
        branding_text="🐝 Powered by Nahla",
        metadata={"tier": "starter"},
        limits=PlanLimits(message_limit=5000, ai_features="basic", campaign_limit=5),
    ),
    PlanResponse(
        id=2,
        tenant_id=None,
        slug="growth",
        name="Growth",
        description="Growth plan with more AI quota and campaign capacity.",
        price_sar=899,
        billing_cycle="monthly",
        is_enterprise=False,
        branding_locked=True,
        branding_text="🐝 Powered by Nahla",
        metadata={"tier": "growth"},
        limits=PlanLimits(message_limit=15000, ai_features="standard", campaign_limit=20),
    ),
    PlanResponse(
        id=3,
        tenant_id=None,
        slug="pro",
        name="Pro",
        description="Advanced plan for larger stores and expanded AI usage.",
        price_sar=1499,
        billing_cycle="monthly",
        is_enterprise=False,
        branding_locked=False,
        branding_text="🐝 Powered by Nahla",
        metadata={"tier": "pro"},
        limits=PlanLimits(message_limit=50000, ai_features="advanced", campaign_limit=50),
    ),
    PlanResponse(
        id=4,
        tenant_id=None,
        slug="enterprise",
        name="Enterprise",
        description="Custom pricing and flexible limits for enterprise customers.",
        price_sar=0,
        billing_cycle="custom",
        is_enterprise=True,
        branding_locked=False,
        branding_text="🐝 Powered by Nahla",
        metadata={"custom_pricing": True},
        limits=PlanLimits(message_limit=1000000, ai_features="enterprise", campaign_limit=500),
    ),
]

def list_plans() -> List[PlanResponse]:
    stored = list_plan_records()
    if stored:
        return [
            PlanResponse(
                id=plan["id"],
                tenant_id=plan.get("tenant_id"),
                slug=plan["slug"],
                name=plan["name"],
                description=plan.get("description"),
                price_sar=plan["price_sar"],
                billing_cycle=plan["billing_cycle"],
                is_enterprise=plan["is_enterprise"],
                branding_locked=plan.get("branding_locked", False),
                branding_text=plan.get("branding_text", "🐝 Powered by Nahla"),
                metadata=plan.get("metadata"),
                limits=resolve_plan_limits(plan),
            )
            for plan in stored
        ]
    return DEFAULT_PLANS

def create_plan(payload: PlanCreate) -> PlanResponse:
    plan_id = create_plan_record(payload)
    return PlanResponse(
        id=plan_id,
        tenant_id=payload.tenant_id,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        price_sar=payload.price_sar,
        billing_cycle=payload.billing_cycle,
        is_enterprise=payload.is_enterprise,
        branding_locked=payload.branding_locked,
        branding_text=payload.branding_text,
        metadata=payload.metadata,
        limits=resolve_plan_limits(payload),
    )

def get_plan(plan_id: int) -> Optional[PlanResponse]:
    stored = get_plan_record(plan_id)
    if stored:
        return PlanResponse(
            id=stored["id"],
            tenant_id=stored.get("tenant_id"),
            slug=stored["slug"],
            name=stored["name"],
            description=stored.get("description"),
            price_sar=stored["price_sar"],
            billing_cycle=stored["billing_cycle"],
            is_enterprise=stored["is_enterprise"],
            branding_locked=stored.get("branding_locked", False),
            branding_text=stored.get("branding_text", "🐝 Powered by Nahla"),
            metadata=stored.get("metadata"),
            limits=resolve_plan_limits(stored),
        )
    for plan in DEFAULT_PLANS:
        if plan.id == plan_id:
            return plan
    return None

from typing import Any


def resolve_plan_limits(plan: Any) -> PlanLimits:
    is_enterprise = plan.get("is_enterprise") if isinstance(plan, dict) else getattr(plan, "is_enterprise", False)
    price_sar = plan.get("price_sar", 0) if isinstance(plan, dict) else getattr(plan, "price_sar", 0)
    if is_enterprise:
        return PlanLimits(message_limit=1000000, ai_features="enterprise", campaign_limit=500)
    if price_sar >= 1499:
        return PlanLimits(message_limit=50000, ai_features="advanced", campaign_limit=50)
    if price_sar >= 899:
        return PlanLimits(message_limit=15000, ai_features="standard", campaign_limit=20)
    return PlanLimits(message_limit=5000, ai_features="basic", campaign_limit=5)


def resolve_branding_policy(plan: Any) -> dict:
    branding_text = plan.get("branding_text") if isinstance(plan, dict) else getattr(plan, "branding_text", "🐝 Powered by Nahla")
    branding_locked = plan.get("branding_locked", False) if isinstance(plan, dict) else getattr(plan, "branding_locked", False)
    branding = {
        "show_nahla_branding": True,
        "branding_text": branding_text,
        "branding_locked": branding_locked,
        "can_override_branding": not branding_locked,
    }
    if branding_locked:
        branding["show_nahla_branding"] = True
    return branding


def create_subscription(payload: SubscriptionCreate) -> SubscriptionResponse:
    id_ = create_subscription_record(payload)
    started_at = datetime.utcnow()
    trial_ends_at = started_at + timedelta(days=payload.trial_days or 0) if payload.trial_days else None
    ends_at = started_at + timedelta(days=30) if not payload.trial_days else started_at + timedelta(days=30 + payload.trial_days)
    return SubscriptionResponse(
        id=id_,
        tenant_id=payload.tenant_id,
        plan_id=payload.plan_id,
        trial_days=payload.trial_days,
        auto_renew=payload.auto_renew,
        metadata=payload.metadata,
        status="active",
        started_at=started_at,
        trial_ends_at=trial_ends_at,
        ends_at=ends_at,
    )

def get_subscription(subscription_id: int) -> Optional[SubscriptionResponse]:
    stored = get_subscription_record(subscription_id)
    if not stored:
        return None
    return SubscriptionResponse(
        id=stored["id"],
        tenant_id=stored["tenant_id"],
        plan_id=stored["plan_id"],
        trial_days=stored.get("trial_days"),
        auto_renew=stored["auto_renew"],
        metadata=stored.get("metadata"),
        status=stored.get("status", "active"),
        started_at=datetime.utcnow(),
        trial_ends_at=None,
        ends_at=None,
    )

def record_payment(payload: PaymentCreate) -> PaymentResponse:
    paid_at = datetime.utcnow() if payload.status == "paid" else None
    payment_id = create_payment_record(payload)
    return PaymentResponse(
        id=payment_id,
        tenant_id=payload.tenant_id,
        subscription_id=payload.subscription_id,
        amount_sar=payload.amount_sar,
        gateway=payload.gateway,
        transaction_reference=payload.transaction_reference,
        status=payload.status,
        metadata=payload.metadata,
        paid_at=paid_at,
    )

def create_invoice(payload: InvoiceCreate) -> InvoiceResponse:
    invoice_id = create_invoice_record(payload)
    return InvoiceResponse(
        id=invoice_id,
        tenant_id=payload.tenant_id,
        subscription_id=payload.subscription_id,
        amount_due_sar=payload.amount_due_sar,
        due_date=payload.due_date,
        line_items=payload.line_items,
        metadata=payload.metadata,
        status="draft",
        issued_date=datetime.utcnow(),
        amount_paid_sar=0,
    )

def get_invoice(invoice_id: int) -> Optional[InvoiceResponse]:
    stored = get_invoice_record(invoice_id)
    if not stored:
        return None
    return InvoiceResponse(
        id=stored["id"],
        tenant_id=stored["tenant_id"],
        subscription_id=stored.get("subscription_id"),
        amount_due_sar=stored["amount_due_sar"],
        due_date=stored.get("due_date"),
        line_items=[
            item if not isinstance(item, dict) else InvoiceLineItem(**item)
            for item in stored.get("line_items", [])
        ],
        metadata=stored.get("metadata"),
        status=stored.get("status", "draft"),
        issued_date=stored.get("issued_date") or datetime.utcnow(),
        amount_paid_sar=stored.get("amount_paid_sar", 0),
    )
