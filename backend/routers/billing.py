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
  POST /billing/hyperpay/payment-link
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import (  # noqa: E402
    BillingPlan,
    BillingSubscription,
    Conversation,
    Order,
    PaymentSession,
    Tenant,
)

from core.billing import (
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
from core.auth import require_admin as require_admin_dep, require_not_support_impersonation
from core.database import get_db
from core.middleware import rate_limit
from core.tenant import (
    DEFAULT_STORE,
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    get_or_create_tenant,
    merge_defaults,
    resolve_tenant_id,
)
from core.wa_notify import (
    notify_payment_invoice,
    notify_payment_link,
    notify_subscription_confirmed,
)

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
    _no_support: dict = Depends(require_not_support_impersonation),
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
                callback_url=cfg.get("callback_url") or "https://api.nahlah.ai/payments/webhook/moyasar",
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
            f"https://pay.nahlah.ai/checkout/{tenant_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
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
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
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
    try:
        ensure_billing_plans(db)
    except Exception as exc:
        logger.error("ensure_billing_plans failed: %s", exc, exc_info=True)
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
    """Return the current subscription status for the tenant.

    Also performs a **lazy self-heal reconcile**: if this tenant has a
    ``pending_payment`` Moyasar sub, we ask Moyasar's invoice API
    whether it's actually paid and activate it on the spot. This is
    what closes the gap left by Moyasar's invoice ``callback_url``
    being a browser-redirect (not a webhook) — without it, a merchant
    who paid in another tab and came back to the dashboard would stay
    on trial forever even though the funds were captured. Tenant 33
    was the production case.
    """
    tenant_id = resolve_tenant_id(request)
    logger.info("billing/status called for tenant_id=%s", tenant_id)
    try:
        ensure_billing_plans(db)
    except Exception as exc:
        logger.error("ensure_billing_plans failed in status: %s", exc, exc_info=True)

    # ── Self-heal: reconcile any pending Moyasar invoice for this tenant ─
    try:
        from services.billing_activation import lazy_reconcile_tenant_pending_subs  # noqa: PLC0415
        await lazy_reconcile_tenant_pending_subs(
            db, int(tenant_id), source="billing_status",
        )
    except Exception as exc:
        # Reconcile failures must NEVER break the status endpoint.
        logger.warning("[Billing] lazy reconcile failed for tenant=%s: %s", tenant_id, exc)

    sub = get_tenant_subscription(db, tenant_id)

    # Use real monthly usage from the WhatsApp usage tracker
    try:
        from core.wa_usage import get_usage_this_month  # noqa: PLC0415
        _usage_data = get_usage_this_month(db, tenant_id)
    except Exception as exc:
        logger.warning("get_usage_this_month failed (non-fatal): %s", exc)
        _usage_data = {
            "conversations_used":           0,
            "conversations_limit":          1000,
            "usage_pct":                    0.0,
            "exceeded":                     False,
            "near_limit":                   False,
            "marketing_blocked":            False,
            "emergency_stop":               False,
            "unlimited":                    False,
            "month":                        1,
            "year":                         2025,
            "reset_date":                   "01/2/2025",
            "alert_80_sent":                False,
            "alert_100_sent":               False,
        }
    conversations_used = _usage_data["conversations_used"]

    tenant = get_or_create_tenant(db, tenant_id)

    from core.billing import compute_trial_info  # noqa: PLC0415

    trial_info           = compute_trial_info(tenant)
    is_trial             = sub is None and trial_info["is_trial"]
    trial_expired        = sub is None and trial_info["trial_expired"]
    trial_pending_wa     = sub is None and trial_info.get("trial_pending_whatsapp", False)
    trial_days_remaining = trial_info["trial_days_remaining"]

    sub_expired = False
    if sub and sub.ends_at:
        from core.billing import _coerce_utc  # noqa: PLC0415
        ends = _coerce_utc(sub.ends_at)
        sub_expired = bool(ends and ends <= datetime.now(timezone.utc))

    if sub is None:
        return {
            "has_subscription":       False,
            "plan":                   None,
            "status":                 trial_info.get("status") or ("trial" if is_trial else "none"),
            "is_trial":               is_trial,
            "trial_pending_whatsapp": trial_pending_wa,
            "trial_days_remaining":   trial_days_remaining,
            "trial_expired":          trial_expired,
            "trial_started_at":       trial_info.get("trial_started_at"),
            "trial_ends_at":          trial_info.get("trial_end"),
            "subscription_started_at": None,
            "subscription_ends_at":   None,
            "subscription_expired":   False,
            "status_reason_ar":       trial_info.get("status_reason_ar", ""),
            "warning_level":          trial_info.get("warning_level", "none"),
            "conversations_used":     conversations_used,
            "conversations_limit":    _usage_data["conversations_limit"],
            "usage_pct":              _usage_data["usage_pct"],
            "conversations_exceeded": _usage_data["exceeded"],
            "launch_discount_active": False,
            "current_price_sar":      0,
            "integration_fee_sar":    INTEGRATION_FEE_SAR,
        }

    plan   = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    meta   = plan.extra_metadata or {} if plan else {}
    launch = is_launch_discount_active(sub)
    price  = meta.get("launch_price_sar", plan.price_sar) if launch else plan.price_sar
    limits = plan.limits or {}

    from core.billing import _coerce_utc  # noqa: PLC0415
    now_utc = datetime.now(timezone.utc)
    sub_ends = _coerce_utc(sub.ends_at) if sub.ends_at else None
    sub_expired = bool(sub_ends and sub_ends <= now_utc)
    days_until_sub_end = 0
    if sub_ends and sub_ends > now_utc:
        days_until_sub_end = max(0, int((sub_ends - now_utc).total_seconds() / 86400) + 1)

    warning_level = "none"
    if sub_expired:
        warning_level = "expired"
    elif days_until_sub_end <= 1:
        warning_level = "1d"
    elif days_until_sub_end <= 3:
        warning_level = "3d"
    elif days_until_sub_end <= 7:
        warning_level = "7d"

    status_reason = (
        "انتهى الاشتراك المدفوع — يرجى التجديد"
        if sub_expired
        else "اشتراك مدفوع نشط"
    )

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
        "status":                  "expired" if sub_expired else sub.status,
        "is_trial":                False,
        "trial_pending_whatsapp":  False,
        "trial_days_remaining":    0,
        "trial_expired":           False,
        "trial_started_at":        None,
        "trial_ends_at":           None,
        "subscription_started_at": sub.started_at.isoformat() if sub.started_at else None,
        "subscription_ends_at":    sub.ends_at.isoformat() if sub.ends_at else None,
        "subscription_expired":    sub_expired,
        "status_reason_ar":        status_reason,
        "warning_level":           warning_level,
        "started_at":              sub.started_at.isoformat() if sub.started_at else None,
        "conversations_used":      conversations_used,
        "conversations_limit":     _usage_data["conversations_limit"],
        "usage_pct":               _usage_data["usage_pct"],
        "conversations_exceeded":  _usage_data["exceeded"],
        "launch_discount_active":  launch,
        "current_price_sar":       price,
        "integration_fee_sar":     INTEGRATION_FEE_SAR,
    }


