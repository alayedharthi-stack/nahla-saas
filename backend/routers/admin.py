"""
routers/admin.py
────────────────
Merchant and tenant management endpoints (admin only).

Routes:
  GET    /admin/merchants                     — list all merchant accounts
  POST   /admin/merchants                     — create merchant + tenant
  POST   /admin/invitations                   — generate invitation link
  PUT    /admin/merchants/{user_id}/toggle    — activate / deactivate merchant
  DELETE /admin/merchants/{user_id}           — delete merchant account
  GET    /tenants/{tenant_id}                 — retrieve a single tenant

All mutating routes require role=admin (enforced by require_admin dependency).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from models import (  # noqa: E402
    AIActionLog,
    BillingInvoice,
    BillingPayment,
    BillingPlan,
    BillingSubscription,
    ConversationLog,
    ConversationTrace,
    Integration,
    Order,
    StoreSyncJob,
    SystemEvent,
    Tenant,
    TenantSettings,
    User,
    WhatsAppConnection,
    WhatsAppUsage,
)

from core.audit import audit
from core.auth import (
    BCRYPT_AVAILABLE,
    JWT_AVAILABLE,
    create_invite_token,
    create_token,
    hash_password,
    require_admin,
)
from core.config import INVITE_EXPIRE_H
from core.database import get_db
from core.tenant import get_or_create_settings
from modules.ai.orchestrator.costing import estimate_call_cost

logger = logging.getLogger("nahla.admin")
router = APIRouter()

_PLATFORM_FEATURES_KEY = "platform_features"
_TENANT_FEATURES_KEY = "tenant_features"
_DEFAULT_PLATFORM_FEATURE_FLAGS = {
    "owner_dashboard": True,
    "tenant_suspend_controls": True,
    "billing_overview": True,
    "ai_usage_insights": True,
    "system_health": True,
    "merchant_troubleshooting": True,
    "support_impersonation": True,
}


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateMerchantIn(BaseModel):
    email:      str
    password:   str
    store_name: str
    phone:      str = ""


class InviteIn(BaseModel):
    email: str  # pre-fill for a specific email, or "" for an open invitation


class UpdateTenantStatusIn(BaseModel):
    is_active: bool


class FeatureFlagUpdateIn(BaseModel):
    enabled: bool = Field(...)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _merchant_row(user: User) -> Dict[str, Any]:
    return {
        "id":         user.id,
        "email":      user.email,
        "role":       user.role,
        "is_active":  user.is_active,
        "tenant_id":  user.tenant_id,
        "store_name": user.tenant.name if user.tenant else "",
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _payment_amount_value(payment: BillingPayment) -> float:
    raw = getattr(payment, "amount_sar", None)
    if raw is None:
        raw = getattr(payment, "amount", 0)
    return float(raw or 0)


def _payment_amount_column():
    return getattr(BillingPayment, "amount_sar", None) or getattr(BillingPayment, "amount")


def _provider_from_model(model_name: str) -> str:
    model = str(model_name or "").lower()
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "gemini"
    if model.startswith("gpt") or model.startswith("o1") or model.startswith("o3"):
        return "openai_compatible"
    return "unknown"


def _estimate_trace_cost(trace: ConversationTrace) -> Dict[str, Any]:
    # ConversationTrace does not persist full prompt size, so use a conservative
    # proxy based on customer message + reply text lengths for owner-level insight.
    message_chars = len(trace.message or "")
    reply_chars = len(trace.response_text or "")
    prompt_proxy_chars = max(message_chars * 6, message_chars + reply_chars)
    provider = _provider_from_model(trace.model_used or "")
    cost = estimate_call_cost(
        provider=provider,
        model=trace.model_used or "unknown",
        prompt_chars=prompt_proxy_chars,
        reply_chars=reply_chars,
    )
    cost["source"] = "conversation_trace_proxy"
    return cost


def _tenant_feature_flags(settings: Optional[TenantSettings]) -> Dict[str, bool]:
    meta = dict(settings.extra_metadata or {}) if settings else {}
    flags = dict(meta.get(_TENANT_FEATURES_KEY) or {})
    return flags


def _platform_feature_flags(settings: Optional[TenantSettings]) -> Dict[str, bool]:
    meta = dict(settings.extra_metadata or {}) if settings else {}
    flags = dict(_DEFAULT_PLATFORM_FEATURE_FLAGS)
    flags.update(meta.get(_PLATFORM_FEATURES_KEY) or {})
    return flags


def _set_settings_flags(settings: TenantSettings, key: str, values: Dict[str, Any]) -> None:
    meta = dict(settings.extra_metadata or {})
    meta[key] = values
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")


def _latest_subscription_for_tenant(db: Session, tenant_id: int) -> Optional[BillingSubscription]:
    return (
        db.query(BillingSubscription)
        .filter(BillingSubscription.tenant_id == tenant_id)
        .order_by(BillingSubscription.started_at.desc(), BillingSubscription.id.desc())
        .first()
    )


def _plan_name(db: Session, plan_id: Optional[int]) -> str:
    if not plan_id:
        return "—"
    plan = db.query(BillingPlan).filter(BillingPlan.id == plan_id).first()
    if not plan:
        return "—"
    return (
        getattr(plan, "name_ar", None)
        or getattr(plan, "name", None)
        or "—"
    )


def _merchant_detail(db: Session, user: User) -> Dict[str, Any]:
    sub = _latest_subscription_for_tenant(db, user.tenant_id)
    wa = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == user.tenant_id
    ).first()
    return {
        "id":           user.id,
        "tenant_id":    user.tenant_id,
        "email":        user.email,
        "store_name":   user.tenant.name if user.tenant else user.username,
        "phone":        getattr(user, "phone", ""),
        "is_active":    user.is_active,
        "plan":         _plan_name(db, sub.plan_id if sub else None),
        "sub_status":   sub.status if sub else "none",
        "wa_status":    wa.status if wa else "not_connected",
        "created_at":   user.created_at.isoformat() if user.created_at else None,
    }


def _salla_integration_summary(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Return the primary Salla integration's diagnostic fields (or empty dict)."""
    integ = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
            Integration.enabled == True,  # noqa: E712
        )
        .order_by(Integration.id.desc())
        .first()
    )
    if not integ:
        integ = (
            db.query(Integration)
            .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
            .order_by(Integration.id.desc())
            .first()
        )
    if not integ:
        return {"external_store_id": None, "integration_id": None, "enabled": None}
    return {
        "integration_id": integ.id,
        "external_store_id": integ.external_store_id,
        "enabled": integ.enabled,
        "provider": integ.provider,
    }


def _tenant_summary_payload(db: Session, tenant: Tenant) -> Dict[str, Any]:
    subscription = _latest_subscription_for_tenant(db, tenant.id)
    wa_conn = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant.id
    ).first()
    order_count = db.query(func.count(Order.id)).filter(Order.tenant_id == tenant.id).scalar() or 0
    conversation_count = db.query(func.count(ConversationLog.id)).filter(
        ConversationLog.tenant_id == tenant.id
    ).scalar() or 0
    revenue_sar = db.query(func.sum(_payment_amount_column())).filter(
        BillingPayment.tenant_id == tenant.id,
        BillingPayment.status == "paid",
    ).scalar() or 0
    return {
        "id": tenant.id,
        "name": tenant.name,
        "domain": tenant.domain,
        "is_active": tenant.is_active,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "subscription": {
            "status": subscription.status if subscription else "none",
            "plan": _plan_name(db, subscription.plan_id if subscription else None),
            "trial_ends_at": subscription.trial_ends_at.isoformat() if subscription and subscription.trial_ends_at else None,
            "ends_at": subscription.ends_at.isoformat() if subscription and subscription.ends_at else None,
        },
        "whatsapp": {
            "status": wa_conn.status if wa_conn else "not_connected",
            "phone_number": wa_conn.phone_number if wa_conn else None,
            "phone_number_id": wa_conn.phone_number_id if wa_conn else None,
            "whatsapp_business_account_id": wa_conn.whatsapp_business_account_id if wa_conn else None,
            "business_display_name": wa_conn.business_display_name if wa_conn else None,
            "sending_enabled": bool(wa_conn.sending_enabled) if wa_conn else False,
            "webhook_verified": bool(wa_conn.webhook_verified) if wa_conn else False,
            "connection_type": wa_conn.connection_type if wa_conn else None,
            "provider": wa_conn.provider if wa_conn else None,
            "connected_at": wa_conn.connected_at.isoformat() if wa_conn and wa_conn.connected_at else None,
            "disconnect_reason": wa_conn.disconnect_reason if wa_conn else None,
            "disconnected_at": wa_conn.disconnected_at.isoformat() if wa_conn and wa_conn.disconnected_at else None,
        },
        "stats": {
            "orders": int(order_count),
            "conversations": int(conversation_count),
            "revenue_sar": float(revenue_sar),
        },
        "integration": _salla_integration_summary(db, tenant.id),
    }


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("/admin/merchants")
async def list_merchants(
    db:     Session          = Depends(get_db),
    _admin: Dict[str, Any]  = Depends(require_admin),
):
    """List all merchant accounts (admin only)."""
    users = (
        db.query(User)
        .filter(User.role == "merchant")
        .order_by(User.created_at.desc())
        .all()
    )
    return {"merchants": [_merchant_row(u) for u in users]}


