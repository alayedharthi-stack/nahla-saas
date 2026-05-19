"""
routers/twofa.py
────────────────
Phase 2A Sprint 1 — TOTP enrolment, confirmation, status, and disable.

Endpoints
─────────
* GET  /auth/2fa/status                 — `{ enabled, enrolled_at }`
* POST /auth/2fa/setup/start            — generate secret + provisioning URI
* POST /auth/2fa/setup/confirm          — verify first OTP, persist secret +
                                          recovery codes (shown ONCE)
* POST /auth/2fa/disable                — wipe 2FA after re-auth

Out of scope for Sprint 1 (planned in 2A Sprint 2/3):
─────────────────────────────────────────────────────
* /auth/2fa/verify     — login challenge flow (Sprint 2)
* /auth/2fa/recovery/regenerate (step-up gated)
* user_sessions table + logout-all
* MFA enforcement middleware on admin routes

Why the "setup_token" handoff
──────────────────────────────
``setup/start`` returns a short-lived (10 min) JWT of ``type=2fa_setup``
that carries the freshly-generated TOTP secret. The DB row is created
only on ``setup/confirm`` AFTER the user proves they can read the QR.
Benefits:
* No half-finished enrolments littering the DB.
* User can re-scan / re-open the page within 10 min without a second
  random secret being issued.
* The setup token is signed by ``JWT_SECRET`` so the user cannot
  tamper with the secret it carries.
"""
from __future__ import annotations

import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.audit import audit
from core.auth import (
    JWT_AVAILABLE,
    JWT_ALGORITHM,
    JWT_SECRET,
    PLATFORM_ADMIN_ROLES,
    get_current_user,
    verify_password,
)
from core.config import ADMIN_EMAIL, ADMIN_PASSWORD
from core.database import get_db
from core.rate_limit import check_rate_limit_or_429
from core.totp_crypto import (
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    generate_secret_b32,
    hash_recovery_code,
    totp_provisioning_uri,
    verify_recovery_code,
    verify_totp,
)
from models import User, UserRecoveryCode, UserTotp

logger = logging.getLogger("nahla.twofa")

router = APIRouter(prefix="/auth/2fa", tags=["auth-2fa"])


# ── Helpers ────────────────────────────────────────────────────────────────────
def _client_ip(request: Request) -> str:
    return (
        request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )


def _is_env_admin_payload(user: Dict[str, Any]) -> bool:
    """
    True when the caller is logged in via the env-var admin fallback in
    ``routers/auth.py`` (no ``user_id`` claim, role in PLATFORM_ADMIN_ROLES,
    sub == ADMIN_EMAIL).

    Why this case is special
    ────────────────────────
    The env-admin path was originally a *credentials-only* login: it never
    inserts a row in ``users``, so the JWT carries no ``user_id``. Every
    account-level feature (2FA, recovery codes, …) is keyed by ``user_id``
    so without auto-provisioning, the very first 2FA request would either
    401 (no claim) or 500 (FK violation). For 2FA this is unacceptable —
    the platform owner must be able to enrol like any other user.
    """
    sub = str(user.get("sub") or "").strip().lower()
    role = str(user.get("role") or "").strip()
    return (
        not user.get("user_id")
        and role in PLATFORM_ADMIN_ROLES
        and sub == ADMIN_EMAIL.strip().lower()
    )