@router.post("/billing/subscribe")
async def subscribe_to_plan(
    body: SubscribeRequest,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
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

    now = datetime.now(timezone.utc)
    from core.trial_lifecycle import subscription_period_end  # noqa: PLC0415
    sub = BillingSubscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        ends_at=subscription_period_end(now).replace(tzinfo=None),
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

    # ── WhatsApp notification ─────────────────────────────────────────────────
    try:
        import asyncio  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415
        _settings    = get_or_create_settings(db, tenant_id)
        _wa_cfg      = merge_defaults(_settings.whatsapp_settings, DEFAULT_WHATSAPP)
        _store_cfg   = merge_defaults(_settings.store_settings,    DEFAULT_STORE)
        owner_phone  = _wa_cfg.get("owner_whatsapp_number", "")
        store_name   = _store_cfg.get("store_name") or f"متجر #{tenant_id}"
        plan_name    = meta.get("name_ar", plan.name)
        if owner_phone:
            next_billing = now + timedelta(days=30)
            asyncio.create_task(notify_subscription_confirmed(
                owner_phone, store_name, plan_name, int(price), next_billing,
            ))
            asyncio.create_task(notify_payment_invoice(
                owner_phone, store_name, plan_name, int(price),
                str(sub.id), now,
            ))
    except Exception as _exc:
        logger.warning("[Billing] WA notify failed: %s", _exc)

    return {
        "success":               True,
        "subscription_id":       sub.id,
        "plan_slug":             plan.slug,
        "launch_discount_active": launch,
        "current_price_sar":     price,
    }


class ResetTrialRequest(BaseModel):
    tenant_id: int
    days: int = 14


@router.post("/billing/reset-trial")
async def reset_trial(
    body: ResetTrialRequest,
    request: Request,
    db: Session = Depends(get_db),
    _admin = Depends(require_admin_dep),
):
    """
    Admin endpoint: reset or extend the free trial for a tenant.
    Sets trial_ends_at = now + body.days.
    """
    tenant = get_or_create_tenant(db, body.tenant_id)
    now = datetime.now(timezone.utc)
    new_end = now + timedelta(days=body.days)

    tenant.trial_ends_at    = new_end
    tenant.trial_started_at = now
    from core.trial_lifecycle import TRIAL_STATUS_ACTIVE  # noqa: PLC0415
    tenant.subscription_status = TRIAL_STATUS_ACTIVE
    db.commit()

    logger.info("[Billing] Trial reset — tenant=%s days=%s new_end=%s", body.tenant_id, body.days, new_end)
    return {
        "success": True,
        "tenant_id": body.tenant_id,
        "trial_days": body.days,
        "trial_ends_at": new_end.isoformat(),
    }


@router.post("/billing/checkout")
async def create_billing_checkout(
    body: CheckoutRequest,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    """
    Create a payment checkout session for a Nahla subscription plan.
    Gateway-agnostic: Moyasar when configured, otherwise demo activation.
    """
    tenant_id = resolve_tenant_id(request)
    try:
        return await _do_checkout(body, request, db, tenant_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[Billing] Unexpected checkout error tenant=%s: %s",
            tenant_id, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code":    "internal_error",
                "message": "حدث خطأ داخلي. حاول مرة أخرى أو تواصل مع الدعم.",
            },
        )


async def _do_checkout(
    body: "CheckoutRequest",
    request: Request,
    db: Session,
    tenant_id: str,
) -> dict:
    """Inner checkout logic — all non-HTTPExceptions bubble up to the caller."""
    try:
        ensure_billing_plans(db)
    except Exception as _ep_exc:
        logger.warning("[Billing] ensure_billing_plans non-fatal: %s", _ep_exc)
        # CRITICAL: if ensure_billing_plans failed mid-transaction the SQLAlchemy
        # session is in an error state. We must rollback before any further query.
        try:
            db.rollback()
        except Exception:
            pass

    plan = (
        db.query(BillingPlan)
        .filter(BillingPlan.slug == body.plan_slug, BillingPlan.tenant_id == None)  # noqa: E711
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    plan_meta = plan.extra_metadata or {}
    now        = datetime.now(timezone.utc)
    is_launch  = now <= LAUNCH_PROMO_UNTIL
    price_sar  = int(plan_meta.get("launch_price_sar", plan.price_sar)) if is_launch else int(plan.price_sar)

    base_success = (body.success_url or "").rstrip("/") or "https://app.nahlah.ai/billing"
    base_error   = (body.error_url   or "").rstrip("/") or "https://app.nahlah.ai/billing"

    gateway_client, gateway_name, gateway_cfg = get_billing_gateway(db, tenant_id)

    # ── Self-heal: maybe the merchant already paid an older sub ───────
    # If the merchant double-clicked Subscribe an hour ago, paid that
    # invoice, and is now clicking again expecting "Pay" but actually
    # already paid — we must NOT create a new sub. Reconcile first
    # so any paid-but-stuck invoice gets activated, and only then
    # decide whether checkout needs a new row.
    try:
        from services.billing_activation import lazy_reconcile_tenant_pending_subs  # noqa: PLC0415
        await lazy_reconcile_tenant_pending_subs(
            db, int(tenant_id), source="checkout_self_heal",
        )
    except Exception as _exc:
        logger.warning("[Billing] checkout self-heal reconcile failed: %s", _exc)

    # If reconcile activated something, the tenant now has an active
    # sub — return it instead of creating a new one. This is the
    # "already paid, please don't bill me again" case.
    existing_active = (
        db.query(BillingSubscription)
        .filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status == "active",
        )
        .order_by(BillingSubscription.id.desc())
        .first()
    )
    if existing_active and existing_active.plan_id == plan.id:
        logger.info(
            "[Billing] checkout idempotent: tenant=%s already active on plan=%s sub=%s",
            tenant_id, plan.slug, existing_active.id,
        )
        return {
            "subscription_id": existing_active.id,
            "checkout_url":    None,
            "gateway":         gateway_name or "active",
            "amount_sar":      price_sar,
            "plan_slug":       plan.slug,
            "demo_mode":       False,
            "already_active":  True,
        }

    if gateway_client is not None:
        # ── Idempotent reuse of existing pending sub for SAME plan ────
        # If the merchant already has a pending_payment subscription
        # for this plan with a Moyasar invoice attached, fetch the
        # invoice's checkout URL from Moyasar and return it instead
        # of creating a new sub. This is the canonical fix for the
        # tenant-33 class of bug — N clicks on Subscribe must not
        # produce N orphan subs.
        existing_pending = (
            db.query(BillingSubscription)
            .filter(
                BillingSubscription.tenant_id == tenant_id,
                BillingSubscription.status == "pending_payment",
                BillingSubscription.plan_id == plan.id,
            )
            .order_by(BillingSubscription.id.desc())
            .first()
        )
        existing_invoice_id = (
            (existing_pending.extra_metadata or {}).get("moyasar_invoice_id")
            if existing_pending else None
        )
        existing_price = (
            int((existing_pending.extra_metadata or {}).get("price_charged_sar") or 0)
            if existing_pending else 0
        )

        if existing_pending and existing_invoice_id and existing_price == price_sar:
            try:
                inv_data = await gateway_client.get_invoice(existing_invoice_id)
                inv_status = (inv_data.get("status") or "").lower() if isinstance(inv_data, dict) else ""
                inv_url = (
                    (inv_data or {}).get("url")
                    or (inv_data or {}).get("source", {}).get("transaction_url")
                )
                # Reuse only if the invoice is still pay-able. Paid
                # invoices were just activated by the reconcile above,
                # so we shouldn't reach here for those. Expired/failed
                # → fall through and create a fresh one.
                if inv_status in ("initiated", "pending") and inv_url:
                    logger.info(
                        "[Billing] checkout idempotent: reusing pending sub=%s "
                        "invoice=%s tenant=%s plan=%s",
                        existing_pending.id, existing_invoice_id, tenant_id, plan.slug,
                    )
                    return {
                        "subscription_id": existing_pending.id,
                        "checkout_url":    inv_url,
                        "gateway":         gateway_name,
                        "amount_sar":      price_sar,
                        "plan_slug":       plan.slug,
                        "demo_mode":       False,
                        "reused":          True,
                    }
            except Exception as _exc:
                logger.warning(
                    "[Billing] could not reuse existing invoice (will create new): %s",
                    _exc,
                )
                # Fall through to fresh-create path.

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
            try:
                db.rollback()
            except Exception:
                pass
            err_text = str(exc).lower()
            logger.error(
                "[Billing] Moyasar invoice error tenant=%s plan=%s: %s",
                tenant_id, plan.slug, exc,
            )

            # ── Detect "account not yet approved / disabled / unauthorized" ─
            # Moyasar typically returns these as 401/403 with messages like:
            #   "merchant disabled", "account not active",
            #   "invalid api key", "unauthorized", "onboarding pending".
            provider_not_ready_signals = (
                "unauthorized",
                "invalid api key",
                "invalid_api_key",
                "merchant disabled",
                "merchant_disabled",
                "account not active",
                "account_not_active",
                "onboarding",
                "not approved",
                "not_approved",
                "disabled",
                "401",
                "403",
            )
            is_provider_not_ready = any(s in err_text for s in provider_not_ready_signals)

            if is_provider_not_ready:
                logger.warning(
                    "[Billing] Payment provider not ready (likely under review) "
                    "tenant=%s plan=%s — surfacing manual-activation path",
                    tenant_id, plan.slug,
                )
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code":    "payment_provider_not_ready",
                        "message": "بوابة الدفع قيد المراجعة حاليًا. يمكنك تفعيل الاشتراك يدويًا عبر الدعم.",
                    },
                )

            raise HTTPException(
                status_code=502,
                detail={
                    "code":    "payment_gateway_error",
                    "message": f"تعذّر الاتصال ببوابة الدفع. حاول لاحقاً أو فعّل الاشتراك يدويًا عبر الدعم. ({exc})",
                },
            )

        invoice_id   = invoice.get("id", "")
        checkout_url = invoice.get("url", "")

        # ── Defensive: gateway accepted the call but didn't return a usable URL ─
        # This happens for some half-onboarded merchant accounts.
        # Do NOT activate the subscription — surface a clear manual path.
        if not checkout_url:
            db.rollback()
            logger.warning(
                "[Billing] Moyasar returned no checkout URL tenant=%s plan=%s invoice=%s",
                tenant_id, plan.slug, invoice_id,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code":    "payment_provider_not_ready",
                    "message": "بوابة الدفع قيد المراجعة حاليًا. يمكنك تفعيل الاشتراك يدويًا عبر الدعم.",
                },
            )

        meta = dict(sub.extra_metadata or {})
        meta["moyasar_invoice_id"] = invoice_id
        sub.extra_metadata = meta
        db.commit()

        logger.info(
            "[Billing] Checkout created tenant=%s plan=%s amount=%s SAR invoice=%s",
            tenant_id, plan.slug, price_sar, invoice_id,
        )

        # ── Send payment link via WhatsApp ────────────────────────────────────
        try:
            import asyncio  # noqa: PLC0415
            from services.billing_formatter import resolve_billing_context, build_nahla_payment_link_message  # noqa: PLC0415

            _ctx = resolve_billing_context(
                db,
                tenant_id=int(tenant_id),
                sub=sub,
                plan_obj=plan,
                payment_id=invoice_id,
                payment_amount_sar=price_sar,
            )
            logger.info(
                "[NAHLA PAYMENT LINK CREATED] tenant=%s merchant=%r store=%r "
                "plan=%s amount=%s SAR invoice=%s",
                tenant_id, _ctx["merchant_name"], _ctx["store_name"],
                plan.slug, price_sar, invoice_id,
            )
            _phone = _ctx["merchant_phone"]
            if _phone and checkout_url:
                _msg = build_nahla_payment_link_message(_ctx, checkout_url)
                asyncio.create_task(notify_payment_link(
                    _phone, _ctx["store_name"],
                    _ctx["plan_name"], price_sar, checkout_url,
                    merchant_name=_ctx["merchant_name"],
                    billing_period=_ctx["billing_period"],
                    tenant_id=int(tenant_id),
                ))
        except Exception as _exc:
            logger.warning("[Billing] WA payment link notify failed: %s", _exc)

        return {
            "subscription_id": sub.id,
            "checkout_url":    checkout_url,
            "gateway":         gateway_name,
            "amount_sar":      price_sar,
            "plan_slug":       plan.slug,
            "demo_mode":       False,
        }

    # Demo / no-gateway flow — activate immediately
    from core.trial_lifecycle import subscription_period_end  # noqa: PLC0415

    db.query(BillingSubscription).filter(
        BillingSubscription.tenant_id == tenant_id,
        BillingSubscription.status == "active",
    ).update({"status": "cancelled"}, synchronize_session=False)

    sub = BillingSubscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        ends_at=subscription_period_end(now).replace(tzinfo=None),
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

    # Notify merchant (demo mode — no gateway)
    try:
        import asyncio as _asyncio  # noqa: PLC0415
        from core.notifications import send_email, email_subscription  # noqa: PLC0415
        from core.wa_notify import notify_subscription_confirmed  # noqa: PLC0415
        merchant = db.query(User).filter(
            User.tenant_id == tenant_id, User.role == "merchant",
            User.is_active == True,  # noqa: E712
        ).first()
        tenant_obj = get_or_create_tenant(db, tenant_id)
        store_name = tenant_obj.name or f"Tenant {tenant_id}"
        ends_str   = sub.ends_at.strftime("%Y-%m-%d") if sub.ends_at else "—"
        meta_name  = (plan.extra_metadata or {}).get("name_ar", plan.name)
        if merchant and merchant.email:
            _asyncio.ensure_future(send_email(
                to=merchant.email,
                subject=f"✅ تم تفعيل اشتراك {meta_name} — نحلة AI",
                html=email_subscription(store_name, meta_name, ends_str),
            ))
        if merchant and merchant.username:
            _asyncio.ensure_future(notify_subscription_confirmed(
                merchant.username, store_name, meta_name, price_sar, ends_str,
            ))
    except Exception as _notify_exc:
        logger.warning("[Billing] Demo checkout notification error: %s", _notify_exc)

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
    """Return subscription status for the payment-result page after a Moyasar
    redirect. **This endpoint is the primary safety net for activation.**

    Why: Moyasar invoices use ``callback_url`` as a *browser redirect URL*
    after the customer pays — not a server-to-server webhook. A
    server-to-server webhook only arrives if the merchant has registered
    one in the Moyasar dashboard AND we're handling the right event
    shape. So relying on the webhook alone left every successful payment
    stuck in ``pending_payment`` and the merchant looking at a polling
    spinner forever (see services/billing_activation.py for full context).

    Fix: when the polling page hits this endpoint, if the subscription
    is still pending and we know the Moyasar invoice id, we reconcile
    *live* against ``GET /v1/invoices/{id}``. If Moyasar reports the
    invoice as paid, we activate immediately — and the very next poll
    from the frontend gets ``activated=true``. Idempotent end-to-end.
    """
    if not sub_id:
        return {"activated": False, "status": "unknown"}

    tenant_id = resolve_tenant_id(request)
    sub = db.query(BillingSubscription).filter(BillingSubscription.id == sub_id).first()
    if not sub:
        return {"activated": False, "status": "not_found"}

    if sub.tenant_id != int(tenant_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # ── Live reconcile when sub is still pending ──────────────────────
    if sub.status not in ("active", "cancelled", "payment_failed"):
        try:
            from services.billing_activation import reconcile_subscription_from_moyasar  # noqa: PLC0415
            activated, reason = await reconcile_subscription_from_moyasar(
                db, sub, source="result_page_poll",
            )
            logger.info(
                "[Billing] payment-result reconcile tenant=%s sub=%s activated=%s reason=%s",
                tenant_id, sub.id, activated, reason,
            )
            if activated:
                # Re-fetch to get committed state.
                db.refresh(sub)
        except Exception as exc:
            # Reconcile failures must never break the polling page —
            # the merchant should still see the current DB state.
            logger.warning(
                "[Billing] payment-result reconcile failed tenant=%s sub=%s: %s",
                tenant_id, sub.id, exc, exc_info=True,
            )

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
    _no_support: dict = Depends(require_not_support_impersonation),
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
        merchant_transaction_id=f"nahla-{tenant_id}-{int(datetime.now(timezone.utc).timestamp())}",
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


# ── GET /billing/debug/current — fast diagnostics ────────────────────────────

@router.get("/billing/debug/current")
async def billing_debug_current(
    request: Request,
    db: Session = Depends(get_db),
    force_reconcile: bool = False,
    include_traceback: bool = True,
):
    """One-stop diagnostic dump for "why doesn't my plan show?" tickets.

    Hardening contract — this endpoint MUST NEVER 500. Every section
    is wrapped in its own try/except so a failure in one block (e.g.
    the reconcile step crashes, the session is dirtied, get_entitlements
    explodes) cannot prevent the rest of the diagnostic JSON from
    being returned. Each section also calls ``db.rollback()`` defensively
    before its query so a previous block's pending exception cannot
    poison subsequent reads.

    Common patterns this catches:

      * ``active_subscription_id`` set but ``latest_subscription_id``
        *different* and pending → merchant clicked Subscribe twice.
      * ``billing_status_endpoint.has_subscription`` true but
        ``entitlements.plan == "none"`` → plan_id→slug join broken.
      * ``billing_subs_count > 1`` with multiple ``active`` rows →
        data integrity bug.
      * Any single ``_section_errors`` entry → bug to investigate.

    No secrets are leaked — only IDs, statuses, slugs, and tracebacks
    of internal Python errors (which never contain user data).
    """
    import traceback as _tb  # noqa: PLC0415
    from core.billing import compute_trial_info, get_tenant_subscription  # noqa: PLC0415
    from core.plan_entitlements import get_entitlements  # noqa: PLC0415

    payload: Dict[str, Any] = {
        "tenant_id":     None,
        "force_reconcile": force_reconcile,
        "_section_errors": {},
    }

    def _safe_section(name: str):
        """Decorator-ish helper: run ``fn`` and stash the result under
        ``payload[name]``; on exception, capture traceback under
        ``payload['_section_errors'][name]`` and rollback the session
        so the next section starts clean. Returns True iff the section
        succeeded — callers can branch on this if a later section
        depends on the result."""
        def _runner(fn):
            try:
                payload[name] = fn()
                return True
            except Exception as exc:
                # Reset the session so subsequent queries don't
                # get an "InFailedSqlTransaction" cascade error.
                try:
                    db.rollback()
                except Exception:
                    pass
                err: Dict[str, Any] = {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
                if include_traceback:
                    err["traceback"] = _tb.format_exc()
                payload["_section_errors"][name] = err
                payload[name] = None
                return False
        return _runner

    # ── 0. Resolve tenant ────────────────────────────────────────────
    tenant: Optional[Tenant] = None
    try:
        payload["tenant_id"] = resolve_tenant_id(request)
        tenant = get_or_create_tenant(db, payload["tenant_id"])
    except Exception as exc:
        payload["_section_errors"]["resolve_tenant"] = {
            "error_type":    type(exc).__name__,
            "error_message": str(exc),
            "traceback":     _tb.format_exc() if include_traceback else None,
        }
        # Without a tenant_id we can't query anything else — return
        # what we have so the caller at least sees the auth error.
        return payload

    tenant_id = payload["tenant_id"]

    # ── 1. Force reconcile (optional) ────────────────────────────────
    # This is the most likely source of crashes (network call, Moyasar
    # parse, DB write conflict). Isolated so a failure here doesn't
    # take the rest of the dump with it. Cannot use _safe_section
    # because that wraps a sync function and we need ``await``.
    if force_reconcile:
        try:
            from services.billing_activation import (  # noqa: PLC0415
                _LAZY_RECONCILE_LAST,
                lazy_reconcile_tenant_pending_subs,
            )
            _LAZY_RECONCILE_LAST.clear()
            _, reconcile_results = await lazy_reconcile_tenant_pending_subs(
                db, int(tenant_id), source="debug_force_reconcile",
            )
            payload["reconcile_actions"] = reconcile_results
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            payload["_section_errors"]["reconcile_actions"] = {
                "error_type":    type(exc).__name__,
                "error_message": str(exc),
                "traceback":     _tb.format_exc() if include_traceback else None,
            }
            payload["reconcile_actions"] = None
    else:
        payload["reconcile_actions"] = []

    # Re-fetch tenant in case reconcile updated tenant.subscription_status.
    try:
        db.rollback()
    except Exception:
        pass
    try:
        tenant = get_or_create_tenant(db, tenant_id)
    except Exception:
        pass

    # ── 2. Tenant snapshot ───────────────────────────────────────────
    @_safe_section("tenant")
    def _tenant_view():
        if tenant is None:
            return None
        return {
            "name":                tenant.name,
            "subscription_status": tenant.subscription_status,
            "trial_started_at":    tenant.trial_started_at.isoformat() if tenant.trial_started_at else None,
            "trial_ends_at":       tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
        }

    # ── 3. All subs + active/latest views ────────────────────────────
    def _sub_view(sub: Optional[BillingSubscription]) -> Optional[Dict[str, Any]]:
        if not sub:
            return None
        plan = None
        try:
            if sub.plan_id:
                plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            plan = None
        meta = sub.extra_metadata or {}
        return {
            "id":                 sub.id,
            "status":             sub.status,
            "plan_id":            sub.plan_id,
            "plan_slug":          plan.slug if plan else None,
            "plan_name_ar":       (plan.extra_metadata or {}).get("name_ar") if plan else None,
            "started_at":         sub.started_at.isoformat() if sub.started_at else None,
            "ends_at":            sub.ends_at.isoformat() if sub.ends_at else None,
            "moyasar_invoice_id": meta.get("moyasar_invoice_id"),
            "moyasar_payment_id": meta.get("moyasar_payment_id"),
            "activation_source":  meta.get("activation_source"),
            "paid_at":            meta.get("paid_at"),
            "price_charged_sar":  meta.get("price_charged_sar"),
        }

    all_subs: list = []

    @_safe_section("billing_subs_summary")
    def _subs_summary():
        nonlocal all_subs
        all_subs = (
            db.query(BillingSubscription)
            .filter(BillingSubscription.tenant_id == tenant_id)
            .order_by(BillingSubscription.id.desc())
            .all()
        )
        return {
            "count":         len(all_subs),
            "by_status":     {s: sum(1 for x in all_subs if x.status == s) for s in {x.status for x in all_subs}},
            "all_sub_ids":   [x.id for x in all_subs],
        }

    @_safe_section("active_subscription")
    def _active_view():
        return _sub_view(get_tenant_subscription(db, tenant_id))

    @_safe_section("latest_subscription")
    def _latest_view():
        return _sub_view(all_subs[0] if all_subs else None)

    # ── 4. Entitlements ──────────────────────────────────────────────
    @_safe_section("entitlements")
    def _ent_view():
        ent = get_entitlements(db, tenant_id)
        # The dataclass field is ``plan_slug``; the JSON-shaped public
        # API uses key ``plan`` via ``to_dict()``. We hand back the
        # full ``to_dict()`` so the debug payload mirrors exactly what
        # /billing/entitlements returns to the dashboard, plus a couple
        # of extra fields we lift to the top level for at-a-glance
        # comparison with active_subscription.plan_slug.
        d = ent.to_dict()
        return {
            **d,
            "plan_slug":  ent.plan_slug,  # dataclass field, == d["plan"]
            "is_active":  ent.is_active,
            "is_blocked": ent.is_blocked,
        }

    # ── 5. Trial info + billing-status endpoint mirror ───────────────
    @_safe_section("trial_info")
    def _trial_view():
        if tenant is None:
            return None
        return compute_trial_info(tenant)

    @_safe_section("billing_status_endpoint")
    def _bstatus_view():
        active_sub = None
        try:
            active_sub = get_tenant_subscription(db, tenant_id)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        trial = compute_trial_info(tenant) if tenant else {"is_trial": False, "trial_expired": False}
        return {
            "has_subscription": active_sub is not None,
            "status_field":     active_sub.status if active_sub else ("trial" if trial.get("is_trial") else "none"),
            "is_trial":         active_sub is None and trial.get("is_trial", False),
            "trial_expired":    active_sub is None and trial.get("trial_expired", False),
        }

    # ── 6. WA usage (the actual source the Overview page reads) ──────
    @_safe_section("wa_usage_overview_source")
    def _wa_usage_view():
        from core.wa_usage import get_usage_this_month  # noqa: PLC0415
        return get_usage_this_month(db, int(tenant_id))

    # ── 7. Plain-DB peek at sub 11 metadata (for tenant 33 case) ─────
    # When the merchant has many subs, this surfaces the row metadata
    # in raw form so we can compare against ``active_subscription``.
    @_safe_section("raw_pending_subs")
    def _raw_pending():
        rows = (
            db.query(BillingSubscription)
            .filter(
                BillingSubscription.tenant_id == tenant_id,
                BillingSubscription.status.in_(["pending_payment", "payment_failed"]),
            )
            .all()
        )
        return [
            {
                "id":                 s.id,
                "status":             s.status,
                "plan_id":            s.plan_id,
                "moyasar_invoice_id": (s.extra_metadata or {}).get("moyasar_invoice_id"),
                "moyasar_payment_id": (s.extra_metadata or {}).get("moyasar_payment_id"),
                "activation_source":  (s.extra_metadata or {}).get("activation_source"),
            }
            for s in rows
        ]

    # If everything succeeded the errors dict is empty, which is the
    # signal "this endpoint is healthy".
    payload["_healthy"] = not payload["_section_errors"]
    return payload


# ── GET /billing/entitlements ──────────────────────────────────────────────────

@router.get("/billing/entitlements")
async def get_billing_entitlements(request: Request, db: Session = Depends(get_db)):
    """
    Return the tenant's current plan entitlements — features, limits, billing status.

    Used by:
    - Frontend useEntitlements() hook
    - FeatureGate component (show/lock UI features)
    - Any endpoint that needs to check plan before returning data
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    # ── Self-heal: same lazy reconcile as /billing/status. Both
    # endpoints are mounted on basically every dashboard page, so
    # whichever fires first will catch a pending Moyasar invoice and
    # flip it to active before this function reads entitlements.
    try:
        from services.billing_activation import lazy_reconcile_tenant_pending_subs  # noqa: PLC0415
        await lazy_reconcile_tenant_pending_subs(
            db, int(tenant_id), source="billing_entitlements",
        )
    except Exception as exc:
        logger.warning(
            "[Billing] lazy reconcile failed for tenant=%s in entitlements: %s",
            tenant_id, exc,
        )

    from core.plan_entitlements import get_entitlements  # noqa: PLC0415
    ent = get_entitlements(db, tenant_id)

    # ── Monthly usage counters ────────────────────────────────────────────────
    from datetime import datetime, timezone  # noqa: PLC0415
    from models import Conversation, Campaign  # noqa: PLC0415

    now     = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    try:
        conv_count = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id >= tenant_id,
                Conversation.tenant_id == tenant_id,
                Conversation.created_at >= month_start.replace(tzinfo=None),
            )
            .count()
        )
    except Exception:
        conv_count = 0

    try:
        camp_count = (
            db.query(Campaign)
            .filter(
                Campaign.tenant_id == tenant_id,
                Campaign.created_at >= month_start.replace(tzinfo=None),
            )
            .count()
        )
    except Exception:
        camp_count = 0

    result = ent.to_dict()
    result["usage"] = {
        "monthly_conversations": conv_count,
        "campaigns_per_month":   camp_count,
    }

    return {"ok": True, **result}
