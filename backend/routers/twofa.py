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
    get_current_user,
    verify_password,
)
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


def _require_user_id(user: Dict[str, Any]) -> int:
    """JWT user payload → DB user.id. 401 if claim is missing."""
    uid = user.get("user_id")
    if not uid:
        raise HTTPException(
            status_code=401,
            detail="هذه العملية تحتاج حساب مستخدم مرتبط بسجل في قاعدة البيانات.",
        )
    try:
        return int(uid)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="معرف المستخدم في التوكن غير صالح.") from exc


def _load_db_user(db: Session, user_id: int) -> User:
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
    return row


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
    user_id = _require_user_id(user)
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
    user_id = _require_user_id(user)

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
    user_id = _require_user_id(user)

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
    user_id = _require_user_id(user)

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

    if not db_user.password_hash or not verify_password(body.password, db_user.password_hash):
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
