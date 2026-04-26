"""
routers/support_access.py
──────────────────────────
Support-access permission system (production-grade).

Security model
══════════════
1. localStorage is display-only on the frontend — all authorisation decisions
   are made from the JWT claims decoded on the backend.  The role
   "support_impersonation" is distinct from "merchant" or "admin" so that
   middleware can identify and restrict the session without trusting any
   client-supplied header.

2. The support JWT carries the following extra claims:
     impersonation   = True
     actor_sub       = admin email (who is doing the impersonation)
     actor_user_id   = admin user.id
     session_version = DB revocation counter at issuance time
     role            = "support_impersonation"          (never "merchant")
     exp             = hard cap of 4 hours

3. Immediate revocation — when the merchant calls /disable the DB
   session_version is incremented.  Every incoming support request is
   validated against the current DB version; any token issued before the
   revocation is rejected by the middleware even before the JWT expires.

4. Full audit trail — every issuance, access attempt, and revocation is
   written to the dedicated "nahla.support_audit" logger with:
     actor, target tenant, IP, path, session_version, token fingerprint.

5. Sensitive operations are blocked at the middleware level (core/middleware.py)
   and by the require_not_support_impersonation dependency attached to each
   protected endpoint (password change, email change, billing, integrations
   secrets, tenant deletion).

6. GET /admin/support-access is admin-only and returns minimal information.

Merchant routes (JWT required — scoped to own tenant):
  GET  /merchant/support-access          — current permission status
  POST /merchant/support-access/enable   — grant access (choose TTL 1-8 h)
  POST /merchant/support-access/disable  — revoke immediately + bump version

Admin routes (role=admin required):
  GET  /admin/support-access             — list tenants with active grants (minimal)
  POST /admin/impersonate/{tenant_id}    — issue support JWT (blocked if no grant)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.auth import (
    create_support_token,
    get_client_ip,
    get_current_user,
    require_admin,
    token_fingerprint,
)
from core.database import get_db
from core.tenant import get_or_create_settings, resolve_tenant_id

logger = logging.getLogger("nahla.support_access")
_audit = logging.getLogger("nahla.support_audit")

router = APIRouter()

# TTL caps:
#   Merchant-initiated (enable) — shorter cap for safety
#   Admin-requested (request-access) — merchant can approve up to 48 h
_MAX_TTL_MERCHANT = 48           # merchant can approve up to 48 h in a request
_MAX_TTL_HOURS    = 8            # backward-compat: merchant self-enable cap
_VALID_TTL_HOURS  = (1, 2, 4, 8)     # for merchant self-enable
_VALID_TTL_ALL    = (1, 2, 4, 8, 24, 48)  # for admin requests / merchant approval


# ── Access Request Helpers ──────────────────────────────────────────────────────

def _get_requests(settings) -> List[dict]:
    """Read pending access requests from extra_metadata."""
    return list(dict(settings.extra_metadata or {}).get("access_requests", []))


def _put_requests(db: Session, settings, requests: List[dict]) -> None:
    """Write access requests back to extra_metadata.

    Uses flag_modified so SQLAlchemy always detects the JSONB mutation —
    nested dict changes are not tracked automatically by the ORM.
    """
    meta = dict(settings.extra_metadata or {})
    meta["access_requests"] = list(requests)   # ensure new list object
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_sa(settings) -> dict:
    """Read the support_access block from extra_metadata (safe defaults)."""
    return dict(settings.extra_metadata or {}).get("support_access", {
        "enabled":         False,
        "granted_at":      None,
        "expires_at":      None,
        "session_version": 0,
    })


def _put_sa(db: Session, settings, data: dict) -> None:
    """Write the support_access block back to extra_metadata atomically.

    Uses flag_modified so SQLAlchemy always detects the JSONB mutation.
    """
    meta = dict(settings.extra_metadata or {})
    meta["support_access"] = dict(data)   # ensure new dict object
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()


def _is_active(sa: dict) -> bool:
    """Return True only when access is both enabled and not yet expired."""
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
            return False
    return True


def _session_version(sa: dict) -> int:
    """Return the current revocation counter (0 if not set)."""
    return int(sa.get("session_version", 0))


# ── Merchant routes ─────────────────────────────────────────────────────────────

@router.get("/merchant/support-access")
async def get_support_access_status(
    request: Request,
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Return the current support-access permission status for this tenant.
    The response contains only public-safe fields — no session_version is
    exposed because clients cannot and should not use it for decisions.
    """
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    sa        = _get_sa(settings)
    active    = _is_active(sa)

    # Auto-expire in DB when TTL has passed
    if sa.get("enabled") and not active and sa.get("expires_at"):
        sa["enabled"] = False
        sa["session_version"] = _session_version(sa) + 1   # invalidate any live tokens
        _put_sa(db, settings, sa)
        _audit.info(
            "SUPPORT_AUTO_EXPIRED tenant=%s sub=%s",
            tenant_id, user.get("sub"),
        )

    return {
        "enabled":    active,
        "granted_at": sa.get("granted_at") if active else None,
        "expires_at": sa.get("expires_at") if active else None,
        "message": (
            "وصول الدعم الفني مفعّل"
            if active
            else "وصول الدعم الفني غير مفعّل — لوحتك محمية"
        ),
    }


