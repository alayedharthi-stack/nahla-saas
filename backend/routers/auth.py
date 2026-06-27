"""
routers/auth.py
───────────────
Authentication endpoints — JWT login, registration, invite flow, and password reset.

Routes:
  POST /auth/login                  — exchange email + password for a JWT
  GET  /auth/me                     — return identity of the authenticated caller
  POST /auth/logout                 — client-side logout acknowledgement
  GET  /auth/invite/{token}         — validate an invitation token
  POST /auth/register               — register a new merchant (invite-gated in production)
  GET  /auth/verify-email           — verify email address via signed link
  POST /auth/forgot-password        — request a password-reset email
  POST /auth/reset-password         — apply a new password using a reset token
  GET  /auth/set-password/verify    — validate a single-use set-password token (no consume)
  POST /auth/set-password           — consume a set-password token + set local password

Security notes:
  • Admin credentials are compared with hmac.compare_digest (timing-safe).
  • Merchant passwords are verified via bcrypt (core/auth.verify_password).
  • /auth/forgot-password always returns 200 to prevent email enumeration.
  • Reset tokens are signed JWTs (legacy, NOT single-use).
  • Set-password tokens are DB-backed, hashed, single-use — see core.password_setup.
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
    JWT_ALGORITHM,
    JWT_SECRET,
    create_reset_token,
    create_token,
    create_verify_token,
    decode_token,
    decode_token_for_refresh,
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
from core.notifications import email_reset, email_verify, send_email
from core.password_setup import (
    ExpiredToken,
    InvalidToken,
    UsedToken,
    WeakPassword,
    consume_token as consume_set_password_token,
    verify_token as verify_set_password_token,
)
from core.rate_limit import check_rate_limit_or_429, hash_email
from core.token_revocation import revoke_jti
from core.wa_notify import notify_welcome


# ── 2FA login gate ───────────────────────────────────────────────────────────
# A successful password verify no longer issues an access_token directly
# when the user has 2FA enabled. Instead we issue a short-lived (5 min)
# JWT of `type=2fa_challenge` carrying the would-be session claims, and
# the dashboard must exchange it via POST /auth/2fa/login/verify by
# proving possession of the TOTP code or a recovery code.

_CHALLENGE_TYPE = "2fa_challenge"
_CHALLENGE_TTL_SEC = 5 * 60


def _make_2fa_challenge_token(
    *,
    user_id: int | None,
    email: str,
    role: str,
    tenant_id: int,
) -> str:
    """Sign a 5-minute JWT that carries the pending session claims.

    The dashboard CANNOT use this token against any normal endpoint —
    `get_current_user` rejects anything whose `type` claim is set
    (only access tokens omit `type` entirely). Its only valid use is
    `/auth/2fa/login/verify`, which decodes it, verifies the TOTP,
    then mints the real access_token.
    """
    from datetime import timedelta  # noqa: PLC0415
    from jose import jwt as _jwt  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "type":      _CHALLENGE_TYPE,
        "sub":       email,
        "role":      role,
        "tenant_id": int(tenant_id),
        "iat":       int(now.timestamp()),
        "exp":       int((now + timedelta(seconds=_CHALLENGE_TTL_SEC)).timestamp()),
    }
    if user_id is not None:
        payload["user_id"] = int(user_id)
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _user_has_2fa_enabled(db: Session, user_id: int | None) -> bool:
    """Best-effort lookup; soft-fail to False on any error.

    We never want a flaky DB read on a half-deployed environment to
    block a login completely. A missing user_totp table (pre-migration)
    or a transient DB blip returns False, which means the user logs in
    as if 2FA were off. Once the migration is in place and a row
    exists with confirmed_at NOT NULL, the gate kicks in.
    """
    if user_id is None:
        return False
    try:
        from models import UserTotp  # noqa: PLC0415

        row = db.query(UserTotp).filter(UserTotp.user_id == int(user_id)).first()
        return bool(row and getattr(row, "confirmed_at", None) is not None
                    and getattr(row, "secret_enc", None))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("nahla-backend").warning(
            "[auth/login] 2FA lookup failed user_id=%s err=%s — defaulting to disabled",
            user_id, exc,
        )
        return False


def _client_ip(request: Request) -> str:
    """First-of-trust IP for rate-limit keys. Mirrors core.auth.get_client_ip."""
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


def _enforce_login_rate_limits(request: Request, email: str) -> None:
    """Per-IP + per-email caps on /auth/login* — Phase 1A.

    * IP:    5 attempts / 15 minutes  — blocks distributed brute force
    * Email: 10 attempts / 1 hour     — blocks targeted credential stuffing

    The ``Retry-After`` header is set by ``check_rate_limit_or_429``.
    """
    ip = _client_ip(request)
    check_rate_limit_or_429(
        bucket="login_ip",
        key=ip,
        max_count=5,
        window_seconds=15 * 60,
        audit_metadata={"path": "/auth/login", "ip": ip},
    )
    if email:
        check_rate_limit_or_429(
            bucket="login_email",
            key=hash_email(email),
            max_count=10,
            window_seconds=60 * 60,
            audit_metadata={"path": "/auth/login", "email_hash": hash_email(email), "ip": ip},
        )


def _enforce_forgot_password_rate_limits(request: Request, email: str) -> None:
    """Per-IP + per-email caps on /auth/forgot-password — Phase 1A."""
    ip = _client_ip(request)
    check_rate_limit_or_429(
        bucket="forgot_ip",
        key=ip,
        max_count=10,
        window_seconds=60 * 60,
        audit_metadata={"path": "/auth/forgot-password", "ip": ip},
    )
    if email:
        check_rate_limit_or_429(
            bucket="forgot_email",
            key=hash_email(email),
            max_count=3,
            window_seconds=60 * 60,
            audit_metadata={"path": "/auth/forgot-password", "email_hash": hash_email(email), "ip": ip},
        )


def _enforce_reset_password_rate_limits(request: Request) -> None:
    """Per-IP cap on /auth/reset-password — Phase 1A."""
    ip = _client_ip(request)
    check_rate_limit_or_429(
        bucket="reset_ip",
        key=ip,
        max_count=5,
        window_seconds=60 * 60,
        audit_metadata={"path": "/auth/reset-password", "ip": ip},
    )


def _enforce_set_password_rate_limits(request: Request, *, bucket_suffix: str) -> None:
    """Per-IP cap on /auth/set-password*. Same envelope as reset-password.

    The set-password verify+consume pair runs on the same IP, so we keep
    the bucket size sized for "human clicks email link, types password,
    submits, retries on typo a few times". 10/hour is generous enough
    for legitimate use and tight enough that a token-guessing attacker
    is forced to spread across IPs (where Cloudflare bot rules apply).
    """
    ip = _client_ip(request)
    check_rate_limit_or_429(
        bucket=f"setpw_{bucket_suffix}_ip",
        key=ip,
        max_count=10,
        window_seconds=60 * 60,
        audit_metadata={"path": f"/auth/set-password/{bucket_suffix}", "ip": ip},
    )

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


class SetPasswordIn(BaseModel):
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


def _auth_login_impl(
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

    # Phase 1A: per-IP + per-email rate limits — applied BEFORE any DB
    # work so a credential-stuffing burst never reaches bcrypt.
    # ``check_rate_limit_or_429`` raises HTTPException(429) on
    # violation; all the work below only runs when we're under the cap.
    _enforce_login_rate_limits(request, email)

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
            # bcrypt is CPU-bound (~50–200 ms). This handler is a normal ``def``
            # route so Starlette runs it in a thread pool — sync DB + bcrypt do
            # NOT block the asyncio event loop (unlike running them inside
            # ``async def``, which would freeze ping/webhooks under DB stalls).
            if verify_password(raw_password, user.password_hash):
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

                # ── 2FA gate (merchant path) ───────────────────────────────
                # If this user has confirmed 2FA, we DO NOT issue a
                # session token. We issue a 5-minute challenge token
                # that the dashboard must redeem at /auth/2fa/login/verify.
                if _user_has_2fa_enabled(db, user.id):
                    challenge = _make_2fa_challenge_token(
                        user_id=user.id,
                        email=user.email,
                        role=role,
                        tenant_id=tenant_id,
                    )
                    audit(
                        "login_2fa_required",
                        role=role, sub=user.email, tenant_id=tenant_id, ip=client_ip,
                    )
                    _ms = int((_time.monotonic() - _t0) * 1000)
                    logger.info(
                        "[AUTH LOGIN] 2FA challenge issued email=%s role=%s tenant_id=%s ms=%s",
                        user.email, role, tenant_id, _ms,
                    )
                    return {
                        "requires_2fa":     True,
                        "challenge_token":  challenge,
                        "challenge_ttl":    _CHALLENGE_TTL_SEC,
                        "email":            user.email,
                        # Role/tenant_id deliberately NOT returned here —
                        # the dashboard learns them only after a successful
                        # verify. Email is fine (the user just typed it).
                    }

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
        # ── 2FA gate (admin path) ──────────────────────────────────────────
        # The env admin may also have enrolled 2FA via the dashboard's
        # security settings. /auth/2fa/setup/confirm calls
        # _provision_env_admin_user() which materialises a real User row
        # with the admin email, so we can look up UserTotp by that user
        # without having to hard-code a magic admin id.
        admin_user_id: int | None = None
        try:
            admin_user = (
                db.query(User)
                  .filter(User.email == ADMIN_EMAIL.lower())
                  .first()
            )
            admin_user_id = int(admin_user.id) if admin_user else None
        except Exception:  # noqa: BLE001
            admin_user_id = None

        if _user_has_2fa_enabled(db, admin_user_id):
            challenge = _make_2fa_challenge_token(
                user_id=admin_user_id,
                email=ADMIN_EMAIL,
                role="admin",
                tenant_id=1,
            )
            audit("login_2fa_required", role="admin", sub=ADMIN_EMAIL, tenant_id=1, ip=client_ip)
            _ms = int((_time.monotonic() - _t0) * 1000)
            logger.info(
                "[AUTH LOGIN] 2FA challenge issued admin email=%s ms=%s",
                ADMIN_EMAIL, _ms,
            )
            return {
                "requires_2fa":     True,
                "challenge_token":  challenge,
                "challenge_ttl":    _CHALLENGE_TTL_SEC,
                "email":            ADMIN_EMAIL,
            }

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
def auth_login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    """JSON login (preflight-required). See _auth_login_impl docstring."""
    return _auth_login_impl(
        raw_email=body.email,
        raw_password=body.password,
        request=request,
        db=db,
        transport="json",
    )


@router.post("/auth/login-form")
def auth_login_form(
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
    return _auth_login_impl(
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


@router.post("/auth/session/refresh")
async def auth_session_refresh(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Rolling session refresh for the merchant dashboard / PWA.

    Accepts the current (possibly recently-expired) session JWT and
    returns a fresh access_token when the signature is valid, the token
    is not revoked, and the user account is still active.
    """
    auth_header = request.headers.get("Authorization") or ""
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail={"code": "missing_token", "message": "Authentication required"})

    payload = decode_token_for_refresh(token)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": "invalid_token", "message": "Invalid or expired token"})

    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail={"code": "invalid_token", "message": "Invalid token subject"})

    db_user = db.query(User).filter(User.email == email).first()
    if not db_user or not db_user.is_active:
        raise HTTPException(status_code=401, detail={"code": "invalid_token", "message": "Account inactive"})

    tenant_id = int(payload.get("tenant_id") or db_user.tenant_id or 0)
    if not tenant_id:
        raise HTTPException(status_code=401, detail={"code": "no_tenant_claim", "message": "Missing tenant scope"})

    if db_user.tenant_id != tenant_id:
        raise HTTPException(status_code=401, detail={"code": "invalid_token", "message": "Tenant scope mismatch"})

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=401, detail={"code": "invalid_token", "message": "Tenant not found"})

    role = str(payload.get("role") or db_user.role or "merchant")
    fresh = create_token(
        email=email,
        role=role,
        tenant_id=tenant_id,
        user_id=int(db_user.id),
        extra_claims={
            k: payload[k]
            for k in ("impersonation", "actor_sub", "actor_user_id", "session_version")
            if k in payload
        } or None,
    )
    return {
        "access_token": fresh,
        "role":         role,
        "tenant_id":    tenant_id,
        "user_id":      db_user.id,
        "email":        email,
    }


