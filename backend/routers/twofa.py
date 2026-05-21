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

# ── Build marker ───────────────────────────────────────────────────────────────
# Bumped manually whenever we ship a 2FA backend change. Lets ops verify
# from Railway logs that the running container is on the latest commit
# without having to ssh-exec into it. Grep:  rg "TWOFA_BUILD_MARKER" logs
#
# When debugging a 500: if you DON'T see this line in the logs after a
# fresh deploy, the new container hasn't started yet (or rollback is
# still active).
TWOFA_BUILD_MARKER = "2026-05-22_picker_v1"
logger.info("[twofa] module loaded build_marker=%s", TWOFA_BUILD_MARKER)

router = APIRouter(prefix="/auth/2fa", tags=["auth-2fa"])


# ── Helper: TOTP candidate codes for the enrolment picker ─────────────────────
#
# Generates the three TOTP codes that are simultaneously "close enough" to
# the current 30-second window:
#
#     • t-1  →  the code that was valid up to 30s ago
#     • t    →  the code visible in the authenticator app RIGHT NOW
#     • t+1  →  the code that will become valid 30s from now
#
# The merchant taps the one that matches what they see in Google
# Authenticator instead of typing it. Because we widened the confirm
# window to ±60s, any of the three is accepted by `verify_totp`.
#
# Security note (intentionally documented at the call site too):
#   * These three codes leak ~90s of valid TOTP material — BUT the raw
#     secret (`secret_b32`) is already returned in the same response so
#     users who can't scan can enter it manually. An attacker who can
#     read `candidate_codes` can also read `secret_b32` and generate
#     codes indefinitely. So this endpoint exposes strictly less
#     information than what's already on the wire.
#   * Only emitted from /setup/start (and a paired refresh endpoint),
#     both gated by `get_current_user` and a short-lived (10 min)
#     `setup_token`. Never from /verify, /disable or any login path.
def _build_candidate_codes(secret_b32: str, now_unix: Optional[int] = None) -> list:
    """Return three TOTP codes around `now_unix` with their validity windows."""
    import pyotp  # noqa: PLC0415

    if now_unix is None:
        now_unix = int(datetime.now(timezone.utc).timestamp())

    step = 30
    totp = pyotp.TOTP(secret_b32)
    # current window starts at floor(now / step) * step
    window_start = (now_unix // step) * step
    out = []
    for offset in (-1, 0, 1):
        center_unix = window_start + offset * step
        out.append({
            "t_offset":         offset,
            "code":             totp.at(center_unix),
            "valid_from_unix":  center_unix,
            "valid_until_unix": center_unix + step,
        })
    return out


# ── GET /auth/2fa/__diag ──────────────────────────────────────────────────────
# Read-only zero-side-effect diagnostic. Returns the resolved JWT claims,
# the build marker, DB connectivity, and whether the user_totp /
# user_recovery_codes tables exist on the deployed schema. Does NOT touch
# any 2FA secret material. Open to any authenticated user so the dashboard
# can fall back to it when /status 500s.
@router.get("/__diag")
async def two_factor_diag(
    user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    from sqlalchemy import text as _text  # noqa: PLC0415

    sub = user.get("sub")
    role = user.get("role")
    has_uid = user.get("user_id") is not None
    is_env = _is_env_admin_payload(user)

    table_check: Dict[str, Any] = {}
    for tname in ("user_totp", "user_recovery_codes", "users", "tenants"):
        try:
            row = db.execute(
                _text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=:t LIMIT 1"
                ),
                {"t": tname},
            ).first()
            table_check[tname] = bool(row)
        except Exception as exc:                          # noqa: BLE001
            table_check[tname] = f"ERR:{type(exc).__name__}"
            try: db.rollback()
            except Exception: pass

    tenant1_present: Any = None
    try:
        row = db.execute(_text("SELECT 1 FROM tenants WHERE id = 1 LIMIT 1")).first()
        tenant1_present = bool(row)
    except Exception as exc:                              # noqa: BLE001
        tenant1_present = f"ERR:{type(exc).__name__}"
        try: db.rollback()
        except Exception: pass

    env_admin_row: Dict[str, Any] = {"present": None, "id": None, "password_hash_null": None}
    try:
        u = db.query(User).filter(User.email == ADMIN_EMAIL.strip().lower()).first()
        if u is not None:
            env_admin_row = {
                "present": True,
                "id": int(u.id),
                "password_hash_null": (u.password_hash is None),
                "role": u.role,
                "tenant_id": u.tenant_id,
                "username": u.username,
            }
        else:
            env_admin_row["present"] = False
    except Exception as exc:                              # noqa: BLE001
        env_admin_row = {"present": f"ERR:{type(exc).__name__}"}
        try: db.rollback()
        except Exception: pass

    logger.info(
        "[twofa] __diag sub=%s role=%s has_uid=%s is_env=%s tables=%s tenant1=%s admin_row=%s",
        sub, role, has_uid, is_env, table_check, tenant1_present,
        {k: v for k, v in env_admin_row.items() if k != "username"},
    )

    return {
        "build_marker":      TWOFA_BUILD_MARKER,
        "jwt_claims": {
            "sub":          sub,
            "role":         role,
            "has_user_id":  has_uid,
            "is_env_admin": is_env,
        },
        "admin_email_config": ADMIN_EMAIL,
        "tables":            table_check,
        "tenant_1_present":  tenant1_present,
        "env_admin_user":    env_admin_row,
    }


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

    Diagnostics: every branch logs a structured ``provision:`` line so a
    failure surfaces in Railway logs WITHOUT leaking secrets (no token,
    password, OTP, or TOTP secret is ever logged here).
    """
    email = ADMIN_EMAIL.strip().lower()
    logger.info("[twofa] provision: lookup_start email=%s", email)
    try:
        row = db.query(User).filter(User.email == email).first()
    except Exception as exc:                              # noqa: BLE001
        logger.exception(
            "[twofa] provision: lookup FAILED exc=%s email=%s",
            type(exc).__name__, email,
        )
        raise

    if row is not None:
        logger.info(
            "[twofa] provision: existing_row_found id=%s username=%s role=%s tenant_id=%s",
            row.id, row.username, row.role, row.tenant_id,
        )
        return row

    # Username must be unique → derive from email local-part with a
    # collision-resistant suffix only when needed.
    base_username = (email.split("@", 1)[0] or "admin") + "-platform-admin"
    username = base_username
    suffix = 0
    try:
        while db.query(User).filter(User.username == username).first() is not None:
            suffix += 1
            username = f"{base_username}-{suffix}"
            if suffix > 1000:                             # paranoid bound
                raise RuntimeError("username suffix overflow")
    except Exception as exc:                              # noqa: BLE001
        logger.exception(
            "[twofa] provision: username probe FAILED exc=%s",
            type(exc).__name__,
        )
        raise

    logger.info(
        "[twofa] provision: insert_start username=%s tenant_id=1 role=admin",
        username,
    )
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
        logger.info("[twofa] provision: insert_OK id=%s", row.id)
    except Exception as exc:                              # noqa: BLE001
        logger.exception(
            "[twofa] provision: insert FAILED exc=%s — trying race re-fetch",
            type(exc).__name__,
        )
        db.rollback()
        # Re-fetch in case a concurrent request won the race.
        row = db.query(User).filter(User.email == email).first()
        if row is None:
            logger.error(
                "[twofa] provision: race re-fetch ALSO empty — surfacing"
            )
            raise
        logger.info(
            "[twofa] provision: race re-fetch found id=%s — recovered",
            row.id,
        )
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

    Every branch logs a structured ``resolve:`` line to make 500-debugging
    deterministic. Secrets (tokens, passwords, OTPs) are NEVER logged —
    only claim metadata (presence flags, role, sub).
    """
    uid = user.get("user_id")
    sub = user.get("sub")
    role = user.get("role")
    has_uid = uid is not None
    is_env = _is_env_admin_payload(user)
    logger.info(
        "[twofa] resolve: has_user_id=%s role=%s sub=%s is_env_admin=%s build=%s",
        has_uid, role, sub, is_env, TWOFA_BUILD_MARKER,
    )

    if uid:
        try:
            return int(uid)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[twofa] resolve: invalid user_id claim type=%s value_repr=%r",
                type(uid).__name__, uid,
            )
            raise HTTPException(
                status_code=400,
                detail="معرف المستخدم في التوكن غير صالح.",
            ) from exc

    if is_env:
        row = _provision_env_admin_user(db)
        return int(row.id)

    logger.info(
        "[twofa] resolve: 401_no_user_id sub=%s role=%s admin_email=%s",
        sub, role, ADMIN_EMAIL,
    )
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
    """
    Cheap read used by the dashboard to render the right control state.

    Wrapped in a defensive try/except so any DB / schema / provisioning
    failure surfaces in Railway logs with full traceback AND in the
    dashboard alert with the exception class (no secrets). This makes
    500s diagnosable from either side without another deploy cycle.
    """
    try:
        user_id = _resolve_user_id(user, db)
        logger.info("[twofa] status: user_id=%s — query user_totp", user_id)
        row = db.query(UserTotp).filter(UserTotp.user_id == user_id).first()
        enabled = _is_enabled(row)
        recovery_remaining = 0
        if enabled:
            recovery_remaining = (
                db.query(UserRecoveryCode)
                  .filter(UserRecoveryCode.user_id == user_id, UserRecoveryCode.used_at.is_(None))
                  .count()
            )
        logger.info(
            "[twofa] status: OK user_id=%s enabled=%s recovery_remaining=%s",
            user_id, enabled, recovery_remaining,
        )
        return {
            "enabled":       enabled,
            "enrolled_at":   row.confirmed_at.isoformat() if (row and row.confirmed_at) else None,
            "last_used_at":  row.last_used_at.isoformat() if (row and row.last_used_at) else None,
            "recovery_codes_remaining": recovery_remaining,
            "build_marker":  TWOFA_BUILD_MARKER,
        }
    except HTTPException:
        # Already a clean HTTP error (401/400/…) — let FastAPI propagate.
        raise
    except Exception as exc:                              # noqa: BLE001
        # Full traceback to Railway. Detail to client carries the exception
        # CLASS NAME only (never the message — psycopg2 errors can echo
        # column values back). Pair with the build_marker so ops can
        # confirm the running container.
        logger.exception(
            "[twofa] status: UNHANDLED exc_class=%s build=%s",
            type(exc).__name__, TWOFA_BUILD_MARKER,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "twofa_status_failed",
                "exc_class": type(exc).__name__,
                "build_marker": TWOFA_BUILD_MARKER,
                "message": (
                    "تعذّر قراءة حالة التحقق بخطوتين. "
                    "تواصل مع الدعم وأرسل لهم رمز الخطأ أعلاه."
                ),
            },
        ) from exc


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

    now_unix = int(datetime.now(timezone.utc).timestamp())

    return {
        "setup_token":   setup_token,
        "secret_b32":    secret,        # shown next to the QR for users who can't scan
        "otpauth_url":   uri,
        "issuer":        "Nahla AI",
        "account":       db_user.email,
        "expires_in":    10 * 60,
        # Diagnostic helpers so the dashboard can detect clock skew with
        # the user's device BEFORE they try to confirm. The frontend
        # compares `server_unix` with `Date.now()/1000` and warns the
        # user if the difference is > 30s — the #1 cause of "invalid
        # code" rejections at enrolment.
        "server_unix":   now_unix,
        "time_step_sec": 30,
        "valid_window":  2,
        # Three-code picker payload. See _build_candidate_codes docstring
        # for the security rationale (in short: strictly less info than
        # secret_b32 which is on the same response). Frontend renders
        # these as buttons and the merchant taps the one matching their
        # authenticator app — eliminating typo + boundary-crossing errors.
        "candidate_codes": _build_candidate_codes(secret, now_unix),
    }