class EnableSupportIn(BaseModel):
    ttl_hours: int = Field(default=4, ge=1, le=_MAX_TTL_HOURS)


@router.post("/merchant/support-access/enable")
async def enable_support_access(
    body: EnableSupportIn,
    request: Request,
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Grant the support team temporary, time-bounded access.

    - Accepted TTLs: 1 / 2 / 4 / 8 hours (hard cap enforced server-side)
    - Access auto-expires; the session_version is NOT reset on enable so that
      any previously issued token that was revoked stays revoked.
    """
    if body.ttl_hours not in _VALID_TTL_HOURS:
        raise HTTPException(
            status_code=400,
            detail=f"المدة المسموح بها: {_VALID_TTL_HOURS} ساعة",
        )

    ip        = get_client_ip(request)
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    now       = datetime.now(timezone.utc)
    expires   = now + timedelta(hours=body.ttl_hours)

    sa = _get_sa(settings)      # preserve existing session_version
    sa.update({
        "enabled":    True,
        "granted_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "granted_by": user.get("sub"),
    })
    _put_sa(db, settings, sa)

    _audit.info(
        "SUPPORT_ENABLED tenant=%s granted_by=%s ttl=%dh expires=%s ip=%s",
        tenant_id, user.get("sub"), body.ttl_hours, expires.isoformat(), ip,
    )
    _write_audit_log(db, tenant_id=tenant_id, action="support_access_enabled",
                     details={"ttl_hours": body.ttl_hours, "by": user.get("sub"),
                              "expires_at": expires.isoformat(), "ip": ip})
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
    """
    Immediately revoke all support access for this tenant.

    Increments session_version so that any currently active support JWT is
    rejected by the middleware on its next request — even before it expires.
    This is the core revocation mechanism; JWT blacklisting is not needed
    because the version counter acts as a lightweight distributed revocation.
    """
    ip        = get_client_ip(request)
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)

    sa       = _get_sa(settings)
    old_ver  = _session_version(sa)
    new_ver  = old_ver + 1

    sa.update({
        "enabled":         False,
        "granted_at":      None,
        "expires_at":      None,
        "revoked_at":      datetime.now(timezone.utc).isoformat(),
        "revoked_by":      user.get("sub"),
        "session_version": new_ver,   # CRITICAL — invalidates all active support tokens
    })
    _put_sa(db, settings, sa)

    _audit.info(
        "SUPPORT_REVOKED tenant=%s revoked_by=%s sv_old=%d sv_new=%d ip=%s",
        tenant_id, user.get("sub"), old_ver, new_ver, ip,
    )
    _write_audit_log(db, tenant_id=tenant_id, action="support_access_revoked",
                     details={"by": user.get("sub"), "ip": ip,
                              "session_version_new": new_ver})
    return {
        "enabled":         False,
        "session_version": new_ver,
        "message":         "تم إلغاء وصول الدعم الفني فوراً. أي جلسة دعم نشطة لن تعمل بعد الآن.",
    }


# ── Internal helpers: AuditLog, Notifications, Email ──────────────────────────

def _write_audit_log(
    db: Session,
    *,
    tenant_id: int,
    action: str,
    details: Dict[str, Any],
) -> None:
    """Write a row to the audit_logs table (non-fatal on error)."""
    try:
        from models import AuditLog  # noqa: PLC0415
        row = AuditLog(
            tenant_id=tenant_id,
            category="support_access",
            resource_type="tenant",
            resource_id=str(tenant_id),
            action=action,
            details=details,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("[SupportAccess] AuditLog write failed (non-fatal): %s", exc)
        db.rollback()


def _store_notification(
    db: Session,
    *,
    tenant_id: int,
    req_id: str,
    reason: str,
    actor: str,
    ttl_hours: int,
) -> None:
    """Store an in-platform notification in TenantSettings.extra_metadata.notifications."""
    try:
        settings = get_or_create_settings(db, tenant_id)
        meta = dict(settings.extra_metadata or {})
        notifications: list = list(meta.get("notifications", []))
        notifications.append({
            "id":         f"sa_{req_id}",
            "type":       "support_access_request",
            "read":       False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "title":      "طلب وصول من فريق نحلة",
            "body":       (
                f"فريق نحلة طلب وصولاً مؤقتاً لمساعدتك في: {reason}. "
                f"المدة المطلوبة: {ttl_hours} ساعة. "
                "يمكنك الموافقة أو الرفض من الإعدادات > الأمان."
            ),
            "action_url": "/settings?tab=security",
        })
        # Keep only last 50 notifications
        notifications = notifications[-50:]
        meta["notifications"] = notifications
        settings.extra_metadata = meta
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        flag_modified(settings, "extra_metadata")
        db.commit()
    except Exception as exc:
        logger.warning("[SupportAccess] store_notification failed (non-fatal): %s", exc)


def _send_access_request_email(
    *,
    merchant_email: str,
    store_name: str,
    actor: str,
    reason: str,
    ttl_hours: int,
) -> None:
    """Send email to merchant about the new access request (non-fatal)."""
    try:
        from services.email_service import send_email  # noqa: PLC0415
        html = f"""
        <div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:24px;background:#fff;border-radius:12px;border:1px solid #e2e8f0;">
          <div style="text-align:center;margin-bottom:24px;">
            <h1 style="color:#d97706;font-size:22px;margin:0;">طلب وصول مؤقت</h1>
          </div>
          <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;padding:16px;margin-bottom:20px;">
            <p style="margin:0;color:#92400e;font-size:14px;font-weight:bold;">
              ⚠️ فريق نحلة طلب وصولاً مؤقتاً إلى لوحتك
            </p>
          </div>
          <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
            <tr style="border-bottom:1px solid #f1f5f9;">
              <td style="padding:10px 0;color:#64748b;font-size:13px;width:40%;">المتجر</td>
              <td style="padding:10px 0;font-weight:bold;color:#1e293b;font-size:13px;">{store_name}</td>
            </tr>
            <tr style="border-bottom:1px solid #f1f5f9;">
              <td style="padding:10px 0;color:#64748b;font-size:13px;">سبب الطلب</td>
              <td style="padding:10px 0;font-weight:bold;color:#1e293b;font-size:13px;">{reason}</td>
            </tr>
            <tr style="border-bottom:1px solid #f1f5f9;">
              <td style="padding:10px 0;color:#64748b;font-size:13px;">المدة المطلوبة</td>
              <td style="padding:10px 0;font-weight:bold;color:#1e293b;font-size:13px;">{ttl_hours} ساعة</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#64748b;font-size:13px;">الطالب</td>
              <td style="padding:10px 0;font-weight:bold;color:#1e293b;font-size:13px;">{actor}</td>
            </tr>
          </table>
          <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:16px;margin-bottom:20px;">
            <p style="margin:0;color:#166534;font-size:13px;">
              🔒 <strong>لوحتك محمية:</strong> لا يستطيع أي أحد الدخول بدون موافقتك الصريحة.
            </p>
          </div>
          <div style="text-align:center;margin-bottom:16px;">
            <a href="https://app.nahlah.ai/settings?tab=security"
               style="display:inline-block;background:#d97706;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:14px;">
              ✅ الموافقة أو الرفض
            </a>
          </div>
          <p style="text-align:center;color:#94a3b8;font-size:11px;margin:0;">
            الإعدادات ← الأمان ← وصول الدعم الفني
          </p>
        </div>
        """
        send_email(
            to=merchant_email,
            subject=f"[نحلة] طلب وصول مؤقت لمتجرك: {reason[:50]}",
            html_body=html,
        )
    except Exception as exc:
        logger.warning("[SupportAccess] email send failed (non-fatal): %s", exc)


# ── Admin routes ────────────────────────────────────────────────────────────────

@router.get("/admin/support-access")
async def admin_list_support_access(
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
):
    """
    List tenants that currently have support access active.

    Returns the absolute minimum — no secrets, no session_version,
    no internal IDs beyond tenant_id (which the admin already knows).
    """
    from models import Tenant, TenantSettings  # noqa: PLC0415

    rows = db.query(TenantSettings).all()
    active = []
    for s in rows:
        sa = _get_sa(s)
        if not _is_active(sa):
            continue
        tenant = db.query(Tenant).filter_by(id=s.tenant_id).first()
        active.append({
            "tenant_id":   s.tenant_id,
            "tenant_name": tenant.name if tenant else "—",
            "expires_at":  sa.get("expires_at"),       # TTL only — no granted_at or granted_by
        })

    _audit.info(
        "ADMIN_LIST_SUPPORT admin=%s count=%d",
        admin.get("sub"), len(active),
    )
    return {"count": len(active), "active_grants": active}


@router.post("/admin/impersonate/{tenant_id}")
async def admin_impersonate(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
):
    """
    Issue a time-limited support JWT for the given tenant.

    BLOCKED if:
    - Merchant has not enabled support access
    - Support access has expired
    - No active user exists for the tenant

    The issued token
    - has role=support_impersonation (never merchant or admin)
    - carries impersonation=True, actor_sub, actor_user_id, session_version
    - has exp = min(4h, remaining grant time) — never exceeds 4 hours
    - is logged with its fingerprint (first 16 hex chars of SHA-256)
    """
    from models import Tenant, TenantSettings, User  # noqa: PLC0415

    ip = get_client_ip(request)

    # 1. Verify grant exists and is active
    settings = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if not settings:
        _audit.warning(
            "IMPERSONATE_BLOCKED_NO_GRANT admin=%s tenant=%d ip=%s",
            admin.get("sub"), tenant_id, ip,
        )
        raise HTTPException(
            status_code=403,
            detail="هذا المتجر لم يمنح إذن وصول للدعم الفني.",
        )

    sa = _get_sa(settings)
    if not _is_active(sa):
        _audit.warning(
            "IMPERSONATE_BLOCKED_EXPIRED admin=%s tenant=%d ip=%s",
            admin.get("sub"), tenant_id, ip,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "إذن وصول الدعم لهذا المتجر منتهٍ أو غير موجود. "
                "يجب على التاجر تفعيله من إعداداته."
            ),
        )

    # 2. Fetch merchant user and tenant
    merchant = db.query(User).filter_by(tenant_id=tenant_id, is_active=True).first()
    tenant   = db.query(Tenant).filter_by(id=tenant_id).first()
    if not merchant or not tenant:
        raise HTTPException(status_code=404, detail="لم يُعثر على حساب التاجر")

    # 3. Compute remaining TTL (never exceed 4 h)
    now            = datetime.now(timezone.utc)
    expires_at_str = sa.get("expires_at", "")
    try:
        grant_exp = datetime.fromisoformat(expires_at_str)
        if grant_exp.tzinfo is None:
            grant_exp = grant_exp.replace(tzinfo=timezone.utc)
        remaining_h = max(1, int((grant_exp - now).total_seconds() // 3600))
    except Exception:
        remaining_h = 4
    ttl_hours = min(remaining_h, 4)

    # 4. Session version from DB — token is only valid as long as DB version matches
    sv = _session_version(sa)

    # 5. Issue the support token
    support_token = create_support_token(
        merchant_email=merchant.email,
        merchant_user_id=merchant.id,
        tenant_id=tenant_id,
        actor_email=admin.get("sub", ""),
        actor_user_id=int(admin.get("user_id") or 0),
        session_version=sv,
        ttl_hours=ttl_hours,
    )
    fp = token_fingerprint(support_token)

    _audit.info(
        "IMPERSONATE_ISSUED actor=%s → tenant=%d merchant=%s sv=%d ttl=%dh fp=%s ip=%s",
        admin.get("sub"), tenant_id, merchant.email, sv, ttl_hours, fp, ip,
    )
    _write_audit_log(db, tenant_id=tenant_id, action="support_impersonate_issued",
                     details={"actor": admin.get("sub"), "ttl_hours": ttl_hours,
                              "token_fp": fp, "ip": ip, "session_version": sv})

    return {
        "access_token":    support_token,
        "token_type":      "bearer",
        "role":            "support_impersonation",
        "tenant_id":       tenant_id,
        "store_name":      tenant.name,
        "merchant_email":  merchant.email,
        "impersonation":   True,
        "session_version": sv,
        "ttl_hours":       ttl_hours,
        "token_fp":        fp,
        "warning":         "هذا الوصول مؤقت ومُسجَّل. سيُلغى فور إلغاء التاجر للإذن.",
    }


# ── Access Request Flow (Admin requests → Merchant approves) ────────────────────

class RequestAccessIn(BaseModel):
    reason:    str = Field(..., min_length=5, max_length=300,
                           description="سبب الطلب — يظهر للتاجر قبل الموافقة (إلزامي)")
    ttl_hours: int = Field(default=4, ge=1, le=_MAX_TTL_MERCHANT,
                           description="المدة المطلوبة بالساعات (1-48)")


@router.post("/admin/request-access/{tenant_id}")
async def admin_request_access(
    tenant_id: int,
    body:      RequestAccessIn,
    request:   Request,
    db:        Session         = Depends(get_db),
    admin:     Dict[str, Any]  = Depends(require_admin),
):
    """
    Admin sends an access request to a merchant.

    Fields:
      reason    — shown to merchant so they know WHY access is requested (required)
      ttl_hours — requested duration; merchant sees this before approving (1-48 h)

    A notification is stored in extra_metadata; the merchant must approve before
    admin can enter.  On approval, support_access is enabled with merchant's chosen TTL.
    """
    from models import Tenant, User  # noqa: PLC0415

    # Validate TTL
    if body.ttl_hours not in _VALID_TTL_ALL:
        raise HTTPException(
            status_code=400,
            detail=f"المدة المقبولة: {_VALID_TTL_ALL} ساعة",
        )

    tenant   = db.query(Tenant).filter_by(id=tenant_id, is_active=True).first()
    merchant = db.query(User).filter_by(tenant_id=tenant_id, is_active=True).first()
    if not tenant or not merchant:
        raise HTTPException(status_code=404, detail="لم يُعثر على المتجر")

    settings = get_or_create_settings(db, tenant_id)
    db.commit()
    requests = _get_requests(settings)

    # Block if already has a pending request
    pending = [r for r in requests if r.get("status") == "pending"]
    if pending:
        raise HTTPException(
            status_code=409,
            detail="يوجد طلب وصول معلّق بالفعل — انتظر موافقة التاجر.",
        )

    # Block if already has active access
    sa = _get_sa(settings)
    if _is_active(sa):
        raise HTTPException(
            status_code=409,
            detail="التاجر منح الوصول مسبقاً — يمكنك الدخول مباشرة.",
        )

    req_id  = str(uuid.uuid4())[:8]
    now     = datetime.now(timezone.utc)
    new_req = {
        "id":            req_id,
        "requested_by":  admin.get("sub"),
        "requested_at":  now.isoformat(),
        "status":        "pending",
        "store_name":    tenant.name,
        "reason":        body.reason.strip(),
        "ttl_hours":     body.ttl_hours,
        "merchant_email": merchant.email,
    }
    requests.append(new_req)
    _put_requests(db, settings, requests)

    _audit.info(
        "ACCESS_REQUEST_SENT admin=%s → tenant=%d req_id=%s reason=%r ttl=%dh",
        admin.get("sub"), tenant_id, req_id, body.reason[:60], body.ttl_hours,
    )

    # ── Write to AuditLog DB ────────────────────────────────────────────────
    _write_audit_log(db, tenant_id=tenant_id, action="support_access_requested",
                     details={
                         "req_id": req_id, "reason": body.reason[:300],
                         "ttl_hours": body.ttl_hours, "actor": admin.get("sub"),
                     })

    # ── In-platform notification ────────────────────────────────────────────
    _store_notification(db, tenant_id=tenant_id, req_id=req_id,
                        reason=body.reason, actor=admin.get("sub"),
                        ttl_hours=body.ttl_hours)

    # ── Email to merchant ───────────────────────────────────────────────────
    _send_access_request_email(
        merchant_email=merchant.email,
        store_name=tenant.name,
        actor=admin.get("sub", "فريق نحلة"),
        reason=body.reason,
        ttl_hours=body.ttl_hours,
    )

    return {
        "request_id": req_id,
        "status":     "pending",
        "message":    f"تم إرسال طلب الوصول إلى {tenant.name}. في انتظار موافقة التاجر.",
    }


@router.get("/merchant/access-requests")
async def merchant_get_access_requests(
    request: Request,
    db:      Session         = Depends(get_db),
    user:    Dict[str, Any]  = Depends(get_current_user),
):
    """Return pending access requests for this tenant's merchant."""
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    requests  = _get_requests(settings)
    pending   = [r for r in requests if r.get("status") == "pending"]
    return {"requests": pending, "count": len(pending)}


class RespondAccessIn(BaseModel):
    approve:   bool
    ttl_hours: int = Field(default=4, ge=1, le=_MAX_TTL_MERCHANT)


@router.post("/merchant/access-requests/{req_id}/respond")
async def merchant_respond_access_request(
    req_id:  str,
    body:    RespondAccessIn,
    request: Request,
    db:      Session         = Depends(get_db),
    user:    Dict[str, Any]  = Depends(get_current_user),
):
    """Merchant approves or rejects a pending admin access request."""
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    requests  = _get_requests(settings)

    target = next((r for r in requests if r.get("id") == req_id and r.get("status") == "pending"), None)
    if not target:
        raise HTTPException(status_code=404, detail="الطلب غير موجود أو تمت معالجته مسبقاً")

    now = datetime.now(timezone.utc)
    target["status"]       = "approved" if body.approve else "rejected"
    target["responded_at"] = now.isoformat()
    target["responded_by"] = user.get("sub")

    if body.approve:
        if body.ttl_hours not in _VALID_TTL_ALL:
            body.ttl_hours = 4
        expires = now + timedelta(hours=body.ttl_hours)
        sa = _get_sa(settings)
        sa.update({
            "enabled":    True,
            "granted_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "granted_by": user.get("sub"),
            "reason":     target.get("reason", ""),   # preserve reason for banner
        })
        # Write both requests list and support_access in a single atomic commit
        meta = dict(settings.extra_metadata or {})
        meta["access_requests"] = list(requests)
        meta["support_access"]  = dict(sa)
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")
        db.commit()
        _audit.info(
            "ACCESS_APPROVED req=%s tenant=%d by=%s ttl=%dh",
            req_id, tenant_id, user.get("sub"), body.ttl_hours,
        )
        _write_audit_log(db, tenant_id=tenant_id, action="support_access_approved",
                         details={
                             "req_id": req_id, "ttl_hours": body.ttl_hours,
                             "by": user.get("sub"), "expires_at": expires.isoformat(),
                             "reason": target.get("reason", ""),
                         })
        return {
            "status":    "approved",
            "ttl_hours": body.ttl_hours,
            "expires_at": expires.isoformat(),
            "message":   f"تم منح الوصول لمدة {body.ttl_hours} ساعة. سيُلغى تلقائياً بعد انتهاء المدة.",
        }
    else:
        # Persist rejection in a single atomic commit
        meta = dict(settings.extra_metadata or {})
        meta["access_requests"] = list(requests)
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")
        db.commit()
        _audit.info(
            "ACCESS_REJECTED req=%s tenant=%d by=%s",
            req_id, tenant_id, user.get("sub"),
        )
        _write_audit_log(db, tenant_id=tenant_id, action="support_access_rejected",
                         details={"req_id": req_id, "by": user.get("sub")})
        return {"status": "rejected", "message": "تم رفض طلب الوصول."}


# ── Notifications endpoint ──────────────────────────────────────────────────────

@router.get("/merchant/notifications")
async def merchant_notifications(
    request: Request,
    db:      Session         = Depends(get_db),
    user:    Dict[str, Any]  = Depends(get_current_user),
):
    """
    Return unread in-platform notifications for this tenant.
    Used by the Header bell icon to show count + list.
    """
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    meta          = dict(settings.extra_metadata or {})
    notifications = list(meta.get("notifications", []))
    unread        = [n for n in notifications if not n.get("read")]
    return {"notifications": unread, "count": len(unread)}


@router.post("/merchant/notifications/{notif_id}/read")
async def mark_notification_read(
    notif_id: str,
    request: Request,
    db:      Session         = Depends(get_db),
    user:    Dict[str, Any]  = Depends(get_current_user),
):
    """Mark a notification as read."""
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    meta          = dict(settings.extra_metadata or {})
    notifications = list(meta.get("notifications", []))
    for n in notifications:
        if n.get("id") == notif_id:
            n["read"] = True
    meta["notifications"] = notifications
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()
    return {"status": "ok"}


# ── Audit log view endpoint (admin) ────────────────────────────────────────────

@router.get("/admin/support-access/audit")
async def admin_support_audit_log(
    db:    Session         = Depends(get_db),
    admin: Dict[str, Any]  = Depends(require_admin),
    limit: int = 100,
):
    """
    Return recent AuditLog entries for support_access category.
    Gives admin full visibility into who accessed what and when.
    """
    try:
        from models import AuditLog  # noqa: PLC0415
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.category == "support_access")
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return {
            "count": len(rows),
            "logs": [
                {
                    "id":         r.id,
                    "tenant_id":  r.tenant_id,
                    "action":     r.action,
                    "details":    r.details,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