@router.post("/auth/logout")
async def auth_logout(request: Request, user: Dict[str, Any] = Depends(get_current_user)):
    """
    Revoke the caller's JWT by adding its ``jti`` to the Redis revocation
    list (TTL = remaining ``exp``). Subsequent ``decode_token`` calls
    return ``None`` for the revoked token, so middleware rejects it
    with HTTP 401.

    Phase 1A: this is the only revocation surface; Phase 2 introduces
    refresh-token rotation which deprecates the denylist for normal
    sessions and lets us drop access-token TTL to 15 minutes.
    """
    jti = user.get("jti")
    exp = user.get("exp")
    if jti and exp:
        revoke_jti(str(jti), int(exp))
    audit(
        "logout",
        sub=user.get("sub"),
        role=user.get("role"),
        tenant_id=user.get("tenant_id"),
        ip=_client_ip(request),
        jti=jti,
    )
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
    from core.trial_lifecycle import init_new_tenant_trial_state  # noqa: PLC0415
    init_new_tenant_trial_state(tenant)
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

    already_verified = bool(getattr(user, "email_verified", False))
    if not already_verified:
        user.email_verified = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            return RedirectResponse(
                url=f"{DASHBOARD_URL}/verify-email?status=invalid",
                status_code=302,
            )
        audit("email_verified", sub=email)

    from core.direct_welcome_email import (  # noqa: PLC0415
        get_notification_settings,
        queue_direct_welcome_email,
        welcome_email_already_sent,
    )

    notification_settings = get_notification_settings(db, user.tenant_id)
    if welcome_email_already_sent(notification_settings):
        audit("email_verify_repeat", sub=email)
        logger.info("Email already verified: %s — welcome already sent", email)
    else:
        store_name = user.tenant.name if user.tenant else email.split("@")[0]
        queue_direct_welcome_email(
            email=email,
            store_name=store_name,
            dashboard_url=DASHBOARD_URL,
            tenant_id=user.tenant_id,
        )
        logger.info("Email verified: %s — welcome email queued", email)

    return RedirectResponse(
        url=f"{DASHBOARD_URL}/verify-email?status=success",
        status_code=302,
    )


