"""
routers/billing.py
───────────────────
Billing, subscription, and payment gateway configuration endpoints.

Routes
  GET  /moyasar/settings
  PUT  /moyasar/settings
  POST /payments/create-session
  GET  /billing/plans
  GET  /billing/status
  POST /billing/subscribe
  POST /billing/checkout
  GET  /billing/payment-result
  POST /billing/stripe/setup-intent
  POST /billing/stripe/subscribe
  POST /billing/hyperpay/payment-link
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
from models import (  # noqa: E402
    BillingPlan,
    BillingSubscription,
    Conversation,
    Order,
    PaymentSession,
    Tenant,
)

from core.billing import (
    FREE_TRIAL_DAYS,
    INTEGRATION_FEE_SAR,
    LAUNCH_PROMO_UNTIL,
    ensure_billing_plans,
    get_billing_gateway,
    get_moyasar_settings,
    get_tenant_subscription,
    is_launch_discount_active,
)
from core.config import (
    HYPERPAY_ACCESS_TOKEN,
    HYPERPAY_ENTITY_ID,
    HYPERPAY_LIVE_MODE,
    HYPERPAY_WEBHOOK_SECRET,
)
from core.database import get_db
from core.middleware import rate_limit
from core.tenant import get_or_create_settings, get_or_create_tenant, resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["Billing"])

_MOYASAR_FAIL_STATUSES = frozenset({"failed", "expired", "canceled", "voided", "refunded"})
_BILLING_ACTIVATABLE   = frozenset({"pending_payment"})


def _get_hyperpay_client():
    if not HYPERPAY_ACCESS_TOKEN or not HYPERPAY_ENTITY_ID:
        raise HTTPException(
            status_code=503,
            detail="HyperPay is not configured. Set HYPERPAY_ACCESS_TOKEN and HYPERPAY_ENTITY_ID.",
        )
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from payment_gateways.hyperpay_client import HyperPayClient  # noqa: PLC0415
    return HyperPayClient(
        access_token=HYPERPAY_ACCESS_TOKEN,
        entity_id=HYPERPAY_ENTITY_ID,
        webhook_secret=HYPERPAY_WEBHOOK_SECRET,
        live_mode=HYPERPAY_LIVE_MODE,
    )


# ── Pydantic models ────────────────────────────────────────────────────────────

class MoyasarSettingsIn(BaseModel):
    enabled:         bool = False
    secret_key:      str  = ""
    publishable_key: str  = ""
    webhook_secret:  str  = ""
    callback_url:    str  = ""
    success_url:     str  = ""
    error_url:       str  = ""


class SubscribeRequest(BaseModel):
    plan_slug: str


class CheckoutRequest(BaseModel):
    plan_slug:   str
    success_url: Optional[str] = None
    error_url:   Optional[str] = None


class HyperPayPaymentLinkRequest(BaseModel):
    amount_sar:  float
    brand:       str = "MADA"
    description: str = "Nahla SaaS Monthly Subscription"


# ── Moyasar settings ───────────────────────────────────────────────────────────

@router.get("/moyasar/settings")
async def get_moyasar_settings_endpoint(request: Request, db: Session = Depends(get_db)):
    """Return Moyasar settings for this tenant (keys masked)."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    cfg = get_moyasar_settings(db, tenant_id)
    return {
        "enabled":           cfg.get("enabled", False),
        "publishable_key":   cfg.get("publishable_key", ""),
        "secret_key_hint":   ("***" + cfg.get("secret_key", "")[-4:]) if cfg.get("secret_key") else "",
        "webhook_secret_set": bool(cfg.get("webhook_secret")),
        "callback_url":      cfg.get("callback_url", ""),
        "success_url":       cfg.get("success_url", ""),
        "error_url":         cfg.get("error_url", ""),
    }


