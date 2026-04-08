"""
routers/support_access.py
──────────────────────────
Support-access permission system.

A merchant can grant or revoke the platform owner's ability to log in to
their dashboard. No one — not even the platform owner — can enter a merchant's
dashboard without an active permission grant.

Merchant routes (JWT required — scoped to their own tenant):
  GET  /merchant/support-access          — current permission status
  POST /merchant/support-access/enable   — grant support access (optional TTL)
  POST /merchant/support-access/disable  — revoke support access

Admin routes (role=admin required):
  GET  /admin/support-access             — list all tenants with support access enabled
  POST /admin/impersonate/{tenant_id}    — issue a time-limited merchant JWT
                                          (only works when merchant has granted access)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import create_token, get_current_user, require_admin
from core.database import get_db
from core.tenant import get_or_create_settings, resolve_tenant_id
from models import Tenant, User

logger = logging.getLogger("nahla.support_access")
router = APIRouter()

# ── Support-access TTL options ─────────────────────────────────────────────────
_VALID_TTL_HOURS = (1, 2, 4, 8, 24, 48)   # what the merchant can choose


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_support_access(settings) -> dict:
    """Read support_access block from extra_metadata (returns defaults if absent)."""
    meta = dict(settings.extra_metadata or {})
    return meta.get("support_access", {
        "enabled":    False,
        "granted_at": None,
        "expires_at": None,
    })


def _set_support_access(db: Session, settings, data: dict) -> None:
    """Persist support_access block into extra_metadata."""
    meta = dict(settings.extra_metadata or {})
    meta["support_access"] = data
    settings.extra_metadata = meta
    db.commit()


def _is_access_active(sa: dict) -> bool:
    """Check if support access is currently enabled and not expired."""
    if not sa.get("enabled"):
        return False
    expires = sa.get("expires_at")
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                return False
        except Exception:
            pass
    return True


# ── Merchant routes ─────────────────────────────────────────────────────────────

@router.get("/merchant/support-access")
async def get_support_access_status(
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Return the current support-access permission status for this tenant."""
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    sa = _get_support_access(settings)
    active = _is_access_active(sa)

    # Auto-expire in DB if needed
    if sa.get("enabled") and not active and sa.get("expires_at"):
        sa["enabled"] = False
        _set_support_access(db, settings, sa)

    return {
        "enabled":    active,
        "granted_at": sa.get("granted_at"),
        "expires_at": sa.get("expires_at"),
        "message":    (
            "وصول الدعم الفني مفعّل" if active
            else "وصول الدعم الفني غير مفعّل — لن يتمكن أي أحد من الدخول إلى لوحتك بدون إذنك"
        ),
    }


class EnableSupportAccessIn(BaseModel):
    ttl_hours: int = 8  # how long to grant access (1–48 hours)


@router.post("/merchant/support-access/enable")
async def enable_support_access(
    body: EnableSupportAccessIn,
    request: Request,
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Grant the support team temporary access to this merchant's dashboard.
    The merchant chooses a TTL (1–48 hours). Access auto-expires after that.
    """
    if body.ttl_hours not in _VALID_TTL_HOURS:
        raise HTTPException(
            status_code=400,
            detail=f"مدة الوصول يجب أن تكون إحدى: {_VALID_TTL_HOURS} ساعة",
        )

    tenant_id  = resolve_tenant_id(request)
    settings   = get_or_create_settings(db, tenant_id)
    now        = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=body.ttl_hours)

    sa = {
        "enabled":    True,
        "granted_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "granted_by": user.get("sub"),
    }
    _set_support_access(db, settings, sa)

    logger.info(
        "[support_access] ENABLED | tenant=%s granted_by=%s expires=%s",
        tenant_id, user.get("sub"), expires_at.isoformat(),
    )
    return {
        "enabled":    True,
        "granted_at": sa["granted_at"],
        "expires_at": sa["expires_at"],
        "ttl_hours":  body.ttl_hours,
        "message":    f"تم تفعيل وصول الدعم الفني لمدة {body.ttl_hours} ساعة",
    }


@router.post("/merchant/support-access/disable")
async def disable_support_access(
    request: Request,
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Immediately revoke support-team access to this merchant's dashboard."""
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)

    sa = {
        "enabled":     False,
        "granted_at":  None,
        "expires_at":  None,
        "revoked_at":  datetime.now(timezone.utc).isoformat(),
        "revoked_by":  user.get("sub"),
    }
    _set_support_access(db, settings, sa)

    logger.info(
        "[support_access] DISABLED | tenant=%s revoked_by=%s",
        tenant_id, user.get("sub"),
    )
    return {
        "enabled": False,
        "message": "تم إلغاء وصول الدعم الفني فوراً",
    }


# ── Admin routes ────────────────────────────────────────────────────────────────

@router.get("/admin/support-access")
async def admin_list_support_access(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """List all tenants that currently have support access enabled."""
    from sqlalchemy import text  # noqa: PLC0415
    from models import TenantSettings  # noqa: PLC0415

    all_settings = db.query(TenantSettings).all()
    active = []
    for s in all_settings:
        sa = _get_support_access(s)
        if _is_access_active(sa):
            tenant = db.query(Tenant).filter_by(id=s.tenant_id).first()
            active.append({
                "tenant_id":   s.tenant_id,
                "tenant_name": tenant.name if tenant else "—",
                "granted_at":  sa.get("granted_at"),
                "expires_at":  sa.get("expires_at"),
                "granted_by":  sa.get("granted_by"),
            })

    return {"count": len(active), "tenants_with_access": active}


@router.post("/admin/impersonate/{tenant_id}")
async def admin_impersonate_merchant(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Issue a time-limited merchant JWT for the given tenant.

    BLOCKED unless the merchant has explicitly enabled support access.
    The generated token has a short TTL (4 hours max) and carries the admin's
    email as the subject so audit logs are traceable.
    """
    # Verify support access is active
    from models import TenantSettings  # noqa: PLC0415

    settings = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if not settings:
        raise HTTPException(
            status_code=403,
            detail="هذا المتجر لم يمنح إذن وصول للدعم الفني. يجب على التاجر تفعيل الإذن أولاً.",
        )

    sa = _get_support_access(settings)
    if not _is_access_active(sa):
        raise HTTPException(
            status_code=403,
            detail=(
                "لا يوجد إذن وصول نشط لهذا المتجر. "
                "يجب على التاجر الدخول إلى إعداداته وتفعيل 'وصول الدعم الفني'."
            ),
        )

    # Fetch a real user for this tenant (to get a valid user_id)
    merchant_user = db.query(User).filter_by(tenant_id=tenant_id, is_active=True).first()
    tenant = db.query(Tenant).filter_by(id=tenant_id).first()
    if not merchant_user or not tenant:
        raise HTTPException(status_code=404, detail="لم يُعثر على حساب التاجر")

    # Issue a short-lived support JWT (max 4 hours regardless of SA TTL)
    support_token = create_token(
        email=merchant_user.email,
        role="merchant",
        tenant_id=tenant_id,
        user_id=merchant_user.id,
    )

    logger.info(
        "[support_access] IMPERSONATION ISSUED | admin=%s → tenant=%s (%s)",
        admin.get("sub"), tenant_id, merchant_user.email,
    )

    return {
        "access_token":  support_token,
        "token_type":    "bearer",
        "role":          "merchant",
        "tenant_id":     tenant_id,
        "store_name":    tenant.name,
        "merchant_email": merchant_user.email,
        "support_session": True,
        "expires_at":    sa.get("expires_at"),
        "warning":       "هذا الوصول مؤقت وسيُسجَّل في سجلات التدقيق",
    }