def _provision_env_admin_user(db: Session) -> User:
    """
    Find-or-create a ``users`` row for the env-fallback platform admin so
    every per-user feature (2FA, recovery codes, audit links, …) has a
    real foreign key to attach to.

    The row is created with ``password_hash = NULL`` because the canonical
    password lives in the ``ADMIN_PASSWORD`` env var, NOT in the DB. The
    ``/disable`` endpoint below special-cases this and verifies against the
    env var via constant-time compare when ``password_hash`` is NULL and
    the caller is the env admin.

    Idempotent: returns the existing row when one already exists.
    """
    email = ADMIN_EMAIL.strip().lower()
    row = db.query(User).filter(User.email == email).first()
    if row is not None:
        return row

    # Username must be unique → derive from email local-part with a
    # collision-resistant suffix only when needed.
    base_username = (email.split("@", 1)[0] or "admin") + "-platform-admin"
    username = base_username
    suffix = 0
    while db.query(User).filter(User.username == username).first() is not None:
        suffix += 1
        username = f"{base_username}-{suffix}"

    row = User(
        username=username,
        email=email,
        password_hash=None,           # password lives in ADMIN_PASSWORD env var
        role="admin",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        tenant_id=1,                  # platform tenant — matches issued JWT
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except Exception:                 # noqa: BLE001
        db.rollback()
        # Re-fetch in case a concurrent request won the race.
        row = db.query(User).filter(User.email == email).first()
        if row is None:
            raise
    audit("2fa.env_admin_provisioned", user_id=row.id, sub=email)
    return row


def _resolve_user_id(user: Dict[str, Any], db: Session) -> int:
    """
    JWT user payload → DB ``users.id``.

    Resolution order:
    1. ``user_id`` claim present → use it directly (normal merchant /
       support / owner-with-DB-row case).
    2. Env-admin fallback (no claim, sub == ADMIN_EMAIL, role in
       PLATFORM_ADMIN_ROLES) → find-or-create a real row and use that id.
    3. Otherwise → 401 with a clear message (a token without ``user_id``
       cannot enrol in 2FA; this only happens for legacy tokens).
    """
    uid = user.get("user_id")
    if uid:
        try:
            return int(uid)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="معرف المستخدم في التوكن غير صالح.",
            ) from exc

    if _is_env_admin_payload(user):
        row = _provision_env_admin_user(db)
        return int(row.id)

    raise HTTPException(
        status_code=401,
        detail="هذه العملية تحتاج حساب مستخدم مرتبط بسجل في قاعدة البيانات.",
    )


def _load_db_user(db: Session, user_id: int) -> User:
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
    return row


def _verify_account_password(db_user: User, supplied: str) -> bool:
    """
    Verify the account password for /disable.

    Two cases:
    * Normal users: hash is in ``users.password_hash`` → bcrypt verify.
    * Env-admin auto-provisioned row: ``password_hash`` is NULL because
      the canonical secret lives in the ``ADMIN_PASSWORD`` env var.
      Compare via :func:`hmac.compare_digest` (constant-time).
    """
    if db_user.password_hash:
        return bool(verify_password(supplied, db_user.password_hash))
    is_env_admin = (
        db_user.email.lower() == ADMIN_EMAIL.strip().lower()
        and db_user.role in PLATFORM_ADMIN_ROLES
    )
    if is_env_admin and ADMIN_PASSWORD:
        return hmac.compare_digest(supplied, ADMIN_PASSWORD)
    return False


def _is_enabled(totp_row: Optional[UserTotp]) -> bool:
    return bool(totp_row and totp_row.confirmed_at)


def _make_setup_token(*, user_id: int, secret_b32: str) -> str:
    """
    Short-lived JWT carrying the pending TOTP secret. Lives only between
    /setup/start and /setup/confirm. ``type=2fa_setup`` so it can never
    be confused with a session token by the middleware.
    """
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT layer غير متاح.")
    from jose import jwt as _jwt  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    payload = {
        "type":       "2fa_setup",
        "user_id":    int(user_id),
        "secret_b32": secret_b32,
        "iat":        int(now.timestamp()),
        "exp":        int((now + timedelta(minutes=10)).timestamp()),
    }
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_setup_token(token: str, *, expected_user_id: int) -> str:
    """Return the base32 secret. Raises 400/401 on any tamper / expiry / mismatch."""
    if not JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="JWT layer غير متاح.")
    from jose import jwt as _jwt, JWTError  # noqa: PLC0415

    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="انتهت صلاحية جلسة إعداد التحقق بخطوتين. ابدأ من جديد.",
        ) from exc

    if payload.get("type") != "2fa_setup":
        raise HTTPException(status_code=400, detail="setup_token غير صالح (نوع خاطئ).")
    if int(payload.get("user_id") or 0) != int(expected_user_id):
        raise HTTPException(status_code=400, detail="setup_token لا يخص هذا المستخدم.")
    secret = payload.get("secret_b32")
    if not secret or not isinstance(secret, str):
        raise HTTPException(status_code=400, detail="setup_token لا يحمل سراً صالحاً.")
    return secret