@router.post("/admin/merchants")
async def create_merchant(
    body:    CreateMerchantIn,
    request: Request,
    db:      Session          = Depends(get_db),
    _admin:  Dict[str, Any]  = Depends(require_admin),
):
    """Create a new merchant account + a dedicated tenant (admin only)."""
    if not BCRYPT_AVAILABLE:
        raise HTTPException(status_code=503, detail="bcrypt not installed")

    email = body.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="البريد الإلكتروني مسجَّل مسبقاً")

    slug = email.split("@")[0]
    tenant = Tenant(
        name=f"{body.store_name} ({slug})",
        domain=f"store-{slug}.nahla.sa",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tenant)
    try:
        db.flush()
    except Exception as exc:
        db.rollback()
        logger.error("create_merchant: tenant flush failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"فشل إنشاء الـ tenant: {exc}")

    user = User(
        username=email,
        email=email,
        password_hash=hash_password(body.password),
        role="merchant",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        tenant_id=tenant.id,
    )
    db.add(user)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("create_merchant: commit failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"فشل حفظ المستخدم: {exc}")
    db.refresh(user)

    audit(
        "merchant_created_by_admin",
        admin=_admin.get("sub"),
        merchant_email=email,
        tenant_id=tenant.id,
        store_name=body.store_name,
    )
    return _merchant_row(user)


@router.post("/admin/invitations")
async def create_invitation(
    body:   InviteIn,
    request: Request,
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Generate an invitation link for a new merchant (admin only).
    Returns a signed token valid for 7 days.
    Set email="" to create an open invitation any email can use.
    """
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    email = body.email.strip().lower()
    token = create_invite_token(email=email)
    invite_url = f"https://app.nahlah.ai/register?invite={token}"
    audit(
        "invitation_created",
        admin=_admin.get("sub"),
        invited_email=email or "(open)",
    )
    return {
        "invite_token":    token,
        "invite_url":      invite_url,
        "invited_email":   email or None,
        "expires_in_hours": INVITE_EXPIRE_H,
    }


@router.put("/admin/merchants/{user_id}/toggle")
async def toggle_merchant(
    user_id: int,
    request: Request,
    db:      Session          = Depends(get_db),
    _admin:  Dict[str, Any]  = Depends(require_admin),
):
    """Activate or deactivate a merchant account (admin only)."""
    user = db.query(User).filter(User.id == user_id, User.role == "merchant").first()
    if not user:
        raise HTTPException(status_code=404, detail="Merchant not found")
    user.is_active = not user.is_active
    db.commit()
    db.refresh(user)
    audit(
        "merchant_toggled",
        admin=_admin.get("sub"),
        merchant_id=user_id,
        is_active=user.is_active,
    )
    return _merchant_row(user)


@router.delete("/admin/merchants/{user_id}")
async def delete_merchant(
    user_id: int,
    request: Request,
    db:      Session          = Depends(get_db),
    _admin:  Dict[str, Any]  = Depends(require_admin),
):
    """Permanently delete a merchant account (admin only)."""
    user = db.query(User).filter(User.id == user_id, User.role == "merchant").first()
    if not user:
        raise HTTPException(status_code=404, detail="Merchant not found")
    audit(
        "merchant_deleted",
        admin=_admin.get("sub"),
        merchant_id=user_id,
        merchant_email=user.email,
        tenant_id=user.tenant_id,
    )
    db.delete(user)
    db.commit()
    return {"deleted": True}


@router.get("/admin/stats")
async def get_platform_stats(
    db:     Session         = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Platform-wide statistics for the owner dashboard."""
    import traceback as _tb  # noqa: PLC0415
    try:
        total_merchants  = db.query(func.count(User.id)).filter(User.role == "merchant").scalar() or 0
        active_merchants = db.query(func.count(User.id)).filter(User.role == "merchant", User.is_active == True).scalar() or 0  # noqa: E712
        total_tenants    = db.query(func.count(Tenant.id)).scalar() or 0

        # Subscriptions by plan
        try:
            active_subs = db.query(func.count(BillingSubscription.id)).filter(
                BillingSubscription.status.in_(["active", "trialing"])
            ).scalar() or 0
            trial_subs = db.query(func.count(BillingSubscription.id)).filter(
                BillingSubscription.status == "trialing"
            ).scalar() or 0
            total_subs = db.query(func.count(BillingSubscription.id)).scalar() or 0
            plans = db.query(BillingPlan).all()
            plan_counts = {}
            for plan in plans:
                cnt = db.query(func.count(BillingSubscription.id)).filter(
                    BillingSubscription.plan_id == plan.id,
                    BillingSubscription.status.in_(["active", "trialing"]),
                ).scalar() or 0
                plan_counts[plan.slug] = {"name_ar": plan.name_ar or plan.name, "count": cnt, "price": float(plan.price_sar)}
        except Exception:
            active_subs = trial_subs = total_subs = 0
            plan_counts = {}

        # Revenue
        try:
            today = date.today()
            total_revenue = db.query(func.sum(_payment_amount_column())).filter(
                BillingPayment.status == "paid"
            ).scalar() or 0
            today_revenue = db.query(func.sum(_payment_amount_column())).filter(
                BillingPayment.status == "paid",
                func.date(BillingPayment.created_at) == today,
            ).scalar() or 0
            mrr = 0.0
            try:
                for sub in db.query(BillingSubscription).filter(
                    BillingSubscription.status.in_(["active", "trialing"])
                ).all():
                    plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
                    if plan:
                        mrr += float(plan.price_sar)
            except Exception:
                mrr = 0.0
            recent_payments = db.query(BillingPayment).order_by(
                BillingPayment.created_at.desc()
            ).limit(10).all()
            payments_list = [
                {
                    "id": p.id, "tenant_id": p.tenant_id,
                    "amount": _payment_amount_value(p), "currency": p.currency,
                    "status": p.status, "gateway": p.gateway,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in recent_payments
            ]
        except Exception:
            total_revenue = today_revenue = mrr = 0.0
            payments_list = []

        # All merchants — use safe builder to avoid lazy-load crashes
        all_merchants = db.query(User).filter(User.role == "merchant").order_by(
            User.created_at.desc()
        ).limit(100).all()

        def _safe_merchant(u: User) -> Dict[str, Any]:
            try:
                return _merchant_detail(db, u)
            except Exception:
                return {
                    "id": u.id, "tenant_id": u.tenant_id, "email": u.email,
                    "store_name": u.username or u.email, "phone": "",
                    "is_active": bool(u.is_active), "plan": "—",
                    "sub_status": "none", "wa_status": "not_connected",
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                }

        # ── SaaS owner metrics ─────────────────────────────────────────────────
        # Paid (non-trial) merchants
        try:
            paid_merchants = db.query(func.count(BillingSubscription.id)).filter(
                BillingSubscription.status == "active"
            ).scalar() or 0
        except Exception:
            paid_merchants = 0

        # Suspended merchants
        try:
            suspended_merchants = db.query(func.count(User.id)).filter(
                User.role == "merchant", User.is_active == False  # noqa: E712
            ).scalar() or 0
        except Exception:
            suspended_merchants = 0

        # New signups this week
        try:
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            new_this_week = db.query(func.count(Tenant.id)).filter(
                Tenant.created_at >= week_ago
            ).scalar() or 0
        except Exception:
            new_this_week = 0

        # Onboarding funnel — based on all merchant tenants
        try:
            all_tenant_ids = [
                row[0] for row in db.query(User.tenant_id).filter(
                    User.role == "merchant", User.tenant_id.isnot(None)
                ).all()
            ]
            # Tenants with active Salla integration
            salla_tenants = set(
                row[0] for row in db.query(Integration.tenant_id).filter(
                    Integration.provider == "salla",
                    Integration.enabled == True,  # noqa: E712
                ).all()
            )
            # Tenants with connected WhatsApp
            wa_tenants = set(
                row[0] for row in db.query(WhatsAppConnection.tenant_id).filter(
                    WhatsAppConnection.status == "connected"
                ).all()
            )
            both_connected   = len(salla_tenants & wa_tenants)
            salla_only       = len(salla_tenants - wa_tenants)
            whatsapp_only    = len(wa_tenants - salla_tenants)
            registered_only  = max(0, len(all_tenant_ids) - len(salla_tenants | wa_tenants))
            wa_connected_count = len(wa_tenants)
        except Exception:
            both_connected = salla_only = whatsapp_only = registered_only = wa_connected_count = 0

        # At-risk: Salla needs_reauth
        try:
            salla_integrations_all = db.query(Integration).filter(
                Integration.provider == "salla"
            ).all()
            salla_needs_reauth = sum(
                1 for intg in salla_integrations_all
                if (intg.config or {}).get("needs_reauth")
            )
        except Exception:
            salla_needs_reauth = 0

        # At-risk: trials expiring within 7 days
        try:
            now_utc   = datetime.now(timezone.utc)
            in_7_days = now_utc + timedelta(days=7)
            trials_expiring_7d = db.query(func.count(BillingSubscription.id)).filter(
                BillingSubscription.status == "trialing",
                BillingSubscription.ends_at <= in_7_days,
                BillingSubscription.ends_at >= now_utc,
            ).scalar() or 0
        except Exception:
            trials_expiring_7d = 0

        return {
            "merchants":       {"total": total_merchants, "active": active_merchants, "trial": trial_subs,
                                "paid": paid_merchants, "suspended": suspended_merchants},
            "tenants":         {"total": total_tenants},
            "subscriptions":   {"active": active_subs, "trial": trial_subs, "total": total_subs, "by_plan": plan_counts},
            "revenue":         {"total_sar": float(total_revenue), "today_sar": float(today_revenue), "mrr_sar": mrr},
            "recent_payments": payments_list,
            "recent_merchants": [_safe_merchant(u) for u in all_merchants[:8]],
            "all_merchants":    [_safe_merchant(u) for u in all_merchants],
            # ── SaaS owner fields ──────────────────────────────────────────────
            "new_this_week":   new_this_week,
            "wa_connected":    wa_connected_count,
            "onboarding": {
                "registered_only": registered_only,
                "salla_only":      salla_only,
                "whatsapp_only":   whatsapp_only,
                "both_connected":  both_connected,
            },
            "at_risk": {
                "trials_expiring_7d": trials_expiring_7d,
                "salla_needs_reauth": salla_needs_reauth,
                "suspended":          suspended_merchants,
            },
        }
    except Exception as exc:
        logger.error("[admin/stats] unhandled error: %s\n%s", exc, _tb.format_exc())
        from fastapi.responses import JSONResponse as _J  # noqa: PLC0415
        return _J(status_code=500, content={"detail": "خطأ في تحميل إحصائيات المنصة", "error": str(exc)})


@router.post("/admin/merchants/{user_id}/impersonate")
async def impersonate_merchant(
    user_id: int,
    request: Request,
    db:      Session          = Depends(get_db),
    _admin:  Dict[str, Any]  = Depends(require_admin),
):
    """
    DEPRECATED — This endpoint is disabled for security reasons.
    Use POST /admin/impersonate/{tenant_id} (support_access.py) which enforces
    merchant consent, impersonation flags, sensitive-path blocking, and session revocation.
    """
    audit(
        "admin_impersonate_merchant_blocked",
        admin=_admin.get("sub"),
        merchant_id=user_id,
        reason="legacy_endpoint_disabled",
    )
    raise HTTPException(
        status_code=403,
        detail=(
            "هذا المسار معطّل لأسباب أمنية. "
            "استخدم POST /admin/impersonate/{tenant_id} بعد الحصول على موافقة التاجر."
        ),
    )


@router.get("/admin/tenants/{tenant_id}")
@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: int,
    request:   Request,
    db:        Session         = Depends(get_db),
    _caller:   Dict[str, Any] = Depends(require_admin),
):
    """Retrieve a single tenant by its numeric ID. Admin-only."""
    tenant = db.query(Tenant).filter(
        Tenant.id == tenant_id,
    ).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _tenant_summary_payload(db, tenant)


# ── Manual Salla store linking ────────────────────────────────────────────────

class LinkSallaStoreRequest(BaseModel):
    tenant_id: int
    salla_store_id: str


@router.post("/admin/link-salla-store")
async def link_salla_store(
    body: LinkSallaStoreRequest,
    db: Session = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """
    Manually link a Salla store_id to an existing tenant.
    Used when OAuth could not complete (e.g. private app restrictions).
    Idempotent: re-running updates the existing Integration record.
    """
    tenant = db.query(Tenant).filter(Tenant.id == body.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    existing = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == body.tenant_id,
            Integration.provider == "salla",
        )
        .first()
    )

    if existing:
        cfg = dict(existing.config or {})
        cfg["store_id"] = body.salla_store_id
        existing.config = cfg
        existing.external_store_id = body.salla_store_id
        existing.enabled = True
        db.commit()
        return {"status": "updated", "tenant_id": body.tenant_id, "salla_store_id": body.salla_store_id}

    new_integ = Integration(
        provider="salla",
        tenant_id=body.tenant_id,
        external_store_id=body.salla_store_id,
        enabled=True,
        config={"store_id": body.salla_store_id},
    )
    db.add(new_integ)
    db.commit()
    return {"status": "created", "tenant_id": body.tenant_id, "salla_store_id": body.salla_store_id}


def _compute_visibility_tag(summary: Dict[str, Any]) -> Optional[str]:
    """
    Classify a tenant for the default-view filter.

    Returns one of: 'archived' | 'disconnected' | 'test' | 'pending_payment'
    Returns None when the tenant should be visible by default (active & relevant).

    Priority (first match wins):
      1. archived       — tenant is disabled (is_active=False)
      2. pending_payment — subscription is past_due / canceled / incomplete
      3. disconnected   — WA explicitly disconnected + no active store integration
      4. test           — no integration, no WA connection, zero activity
    """
    if not summary.get("is_active", True):
        return "archived"

    sub_status = (summary.get("subscription") or {}).get("status", "")
    if sub_status in ("past_due", "canceled", "incomplete"):
        return "pending_payment"

    wa_status = (summary.get("whatsapp") or {}).get("status", "not_connected")
    integ = summary.get("integration") or {}
    has_active_integration = bool(integ.get("external_store_id")) and integ.get("enabled") is not False

    if wa_status == "disconnected" and not has_active_integration:
        return "disconnected"

    stats = summary.get("stats") or {}
    no_activity = (
        stats.get("orders", 0) == 0
        and stats.get("conversations", 0) == 0
        and stats.get("revenue_sar", 0.0) == 0.0
    )
    if not has_active_integration and wa_status in ("not_connected", "") and no_activity:
        return "test"

    return None


@router.get("/admin/tenants")
async def list_tenants(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
    search: str = "",
    status: str = Query("", pattern="^(|active|inactive)$"),
    show_all: bool = False,
    limit: int = 50,
    offset: int = 0,
):
    """
    List tenants for the owner admin panel.

    Default (show_all=false): returns only active, relevant tenants —
    hides archived, test, disconnected, and pending-payment stores.

    show_all=true: returns every tenant record with its visibility_tag badge.
    """
    query = db.query(Tenant)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter((Tenant.name.ilike(like)) | (Tenant.domain.ilike(like)))
    if status == "active":
        query = query.filter(Tenant.is_active.is_(True))
    elif status == "inactive":
        query = query.filter(Tenant.is_active.is_(False))

    # Fetch all matching rows (we filter by visibility in Python after computing summaries)
    rows = (
        query.order_by(Tenant.created_at.desc(), Tenant.id.desc())
        .limit(2000)   # safety cap — owners rarely have more than a few hundred
        .all()
    )

    def _safe_summary(t: Tenant) -> Optional[Dict[str, Any]]:
        try:
            s = _tenant_summary_payload(db, t)
        except Exception as _e:
            logger.warning("[admin/tenants] summary failed for tenant %s: %s", t.id, _e)
            s = {
                "id": t.id, "name": t.name or f"tenant-{t.id}", "domain": t.domain,
                "is_active": bool(t.is_active),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "subscription": {"status": "none", "plan": "—", "trial_ends_at": None, "ends_at": None},
                "whatsapp": {
                    "status": "not_connected", "phone_number": None,
                    "phone_number_id": None, "whatsapp_business_account_id": None,
                    "business_display_name": None, "sending_enabled": False,
                    "webhook_verified": False, "connection_type": None,
                    "provider": None, "connected_at": None,
                    "disconnect_reason": None, "disconnected_at": None,
                },
                "stats":       {"orders": 0, "conversations": 0, "revenue_sar": 0.0},
                "integration": {"integration_id": None, "external_store_id": None, "enabled": None, "provider": None},
            }
        s["visibility_tag"] = _compute_visibility_tag(s)
        return s

    all_summaries = [s for t in rows if (s := _safe_summary(t)) is not None]
    total_hidden  = sum(1 for s in all_summaries if s["visibility_tag"] is not None)
    total_active  = len(all_summaries) - total_hidden

    visible = all_summaries if show_all else [s for s in all_summaries if s["visibility_tag"] is None]

    # Apply pagination AFTER visibility filter
    page = visible[offset : offset + min(limit, 200)]

    return {
        "total":        len(visible),
        "total_active": total_active,
        "total_hidden": total_hidden,
        "offset":       offset,
        "limit":        limit,
        "tenants":      page,
    }


@router.patch("/admin/tenants/{tenant_id}/status")
async def update_tenant_status(
    tenant_id: int,
    body: UpdateTenantStatusIn,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant.is_active = body.is_active
    users = db.query(User).filter(User.tenant_id == tenant_id).all()
    for user in users:
        if user.role == "merchant":
            user.is_active = body.is_active
    db.commit()
    audit(
        "tenant_status_updated",
        admin=admin.get("sub"),
        tenant_id=tenant_id,
        is_active=body.is_active,
    )
    return _tenant_summary_payload(db, tenant)


@router.get("/admin/tenants/{tenant_id}/summary")
async def get_tenant_summary(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _tenant_summary_payload(db, tenant)


@router.get("/admin/tenants/{tenant_id}/users")
async def get_tenant_users(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    users = (
        db.query(User)
        .filter(User.tenant_id == tenant_id)
        .order_by(User.created_at.desc(), User.id.desc())
        .all()
    )
    return {
        "tenant_id": tenant_id,
        "users": [
            {
                "id": user.id,
                "email": user.email,
                "role": user.role,
                "is_active": user.is_active,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            }
            for user in users
        ],
    }


# ─── User ↔ Tenant binding management ─────────────────────────────────────────
# These exist because the previous /auth/login behaviour invented tenant
# assignments on the fly, leaving the platform with multiple users orphaned
# from their real merchant tenant. See backend/routers/auth.py for the
# refusal logic that now blocks unassigned logins.

class _AssignTenantBody(BaseModel):
    tenant_id: int = Field(..., gt=0)
    move_existing_data: bool = Field(
        False,
        description=(
            "If True and the user already has a tenant_id, also reassign "
            "the user row only — does NOT migrate WA conn or messages. "
            "Use the dedicated reassignment tools for data migration."
        ),
    )


@router.get("/admin/users/lookup")
async def admin_users_lookup(
    email: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Look up a user by email (case-insensitive) or numeric id and surface
    their tenant binding plus the tenant's WhatsApp connection summary.
    Used to diagnose "I see/don't see conversations" complaints.
    """
    if not email and not user_id:
        raise HTTPException(status_code=400, detail="Provide email or user_id")

    q = db.query(User)
    if user_id:
        q = q.filter(User.id == user_id)
    elif email:
        q = q.filter(func.lower(User.email) == email.strip().lower())

    user = q.first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tenant = (
        db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
        if user.tenant_id else None
    )
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == user.tenant_id)
        .first()
        if user.tenant_id else None
    )

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "is_active": bool(user.is_active),
            "tenant_id": user.tenant_id,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "tenant": (
            {
                "id": tenant.id,
                "name": tenant.name,
                "is_platform_tenant": bool(getattr(tenant, "is_platform_tenant", False)),
                "is_active": bool(tenant.is_active),
            } if tenant else None
        ),
        "whatsapp_connection": (
            {
                "status": wa_conn.status,
                "phone_number_id": wa_conn.phone_number_id,
                "phone_number": wa_conn.phone_number,
                "business_display_name": wa_conn.business_display_name,
                "webhook_verified": bool(wa_conn.webhook_verified),
                "sending_enabled": bool(wa_conn.sending_enabled),
            } if wa_conn else None
        ),
    }


@router.post("/admin/users/{user_id}/assign-tenant")
async def admin_users_assign_tenant(
    user_id: int,
    body: _AssignTenantBody,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Bind a user account to a specific tenant. Idempotent: setting the same
    tenant is a no-op. Refuses to silently overwrite an existing different
    binding unless `move_existing_data=true` (a brake against accidental
    cross-tenant moves).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tenant = db.query(Tenant).filter(Tenant.id == body.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    previous = user.tenant_id
    if previous == body.tenant_id:
        return {"status": "noop", "user_id": user_id, "tenant_id": body.tenant_id}

    if previous and not body.move_existing_data:
        raise HTTPException(
            status_code=409,
            detail=(
                f"User already bound to tenant_id={previous}. "
                "Pass move_existing_data=true to acknowledge and overwrite."
            ),
        )

    user.tenant_id = body.tenant_id
    db.add(user)
    db.commit()

    audit(
        "admin.user.assign_tenant",
        sub=user.email,
        from_tenant=previous,
        to_tenant=body.tenant_id,
    )
    logger.warning(
        "[admin] assign-tenant user_id=%s email=%s from=%s to=%s",
        user_id, user.email, previous, body.tenant_id,
    )
    return {
        "status": "assigned",
        "user_id": user_id,
        "email": user.email,
        "previous_tenant_id": previous,
        "tenant_id": body.tenant_id,
    }


# ─── WhatsApp permanent token injection ───────────────────────────────────────
# Lets the operator paste a System User permanent token into a tenant's
# WhatsAppConnection so we stop relying on the 60-day OAuth user token that
# embedded signup persists by default.

class _SetWaTokenBody(BaseModel):
    access_token: str = Field(..., min_length=20)
    token_type: str = Field("permanent_system_user", max_length=64)
    note: Optional[str] = Field(None, max_length=500)


@router.post("/admin/whatsapp/{tenant_id}/set-token")
async def admin_whatsapp_set_token(
    tenant_id: int,
    body: _SetWaTokenBody,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Replace the stored Meta access_token for a tenant with an explicitly
    provided one (typically a System User permanent token from Business
    Manager → System Users → Generate New Token, scoped to whatsapp_business_*
    permissions on the WABA).

    Side effects:
      - Sets `token_type` to the provided value (default 'permanent_system_user')
      - Sets `token_expires_at = NULL` (permanent tokens don't expire)
      - Clears any `oauth_session_status=invalid` / `needs_reauth` flags in
        extra_metadata so the dashboard banner disappears.
      - Audited.
    """
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if not wa_conn:
        raise HTTPException(status_code=404, detail="No WhatsAppConnection for tenant")

    old_tail = (wa_conn.access_token or "")[-6:] if wa_conn.access_token else None
    wa_conn.access_token = body.access_token.strip()
    wa_conn.token_type = body.token_type
    wa_conn.token_expires_at = None

    meta = dict(wa_conn.extra_metadata or {})
    meta["token_status"] = "permanent"
    meta["token_health"] = "healthy"
    meta["active_graph_token_source"] = "permanent_system_user"
    if body.note:
        meta["last_token_set_note"] = body.note
    meta["last_token_set_at"] = datetime.now(timezone.utc).isoformat()
    if meta.get("oauth_session_status") in {"expired", "invalid", "missing"}:
        meta["oauth_session_status"] = "replaced_with_permanent"
    meta["oauth_session_needs_reauth"] = False
    wa_conn.extra_metadata = meta
    flag_modified(wa_conn, "extra_metadata")

    db.add(wa_conn)
    db.commit()

    audit(
        "admin.whatsapp.set_token",
        tenant_id=tenant_id,
        from_token_tail=old_tail,
        to_token_tail=body.access_token[-6:],
        token_type=body.token_type,
    )
    logger.warning(
        "[admin] WhatsApp token replaced tenant_id=%s old_tail=%s new_tail=%s type=%s",
        tenant_id, old_tail, body.access_token[-6:], body.token_type,
    )
    return {
        "status": "ok",
        "tenant_id": tenant_id,
        "token_type": body.token_type,
        "token_tail": body.access_token[-6:],
        "previous_token_tail": old_tail,
    }


@router.get("/admin/billing/overview")
async def admin_billing_overview(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    today = date.today()
    total_subscriptions = db.query(func.count(BillingSubscription.id)).scalar() or 0
    active_subscriptions = db.query(func.count(BillingSubscription.id)).filter(
        BillingSubscription.status.in_(["active", "trialing"])
    ).scalar() or 0
    total_revenue = db.query(func.sum(_payment_amount_column())).filter(
        BillingPayment.status == "paid"
    ).scalar() or 0
    today_revenue = db.query(func.sum(_payment_amount_column())).filter(
        BillingPayment.status == "paid",
        func.date(BillingPayment.created_at) == today,
    ).scalar() or 0
    invoices_due = db.query(func.count(BillingInvoice.id)).filter(
        BillingInvoice.status.in_(["open", "overdue", "pending"])
    ).scalar() or 0

    by_plan: Dict[str, Dict[str, Any]] = {}
    for plan in db.query(BillingPlan).order_by(BillingPlan.price_sar.asc()).all():
        by_plan[plan.slug] = {
            "name": plan.name,
            "name_ar": plan.name_ar or plan.name,
            "price_sar": float(plan.price_sar),
            "active_count": db.query(func.count(BillingSubscription.id)).filter(
                BillingSubscription.plan_id == plan.id,
                BillingSubscription.status.in_(["active", "trialing"]),
            ).scalar() or 0,
        }

    return {
        "subscriptions": {
            "total": int(total_subscriptions),
            "active": int(active_subscriptions),
        },
        "revenue": {
            "total_sar": float(total_revenue),
            "today_sar": float(today_revenue),
        },
        "invoices_due": int(invoices_due),
        "by_plan": by_plan,
    }


@router.get("/admin/billing/subscriptions")
async def admin_billing_subscriptions(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
    limit: int = 100,
):
    rows = (
        db.query(BillingSubscription)
        .order_by(BillingSubscription.started_at.desc(), BillingSubscription.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {
        "subscriptions": [
            {
                "id": row.id,
                "tenant_id": row.tenant_id,
                "tenant_name": row.tenant.name if row.tenant else "—",
                "plan": _plan_name(db, row.plan_id),
                "status": row.status,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "trial_ends_at": row.trial_ends_at.isoformat() if row.trial_ends_at else None,
                "ends_at": row.ends_at.isoformat() if row.ends_at else None,
                "auto_renew": bool(row.auto_renew),
            }
            for row in rows
        ]
    }


@router.get("/admin/billing/payments")
async def admin_billing_payments(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
    status: str = "",
    limit: int = 100,
):
    query = db.query(BillingPayment)
    if status:
        query = query.filter(BillingPayment.status == status)
    rows = (
        query.order_by(BillingPayment.created_at.desc(), BillingPayment.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {
        "payments": [
            {
                "id": row.id,
                "tenant_id": row.tenant_id,
                "tenant_name": row.tenant.name if row.tenant else "—",
                "amount_sar": _payment_amount_value(row),
                "currency": row.currency,
                "gateway": row.gateway,
                "status": row.status,
                "paid_at": row.paid_at.isoformat() if row.paid_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.get("/admin/revenue/summary")
async def admin_revenue_summary(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    today = date.today()
    paid_query = db.query(BillingPayment).filter(BillingPayment.status == "paid")
    payments = paid_query.all()
    total_sar = sum(_payment_amount_value(payment) for payment in payments)
    today_sar = sum(
        _payment_amount_value(payment)
        for payment in payments
        if payment.created_at and payment.created_at.date() == today
    )
    active_subs = (
        db.query(BillingSubscription)
        .filter(BillingSubscription.status.in_(["active", "trialing"]))
        .all()
    )
    mrr_sar = 0.0
    for subscription in active_subs:
        plan = db.query(BillingPlan).filter(BillingPlan.id == subscription.plan_id).first()
        if plan:
            mrr_sar += float(plan.price_sar)

    failed_count = db.query(func.count(BillingPayment.id)).filter(BillingPayment.status == "failed").scalar() or 0
    paid_count = len(payments)
    avg_payment = (total_sar / paid_count) if paid_count else 0.0
    return {
        "total_sar": round(total_sar, 2),
        "today_sar": round(today_sar, 2),
        "mrr_sar": round(mrr_sar, 2),
        "paid_count": int(paid_count),
        "failed_count": int(failed_count),
        "avg_payment_sar": round(avg_payment, 2),
    }


@router.get("/admin/revenue/timeseries")
async def admin_revenue_timeseries(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
    days: int = Query(30, ge=7, le=180),
):
    start_date = date.today() - timedelta(days=days - 1)
    rows = (
        db.query(BillingPayment)
        .filter(
            BillingPayment.status == "paid",
            func.date(BillingPayment.created_at) >= start_date,
        )
        .order_by(BillingPayment.created_at.asc(), BillingPayment.id.asc())
        .all()
    )
    totals: Dict[str, float] = {}
    for idx in range(days):
        day = start_date + timedelta(days=idx)
        totals[day.isoformat()] = 0.0
    for row in rows:
        if not row.created_at:
            continue
        key = row.created_at.date().isoformat()
        if key in totals:
            totals[key] += _payment_amount_value(row)
    return {
        "days": days,
        "points": [
            {"date": key, "revenue_sar": round(value, 2)}
            for key, value in totals.items()
        ],
    }


def _tenant_ai_usage_payload(db: Session, tenant_id: int) -> Dict[str, Any]:
    turns = (
        db.query(ConversationTrace)
        .filter(ConversationTrace.tenant_id == tenant_id)
        .order_by(ConversationTrace.created_at.desc(), ConversationTrace.id.desc())
        .all()
    )
    action_count = db.query(func.count(AIActionLog.id)).filter(AIActionLog.tenant_id == tenant_id).scalar() or 0
    models: Dict[str, int] = {}
    providers: Dict[str, int] = {}
    est_cost_usd = 0.0
    est_tokens = 0
    latency_values: List[int] = []

    orchestrated_turns = 0
    for turn in turns:
        if turn.orchestrator_used:
            orchestrated_turns += 1
        model = turn.model_used or "unknown"
        models[model] = models.get(model, 0) + 1
        provider = _provider_from_model(model)
        providers[provider] = providers.get(provider, 0) + 1
        if turn.latency_ms is not None:
            latency_values.append(int(turn.latency_ms))
        cost = _estimate_trace_cost(turn)
        est_cost_usd += float(cost.get("est_cost_usd", 0.0))
        est_tokens += int(cost.get("est_total_tokens", 0))

    avg_latency = round(sum(latency_values) / len(latency_values), 1) if latency_values else 0.0
    return {
        "tenant_id": tenant_id,
        "turns_total": len(turns),
        "turns_orchestrated": orchestrated_turns,
        "ai_actions_logged": int(action_count),
        "avg_latency_ms": avg_latency,
        "estimated_total_tokens": int(est_tokens),
        "estimated_total_cost_usd": round(est_cost_usd, 6),
        "models": [{"model": model, "count": count} for model, count in sorted(models.items(), key=lambda item: item[1], reverse=True)],
        "providers": [{"provider": provider, "count": count} for provider, count in sorted(providers.items(), key=lambda item: item[1], reverse=True)],
    }


@router.get("/admin/ai/usage")
async def admin_ai_usage(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
    limit: int = 100,
):
    tenants = db.query(Tenant).order_by(Tenant.created_at.desc(), Tenant.id.desc()).limit(min(limit, 200)).all()
    rows = []
    for tenant in tenants:
        payload = _tenant_ai_usage_payload(db, tenant.id)
        payload["tenant_name"] = tenant.name
        rows.append(payload)
    return {"tenants": rows}


@router.get("/admin/ai/usage/{tenant_id}")
async def admin_ai_usage_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    payload = _tenant_ai_usage_payload(db, tenant_id)
    payload["tenant_name"] = tenant.name
    return payload


@router.get("/admin/ai/costs")
async def admin_ai_costs(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    tenants = db.query(Tenant).all()
    per_tenant = []
    total_cost = 0.0
    total_tokens = 0
    for tenant in tenants:
        usage = _tenant_ai_usage_payload(db, tenant.id)
        total_cost += usage["estimated_total_cost_usd"]
        total_tokens += usage["estimated_total_tokens"]
        per_tenant.append({
            "tenant_id": tenant.id,
            "tenant_name": tenant.name,
            "estimated_total_cost_usd": usage["estimated_total_cost_usd"],
            "estimated_total_tokens": usage["estimated_total_tokens"],
        })
    per_tenant.sort(key=lambda item: item["estimated_total_cost_usd"], reverse=True)
    return {
        "estimated_total_cost_usd": round(total_cost, 6),
        "estimated_total_tokens": int(total_tokens),
        "tenants": per_tenant[:100],
    }


@router.get("/admin/ai/providers")
async def admin_ai_providers(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    rows = db.query(ConversationTrace).all()
    provider_counts: Dict[str, int] = {}
    model_counts: Dict[str, int] = {}
    for row in rows:
        provider = _provider_from_model(row.model_used or "")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        model = row.model_used or "unknown"
        model_counts[model] = model_counts.get(model, 0) + 1
    return {
        "providers": [{"provider": key, "count": value} for key, value in sorted(provider_counts.items(), key=lambda item: item[1], reverse=True)],
        "models": [{"model": key, "count": value} for key, value in sorted(model_counts.items(), key=lambda item: item[1], reverse=True)],
    }


@router.get("/admin/system/health")
async def admin_system_health(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    from observability.health import (  # noqa: PLC0415
        check_database,
        check_orchestrator,
        overall_status,
    )
    components = {
        "database": await check_database(db),
        "orchestrator": await check_orchestrator(os.environ.get("ORCHESTRATOR_URL", "http://localhost:8016")),
        "whatsapp_connections": {
            "status": "ok",
            "connected": db.query(func.count(WhatsAppConnection.id)).filter(
                WhatsAppConnection.status == "connected",
                WhatsAppConnection.sending_enabled == True,  # noqa: E712
            ).scalar() or 0,
        },
        "support_access": {
            "status": "ok",
            "active_grants": db.query(TenantSettings).count(),
        },
    }
    return {
        "status": overall_status(components),
        "components": components,
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
    }


@router.get("/admin/system/dependencies")
async def admin_system_dependencies(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    return {
        "database": {"configured": True},
        "orchestrator_url": os.environ.get("ORCHESTRATOR_URL", "http://localhost:8016"),
        "connected_whatsapp_tenants": db.query(func.count(WhatsAppConnection.id)).filter(
            WhatsAppConnection.status == "connected",
            WhatsAppConnection.sending_enabled == True,  # noqa: E712
        ).scalar() or 0,
        "salla_integrations_enabled": db.query(func.count(Integration.id)).filter(
            Integration.provider == "salla",
            Integration.enabled == True,  # noqa: E712
        ).scalar() or 0,
        "zid_integrations_enabled": db.query(func.count(Integration.id)).filter(
            Integration.provider == "zid",
            Integration.enabled == True,  # noqa: E712
        ).scalar() or 0,
    }


@router.get("/admin/system/tenant-isolation")
async def admin_tenant_isolation(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    issues: List[str] = []
    merchant_users_missing_tenant = db.query(func.count(User.id)).filter(
        User.role == "merchant",
        User.tenant_id == None,  # noqa: E711
    ).scalar() or 0
    if merchant_users_missing_tenant:
        issues.append(f"{merchant_users_missing_tenant} merchant users are missing tenant_id")

    orphan_whatsapp = db.query(func.count(WhatsAppConnection.id)).filter(
        WhatsAppConnection.tenant_id == None,  # noqa: E711
    ).scalar() or 0
    if orphan_whatsapp:
        issues.append(f"{orphan_whatsapp} WhatsApp connections are missing tenant_id")

    missing_settings = (
        db.query(func.count(Tenant.id))
        .outerjoin(TenantSettings, TenantSettings.tenant_id == Tenant.id)
        .filter(TenantSettings.id == None)  # noqa: E711
        .scalar() or 0
    )
    if missing_settings:
        issues.append(f"{missing_settings} tenants do not yet have tenant_settings")

    return {
        "all_checks_passed": len(issues) == 0,
        "issues": issues,
        "checked_at": datetime.now(timezone.utc).isoformat() + "Z",
    }


@router.get("/admin/system/events")
async def admin_system_events(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
    category: str = "",
    severity: str = "",
    tenant_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
):
    query = db.query(SystemEvent)
    if category:
        query = query.filter(SystemEvent.category == category)
    if severity:
        query = query.filter(SystemEvent.severity == severity)
    if tenant_id is not None:
        query = query.filter(SystemEvent.tenant_id == tenant_id)
    total = query.count()
    rows = (
        query.order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
        .offset(offset)
        .limit(min(limit, 200))
        .all()
    )
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "events": [
            {
                "id": row.id,
                "tenant_id": row.tenant_id,
                "tenant_name": row.tenant.name if row.tenant else "—",
                "category": row.category,
                "event_type": row.event_type,
                "severity": row.severity,
                "summary": row.summary,
                "payload": row.payload,
                "reference_id": row.reference_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.get("/admin/features")
async def admin_get_global_features(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    settings = get_or_create_settings(db, 1)
    db.commit()
    return {"features": _platform_feature_flags(settings)}


@router.put("/admin/features/{feature_key}")
async def admin_update_global_feature(
    feature_key: str,
    body: FeatureFlagUpdateIn,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
):
    settings = get_or_create_settings(db, 1)
    flags = _platform_feature_flags(settings)
    flags[feature_key] = bool(body.enabled)
    _set_settings_flags(settings, _PLATFORM_FEATURES_KEY, flags)
    db.add(settings)
    db.commit()
    audit(
        "platform_feature_updated",
        admin=admin.get("sub"),
        feature_key=feature_key,
        enabled=body.enabled,
    )
    return {"feature_key": feature_key, "enabled": body.enabled, "features": flags}


@router.get("/admin/tenants/{tenant_id}/features")
async def admin_get_tenant_features(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    settings = get_or_create_settings(db, tenant_id)
    db.commit()
    return {
        "tenant_id": tenant_id,
        "features": _tenant_feature_flags(settings),
        "global_defaults": _platform_feature_flags(get_or_create_settings(db, 1)),
    }


@router.put("/admin/tenants/{tenant_id}/features/{feature_key}")
async def admin_update_tenant_feature(
    tenant_id: int,
    feature_key: str,
    body: FeatureFlagUpdateIn,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
):
    settings = get_or_create_settings(db, tenant_id)
    flags = _tenant_feature_flags(settings)
    flags[feature_key] = bool(body.enabled)
    _set_settings_flags(settings, _TENANT_FEATURES_KEY, flags)
    db.add(settings)
    db.commit()
    audit(
        "tenant_feature_updated",
        admin=admin.get("sub"),
        tenant_id=tenant_id,
        feature_key=feature_key,
        enabled=body.enabled,
    )
    return {"tenant_id": tenant_id, "feature_key": feature_key, "enabled": body.enabled, "features": flags}


@router.get("/admin/troubleshooting/tenants/{tenant_id}")
async def admin_troubleshoot_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    summary = _tenant_summary_payload(db, tenant)
    latest_sync = (
        db.query(StoreSyncJob)
        .filter(StoreSyncJob.tenant_id == tenant_id)
        .order_by(StoreSyncJob.created_at.desc(), StoreSyncJob.id.desc())
        .first()
    )
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    support_access = dict((settings.extra_metadata or {}).get("support_access") or {}) if settings else {}
    recent_events = (
        db.query(SystemEvent)
        .filter(SystemEvent.tenant_id == tenant_id)
        .order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
        .limit(10)
        .all()
    )
    return {
        "tenant": summary,
        "support_access": {
            "enabled": bool(support_access.get("enabled")),
            "expires_at": support_access.get("expires_at"),
        },
        "latest_sync": {
            "status": latest_sync.status if latest_sync else "none",
            "sync_type": latest_sync.sync_type if latest_sync else None,
            "created_at": latest_sync.created_at.isoformat() if latest_sync and latest_sync.created_at else None,
            "error_message": latest_sync.error_message if latest_sync else None,
        },
        "recent_events": [
            {
                "id": row.id,
                "category": row.category,
                "event_type": row.event_type,
                "severity": row.severity,
                "summary": row.summary,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in recent_events
        ],
    }


@router.get("/admin/troubleshooting/tenants/{tenant_id}/whatsapp")
async def admin_troubleshoot_whatsapp(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    wa_conn = db.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == tenant_id).first()
    usage_rows = (
        db.query(WhatsAppUsage)
        .filter(WhatsAppUsage.tenant_id == tenant_id)
        .order_by(WhatsAppUsage.year.desc(), WhatsAppUsage.month.desc(), WhatsAppUsage.id.desc())
        .limit(6)
        .all()
    )

    # ── Promote OAuth-session signals to top-level so the admin UI doesn't
    # have to dig into extra_metadata. These were the buried fields that
    # masked the broken merchant OAuth on tenant=1 — see the RCA at
    # docs/runbooks/whatsapp-half-bootstrap-rca.md ─────────────────────────
    extra_meta_dict: dict = wa_conn.extra_metadata if (wa_conn and wa_conn.extra_metadata) else {}
    if not isinstance(extra_meta_dict, dict):
        extra_meta_dict = {}
    oauth_status = extra_meta_dict.get("oauth_session_status")
    needs_reauth_meta = extra_meta_dict.get("oauth_session_needs_reauth")
    if needs_reauth_meta is None:
        needs_reauth_meta = oauth_status in {"expired", "invalid", "missing"}
    half_bootstrapped = bool(
        wa_conn
        and wa_conn.status == "connected"
        and (not wa_conn.phone_number or not wa_conn.business_display_name)
    )
    return {
        "tenant_id": tenant_id,
        "tenant_name": tenant.name,
        "connection": {
            "status": wa_conn.status if wa_conn else "not_connected",
            "phone_number": wa_conn.phone_number if wa_conn else None,
            "business_display_name": wa_conn.business_display_name if wa_conn else None,
            "connection_type": wa_conn.connection_type if wa_conn else None,
            "provider": wa_conn.provider if wa_conn else None,
            "webhook_verified": bool(wa_conn.webhook_verified) if wa_conn else False,
            "sending_enabled": bool(wa_conn.sending_enabled) if wa_conn else False,
            "last_error": wa_conn.last_error if wa_conn else None,
            "extra_metadata": wa_conn.extra_metadata if wa_conn else None,
            "updated_at": wa_conn.updated_at.isoformat() if wa_conn and wa_conn.updated_at else None,
            # Promoted diagnostic signals — flat for easy UI binding.
            "oauth_session_status":        oauth_status,
            "oauth_session_message":       extra_meta_dict.get("oauth_session_message"),
            "oauth_session_needs_reauth":  bool(needs_reauth_meta),
            "active_graph_token_source":   extra_meta_dict.get("active_graph_token_source"),
            "token_status":                extra_meta_dict.get("token_status"),
            "token_health":                extra_meta_dict.get("token_health"),
            "half_bootstrapped":           half_bootstrapped,
        },
        "usage": [
            {
                "year": row.year,
                "month": row.month,
                "service_conversations_used": row.service_conversations_used,
                "marketing_conversations_used": row.marketing_conversations_used,
                "conversations_limit": row.conversations_limit,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in usage_rows
        ],
    }


@router.get("/admin/troubleshooting/whatsapp/lookup")
async def admin_troubleshoot_whatsapp_lookup(
    phone_number_id: Optional[str] = Query(None, description="Meta phone_number_id (recommended)"),
    phone_number: Optional[str] = Query(None, description="E.164 number, optional"),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Diagnostic: locate every WhatsAppConnection that claims a given
    phone_number_id (or display number), so we can detect the case where
    a webhook is being routed to a *different* tenant than the merchant
    expects (typical symptom: dashboard conversations look empty even
    though logs show successful inbound + reply on tenant=X).

    Read-only; admin-only. Safe to call in production.
    """
    if not phone_number_id and not phone_number:
        raise HTTPException(
            status_code=400,
            detail="Provide phone_number_id and/or phone_number",
        )

    q = db.query(WhatsAppConnection)
    if phone_number_id:
        q = q.filter(WhatsAppConnection.phone_number_id == phone_number_id)
    elif phone_number:
        q = q.filter(WhatsAppConnection.phone_number == phone_number)

    rows = q.all()

    out: List[Dict[str, Any]] = []
    for wa in rows:
        tenant = db.query(Tenant).filter(Tenant.id == wa.tenant_id).first()
        from models import Conversation, MessageEvent  # noqa: PLC0415

        convo_count = (
            db.query(func.count(Conversation.id))
            .filter(Conversation.tenant_id == wa.tenant_id)
            .scalar()
            or 0
        )
        msg_count = (
            db.query(func.count(MessageEvent.id))
            .filter(MessageEvent.tenant_id == wa.tenant_id)
            .scalar()
            or 0
        )
        # Dashboard /conversations endpoint reads from ConversationTrace,
        # not MessageEvent — if this is 0 the inbox will look empty even
        # if MessageEvent has rows. Critical signal for routing RCAs.
        trace_count = 0
        last_trace_at = None
        last_event_at = None
        try:
            trace_count = (
                db.query(func.count(ConversationTrace.id))
                .filter(ConversationTrace.tenant_id == wa.tenant_id)
                .scalar()
                or 0
            )
            last_trace = (
                db.query(ConversationTrace)
                .filter(ConversationTrace.tenant_id == wa.tenant_id)
                .order_by(ConversationTrace.created_at.desc())
                .first()
            )
            if last_trace and last_trace.created_at:
                last_trace_at = last_trace.created_at.isoformat()
        except Exception:
            pass
        try:
            last_event = (
                db.query(MessageEvent)
                .filter(MessageEvent.tenant_id == wa.tenant_id)
                .order_by(MessageEvent.created_at.desc())
                .first()
            )
            if last_event and last_event.created_at:
                last_event_at = last_event.created_at.isoformat()
        except Exception:
            pass
        token_tail = (wa.access_token or "")[-6:] if wa.access_token else None
        out.append({
            "tenant_id": wa.tenant_id,
            "tenant_name": getattr(tenant, "name", None),
            "is_platform_tenant": bool(getattr(tenant, "is_platform_tenant", False)),
            "wa_status": wa.status,
            "phone_number_id": wa.phone_number_id,
            "phone_number": wa.phone_number,
            "business_display_name": wa.business_display_name,
            "whatsapp_business_account_id": wa.whatsapp_business_account_id,
            "connection_type": wa.connection_type,
            "provider": wa.provider,
            "webhook_verified": bool(wa.webhook_verified),
            "sending_enabled": bool(wa.sending_enabled),
            "token_tail": token_tail,
            "token_expires_at": wa.token_expires_at.isoformat() if wa.token_expires_at else None,
            "last_webhook_received_at": (
                wa.last_webhook_received_at.isoformat()
                if wa.last_webhook_received_at else None
            ),
            "updated_at": wa.updated_at.isoformat() if wa.updated_at else None,
            "conversations_in_this_tenant": convo_count,
            "message_events_in_this_tenant": msg_count,
            "conversation_traces_in_this_tenant": trace_count,
            "last_message_event_at": last_event_at,
            "last_conversation_trace_at": last_trace_at,
        })

    return {
        "query": {"phone_number_id": phone_number_id, "phone_number": phone_number},
        "match_count": len(out),
        "matches": out,
    }


@router.get("/admin/troubleshooting/tenants/{tenant_id}/integrations")
async def admin_troubleshoot_integrations(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    integrations = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id)
        .order_by(Integration.provider.asc(), Integration.id.desc())
        .all()
    )
    sync_jobs = (
        db.query(StoreSyncJob)
        .filter(StoreSyncJob.tenant_id == tenant_id)
        .order_by(StoreSyncJob.created_at.desc(), StoreSyncJob.id.desc())
        .limit(10)
        .all()
    )
    return {
        "tenant_id": tenant_id,
        "tenant_name": tenant.name,
        "integrations": [
            {
                "id": row.id,
                "provider": row.provider,
                "enabled": bool(row.enabled),
                "config": row.config,
            }
            for row in integrations
        ],
        "sync_jobs": [
            {
                "id": row.id,
                "status": row.status,
                "sync_type": row.sync_type,
                "triggered_by": row.triggered_by,
                "products_synced": row.products_synced,
                "orders_synced": row.orders_synced,
                "coupons_synced": row.coupons_synced,
                "error_message": row.error_message,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in sync_jobs
        ],
    }


# ── Admin: force-connect WhatsApp with raw Meta credentials ─────────────────

class AdminWhatsAppForceConnectIn(BaseModel):
    tenant_id:        int
    phone_number_id:  str
    access_token:     str
    waba_id:          str
    phone_number:     Optional[str] = None
    display_name:     Optional[str] = None


@router.post("/admin/whatsapp/force-connect")
async def admin_force_connect_whatsapp(
    body: AdminWhatsAppForceConnectIn,
    db:     Session         = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Directly connect a WhatsApp number for any tenant by writing raw Meta
    credentials (Phone Number ID, Access Token, WABA ID) — no OTP flow needed.
    Intended for platform admins adding test numbers or manually onboarding merchants.
    """
    tenant = db.query(Tenant).filter(Tenant.id == body.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # ── Canonical write through service (integrity + webhook surfaced) ──────────
    from services.whatsapp_connection_service import (  # noqa: PLC0415
        commit_connection,
        WhatsAppConnectionConflict,
        WhatsAppConnectionError,
    )
    try:
        result = commit_connection(
            db,
            tenant_id       = body.tenant_id,
            phone_number_id = body.phone_number_id.strip(),
            waba_id         = body.waba_id.strip(),
            access_token    = body.access_token.strip(),
            connection_type = "cloud_api",
            phone_number    = (body.phone_number or "").strip(),
            display_name    = (body.display_name or "").strip(),
            actor           = "admin_force_connect",
        )
    except WhatsAppConnectionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WhatsAppConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result.to_api_dict()


# ── Coexistence (360dialog) request management ──────────────────────────────

@router.post("/admin/whatsapp/disconnect/{tenant_id}")
async def admin_force_disconnect_whatsapp(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Force-disconnect WhatsApp for any tenant (admin only).
    Wipes WABA_ID, PHONE_NUMBER_ID, ACCESS_TOKEN and sets status to disconnected.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    conn = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant_id
    ).first()

    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد اتصال واتساب لهذا التاجر")

    now           = datetime.now(timezone.utc)
    actor_user_id = _admin.get("user_id")

    conn.status                       = "disconnected"
    conn.whatsapp_business_account_id = None
    conn.phone_number_id              = None
    conn.access_token                 = None
    conn.token_type                   = None
    conn.token_expires_at             = None
    conn.sending_enabled              = False
    conn.webhook_verified             = False
    conn.last_error                   = None
    conn.updated_at                   = now
    conn.disconnect_reason            = "admin_forced_disconnect"
    conn.disconnected_at              = now
    conn.disconnected_by_user_id      = actor_user_id

    db.commit()
    audit(
        "admin_force_disconnect_whatsapp",
        admin=_admin.get("sub"),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
    )
    return {"ok": True, "tenant_id": tenant_id, "status": "disconnected"}


@router.post("/admin/whatsapp/resubscribe-webhook/{tenant_id}")
async def admin_resubscribe_webhook(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Re-subscribe the tenant's WABA to Meta webhooks using their stored access_token.
    Useful when manual-connect was done but webhook is not receiving messages.
    """
    conn = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant_id
    ).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد اتصال واتساب")
    if not conn.phone_number_id and not conn.whatsapp_business_account_id:
        raise HTTPException(
            status_code=400,
            detail="Phone Number ID و WABA ID غير متوفرين — أعد الربط اليدوي أولاً",
        )
    if not conn.access_token:
        raise HTTPException(status_code=400, detail="Access Token غير متوفر — أعد الربط اليدوي أولاً")

    from services.whatsapp_connection_service import subscribe_phone_webhook  # noqa: PLC0415

    waba_id  = conn.whatsapp_business_account_id
    phone_id = conn.phone_number_id
    token    = conn.access_token

    success, err = subscribe_phone_webhook(
        phone_id or "", token, tenant_id, waba_id=waba_id,
    )
    if success:
        conn.webhook_verified = True
        conn.updated_at       = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "[admin] webhook resubscribed tenant=%s phone=%s waba=%s",
            tenant_id, phone_id, waba_id,
        )
    return {
        "ok":              success,
        "tenant_id":       tenant_id,
        "phone_number_id": phone_id,
        "waba_id":         waba_id,
        "error":           err,
        "note": (
            "تم الاشتراك بنجاح — يجب أن تصل الرسائل الآن." if success
            else "فشل الاشتراك — تحقق من صلاحية الـ Access Token وأنه يملك صلاحية whatsapp_business_messaging"
        ),
    }


@router.delete("/admin/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Permanently delete a tenant and ALL related data (admin only).
    Removes: users, integrations, whatsapp_connections, sync_jobs, billing records.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    audit(
        "tenant_deleted_by_admin",
        admin=_admin.get("sub"),
        tenant_id=tenant_id,
        tenant_name=tenant.name,
    )

    # Delete child records that have FK to tenant
    from sqlalchemy import text  # noqa: PLC0415
    tables_with_tenant_fk = [
        "whatsapp_connections",
        "whatsapp_usage",
        "integrations",
        "store_sync_jobs",
        "store_knowledge_snapshots",
        "billing_subscriptions",
        "billing_invoices",
        "billing_payments",
        "conversation_logs",
        "conversation_traces",
        "ai_action_logs",
        "system_events",
        "orders",
        "products",
        "customers",
        "coupon_codes",
        "tenant_settings",
        "merchant_addons",
        "merchant_widgets",
        "users",
    ]
    for table in tables_with_tenant_fk:
        try:
            db.execute(text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": tenant_id})
        except Exception as _e:
            logger.warning("delete_tenant: could not delete from %s: %s", table, _e)

    db.delete(tenant)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("delete_tenant: commit failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"فشل حذف المتجر: {exc}")

    return {"deleted": True, "tenant_id": tenant_id}


# ── Admin Tools: Duplicate Detection & Cleanup ───────────────────────────────

@router.get("/admin/tools/duplicate-salla-stores")
async def list_duplicate_salla_stores(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Detect Salla stores where multiple tenants share the same store_id.
    Returns groups of duplicates sorted by store_id.
    """
    salla_integrations = (
        db.query(Integration)
        .filter(Integration.provider == "salla")
        .order_by(Integration.id.asc())
        .all()
    )

    groups: Dict[str, list] = {}
    for intg in salla_integrations:
        store_id = (intg.config or {}).get("store_id", "")
        if not store_id:
            continue
        if store_id not in groups:
            groups[store_id] = []

        tenant = db.query(Tenant).filter(Tenant.id == intg.tenant_id).first()
        wa = db.query(WhatsAppConnection).filter(
            WhatsAppConnection.tenant_id == intg.tenant_id
        ).first()
        users = db.query(User).filter(User.tenant_id == intg.tenant_id).all()

        groups[store_id].append({
            "integration_id": intg.id,
            "tenant_id":      intg.tenant_id,
            "tenant_name":    tenant.name if tenant else "—",
            "is_active":      bool(tenant.is_active) if tenant else False,
            "store_id":       store_id,
            "store_name":     (intg.config or {}).get("store_name", ""),
            "enabled":        bool(intg.enabled),
            "created_at":     tenant.created_at.isoformat() if tenant and tenant.created_at else None,
            "wa_status":      wa.status if wa else "not_connected",
            "wa_connected":   bool(wa and wa.status == "connected" and wa.sending_enabled),
            "user_count":     len(users),
            "user_emails":    [u.email for u in users],
        })

    duplicates = [
        {"store_id": sid, "count": len(entries), "entries": entries}
        for sid, entries in groups.items()
        if len(entries) > 1
    ]
    duplicates.sort(key=lambda g: g["count"], reverse=True)

    return {
        "total_duplicate_groups": len(duplicates),
        "total_extra_tenants": sum(g["count"] - 1 for g in duplicates),
        "groups": duplicates,
    }


class FixDuplicatesIn(BaseModel):
    store_id:    str
    keep_tenant_id: Optional[int] = None  # If None, keeps the one with highest ID (newest)
    dry_run:     bool = True


@router.post("/admin/tools/fix-duplicates")
async def fix_duplicate_salla_stores(
    body: FixDuplicatesIn,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Merge duplicate tenants for a given Salla store_id.
    Keeps the specified tenant (or the newest by ID) and deletes the rest.
    Set dry_run=false to actually apply changes.
    """
    salla_integrations = (
        db.query(Integration)
        .filter(
            Integration.provider == "salla",
            Integration.external_store_id == body.store_id,
        )
        .order_by(Integration.tenant_id.desc())
        .all()
    )

    if len(salla_integrations) <= 1:
        return {"message": "لا يوجد تكرار لهذا المتجر", "store_id": body.store_id, "count": len(salla_integrations)}

    # Determine which tenant to keep
    if body.keep_tenant_id:
        keep_tenant_id = body.keep_tenant_id
    else:
        # Keep the one with the highest tenant_id (most recently created)
        keep_tenant_id = max(intg.tenant_id for intg in salla_integrations)

    to_delete = [intg.tenant_id for intg in salla_integrations if intg.tenant_id != keep_tenant_id]

    preview = {
        "store_id":       body.store_id,
        "keep_tenant_id": keep_tenant_id,
        "delete_tenant_ids": to_delete,
        "dry_run":        body.dry_run,
    }

    if body.dry_run:
        return {**preview, "status": "dry_run — no changes made"}

    from sqlalchemy import text  # noqa: PLC0415
    tables_with_tenant_fk = [
        "whatsapp_connections",
        "whatsapp_usage",
        "integrations",
        "store_sync_jobs",
        "store_knowledge_snapshots",
        "billing_subscriptions",
        "billing_invoices",
        "billing_payments",
        "conversation_logs",
        "conversation_traces",
        "ai_action_logs",
        "system_events",
        "orders",
        "products",
        "customers",
        "coupon_codes",
        "tenant_settings",
        "merchant_addons",
        "merchant_widgets",
        "users",
    ]

    deleted_tenant_ids = []
    for tid in to_delete:
        tenant = db.query(Tenant).filter(Tenant.id == tid).first()
        if not tenant:
            continue
        audit(
            "duplicate_tenant_deleted",
            admin=_admin.get("sub"),
            store_id=body.store_id,
            deleted_tenant_id=tid,
            kept_tenant_id=keep_tenant_id,
        )
        for table in tables_with_tenant_fk:
            try:
                db.execute(text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": tid})
            except Exception as _e:
                logger.warning("fix_duplicates: could not delete from %s tenant=%s: %s", table, tid, _e)
        db.delete(tenant)
        deleted_tenant_ids.append(tid)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("fix_duplicates: commit failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"فشل تنظيف التكرارات: {exc}")

    return {
        **preview,
        "status":              "done",
        "deleted_tenant_ids":  deleted_tenant_ids,
    }


@router.get("/admin/coexistence/requests")
async def admin_list_coexistence_requests(
    status_filter: str = "request_submitted",
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    List all WhatsApp coexistence (360dialog) requests.
    By default returns requests with status='request_submitted'.
    Pass ?status_filter=all to get every dialog360 connection.
    """
    import traceback as _tb  # noqa: PLC0415
    try:
        # Match both provider values to handle any legacy data
        query = db.query(WhatsAppConnection).filter(
            WhatsAppConnection.connection_type == "coexistence"
        )
        if status_filter != "all":
            query = query.filter(WhatsAppConnection.status == status_filter)
        connections = query.order_by(WhatsAppConnection.last_attempt_at.desc().nullslast()).all()

        rows = []
        for conn in connections:
            try:
                tenant = db.query(Tenant).filter(Tenant.id == conn.tenant_id).first()
                user = (
                    db.query(User)
                    .filter(User.tenant_id == conn.tenant_id)
                    .order_by(User.id.asc())
                    .first()
                )
                coex_meta = dict((conn.extra_metadata or {}).get("coexistence") or {})
                request_data = dict(coex_meta.get("request") or {})
                rows.append({
                    "tenant_id":          conn.tenant_id,
                    "tenant_name":        tenant.name if tenant else None,
                    "merchant_email":     user.email if user else None,
                    "merchant_phone":     getattr(user, "phone", None) if user else None,
                    "wa_status":          conn.status,
                    "connection_type":    conn.connection_type,
                    "provider":           conn.provider,
                    "requested_phone":    request_data.get("phone_number") or conn.phone_number,
                    "display_name":       request_data.get("display_name") or conn.business_display_name,
                    "notes":              request_data.get("notes"),
                    "submitted_at":       request_data.get("submitted_at"),
                    "has_whatsapp_business_app": request_data.get("has_whatsapp_business_app"),
                    "phone_number_id":    conn.phone_number_id,
                    "waba_id":            conn.whatsapp_business_account_id,
                    "last_attempt_at":    conn.last_attempt_at.isoformat() if conn.last_attempt_at else None,
                    "last_error":         conn.last_error,
                    "sending_enabled":    bool(conn.sending_enabled),
                    "webhook_verified":   bool(conn.webhook_verified),
                    "connected_at":       conn.connected_at.isoformat() if conn.connected_at else None,
                })
            except Exception as row_exc:
                logger.warning("[admin/coexistence] row error tenant=%s: %s", getattr(conn, "tenant_id", "?"), row_exc)
                continue

        return {"requests": rows, "total": len(rows)}
    except Exception as exc:
        logger.error("[admin/coexistence/requests] unhandled error: %s\n%s", exc, _tb.format_exc())
        from fastapi.responses import JSONResponse as _J  # noqa: PLC0415
        return _J(status_code=500, content={"detail": "خطأ في تحميل طلبات الربط", "error": str(exc)})


# ── Admin: trigger coupon pool generation immediately ────────────────────────

@router.post("/admin/coupons/generate-pool")
async def admin_trigger_coupon_pool(
    tenant_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Immediately trigger coupon pool generation for one or all tenants.
    Useful after fixing coupon creation bugs to replenish pools without
    waiting for the 6-hour scheduler cycle.
    """
    from models import Integration  # noqa: PLC0415
    from services.coupon_generator import CouponGeneratorService  # noqa: PLC0415

    query = db.query(Integration).filter(
        Integration.provider == "salla",
        Integration.enabled == True,  # noqa: E712
    )
    if tenant_id is not None:
        query = query.filter(Integration.tenant_id == tenant_id)

    integrations = query.all()
    if not integrations:
        return {"ok": False, "detail": "لا توجد تكاملات سلة مفعّلة"}

    results = {}
    for intg in integrations:
        tid = intg.tenant_id
        try:
            svc = CouponGeneratorService(db, tid)
            created = await svc.ensure_coupon_pool()
            results[tid] = {"created": created, "total": sum(created.values())}
            logger.info("[admin/coupons/generate-pool] tenant=%s created=%s", tid, created)
        except Exception as exc:
            results[tid] = {"error": str(exc)}
            logger.error("[admin/coupons/generate-pool] tenant=%s error: %s", tid, exc)

    return {"ok": True, "results": results}


# ── Tenant Integrity: audit, reconcile, events ───────────────────────────────

@router.get("/admin/tenant-integrity")
async def admin_tenant_integrity_audit(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Full tenant integrity audit.
    Returns per-tenant health, duplicate detection lists, and orphan detection.
    Run this after every deployment or any time routing seems wrong.
    """
    from core.tenant_integrity import run_integrity_audit  # noqa: PLC0415
    return run_integrity_audit(db)


class ReconcileIn(BaseModel):
    source_tenant_id: int
    target_tenant_id: int
    dry_run: bool = True


@router.post("/admin/tenant-integrity/reconcile")
async def admin_reconcile_tenants(
    body: ReconcileIn,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Merge source_tenant into target_tenant.
    Always run with dry_run=true first to review the plan.
    Set dry_run=false to execute the merge.
    """
    from core.tenant_integrity import reconcile_tenants  # noqa: PLC0415
    actor = f"admin:{_admin.get('sub', 'unknown')}"
    result = reconcile_tenants(
        db=db,
        source_tenant_id=body.source_tenant_id,
        target_tenant_id=body.target_tenant_id,
        dry_run=body.dry_run,
        actor=actor,
    )
    return result


@router.get("/admin/tenant-integrity/events")
async def admin_integrity_events(
    tenant_id: Optional[int] = None,
    event: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Recent integrity audit events (newest first)."""
    from database.models import IntegrityEvent  # noqa: PLC0415

    q = db.query(IntegrityEvent)
    if tenant_id is not None:
        q = q.filter(IntegrityEvent.tenant_id == tenant_id)
    if event:
        q = q.filter(IntegrityEvent.event == event)
    entries = q.order_by(IntegrityEvent.created_at.desc()).limit(limit).all()

    return {
        "entries": [
            {
                "id":              e.id,
                "event":           e.event,
                "tenant_id":       e.tenant_id,
                "other_tenant_id": e.other_tenant_id,
                "phone_number_id": e.phone_number_id,
                "waba_id":         e.waba_id,
                "store_id":        e.store_id,
                "provider":        e.provider,
                "action":          e.action,
                "result":          e.result,
                "detail":          e.detail,
                "actor":           e.actor,
                "dry_run":         e.dry_run,
                "created_at":      e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
    }


# ── Webhook Guardian: health overview + manual resubscribe ───────────────────

@router.get("/admin/whatsapp/health")
async def admin_webhook_health(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Return webhook health for every connected WhatsApp tenant.

    Health status values:
        active      – webhook_verified=true AND received event in last 15 min
        warning     – webhook_verified=true BUT last event >15 min ago (or never)
        critical    – webhook_verified=false while status=connected
        disconnected – status != connected
    """
    from datetime import timezone as _tz  # noqa: PLC0415

    conns = db.query(WhatsAppConnection).order_by(WhatsAppConnection.id.asc()).all()
    now   = datetime.now(_tz.utc)

    rows = []
    for conn in conns:
        tenant = db.query(Tenant).filter(Tenant.id == conn.tenant_id).first()

        last_recv = conn.last_webhook_received_at
        if last_recv and last_recv.tzinfo is None:
            last_recv = last_recv.replace(tzinfo=_tz.utc)

        minutes_since_last = None
        if last_recv:
            minutes_since_last = int((now - last_recv).total_seconds() / 60)

        if conn.status != "connected":
            health = "disconnected"
        elif not conn.webhook_verified:
            health = "critical"
        elif last_recv is None or minutes_since_last > 15:
            health = "warning"
        else:
            health = "active"

        rows.append({
            "tenant_id":              conn.tenant_id,
            "tenant_name":            tenant.name if tenant else f"#{conn.tenant_id}",
            "phone_number":           conn.phone_number,
            "phone_number_id":        conn.phone_number_id,
            "waba_id":                conn.whatsapp_business_account_id,
            "status":                 conn.status,
            "webhook_verified":       bool(conn.webhook_verified),
            "sending_enabled":        bool(conn.sending_enabled),
            "health":                 health,
            "last_webhook_received_at": last_recv.isoformat() if last_recv else None,
            "minutes_since_last_event": minutes_since_last,
            "last_error":             conn.last_error,
            "connection_type":        conn.connection_type,
            "provider":               conn.provider,
        })

    summary = {
        "active":       sum(1 for r in rows if r["health"] == "active"),
        "warning":      sum(1 for r in rows if r["health"] == "warning"),
        "critical":     sum(1 for r in rows if r["health"] == "critical"),
        "disconnected": sum(1 for r in rows if r["health"] == "disconnected"),
        "total":        len(rows),
    }

    return {"summary": summary, "connections": rows}


@router.post("/admin/whatsapp/resubscribe-all")
async def admin_resubscribe_all_webhooks(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Force-resubscribe every connected merchant WABA.
    Useful after a major deployment to ensure no merchant lost their webhook.
    """
    from core.webhook_guardian import _subscribe_phone, _guardian_log  # noqa: PLC0415
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
    from datetime import timezone as _tz  # noqa: PLC0415
    from sqlalchemy import or_  # noqa: PLC0415

    conns = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.status == "connected",
        WhatsAppConnection.access_token.isnot(None),
        or_(
            WhatsAppConnection.phone_number_id.isnot(None),
            WhatsAppConnection.whatsapp_business_account_id.isnot(None),
        ),
    ).all()

    results = {}
    for conn in conns:
        try:
            ok = await _subscribe_phone(
                conn.phone_number_id,
                conn.whatsapp_business_account_id,
                conn.access_token,
                META_GRAPH_API_VERSION,
            )
            _guardian_log(
                db, conn.tenant_id, conn.phone_number_id,
                conn.whatsapp_business_account_id,
                "webhook_resubscribed" if ok else "webhook_verification_failed",
                success=ok,
                detail=f"admin_resubscribe_all by {_admin.get('sub', 'admin')}",
            )
            if ok:
                conn.webhook_verified = True
                conn.updated_at = datetime.now(_tz.utc)
            results[conn.tenant_id] = {"ok": ok}
        except Exception as exc:
            results[conn.tenant_id] = {"ok": False, "error": str(exc)}

    db.commit()
    audit(
        "admin_resubscribe_all_webhooks",
        admin=_admin.get("sub"),
        total=len(conns),
        succeeded=sum(1 for v in results.values() if v.get("ok")),
    )
    return {"ok": True, "total": len(conns), "results": results}


@router.get("/admin/whatsapp/guardian-log")
async def admin_guardian_log(
    tenant_id: Optional[int] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Recent webhook guardian audit log entries (newest first)."""
    from database.models import WebhookGuardianLog  # noqa: PLC0415

    q = db.query(WebhookGuardianLog)
    if tenant_id is not None:
        q = q.filter(WebhookGuardianLog.tenant_id == tenant_id)
    entries = q.order_by(WebhookGuardianLog.created_at.desc()).limit(limit).all()

    return {
        "entries": [
            {
                "id":               e.id,
                "tenant_id":        e.tenant_id,
                "phone_number_id":  e.phone_number_id,
                "waba_id":          e.waba_id,
                "event":            e.event,
                "success":          e.success,
                "detail":           e.detail,
                "created_at":       e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
    }


# ── Webhook events (durable queue) ─────────────────────────────────────────────
# Inspect and replay rows in the webhook_events table. This is the admin-facing
# DLQ tool: every inbound Salla/WhatsApp/Moyasar webhook that failed business
# processing lands in status='dead_letter' after exhausting retries and shows
# up here for manual review.


def _webhook_event_row(ev) -> Dict[str, Any]:
    return {
        "id": ev.id,
        "tenant_id": ev.tenant_id,
        "provider": ev.provider,
        "event_type": ev.event_type,
        "external_event_id": ev.external_event_id,
        "store_id": ev.store_id,
        "signature_valid": ev.signature_valid,
        "status": ev.status,
        "attempts": ev.attempts,
        "last_error": ev.last_error,
        "last_error_at": ev.last_error_at.isoformat() if ev.last_error_at else None,
        "next_retry_at": ev.next_retry_at.isoformat() if ev.next_retry_at else None,
        "received_at": ev.received_at.isoformat() if ev.received_at else None,
        "processed_at": ev.processed_at.isoformat() if ev.processed_at else None,
    }


@router.get("/admin/webhook-events")
async def admin_list_webhook_events(
    status: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    tenant_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    List recent webhook_events (newest first), filterable by status/provider/tenant.

    Statuses: received | processing | processed | failed | dead_letter
    """
    from database.models import WebhookEvent  # noqa: PLC0415
    from core.webhook_events import count_by_status  # noqa: PLC0415

    q = db.query(WebhookEvent)
    if status:
        q = q.filter(WebhookEvent.status == status)
    if provider:
        q = q.filter(WebhookEvent.provider == provider)
    if tenant_id is not None:
        q = q.filter(WebhookEvent.tenant_id == tenant_id)

    rows = q.order_by(WebhookEvent.received_at.desc()).limit(limit).all()

    return {
        "summary": count_by_status(db, provider=provider),
        "events": [_webhook_event_row(e) for e in rows],
    }


@router.get("/admin/webhook-events/{event_id}")
async def admin_get_webhook_event(
    event_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Return a single webhook_event with full payload + headers for debugging."""
    from database.models import WebhookEvent  # noqa: PLC0415

    ev = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
    if ev is None:
        raise HTTPException(status_code=404, detail="webhook_event not found")

    row = _webhook_event_row(ev)
    row["raw_headers"] = ev.raw_headers
    row["raw_body"] = ev.raw_body
    row["parsed_payload"] = ev.parsed_payload
    return row


@router.post("/admin/webhook-events/{event_id}/replay")
async def admin_replay_webhook_event(
    event_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Reset a webhook_event back to status='received' so the dispatcher picks it
    up on the next tick. Safe for failed and dead_letter rows. Refuses to touch
    rows currently in 'processing' (would race a live worker).
    """
    from core.webhook_events import replay  # noqa: PLC0415

    ev = replay(db, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="webhook_event not found")
    return {"ok": True, "event": _webhook_event_row(ev)}


class WebhookReplayBulkIn(BaseModel):
    status: str = "dead_letter"
    provider: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=1000)


@router.post("/admin/webhook-events/replay-bulk")
async def admin_replay_webhook_events_bulk(
    body: WebhookReplayBulkIn,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Reset ALL events matching filters back to 'received'. Use for DLQ flushes."""
    from core.webhook_events import replay_bulk  # noqa: PLC0415

    n = replay_bulk(db, status=body.status, provider=body.provider, limit=body.limit)
    return {"ok": True, "replayed": n, "status_from": body.status, "provider": body.provider}


# ── Order data repair (status & customer-profile backfill) ─────────────────
# Fixes the 2026-04-17 corruption where Salla order statuses were stored as a
# Python repr of the upstream dict (e.g. "{'id': 566146469, 'name': '...',
# 'slug': 'under_review'}") instead of just the slug. Symptoms:
#   • dashboard shows every order as "ملغي"
#   • customers with real orders classified as "غير نشط" / "محتمل"
# Run after deploying the salla_adapter fix to heal historical rows.

@router.post("/admin/orders/backfill-status")
async def admin_backfill_order_status(
    tenant_id: Optional[int] = Query(None, description="Limit to one tenant; omit for all"),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Repair Order.status rows that were stored as a stringified Salla dict,
    then rebuild every affected customer's profile so segmentation reflects
    the recovered statuses.

    Idempotent: rows with already-clean slugs are skipped.
    """
    import ast as _ast  # noqa: PLC0415
    from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

    q = db.query(Order)
    if tenant_id is not None:
        q = q.filter(Order.tenant_id == tenant_id)

    repaired_per_tenant: Dict[int, int] = {}
    examined = 0
    repaired = 0
    failed = 0
    affected_tenants: set[int] = set()

    for order in q.yield_per(500):
        examined += 1
        raw = (order.status or "").strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            parsed = _ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            failed += 1
            continue
        if not isinstance(parsed, dict):
            failed += 1
            continue
        slug = (
            parsed.get("slug")
            or parsed.get("name")
            or parsed.get("code")
        )
        if not slug:
            failed += 1
            continue
        order.status = str(slug)
        repaired += 1
        repaired_per_tenant[order.tenant_id] = repaired_per_tenant.get(order.tenant_id, 0) + 1
        affected_tenants.add(order.tenant_id)

    db.commit()

    # Recompute every affected tenant's customer profiles so the dashboard
    # reflects the repaired statuses immediately (no need to wait for the
    # next full sync).
    profile_results: Dict[str, int] = {}
    for tid in sorted(affected_tenants):
        try:
            svc = CustomerIntelligenceService(db, tid)
            n = svc.rebuild_profiles_for_tenant(
                reason="admin_backfill_order_status",
                commit=True,
                emit_event=False,
            )
            profile_results[str(tid)] = n
        except Exception as exc:
            logger.exception("[backfill] profile rebuild failed for tenant=%s: %s", tid, exc)
            profile_results[str(tid)] = -1

    audit(
        "admin_backfill_order_status",
        admin=_admin.get("sub"),
        tenant_id=tenant_id,
        examined=examined,
        repaired=repaired,
        failed=failed,
        tenants_touched=len(affected_tenants),
    )
    return {
        "ok": True,
        "examined": examined,
        "repaired": repaired,
        "failed": failed,
        "tenants_repaired": {str(k): v for k, v in repaired_per_tenant.items()},
        "profiles_rebuilt": profile_results,
    }


@router.post("/admin/customers/recompute-profiles")
async def admin_recompute_customer_profiles(
    tenant_id: int = Query(..., description="Tenant whose profiles will be rebuilt"),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Force a fresh customer-profile rebuild for one tenant. Useful after a
    manual fix in the database, or to verify classification after an order
    backfill. Equivalent to what `full_sync` runs at the end.
    """
    from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

    svc = CustomerIntelligenceService(db, tenant_id)
    n = svc.rebuild_profiles_for_tenant(
        reason="admin_recompute_profiles",
        commit=True,
        emit_event=True,
    )
    audit(
        "admin_recompute_customer_profiles",
        admin=_admin.get("sub"),
        tenant_id=tenant_id,
        profiles_rebuilt=n,
    )
    return {"ok": True, "tenant_id": tenant_id, "profiles_rebuilt": n}