@router.put("/moyasar/settings")
async def put_moyasar_settings(
    body: MoyasarSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    s = get_or_create_settings(db, tenant_id)
    meta = dict(s.extra_metadata or {})
    meta["moyasar"] = body.dict()
    s.extra_metadata = meta
    db.add(s)
    db.commit()
    return {"status": "saved"}


# ── Payment session ────────────────────────────────────────────────────────────

@router.post("/payments/create-session")
async def create_payment_session(
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a Moyasar payment session for an order."""
    body = await request.json()
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order_id   = body.get("order_id")
    amount_sar = float(body.get("amount_sar", 0))
    description = str(body.get("description", f"طلب #{order_id}"))

    if amount_sar <= 0:
        raise HTTPException(status_code=422, detail="amount_sar must be > 0")

    if order_id:
        _order_guard = db.query(Order).filter(
            Order.id == order_id, Order.tenant_id == tenant_id,
        ).first()
        if not _order_guard:
            raise HTTPException(status_code=404, detail="Order not found")

    rate_limit(f"pay:{tenant_id}:{order_id or 'anon'}", max_count=3, window_seconds=3600)

    cfg = get_moyasar_settings(db, tenant_id)

    if cfg.get("enabled") and cfg.get("secret_key"):
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from payment_gateways.moyasar import MoyasarClient  # noqa: PLC0415
        client = MoyasarClient(
            secret_key=cfg["secret_key"],
            publishable_key=cfg.get("publishable_key", ""),
        )
        try:
            invoice = await client.create_invoice(
                amount_sar=amount_sar,
                description=description,
                callback_url=cfg.get("callback_url") or "https://api.nahla.ai/payments/webhook/moyasar",
                success_url=cfg.get("success_url", ""),
                error_url=cfg.get("error_url", ""),
                metadata={"order_id": str(order_id), "tenant_id": str(tenant_id)},
            )
            gateway_id   = invoice.get("id", "")
            payment_link = invoice.get("url", "")
            gateway      = "moyasar"
        except Exception as exc:
            logger.error("[Moyasar] create_invoice failed for tenant=%s: %s", tenant_id, exc)
            raise HTTPException(status_code=502, detail=f"Payment gateway error: {exc}")
    else:
        gateway_id   = ""
        payment_link = (
            f"https://pay.nahla.ai/checkout/{tenant_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        )
        gateway = "placeholder"
        logger.warning("[Payment] Moyasar not configured for tenant=%s, returning placeholder", tenant_id)

    session = PaymentSession(
        tenant_id=tenant_id,
        order_id=order_id,
        gateway=gateway,
        gateway_payment_id=gateway_id,
        amount_sar=amount_sar,
        currency="SAR",
        status="pending",
        payment_link=payment_link,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(session)

    if order_id:
        _order_guard.checkout_url = payment_link  # type: ignore[possibly-undefined]

    from observability.event_logger import log_event  # noqa: PLC0415
    log_event(
        db, tenant_id, category="payment", event_type="payment.session_created",
        summary=f"رابط دفع بقيمة {amount_sar} ر.س [{gateway}]",
        severity="info" if gateway != "placeholder" else "warning",
        payload={"amount_sar": amount_sar, "gateway": gateway, "order_id": order_id},
        reference_id=str(order_id) if order_id else None,
    )
    db.commit()

    return {
        "session_id":  session.id,
        "payment_link": payment_link,
        "gateway":     gateway,
        "amount_sar":  amount_sar,
    }


# ── Nahla subscription plans ───────────────────────────────────────────────────

@router.get("/billing/plans")
async def list_billing_plans(db: Session = Depends(get_db)):
    """Return all available Nahla SaaS subscription plans."""
    ensure_billing_plans(db)
    plans = (
        db.query(BillingPlan)
        .filter(BillingPlan.tenant_id == None)  # noqa: E711
        .order_by(BillingPlan.price_sar)
        .all()
    )
    result = []
    for p in plans:
        meta = p.extra_metadata or {}
        result.append({
            "id":               p.id,
            "slug":             p.slug,
            "name":             p.name,
            "name_ar":          meta.get("name_ar", p.name),
            "description":      p.description,
            "price_sar":        p.price_sar,
            "launch_price_sar": meta.get("launch_price_sar", p.price_sar),
            "billing_cycle":    p.billing_cycle,
            "features":         p.features or [],
            "limits":           p.limits or {},
        })
    return {"plans": result, "integration_fee_sar": INTEGRATION_FEE_SAR}


@router.get("/billing/status")
async def get_billing_status(request: Request, db: Session = Depends(get_db)):
    """Return the current subscription status for the tenant."""
    tenant_id = resolve_tenant_id(request)
    ensure_billing_plans(db)

    sub = get_tenant_subscription(db, tenant_id)

    conversations_used = (
        db.query(Conversation).filter(Conversation.tenant_id == tenant_id).count()
    )

    tenant = get_or_create_tenant(db, tenant_id)
    now = datetime.utcnow()
    trial_start        = tenant.created_at or now
    trial_elapsed      = (now - trial_start).days
    trial_days_remaining = max(0, FREE_TRIAL_DAYS - trial_elapsed)
    is_trial     = sub is None and trial_days_remaining > 0
    trial_expired = sub is None and trial_days_remaining == 0

    if sub is None:
        return {
            "has_subscription":       False,
            "plan":                   None,
            "status":                 "trial" if is_trial else "none",
            "is_trial":               is_trial,
            "trial_days_remaining":   trial_days_remaining,
            "trial_expired":          trial_expired,
            "conversations_used":     conversations_used,
            "conversations_limit":    100,
            "launch_discount_active": False,
            "current_price_sar":      0,
            "integration_fee_sar":    INTEGRATION_FEE_SAR,
        }

    plan   = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    meta   = plan.extra_metadata or {} if plan else {}
    launch = is_launch_discount_active(sub)
    price  = meta.get("launch_price_sar", plan.price_sar) if launch else plan.price_sar
    limits = plan.limits or {}

    return {
        "has_subscription":        True,
        "plan": {
            "id":               plan.id,
            "slug":             plan.slug,
            "name":             plan.name,
            "name_ar":          meta.get("name_ar", plan.name),
            "price_sar":        plan.price_sar,
            "launch_price_sar": meta.get("launch_price_sar", plan.price_sar),
            "features":         plan.features or [],
            "limits":           limits,
        },
        "status":                  sub.status,
        "is_trial":                False,
        "trial_days_remaining":    0,
        "trial_expired":           False,
        "started_at":              sub.started_at.isoformat() if sub.started_at else None,
        "conversations_used":      conversations_used,
        "conversations_limit":     limits.get("conversations_per_month", -1),
        "launch_discount_active":  launch,
        "current_price_sar":       price,
        "integration_fee_sar":     INTEGRATION_FEE_SAR,
    }


@router.post("/billing/subscribe")
async def subscribe_to_plan(
    body: SubscribeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Activate a Nahla subscription plan for the tenant."""
    tenant_id = resolve_tenant_id(request)
    ensure_billing_plans(db)

    plan = (
        db.query(BillingPlan)
        .filter(BillingPlan.slug == body.plan_slug, BillingPlan.tenant_id == None)  # noqa: E711
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    db.query(BillingSubscription).filter(
        BillingSubscription.tenant_id == tenant_id,
        BillingSubscription.status == "active",
    ).update({"status": "cancelled"}, synchronize_session=False)

    now = datetime.utcnow()
    sub = BillingSubscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        auto_renew=True,
        extra_metadata={"activated_by": "dashboard"},
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    meta   = plan.extra_metadata or {}
    launch = is_launch_discount_active(sub)
    price  = meta.get("launch_price_sar", plan.price_sar) if launch else plan.price_sar

    logger.info(
        "[Billing] Tenant %s subscribed to plan '%s' (launch=%s)", tenant_id, body.plan_slug, launch,
    )
    return {
        "success":               True,
        "subscription_id":       sub.id,
        "plan_slug":             plan.slug,
        "launch_discount_active": launch,
        "current_price_sar":     price,
    }


@router.post("/billing/checkout")
async def create_billing_checkout(
    body: CheckoutRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Create a payment checkout session for a Nahla subscription plan.
    Gateway-agnostic: Moyasar when configured, otherwise demo activation.
    """
    tenant_id = resolve_tenant_id(request)
    ensure_billing_plans(db)

    plan = (
        db.query(BillingPlan)
        .filter(BillingPlan.slug == body.plan_slug, BillingPlan.tenant_id == None)  # noqa: E711
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    plan_meta = plan.extra_metadata or {}
    now        = datetime.utcnow()
    is_launch  = now <= LAUNCH_PROMO_UNTIL
    price_sar  = int(plan_meta.get("launch_price_sar", plan.price_sar)) if is_launch else int(plan.price_sar)

    base_success = (body.success_url or "").rstrip("/") or "https://app.nahlah.ai/billing"
    base_error   = (body.error_url   or "").rstrip("/") or "https://app.nahlah.ai/billing"

    gateway_client, gateway_name, gateway_cfg = get_billing_gateway(db, tenant_id)

    if gateway_client is not None:
        db.query(BillingSubscription).filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status == "pending_payment",
        ).update({"status": "cancelled"}, synchronize_session=False)

        sub = BillingSubscription(
            tenant_id=tenant_id,
            plan_id=plan.id,
            status="pending_payment",
            started_at=now,
            auto_renew=True,
            extra_metadata={
                "gateway": gateway_name,
                "price_charged_sar": price_sar,
                "launch_discount": is_launch,
            },
        )
        db.add(sub)
        db.flush()

        success_redirect = f"{base_success}?status=paid&sub_id={sub.id}"
        error_redirect   = f"{base_error}?status=failed&sub_id={sub.id}"

        try:
            invoice = await gateway_client.create_invoice(
                amount_sar=float(price_sar),
                description=f"نحلة — خطة {plan_meta.get('name_ar', plan.name)} (شهري)",
                callback_url="https://api.nahlah.ai/billing/webhook/moyasar/subscription",
                success_url=success_redirect,
                error_url=error_redirect,
                metadata={
                    "subscription_id": str(sub.id),
                    "tenant_id":       str(tenant_id),
                    "plan_slug":       plan.slug,
                },
            )
        except Exception as exc:
            db.rollback()
            logger.error("[Billing] Moyasar invoice error tenant=%s: %s", tenant_id, exc)
            raise HTTPException(status_code=502, detail=f"Payment gateway error: {exc}")

        invoice_id   = invoice.get("id", "")
        checkout_url = invoice.get("url", "")

        meta = dict(sub.extra_metadata or {})
        meta["moyasar_invoice_id"] = invoice_id
        sub.extra_metadata = meta
        db.commit()

        logger.info(
            "[Billing] Checkout created tenant=%s plan=%s amount=%s SAR invoice=%s",
            tenant_id, plan.slug, price_sar, invoice_id,
        )
        return {
            "subscription_id": sub.id,
            "checkout_url":    checkout_url,
            "gateway":         gateway_name,
            "amount_sar":      price_sar,
            "plan_slug":       plan.slug,
            "demo_mode":       False,
        }

    # Demo / no-gateway flow — activate immediately
    db.query(BillingSubscription).filter(
        BillingSubscription.tenant_id == tenant_id,
        BillingSubscription.status == "active",
    ).update({"status": "cancelled"}, synchronize_session=False)

    sub = BillingSubscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        auto_renew=True,
        extra_metadata={
            "activated_by":      "demo_checkout",
            "price_charged_sar": price_sar,
            "launch_discount":   is_launch,
        },
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    logger.info("[Billing] Demo checkout: tenant=%s plan=%s activated directly", tenant_id, plan.slug)
    return {
        "subscription_id":        sub.id,
        "checkout_url":           None,
        "gateway":                "demo",
        "amount_sar":             price_sar,
        "plan_slug":              plan.slug,
        "demo_mode":              True,
        "success":                True,
        "launch_discount_active": is_launch,
        "current_price_sar":      price_sar,
    }


@router.get("/billing/payment-result")
async def billing_payment_result(
    request: Request,
    db: Session = Depends(get_db),
    sub_id: Optional[int] = None,
    status: Optional[str] = None,
):
    """Return subscription status for the payment-result page after Moyasar redirect."""
    if not sub_id:
        return {"activated": False, "status": "unknown"}

    tenant_id = resolve_tenant_id(request)
    sub = db.query(BillingSubscription).filter(BillingSubscription.id == sub_id).first()
    if not sub:
        return {"activated": False, "status": "not_found"}

    if sub.tenant_id != int(tenant_id):
        raise HTTPException(status_code=403, detail="Access denied")

    plan      = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    plan_meta = plan.extra_metadata or {} if plan else {}

    return {
        "subscription_id": sub.id,
        "status":          sub.status,
        "activated":       sub.status == "active",
        "plan_slug":       plan.slug if plan else None,
        "plan_name_ar":    plan_meta.get("name_ar", plan.name if plan else ""),
        "amount_sar":      (sub.extra_metadata or {}).get("price_charged_sar"),
    }





# ── HyperPay billing ───────────────────────────────────────────────────────────

@router.post("/billing/hyperpay/payment-link")
async def hyperpay_create_payment_link(
    body:    HyperPayPaymentLinkRequest,
    request: Request,
    db:      Session = Depends(get_db),
):
    """
    Create a HyperPay checkout session for Saudi local payment methods
    (MADA, Apple Pay, STC Pay).
    """
    tenant_id = resolve_tenant_id(request)
    tenant    = get_or_create_tenant(db, tenant_id)
    hp        = _get_hyperpay_client()

    result = await hp.create_checkout(
        amount=body.amount_sar,
        currency="SAR",
        brand=body.brand,
        merchant_transaction_id=f"nahla-{tenant_id}-{int(datetime.utcnow().timestamp())}",
        description=body.description,
        metadata={"tenant_id": str(tenant_id)},
    )

    checkout_id = result.get("id", "")
    result_code = result.get("result", {}).get("code", "")

    tenant.hyperpay_payment_id = checkout_id
    tenant.billing_provider    = "hyperpay"
    tenant.billing_status      = "pending"
    db.commit()

    logger.info(
        "[HyperPay] Checkout created for tenant %s: id=%s brand=%s amount=%s SAR",
        tenant_id, checkout_id, body.brand, body.amount_sar,
    )
    return {
        "checkout_id":        checkout_id,
        "result_code":        result_code,
        "payment_widget_url": hp.build_payment_page_url(checkout_id, body.brand),
    }
