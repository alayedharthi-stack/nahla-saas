"""
routers/auth.py
───────────────
Authentication endpoints — JWT login, registration, invite flow, and password reset.

Routes:
  POST /auth/login            — exchange email + password for a JWT
  GET  /auth/me               — return identity of the authenticated caller
  POST /auth/logout           — client-side logout acknowledgement
  GET  /auth/invite/{token}   — validate an invitation token
  POST /auth/register         — register a new merchant (invite-gated in production)
  GET  /auth/verify-email     — verify email address via signed link
  POST /auth/forgot-password  — request a password-reset email
  POST /auth/reset-password   — apply a new password using a reset token

Security notes:
  • Admin credentials are compared with hmac.compare_digest (timing-safe).
  • Merchant passwords are verified via bcrypt (core/auth.verify_password).
  • /auth/forgot-password always returns 200 to prevent email enumeration.
  • All tokens are signed JWTs (python-jose); bcrypt handles password storage.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime, timezone
from typing import Any, Dict

import sqlalchemy
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import os
from models import Tenant, User  # noqa: E402

from core.audit import audit
from core.auth import (
    BCRYPT_AVAILABLE,
    JWT_AVAILABLE,
    create_reset_token,
    create_token,
    create_verify_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from core.config import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    DASHBOARD_URL,
    REQUIRE_INVITE,
)
from core.database import get_db
from core.notifications import email_reset, email_verify, email_welcome, send_email
from core.wa_notify import notify_welcome

logger = logging.getLogger("nahla.auth")
router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    email:    str
    password: str


class RegisterIn(BaseModel):
    email:        str
    password:     str
    store_name:   str
    phone:        str = ""
    invite_token: str = ""  # required when REQUIRE_INVITE=true


class ForgotPasswordIn(BaseModel):
    email: str


class ResetPasswordIn(BaseModel):
    token:    str
    password: str


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/auth/ping")
async def auth_ping(request: Request) -> Dict[str, Any]:
    """
    Diagnostic ping — no DB, no JWT, no bcrypt.

    Used by the login page to verify that the frontend can actually reach
    `https://api.nahlah.ai` over CORS before submitting credentials. If
    the spinner gets stuck on "جارٍ تسجيل الدخول…" but `/auth/ping`
    returns OK, the auth path itself is the problem (DB / bcrypt /
    password). If `/auth/ping` itself fails, the issue is the network
    layer (API base URL, CORS allow-list, service-worker cache, proxy)
    and the credentials never had a chance.

    Always returns 200 with the request method + a server timestamp.
    Cheap enough to be called on every page load if needed.
    """
    import time as _time  # noqa: PLC0415
    return {
        "ok":     True,
        "service": "auth",
        "ts":     _time.time(),
        "origin": request.headers.get("origin", ""),
    }


async def _auth_login_impl(
    *,
    raw_email: str,
    raw_password: str,
    request: Request,
    db: Session,
    transport: str,
) -> Dict[str, Any]:
    """
    Shared login implementation used by both /auth/login (JSON, fires a
    CORS preflight) and /auth/login-form (form-urlencoded, "simple
    request", browser does NOT preflight). Identical behaviour and
    structured logs — the only difference is how the body was decoded.

    ``transport`` is logged on every line so the operator can tell from
    Railway whether the browser hit the JSON endpoint or fell back to
    the form endpoint when CORS preflight is broken upstream.
    """
    import time as _time  # noqa: PLC0415
    _t0 = _time.monotonic()

    email      = (raw_email or "").strip().lower()
    client_ip  = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    logger.info("[AUTH LOGIN] start email=%s ip=%s transport=%s", email, client_ip, transport)

    if not JWT_AVAILABLE:
        logger.error("[AUTH LOGIN] aborted email=%s reason=jwt_unavailable", email)
        raise HTTPException(status_code=503, detail="Auth service unavailable — python-jose not installed")

    _INVALID = HTTPException(status_code=401, detail="البريد الإلكتروني أو كلمة المرور غير صحيحة")

    # 1. Merchant credentials — checked FIRST so the platform owner can also
    #    log in as a regular merchant (e.g. test store) with the same email.
    if BCRYPT_AVAILABLE:
        try:
            user = db.query(User).filter(User.email == email, User.is_active == True).first()  # noqa: E712
        except Exception as exc:
            logger.error("[AUTH LOGIN] db error email=%s exc=%s", email, exc, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
            raise HTTPException(
                status_code=503,
                detail="تعذّر الاتصال بقاعدة البيانات. حاول لاحقًا.",
            ) from exc
        logger.info(
            "[AUTH LOGIN] db ok email=%s user_found=%s has_password_hash=%s",
            email, bool(user),
            bool(user and getattr(user, "password_hash", None)),
        )
        if user and getattr(user, "password_hash", None):
            # bcrypt is CPU-bound (deliberately ~50–200 ms per call). Running
            # it inline in this async handler blocks the event loop, which
            # under any concurrent traffic delays /healthz, /auth/login, and
            # webhook acks. asyncio.to_thread punts the work to the default
            # thread executor so the loop stays free.
            import asyncio as _asyncio  # noqa: PLC0415
            if await _asyncio.to_thread(verify_password, raw_password, user.password_hash):
                role = user.role or "merchant"
                logger.info("[AUTH LOGIN] password verified email=%s role=%s", email, role)

                # Use the tenant already assigned to this user. We REFUSE to
                # invent or "snap" a tenant on the fly here — the previous
                # behaviour silently linked any user with `tenant_id=NULL` to
                # whichever tenant happened to own a WhatsAppConnection (which
                # ended up being tenant=1 in production), causing the
                # "conversations sometimes appear, sometimes vanish"
                # symptom because the same physical owner had multiple
                # accounts each landing on a different tenant after the
                # first login. Tenant assignment is a deliberate operation,
                # not a side-effect of `/auth/login`.
                tenant_id = user.tenant_id

                if not tenant_id:
                    audit(
                        "login_blocked_unassigned_tenant",
                        sub=user.email, role=role, ip=client_ip,
                    )
                    logger.warning(
                        "[auth/login] BLOCKED — user=%s has no tenant_id; "
                        "use POST /admin/users/{user_id}/assign-tenant first.",
                        email,
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "حسابك غير مربوط بأي متجر. الرجاء التواصل مع "
                            "الدعم لربط حسابك بالمتجر الصحيح."
                        ),
                    )

                token = create_token(
                    email=user.email,
                    role=role,
                    tenant_id=tenant_id,
                    user_id=user.id,
                )
                logger.info(
                    "[AUTH LOGIN] token issued email=%s tenant_id=%s user_id=%s role=%s",
                    user.email, tenant_id, user.id, role,
                )
                audit("login_success", role=role, sub=user.email, tenant_id=tenant_id, ip=client_ip)
                _ms = int((_time.monotonic() - _t0) * 1000)
                logger.info(
                    "[AUTH LOGIN] response sent email=%s role=%s tenant_id=%s ms=%s",
                    user.email, role, tenant_id, _ms,
                )
                return {
                    "access_token": token,
                    "token_type":   "bearer",
                    "role":         role,
                    "email":        user.email,
                    "tenant_id":    tenant_id,
                    "user_id":      user.id,
                }
            else:
                logger.info("[AUTH LOGIN] password mismatch email=%s", email)

    # 2. Admin credentials (env-var fallback — only if no merchant account matched)
    email_ok    = hmac.compare_digest(email,        ADMIN_EMAIL.lower())
    password_ok = hmac.compare_digest(raw_password, ADMIN_PASSWORD)
    if email_ok and password_ok:
        token = create_token(email=ADMIN_EMAIL, role="admin", tenant_id=1)
        logger.info("[AUTH LOGIN] token issued email=%s tenant_id=1 role=admin", ADMIN_EMAIL)
        audit("login_success", role="admin", sub=ADMIN_EMAIL, ip=client_ip)
        _ms = int((_time.monotonic() - _t0) * 1000)
        logger.info(
            "[AUTH LOGIN] response sent email=%s role=admin tenant_id=1 ms=%s",
            ADMIN_EMAIL, _ms,
        )
        return {
            "access_token": token,
            "token_type":   "bearer",
            "role":         "admin",
            "email":        ADMIN_EMAIL,
            "tenant_id":    1,
        }

    # 3. Nothing matched
    _ms = int((_time.monotonic() - _t0) * 1000)
    audit("login_failed", reason="invalid_credentials", sub=email, ip=client_ip)
    logger.warning("[AUTH LOGIN] FAILED email=%s ip=%s ms=%s", email, client_ip, _ms)
    raise _INVALID


# ── Public route adapters ───────────────────────────────────────────────────
# Two endpoints share the same _auth_login_impl helper above.
#
# /auth/login      — JSON body {email,password}. The browser fires a CORS
#                    preflight (OPTIONS) before this request because
#                    Content-Type is application/json. Used by JS clients
#                    that can rely on a healthy preflight path.
#
# /auth/login-form — application/x-www-form-urlencoded body. Per CORS spec
#                    this is a "simple request" — the browser does NOT
#                    fire a preflight OPTIONS. This is the escape hatch
#                    when an upstream proxy (Cloudflare / Railway edge /
#                    ISP) drops or aborts OPTIONS requests
#                    (NS_BINDING_ABORTED in Firefox, net::ERR_CONNECTION
#                    _CLOSED in Chrome). Frontend tries the form
#                    endpoint first when configured to do so.

@router.post("/auth/login")
async def auth_login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    """JSON login (preflight-required). See _auth_login_impl docstring."""
    return await _auth_login_impl(
        raw_email=body.email,
        raw_password=body.password,
        request=request,
        db=db,
        transport="json",
    )


@router.post("/auth/login-form")
async def auth_login_form(
    request: Request,
    email:    str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Form-encoded login — bypasses CORS preflight entirely.

    Posting application/x-www-form-urlencoded with Content-Type set
    automatically by the browser is a CORS "simple request" per the
    Fetch spec, so the browser sends it directly without firing an
    OPTIONS preflight first. This is the path used by the dashboard
    when the JSON preflight is blocked upstream of the application.
    """
    return await _auth_login_impl(
        raw_email=email,
        raw_password=password,
        request=request,
        db=db,
        transport="form",
    )


@router.get("/auth/me")
async def auth_me(user: Dict[str, Any] = Depends(get_current_user)):
    """Return the identity of the currently authenticated user."""
    return {
        "email":     user.get("sub"),
        "role":      user.get("role"),
        "tenant_id": user.get("tenant_id"),
    }


@router.get("/auth/me/full")
async def auth_me_full(
    user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Extended identity endpoint — returns user, tenant, and WhatsApp status.
    Use this to diagnose tenant-resolution or role issues.
    """
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415
    email     = user.get("sub")
    tenant_id = int(user.get("tenant_id", 0))
    if not tenant_id:
        raise HTTPException(status_code=401, detail="JWT missing tenant_id")

    db_user = db.query(User).filter_by(email=email).first()
    tenant  = db.query(Tenant).filter_by(id=tenant_id).first()
    wa_conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()

    return {
        "jwt_claims": {
            "sub":       email,
            "role":      user.get("role"),
            "tenant_id": tenant_id,
        },
        "user_in_db": {
            "id":           db_user.id        if db_user else None,
            "email":        db_user.email     if db_user else None,
            "role":         db_user.role      if db_user else None,
            "tenant_id":    db_user.tenant_id if db_user else None,
            "is_active":    db_user.is_active if db_user else None,
            "has_password": bool(getattr(db_user, "password_hash", None)) if db_user else False,
        },
        "tenant_in_db": {
            "id":   tenant.id   if tenant else None,
            "name": tenant.name if tenant else None,
        },
        "whatsapp": {
            "connected": bool(wa_conn and wa_conn.status == "connected" and wa_conn.sending_enabled),
            "status":    wa_conn.status       if wa_conn else "none",
            "phone":     wa_conn.phone_number if wa_conn else None,
        },
        "tenant_mismatch": (
            db_user is not None and db_user.tenant_id != tenant_id
        ),
    }


@router.post("/auth/logout")
async def auth_logout():
    """Client-side logout — token invalidation is handled by the frontend."""
    return {"detail": "logged out"}


@router.get("/auth/invite/{token}")
async def validate_invite(token: str):
    """Check whether an invitation token is valid and return the pre-filled email."""
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    payload = decode_token(token)
    if not payload or payload.get("type") != "invite":
        raise HTTPException(status_code=400, detail="رابط الدعوة غير صالح أو منتهي الصلاحية")
    return {
        "valid":          True,
        "invited_email":  payload.get("invited_email", ""),
        "tenant_id_hint": payload.get("tenant_id_hint"),
    }


@router.post("/auth/register")
async def auth_register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    """
    Register a new merchant account.
    When REQUIRE_INVITE=true (production default), a valid invite_token is mandatory.
    Creates a dedicated tenant + merchant user, returns a JWT token.
    """
    if not BCRYPT_AVAILABLE:
        raise HTTPException(status_code=503, detail="bcrypt not installed")

    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    email = body.email.strip().lower()
    if not email or not body.password or not body.store_name.strip():
        raise HTTPException(status_code=400, detail="البريد وكلمة المرور واسم المتجر مطلوبة")

    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تكون 8 أحرف على الأقل")

    # ── Invitation gate ────────────────────────────────────────────────────────
    if REQUIRE_INVITE:
        if not body.invite_token:
            audit("register_denied", reason="missing_invite", sub=email, ip=client_ip)
            raise HTTPException(
                status_code=403,
                detail="التسجيل يتطلب رابط دعوة صالح. تواصل مع المالك للحصول على رابط دعوة.",
            )
        invite = decode_token(body.invite_token)
        if not invite or invite.get("type") != "invite":
            audit("register_denied", reason="invalid_invite", sub=email, ip=client_ip)
            raise HTTPException(status_code=403, detail="رابط الدعوة غير صالح أو منتهي الصلاحية")
        # If the invite was for a specific email, enforce it
        invited_email = invite.get("invited_email", "")
        if invited_email and invited_email.lower() != email:
            audit(
                "register_denied",
                reason="email_mismatch",
                sub=email,
                invited=invited_email,
                ip=client_ip,
            )
            raise HTTPException(
                status_code=403,
                detail="البريد الإلكتروني لا يطابق الدعوة المرسلة",
            )

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="البريد الإلكتروني مسجَّل مسبقاً")

    # Create a dedicated tenant — use email slug as suffix to guarantee uniqueness
    slug = email.split("@")[0]
    tenant = Tenant(
        name=f"{body.store_name.strip()} ({slug})",
        domain=f"store-{slug}.nahla.sa",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tenant)
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="اسم المتجر أو النطاق مسجَّل مسبقاً")

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
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="فشل إنشاء الحساب — حاول مرة أخرى")
    db.refresh(user)

    audit(
        "merchant_registered",
        sub=email,
        tenant_id=tenant.id,
        store_name=body.store_name.strip(),
        ip=client_ip,
    )

    # ── Send verification email (fire-and-forget) ──────────────────────────────
    verify_token = create_verify_token(email)
    verify_url   = f"{DASHBOARD_URL}/verify-email?token={verify_token}"
    asyncio.ensure_future(send_email(
        to      = email,
        subject = "أكّد بريدك الإلكتروني — نحلة AI",
        html    = email_verify(body.store_name.strip(), verify_url),
    ))
    logger.info("Verification email queued for %s", email)

    # ── WhatsApp welcome message (fire-and-forget) ─────────────────────────────
    if body.phone:
        asyncio.ensure_future(notify_welcome(body.phone.strip(), body.store_name.strip()))

    token = create_token(email=email, role="merchant", tenant_id=tenant.id, user_id=user.id)
    return {
        "access_token":   token,
        "token_type":     "bearer",
        "role":           "merchant",
        "tenant_id":      tenant.id,
        "user_id":        user.id,
        "email_verified": False,
    }


@router.get("/auth/verify-email")
async def verify_email(token: str, db: Session = Depends(get_db)):
    """Verify a merchant's email address via signed token link."""
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    payload = decode_token(token)
    if not payload or payload.get("type") != "verify_email":
        return RedirectResponse(
            url=f"{DASHBOARD_URL}/verify-email?status=invalid",
            status_code=302,
        )
    email = payload.get("sub", "")
    user  = db.query(User).filter(User.email == email).first()
    if not user:
        return RedirectResponse(
            url=f"{DASHBOARD_URL}/verify-email?status=not_found",
            status_code=302,
        )
    # Mark verified (column added by start.sh migration)
    try:
        db.execute(
            sqlalchemy.text("UPDATE users SET email_verified=true WHERE email=:e"),
            {"e": email},
        )
        db.commit()
    except Exception:
        db.rollback()

    audit("email_verified", sub=email)

    # Send welcome email now that verification is confirmed (fire-and-forget)
    store_name = user.tenant.name if user.tenant else email.split("@")[0]
    asyncio.ensure_future(send_email(
        to      = email,
        subject = "مرحباً بك في نحلة AI 🎉",
        html    = email_welcome(store_name, DASHBOARD_URL),
    ))
    logger.info("Email verified: %s — welcome email queued", email)
    return RedirectResponse(
        url=f"{DASHBOARD_URL}/verify-email?status=success",
        status_code=302,
    )


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordIn, db: Session = Depends(get_db)):
    """Send a password reset link to the given email if it exists."""
    email = body.email.strip().lower()
    user  = db.query(User).filter(User.email == email, User.is_active == True).first()  # noqa: E712
    # Always return 200 to prevent email enumeration
    if user:
        reset_token = create_reset_token(email)
        reset_url   = f"{DASHBOARD_URL}/reset-password?token={reset_token}"
        asyncio.ensure_future(send_email(
            to      = email,
            subject = "إعادة تعيين كلمة المرور — نحلة AI",
            html    = email_reset(reset_url),
        ))
        audit("password_reset_requested", sub=email)
        logger.info("Password reset email queued for %s", email)
    return {"detail": "إذا كان البريد مسجَّلاً ستصلك رسالة قريباً"}


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordIn, db: Session = Depends(get_db)):
    """Reset password using a signed token from the email link."""
    if not JWT_AVAILABLE or not BCRYPT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    payload = decode_token(body.token)
    if not payload or payload.get("type") != "password_reset":
        raise HTTPException(status_code=400, detail="الرابط غير صالح أو منتهي الصلاحية")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تكون 8 أحرف على الأقل")
    email = payload.get("sub", "")
    user  = db.query(User).filter(User.email == email, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=404, detail="الحساب غير موجود")
    user.password_hash = hash_password(body.password)
    db.commit()
    audit("password_reset_done", sub=email)
    logger.info("Password reset completed for %s", email)
    return {"detail": "تم تغيير كلمة المرور بنجاح"}