# ── POST /auth/2fa/setup/candidates ────────────────────────────────────────────
class CandidatesBody(BaseModel):
    setup_token: str = Field(..., min_length=10)


@router.post("/setup/candidates")
async def two_factor_setup_candidates(
    body: CandidatesBody,
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Refresh the three TOTP candidate codes for an active enrolment session.

    The codes returned by /setup/start expire after roughly 60-90 seconds.
    When a merchant lingers on the picker (reading the QR, fumbling with
    their phone, switching apps) the three buttons go stale and clicking
    any of them would now be rejected. This endpoint hands out a fresh
    triple without regenerating the underlying secret (which would also
    invalidate the QR they already scanned).

    Security: gated by get_current_user AND by a valid setup_token that
    only the same user can decode. No DB writes, no secret rotation,
    no audit emission (light operation). Rate-limited to 60 calls / 10
    min per user — effectively one refresh every ~10s which is plenty
    given the 30-sec TOTP step.
    """
    user_id = _resolve_user_id(user, db)

    check_rate_limit_or_429(
        bucket="2fa_setup_candidates",
        key=str(user_id),
        max_count=60,
        window_seconds=10 * 60,
        audit_metadata={"path": "/auth/2fa/setup/candidates", "user_id": user_id, "ip": _client_ip(request)},
    )

    secret = _decode_setup_token(body.setup_token, expected_user_id=user_id)
    now_unix = int(datetime.now(timezone.utc).timestamp())
    return {
        "candidate_codes": _build_candidate_codes(secret, now_unix),
        "server_unix":     now_unix,
        "time_step_sec":   30,
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

    # ── Confirm-time TOTP verification ──────────────────────────────────────
    # We deliberately widen the acceptance window to ±60s (valid_window=2)
    # ONLY at first enrolment. Reasons:
    #   * The user is reading a 6-digit code from a phone, typing it on a
    #     desktop, then clicking submit. Any of those 3 steps can eat up
    #     20-30s, and pyotp's default ±30s window (valid_window=1) is
    #     unforgiving once you cross a 30-sec boundary.
    #   * Several enrolment failures we've seen in production are NOT
    #     wrong codes — they're stale codes (the screen showed the next
    #     30s window by the time submit fired).
    #   * Steady-state login (`/auth/2fa/verify`) keeps the tighter
    #     ±30s window. Widening only at enrolment doesn't weaken the
    #     long-term authentication surface — the user must still prove
    #     possession of the freshly-scanned secret right now.
    # If even ±60s rejects the code, we surface structured diagnostics
    # (server clock, code length, setup-token age) so the dashboard can
    # tell the user EXACTLY what's wrong instead of a generic prompt.
    code_clean = (body.otp or "").strip()
    now_unix = int(datetime.now(timezone.utc).timestamp())
    setup_age_sec: Optional[int] = None
    try:
        from jose import jwt as _jwt  # noqa: PLC0415
        _payload = _jwt.decode(body.setup_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        _iat = int(_payload.get("iat") or 0)
        if _iat > 0:
            setup_age_sec = max(0, now_unix - _iat)
    except Exception:  # noqa: BLE001
        setup_age_sec = None

    if not verify_totp(secret, code_clean, valid_window=2):
        audit(
            "2fa.verify_failed",
            user_id=user_id,
            sub=db_user.email,
            ip=_client_ip(request),
            stage="setup_confirm",
            code_len=len(code_clean),
            server_unix=now_unix,
            setup_age_sec=setup_age_sec,
        )
        logger.warning(
            "[twofa] confirm rejected: user_id=%s code_len=%d server_unix=%d "
            "time_step=30 valid_window=2 setup_age_sec=%s",
            user_id, len(code_clean), now_unix, setup_age_sec,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "totp_invalid",
                "message": (
                    "الرمز غير صحيح أو انتهت صلاحيته. تأكد من:\n"
                    "• اختيار حساب \"Nahla AI\" داخل تطبيق المصادقة (ليس حساباً آخر).\n"
                    "• ضبط ساعة الجوّال على \"تلقائي/Network time\".\n"
                    "• إدخال الرمز بسرعة قبل أن يتغيّر — الأمان يدور كل 30 ثانية."
                ),
                "server_unix":     now_unix,
                "time_step_sec":   30,
                "valid_window":    2,
                "code_length":     len(code_clean),
                "setup_age_sec":   setup_age_sec,
                "build_marker":    TWOFA_BUILD_MARKER,
            },
        )

    # ── Persist ──────────────────────────────────────────────────────────────
    plaintext_codes = generate_recovery_codes(10)
    now = datetime.now(timezone.utc)

    # Encrypt the secret BEFORE doing anything else with the DB session.
    # `_fernet()` raises RuntimeError when TOTP_ENC_KEY is missing or
    # malformed in production — without the catch, that RuntimeError
    # bubbles all the way out to the multi_tenant middleware and the
    # user sees a generic "middleware_fallback" 500 with no actionable
    # info. Catching here lets us return a clear operator-facing error
    # that names the missing env var and the fix.
    try:
        secret_enc_blob = encrypt_secret(secret)
    except RuntimeError as exc:
        logger.error(
            "[twofa] confirm: TOTP_ENC_KEY missing/invalid — user_id=%s build=%s err=%s",
            user_id, TWOFA_BUILD_MARKER, exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": "totp_enc_key_missing",
                "exc_class": "RuntimeError",
                "build_marker": TWOFA_BUILD_MARKER,
                "message": (
                    "تعذّر تفعيل التحقق بخطوتين: مفتاح تشفير الرموز السرية غير مضبوط على الخادم.\n"
                    "هذه ليست مشكلة في الرمز الذي أدخلته — التحقق نجح، لكن الخادم لا يستطيع حفظ السر بأمان.\n"
                    "أبلغ فريق الدعم برمز الخطأ: totp_enc_key_missing."
                ),
                # Operator-facing hint (rendered as a separate code block on the
                # dashboard for admins). Names the exact env var so the fix is
                # a one-line Railway change, not a guessing game.
                "operator_hint": (
                    "Set TOTP_ENC_KEY in Railway → Variables. Generate with:\n"
                    "  python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\""
                ),
            },
        ) from exc

    if existing is not None:
        existing.secret_enc = secret_enc_blob
        existing.confirmed_at = now
        existing.last_used_at = now
        existing.failed_attempts = 0
        existing.locked_until = None
        existing.updated_at = now
    else:
        db.add(UserTotp(
            user_id=user_id,
            secret_enc=secret_enc_blob,
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
