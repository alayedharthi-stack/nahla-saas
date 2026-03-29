from typing import Any, Dict, List, Optional

from models.models import InvoiceCreate, PaymentCreate, PlanCreate, SubscriptionCreate

_PLAN_STORE: List[Dict[str, Any]] = []
_SUBSCRIPTION_STORE: List[Dict[str, Any]] = []
_PAYMENT_STORE: List[Dict[str, Any]] = []
_INVOICE_STORE: List[Dict[str, Any]] = []


def create_plan_record(payload: PlanCreate) -> int:
    plan_id = len(_PLAN_STORE) + 1
    _PLAN_STORE.append({
        "id": plan_id,
        "tenant_id": payload.tenant_id,
        "slug": payload.slug,
        "name": payload.name,
        "description": payload.description,
        "price_sar": payload.price_sar,
        "billing_cycle": payload.billing_cycle,
        "is_enterprise": payload.is_enterprise,
        "branding_locked": payload.branding_locked,
        "branding_text": payload.branding_text,
        "metadata": payload.metadata,
    })
    return plan_id


def list_plan_records() -> List[Dict[str, Any]]:
    return list(_PLAN_STORE)


def get_plan_record(plan_id: int) -> Optional[Dict[str, Any]]:
    return next((plan for plan in _PLAN_STORE if plan["id"] == plan_id), None)


def create_subscription_record(payload: SubscriptionCreate) -> int:
    subscription_id = len(_SUBSCRIPTION_STORE) + 1
    _SUBSCRIPTION_STORE.append({
        "id": subscription_id,
        "tenant_id": payload.tenant_id,
        "plan_id": payload.plan_id,
        "trial_days": payload.trial_days,
        "auto_renew": payload.auto_renew,
        "metadata": payload.metadata,
        "status": "active",
        "created_at": None,
    })
    return subscription_id


def get_subscription_record(subscription_id: int) -> Optional[Dict[str, Any]]:
    return next((sub for sub in _SUBSCRIPTION_STORE if sub["id"] == subscription_id), None)


def create_payment_record(payload: PaymentCreate) -> int:
    payment_id = len(_PAYMENT_STORE) + 1
    _PAYMENT_STORE.append({
        "id": payment_id,
        "tenant_id": payload.tenant_id,
        "subscription_id": payload.subscription_id,
        "amount_sar": payload.amount_sar,
        "gateway": payload.gateway,
        "transaction_reference": payload.transaction_reference,
        "status": payload.status,
        "metadata": payload.metadata,
        "paid_at": None,
        "created_at": None,
    })
    return payment_id


def create_invoice_record(payload: InvoiceCreate) -> int:
    invoice_id = len(_INVOICE_STORE) + 1
    _INVOICE_STORE.append({
        "id": invoice_id,
        "tenant_id": payload.tenant_id,
        "subscription_id": payload.subscription_id,
        "amount_due_sar": payload.amount_due_sar,
        "due_date": payload.due_date,
        "line_items": [item.dict() for item in payload.line_items],
        "metadata": payload.metadata,
        "status": "draft",
        "issued_date": None,
        "amount_paid_sar": 0,
    })
    return invoice_id


def get_invoice_record(invoice_id: int) -> Optional[Dict[str, Any]]:
    return next((invoice for invoice in _INVOICE_STORE if invoice["id"] == invoice_id), None)
