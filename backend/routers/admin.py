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

from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
from models import Tenant, User  # noqa: E402

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

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateMerchantIn(BaseModel):
    email:      str
    password:   str
    store_name: str
    phone:      str = ""


class InviteIn(BaseModel):
    email: str  # pre-fill for a specific email, or "" for an open invitation


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


# ── Routes ─────────────────────────────────────────────────────────────────────

import logging
logger = logging.getLogger("nahla.admin")


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
        created_at=datetime.utcnow(),
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
        created_at=datetime.utcnow(),
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
    from sqlalchemy import func
    from datetime import date
    from models import BillingSubscription, BillingPayment, BillingPlan, WhatsAppConnection  # noqa: E402

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

        # Per-plan counts
        plans = db.query(BillingPlan).all()
        plan_counts = {}
        for plan in plans:
            cnt = db.query(func.count(BillingSubscription.id)).filter(
                BillingSubscription.plan_id == plan.id,
                BillingSubscription.status.in_(["active", "trialing"]),
            ).scalar() or 0
            plan_counts[plan.slug] = {"name_ar": plan.name_ar or plan.name, "count": cnt, "price": float(plan.price_sar)}
    except Exception:
        active_subs  = 0
        trial_subs   = 0
        total_subs   = 0
        plan_counts  = {}

    # Revenue
    try:
        today = date.today()
        total_revenue = db.query(func.sum(BillingPayment.amount)).filter(
            BillingPayment.status == "paid"
        ).scalar() or 0
        today_revenue = db.query(func.sum(BillingPayment.amount)).filter(
            BillingPayment.status == "paid",
            func.date(BillingPayment.created_at) == today,
        ).scalar() or 0
        # MRR: sum of active subscription plan prices
        mrr = 0.0
        try:
            active_sub_list = db.query(BillingSubscription).filter(
                BillingSubscription.status.in_(["active", "trialing"])
            ).all()
            for sub in active_sub_list:
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
                "id":         p.id,
                "tenant_id":  p.tenant_id,
                "amount":     float(p.amount),
                "currency":   p.currency,
                "status":     p.status,
                "gateway":    p.gateway,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in recent_payments
        ]
    except Exception:
        total_revenue   = 0
        today_revenue   = 0
        mrr             = 0.0
        payments_list   = []

    # All merchants with details
    all_merchants = db.query(User).filter(User.role == "merchant").order_by(
        User.created_at.desc()
    ).limit(100).all()

    def _merchant_detail(u: User) -> dict:
        sub = None
        plan_name = "—"
        sub_status = "none"
        try:
            sub = db.query(BillingSubscription).filter(
                BillingSubscription.tenant_id == u.tenant_id
            ).order_by(BillingSubscription.created_at.desc()).first()
            if sub:
                plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
                plan_name  = plan.name_ar or plan.name if plan else "—"
                sub_status = sub.status
        except Exception:
            pass
        wa_status = "not_connected"
        try:
            wa = db.query(WhatsAppConnection).filter(
                WhatsAppConnection.tenant_id == u.tenant_id
            ).first()
            if wa:
                wa_status = wa.status
        except Exception:
            pass
        return {
            "id":           u.id,
            "email":        u.email,
            "store_name":   u.username,
            "phone":        getattr(u, "phone", ""),
            "is_active":    u.is_active,
            "plan":         plan_name,
            "sub_status":   sub_status,
            "wa_status":    wa_status,
            "created_at":   u.created_at.isoformat() if u.created_at else None,
        }

    return {
        "merchants": {
            "total":  total_merchants,
            "active": active_merchants,
            "trial":  trial_subs,
        },
        "tenants": {
            "total": total_tenants,
        },
        "subscriptions": {
            "active":  active_subs,
            "trial":   trial_subs,
            "total":   total_subs,
            "by_plan": plan_counts,
        },
        "revenue": {
            "total_sar": float(total_revenue),
            "today_sar": float(today_revenue),
            "mrr_sar":   mrr,
        },
        "recent_payments":  payments_list,
        "recent_merchants": [_merchant_detail(u) for u in all_merchants[:5]],
        "all_merchants":    [_merchant_detail(u) for u in all_merchants],
    }


@router.post("/admin/merchants/{user_id}/impersonate")
async def impersonate_merchant(
    user_id: int,
    request: Request,
    db:      Session          = Depends(get_db),
    _admin:  Dict[str, Any]  = Depends(require_admin),
):
    """
    Generate a short-lived token (2 h) scoped to the merchant's tenant.
    The admin can use it to view/edit the merchant's dashboard on their behalf.
    """
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    user = db.query(User).filter(User.id == user_id, User.role == "merchant").first()
    if not user:
        raise HTTPException(status_code=404, detail="Merchant not found")
    if not user.tenant_id:
        raise HTTPException(status_code=400, detail="هذا التاجر ليس لديه tenant مرتبط")

    from datetime import timedelta
    from jose import jwt as _jwt
    from core.config import JWT_SECRET, JWT_ALGORITHM

    payload = {
        "sub":              user.email,
        "role":             "merchant",
        "tenant_id":        user.tenant_id,
        "impersonated_by":  _admin.get("sub", "admin"),
        "exp":              datetime.utcnow() + timedelta(hours=2),
    }
    token = _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    audit(
        "admin_impersonate_merchant",
        admin=_admin.get("sub"),
        merchant_id=user_id,
        merchant_email=user.email,
        tenant_id=user.tenant_id,
    )

    return {
        "access_token":   token,
        "token_type":     "bearer",
        "expires_in":     7200,
        "merchant_email": user.email,
        "store_name":     user.tenant.name if user.tenant else "",
        "tenant_id":      user.tenant_id,
    }


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: int, db: Session = Depends(get_db)):
    """Retrieve a single tenant by its numeric ID."""
    tenant = db.query(Tenant).filter(
        Tenant.id == tenant_id,
        Tenant.is_active == True,  # noqa: E712
    ).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "id":        tenant.id,
        "name":      tenant.name,
        "domain":    tenant.domain,
        "is_active": tenant.is_active,
    }