@router.post("/auth/forgot-password")
async def forgot_password(
    body: ForgotPasswordIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Send a password reset link to the given email if it exists."""
    email = body.email.strip().lower()
    # Phase 1A rate limits: 3/hour per email + 10/hour per IP.
    _enforce_forgot_password_rate_limits(request, email)
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
async def reset_password(
    body: ResetPasswordIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Reset password using a signed token from the email link."""
    # Phase 1A rate limit: 5 attempts / hour per IP.
    _enforce_reset_password_rate_limits(request)
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


# ── Set-password (single-use, DB-backed token) ────────────────────────────────
#
# This pair of endpoints powers the "أهلاً بك في نحلة" welcome email sent
# on Salla / Zid auto-create. The merchant clicks /set-password?token=...
# in the dashboard, the page calls verify, then submits the new password
# which calls set-password proper. The token is single-use and stored as
# SHA-256 hash — see core.password_setup for the security model.
#
# Decoupled from /auth/forgot-password on purpose:
#   • forgot-password issues a JWT (not single-use) — Phase 1A legacy
#   • set-password issues a DB row (single-use, hashed)             — new
# This separation means an OAuth-issued welcome link can never be
# replayed by an attacker who later compromises the merchant's email
# account, even if they re-trigger forgot-password — different secret
# space, different revocation surface.

@router.get("/auth/set-password/verify")
async def set_password_verify(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Validate a set-password token without consuming it.

    Used by the dashboard set-password page to render a friendly
    "Set password for foo@bar.com" form before the user types. We
    return the email so the page can echo it back; we never return
    anything that could be used to authenticate.

    Failure modes are surfaced as ``{"valid": false, "reason": "..."}``
    with HTTP 200 — the page renders a tailored error UI per reason.
    """
    _enforce_set_password_rate_limits(request, bucket_suffix="verify")
    raw = (token or "").strip()
    if not raw:
        return {"valid": False, "reason": "missing"}

    row = verify_set_password_token(db, raw)
    if row is None:
        # Distinguish expired/used/missing for the UI by looking up the
        # row directly (verify_set_password_token returns None for all
        # three).
        from models import PasswordSetupToken  # noqa: PLC0415
        import hashlib  # noqa: PLC0415
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        existing = (
            db.query(PasswordSetupToken)
            .filter(PasswordSetupToken.token_hash == h)
            .first()
        )
        if existing is None:
            return {"valid": False, "reason": "invalid"}
        if existing.used_at is not None:
            return {"valid": False, "reason": "used"}
        return {"valid": False, "reason": "expired"}

    user = db.query(User).filter(User.id == row.user_id).first()
    if user is None or not user.is_active:
        return {"valid": False, "reason": "invalid"}

    return {
        "valid":      True,
        "email":      user.email,
        "purpose":    row.purpose,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


@router.post("/auth/set-password")
async def set_password(
    body: SetPasswordIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Consume a set-password token and apply the merchant's new password.

    On success the local password is set and the token is invalidated
    in a single DB transaction (see core.password_setup.consume_token).
    The response intentionally does not issue a session JWT — the
    merchant is sent to /login to sign in normally, which proves they
    actually know the password they just set.
    """
    if not BCRYPT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    _enforce_set_password_rate_limits(request, bucket_suffix="apply")
    ip = _client_ip(request)

    try:
        user = consume_set_password_token(
            db,
            (body.token or "").strip(),
            body.password,
            ip=ip,
        )
    except WeakPassword as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "كلمة المرور ضعيفة")
    except UsedToken:
        raise HTTPException(status_code=410, detail="الرابط مستخدم من قبل")
    except ExpiredToken:
        raise HTTPException(status_code=410, detail="الرابط منتهي الصلاحية")
    except InvalidToken:
        raise HTTPException(status_code=400, detail="الرابط غير صالح")

    audit("password_setup_token_consumed", sub=user.email, ip=ip)
    logger.info("[set-password] consumed | user_id=%s email=%s", user.id, user.email)
    return {"detail": "تم تعيين كلمة المرور بنجاح", "email": user.email}