# ── GET /auth/2fa/status ──────────────────────────────────────────────────────
@router.get("/status")
async def two_factor_status(
    user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Cheap read used by the dashboard to render the right control state."""
    user_id = _resolve_user_id(user, db)
    row = db.query(UserTotp).filter(UserTotp.user_id == user_id).first()
    enabled = _is_enabled(row)
    return {
        "enabled":       enabled,
        "enrolled_at":   row.confirmed_at.isoformat() if (row and row.confirmed_at) else None,
        "last_used_at":  row.last_used_at.isoformat() if (row and row.last_used_at) else None,
        # Recovery codes count helps the UI nudge "regenerate" when low.
        "recovery_codes_remaining": (
            db.query(UserRecoveryCode)
              .filter(UserRecoveryCode.user_id == user_id, UserRecoveryCode.used_at.is_(None))
              .count()
            if enabled else 0
        ),
    }


# ── POST /auth/2fa/setup/start ─────────────────────────────────────────────────
@router.post("/setup/start")
async def two_factor_setup_start(
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Generate a fresh TOTP secret + provisioning URI. NOTHING is written
    to the DB yet — the secret travels back to the user inside a signed
    ``setup_token`` they MUST submit with the first OTP in /setup/confirm.
    """
    user_id = _resolve_user_id(user, db)

    # Per-user rate limit: 5 starts / hour. Prevents abuse generating
    # endless QR codes / spamming the dashboard with fresh secrets.
    check_rate_limit_or_429(
        bucket="2fa_setup_start",
        key=str(user_id),
        max_count=5,
        window_seconds=60 * 60,
        audit_metadata={"path": "/auth/2fa/setup/start", "user_id": user_id, "ip": _client_ip(request)},
    )

    db_user = _load_db_user(db, user_id)
    existing = db.query(UserTotp).filter(UserTotp.user_id == user_id).first()
    if _is_enabled(existing):
        raise HTTPException(
            status_code=409,
            detail="التحقق بخطوتين مفعّل مسبقاً. عطّله أولاً قبل إعداد سر جديد.",
        )

    secret = generate_secret_b32()
    uri = totp_provisioning_uri(db_user.email, secret)
    setup_token = _make_setup_token(user_id=user_id, secret_b32=secret)

    audit(
        "2fa.setup_started",
        user_id=user_id,
        sub=db_user.email,
        ip=_client_ip(request),
    )

    return {
        "setup_token":   setup_token,
        "secret_b32":    secret,        # shown next to the QR for users who can't scan
        "otpauth_url":   uri,
        "issuer":        "Nahla AI",
        "account":       db_user.email,
        "expires_in":    10 * 60,
    }


# ── POST /auth/2fa/setup/confirm ───────────────────────────────────────────────
class ConfirmBody(BaseModel):
    setup_token: str = Field(..., min_length=10)
    otp:         str = Field(..., min_length=6, max_length=8)


@router.post("/setup/confirm")
async def two_factor_setup_confirm(
    body: ConfirmBody,
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Verify the first OTP using the secret carried in ``setup_token``.
    On success: persist ``user_totp`` + 10 bcrypt-hashed recovery codes,
    return the plaintext codes ONCE. Failure leaves nothing behind.
    """
    user_id = _resolve_user_id(user, db)

    # OTP confirmation rate limit — 5 attempts / 15 min per user.
    check_rate_limit_or_429(
        bucket="2fa_setup_confirm",
        key=str(user_id),
        max_count=5,
        window_seconds=15 * 60,
        audit_metadata={"path": "/auth/2fa/setup/confirm", "user_id": user_id, "ip": _client_ip(request)},
    )

    db_user = _load_db_user(db, user_id)
    existing = db.query(UserTotp).filter(UserTotp.user_id == user_id).first()
    if _is_enabled(existing):
        raise HTTPException(status_code=409, detail="التحقق بخطوتين مفعّل مسبقاً.")

    secret = _decode_setup_token(body.setup_token, expected_user_id=user_id)
    if not verify_totp(secret, body.otp):
        audit(
            "2fa.verify_failed",
            user_id=user_id,
            sub=db_user.email,
            ip=_client_ip(request),
            stage="setup_confirm",
        )
        raise HTTPException(
            status_code=400,
            detail="رمز التحقق غير صحيح. تأكد من الوقت في جهازك ثم أعد المحاولة.",
        )

    # ── Persist ──────────────────────────────────────────────────────────────
    plaintext_codes = generate_recovery_codes(10)
    now = datetime.now(timezone.utc)

    if existing is not None:
        existing.secret_enc = encrypt_secret(secret)
        existing.confirmed_at = now
        existing.last_used_at = now
        existing.failed_attempts = 0
        existing.locked_until = None
        existing.updated_at = now
    else:
        db.add(UserTotp(
            user_id=user_id,
            secret_enc=encrypt_secret(secret),
            confirmed_at=now,
            last_used_at=now,
            failed_attempts=0,
            locked_until=None,
            created_at=now,
            updated_at=now,
        ))

    # Replace any prior recovery codes wholesale — the user could have
    # half-set up before and abandoned, leaving stale rows that would
    # otherwise outlive this enrolment.
    db.query(UserRecoveryCode).filter(UserRecoveryCode.user_id == user_id).delete()
    for code in plaintext_codes:
        db.add(UserRecoveryCode(
            user_id=user_id,
            code_hash=hash_recovery_code(code),
            created_at=now,
            used_at=None,
        ))

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("[2fa] DB commit failed on setup confirm user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="تعذّر حفظ إعدادات التحقق بخطوتين.") from exc

    audit(
        "2fa.enabled",
        user_id=user_id,
        sub=db_user.email,
        ip=_client_ip(request),
        recovery_codes_issued=len(plaintext_codes),
    )

    return {
        "enabled":        True,
        "enrolled_at":    now.isoformat(),
        "recovery_codes": plaintext_codes,
        "warning": (
            "احفظ هذه الأكواد الآن — لن تظهر مرة أخرى. "
            "كل كود يُستخدم مرة واحدة فقط للدخول بدون تطبيق المصادقة."
        ),
    }


# ── POST /auth/2fa/disable ─────────────────────────────────────────────────────
class DisableBody(BaseModel):
    password: str = Field(..., min_length=1)
    otp:      str = Field(..., min_length=6, max_length=14)  # OTP or recovery code


@router.post("/disable")
async def two_factor_disable(
    body: DisableBody,
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Wipe the user's TOTP secret + recovery codes. Requires:
      * current password AND
      * a valid OTP from the authenticator OR a valid recovery code.

    Step-up dependency is Sprint 4 — for Sprint 1 we mimic it inline by
    asking for the password again on this endpoint, so the security
    surface is correct even if the dashboard later gets compromised
    by a logged-in but un-trusted device.
    """
    user_id = _resolve_user_id(user, db)

    check_rate_limit_or_429(
        bucket="2fa_disable",
        key=str(user_id),
        max_count=3,
        window_seconds=60 * 60,
        audit_metadata={"path": "/auth/2fa/disable", "user_id": user_id, "ip": _client_ip(request)},
    )

    db_user = _load_db_user(db, user_id)
    row = db.query(UserTotp).filter(UserTotp.user_id == user_id).first()
    if not _is_enabled(row):
        raise HTTPException(status_code=409, detail="التحقق بخطوتين غير مفعّل.")

    if not _verify_account_password(db_user, body.password):
        audit(
            "2fa.disable_failed",
            user_id=user_id, sub=db_user.email, ip=_client_ip(request),
            reason="bad_password",
        )
        raise HTTPException(status_code=401, detail="كلمة المرور غير صحيحة.")

    # Accept either a TOTP code or one of the user's recovery codes.
    secret = decrypt_secret(row.secret_enc)
    otp_ok = verify_totp(secret, body.otp)
    recovery_row = None
    if not otp_ok:
        for rc in db.query(UserRecoveryCode).filter(
            UserRecoveryCode.user_id == user_id,
            UserRecoveryCode.used_at.is_(None),
        ).all():
            if verify_recovery_code(body.otp, rc.code_hash):
                recovery_row = rc
                break

    if not otp_ok and recovery_row is None:
        audit(
            "2fa.disable_failed",
            user_id=user_id, sub=db_user.email, ip=_client_ip(request),
            reason="bad_otp",
        )
        raise HTTPException(status_code=401, detail="رمز التحقق غير صحيح.")

    # ── Wipe ───────────────────────────────────────────────────────────────
    db.query(UserRecoveryCode).filter(UserRecoveryCode.user_id == user_id).delete()
    db.query(UserTotp).filter(UserTotp.user_id == user_id).delete()
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("[2fa] DB commit failed on disable user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="تعذّر تعطيل التحقق بخطوتين.") from exc

    audit(
        "2fa.disabled",
        user_id=user_id, sub=db_user.email, ip=_client_ip(request),
        used_recovery_code=bool(recovery_row),
    )

    return {"enabled": False, "disabled_at": int(time.time())}
