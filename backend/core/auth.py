"""
core/auth.py
────────────
JWT helpers, password hashing, and FastAPI authentication dependencies.
All auth logic lives here — routers import what they need.
"""
from __future__ import annotations

import hashlib
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import (
    JWT_ALGORITHM,
    JWT_EXPIRE_H,
    JWT_REFRESH_GRACE_DAYS,
    JWT_SECRET,
    INVITE_EXPIRE_H,
)
from core.audit import audit
from core.token_revocation import is_jti_revoked

_support_audit = logging.getLogger("nahla.support_audit")

# Platform staff roles are allowed to access owner/admin APIs.
# Keep legacy names for backward compatibility with existing tokens/frontend code.
PLATFORM_ADMIN_ROLES = frozenset({
    "admin",
    "owner",
    "super_admin",
    "platform_admin",
    "platform_owner",
})

# ── JWT availability ───────────────────────────────────────────────────────────
try:
    from jose import JWTError, jwt as _jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False

# ── bcrypt availability ────────────────────────────────────────────────────────
try:
    import bcrypt as _bcrypt_lib
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False

_bearer_scheme = HTTPBearer(auto_error=False)


# ── Password hashing ───────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Truncates to 72 bytes (bcrypt hard limit)."""
    if not BCRYPT_AVAILABLE:
        raise RuntimeError("bcrypt is not installed")
    hashed = _bcrypt_lib.hashpw(password[:72].encode("utf-8"), _bcrypt_lib.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    if not BCRYPT_AVAILABLE:
        return False
    return _bcrypt_lib.checkpw(plain[:72].encode("utf-8"), hashed.encode("utf-8"))


# ── Token creation ─────────────────────────────────────────────────────────────

def _new_jti() -> str:
    """Cryptographically random token id used for revocation lookups."""
    return _secrets.token_urlsafe(16)


def create_token(
    email: str,
    role: str,
    tenant_id: int,
    user_id: Optional[int] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Issue a signed JWT for a user session.

    Claims
    ------
    sub        — user email (standard JWT subject)
    role       — merchant | admin | staff | owner | support_impersonation
    tenant_id  — immutable tenant scope (every merchant call must be scoped to this)
    user_id    — database user.id
    exp        — expiry timestamp
    iat        — issued-at timestamp (UTC)
    jti        — opaque token id used by ``core.token_revocation`` so a
                 leaked JWT can be invalidated before its natural exp via
                 ``POST /auth/logout``.
    extra_claims — any additional structured claims (e.g. impersonation metadata)
    """
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub":       email,
        "role":      role,
        "tenant_id": tenant_id,
        "iat":       now,
        "exp":       now + timedelta(hours=JWT_EXPIRE_H),
        "jti":       _new_jti(),
    }
    if user_id is not None:
        payload["user_id"] = user_id
    if extra_claims:
        # Never let extra_claims overwrite the security-critical claims
        # we just set (jti / exp / iat). A caller that *really* wants a
        # custom jti must use create_support_token / create_*_token.
        for k, v in extra_claims.items():
            if k in ("jti", "exp", "iat"):
                continue
            payload[k] = v
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_support_token(
    *,
    merchant_email: str,
    merchant_user_id: int,
    tenant_id: int,
    actor_email: str,
    actor_user_id: int,
    session_version: int,
    ttl_hours: int = 4,
) -> str:
    """
    Issue a clearly-distinct support-impersonation JWT.

    Extra claims vs a normal merchant token
    ────────────────────────────────────────
    role               = "support_impersonation"   (never "merchant" or "admin")
    impersonation      = True
    actor_sub          = admin/support email       (who is doing the impersonation)
    actor_user_id      = admin user.id
    session_version    = DB revocation counter at the time of issuance
    exp                = min(ttl_hours, 4h)        (hard cap of 4 h regardless)

    The role value is intentionally different from all normal roles so that:
    - Middleware can detect and restrict sensitive operations
    - Audit logs unambiguously identify support sessions
    - Frontend can show a visible "support mode" banner
    """
    actual_ttl = min(ttl_hours, 4)          # hard cap
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=actual_ttl)
    payload: Dict[str, Any] = {
        "sub":             merchant_email,
        "role":            "support_impersonation",
        "tenant_id":       tenant_id,
        "user_id":         merchant_user_id,
        "impersonation":   True,
        "actor_sub":       actor_email,
        "actor_user_id":   actor_user_id,
        "session_version": session_version,
        "iat":             now,
        "exp":             exp,
        "jti":             _new_jti(),
    }
    _support_audit.info(
        "SUPPORT_TOKEN_ISSUED actor=%s → tenant=%s merchant=%s sv=%d exp=%s",
        actor_email, tenant_id, merchant_email, session_version, exp.isoformat(),
    )
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def token_fingerprint(token: str) -> str:
    """Return the first 16 hex chars of SHA-256(token) — safe for audit logs."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def create_invite_token(email: str, tenant_id_hint: Optional[int] = None) -> str:
    """Short-lived invitation JWT (type=invite, 7-day expiry)."""
    payload = {
        "type":           "invite",
        "invited_email":  email,
        "tenant_id_hint": tenant_id_hint,
        "exp":            datetime.now(timezone.utc) + timedelta(hours=INVITE_EXPIRE_H),
    }
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_verify_token(email: str) -> str:
    """24-hour email verification JWT."""
    payload = {
        "type": "verify_email",
        "sub":  email,
        "exp":  datetime.now(timezone.utc) + timedelta(hours=24),
    }
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_reset_token(email: str) -> str:
    """1-hour password reset JWT."""
    payload = {
        "type": "password_reset",
        "sub":  email,
        "exp":  datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token_for_refresh(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode a merchant session JWT for rolling refresh.

    Unlike ``decode_token``, accepts tokens that expired within
    ``JWT_REFRESH_GRACE_DAYS`` so a PWA reopened after a short absence
    can restore the session without forcing re-login. Signature and
    revocation are still enforced.
    """
    if not JWT_AVAILABLE:
        return None
    try:
        payload = _jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except JWTError:
        return None

    token_type = payload.get("type")
    if token_type in ("password_reset", "verify_email", "invite"):
        return None

    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        exp_dt = datetime.fromtimestamp(int(exp), tz=timezone.utc)
        if datetime.now(timezone.utc) - exp_dt > timedelta(days=JWT_REFRESH_GRACE_DAYS):
            return None

    if is_jti_revoked(payload.get("jti")):
        return None
    return payload


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Verify and decode a JWT. Returns ``None`` for any of:

    * Library not installed (extremely rare — surfaced at /auth/login).
    * Invalid signature / malformed payload / expired token.
    * Token whose ``jti`` is on the Redis revocation list (logout).

    Revocation lookup is best-effort: if Redis is briefly unreachable
    the in-process fallback in ``core.token_revocation`` answers; if
    that is also empty the token is treated as live until exp. Phase 2
    moves to refresh-token rotation which removes this dependency on
    a denylist entirely.
    """
    if not JWT_AVAILABLE:
        return None
    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    # Reset / verify / invite tokens never go through the revocation
    # list — they are short-lived and bound to a one-shot purpose
    # (``type`` claim). Only session tokens are denylisted.
    if payload.get("type") in ("password_reset", "verify_email", "invite"):
        return payload
    if is_jti_revoked(payload.get("jti")):
        return None
    return payload


# ── FastAPI dependencies ───────────────────────────────────────────────────────

def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Dependency — raises 401 if token is missing or invalid."""
    if not creds:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_token(creds.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


def is_platform_admin_role(role: Any) -> bool:
    """Return True when the role is allowed to access owner/admin surfaces."""
    return str(role or "").strip() in PLATFORM_ADMIN_ROLES


def _actor_is_still_platform_admin(actor_user_id: Optional[int]) -> bool:
    """Verify that the user behind a support-impersonation token is
    *currently* a platform admin in the database.

    Why a DB check (not just trusting the JWT)
    ──────────────────────────────────────────
    A support-impersonation JWT is minted at the moment an admin
    starts a session. If that admin is later demoted (role changed
    to ``merchant`` or account deactivated) WHILE their support
    session is still live, the JWT keeps working — its claims are
    frozen at issuance. For sensitive endpoints we revalidate the
    actor's admin status on every call so a demoted / deactivated
    admin loses access immediately, without waiting for the JWT to
    expire.

    Returns False on any error (DB down, user not found, role
    mismatch, inactive flag). Fail-closed by design.
    """
    if actor_user_id is None:
        return False
    try:
        actor_uid = int(actor_user_id)
    except (TypeError, ValueError):
        return False

    try:
        from core.database import SessionLocal  # noqa: PLC0415
        from database.models import User  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _support_audit.warning(
            "ADMIN_REVALIDATE_IMPORT_FAILED actor_user_id=%s err=%s",
            actor_uid, exc,
        )
        return False

    try:
        with SessionLocal() as db:
            actor = db.query(User).filter(User.id == actor_uid).first()
        if actor is None:
            return False
        if not getattr(actor, "is_active", True):
            return False
        return is_platform_admin_role(getattr(actor, "role", None))
    except Exception as exc:  # noqa: BLE001
        _support_audit.warning(
            "ADMIN_REVALIDATE_DB_FAILED actor_user_id=%s err=%s",
            actor_uid, exc,
        )
        return False


def require_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Dependency — admit either:

      (a) a regular platform-admin JWT (role in PLATFORM_ADMIN_ROLES), OR
      (b) a support-impersonation JWT whose ``actor_user_id`` STILL
          maps to a live platform-admin user in the DB.

    Branch (b) makes the internal debug surface (``/admin/debug/*``,
    ``/admin/debug/whatsapp/send-template``, media-env, etc.) usable
    during an active support-access session, so support staff can run
    diagnostics without dropping out of the impersonated tenant.

    Defense-in-depth on branch (b):
      * Token must explicitly carry ``impersonation == True`` AND
        ``role == "support_impersonation"`` — both required, in case
        a future code path forgets to set one of them.
      * Actor admin status is REVALIDATED against the DB every call
        (see `_actor_is_still_platform_admin`). A demoted /
        deactivated admin loses access immediately, without waiting
        for the JWT to expire or for session_version to be bumped.
      * Audit log distinguishes ``admin_access_granted`` (regular
        admin) from ``admin_access_granted_via_support`` so the
        merchant has full visibility into what support touched.

    The blocking-prefix audit in ``support_session_middleware`` runs
    BEFORE this dependency. If the path is on the blocked list and
    NOT on the read allow-list, the middleware rejects with 403
    ``support_sensitive_blocked`` first — this function never sees
    those requests. ``/admin/debug/*`` paths are not on the blocked
    list, so they reach this dependency unhindered.
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    user = get_current_user(creds)

    # ── Branch (a): regular platform admin ─────────────────────────
    if is_platform_admin_role(user.get("role")):
        audit(
            "admin_access_granted",
            path=str(request.url.path),
            method=request.method,
            sub=user.get("sub"),
            ip=client_ip,
        )
        return user

    # ── Branch (b): support-impersonation token from a real admin ──
    is_support = (
        user.get("impersonation") is True
        and user.get("role") == "support_impersonation"
    )
    if is_support:
        actor_user_id = user.get("actor_user_id")
        actor_sub = user.get("actor_sub")
        if _actor_is_still_platform_admin(actor_user_id):
            # Audit on the SUPPORT channel too — the merchant's
            # session audit log needs every admin-only endpoint
            # that was hit during impersonation.
            _support_audit.info(
                "SUPPORT_ADMIN_ACCESS actor=%s actor_user_id=%s "
                "tenant=%s path=%s method=%s ip=%s",
                actor_sub, actor_user_id,
                user.get("tenant_id"),
                request.url.path, request.method, client_ip,
            )
            audit(
                "admin_access_granted_via_support",
                path=str(request.url.path),
                method=request.method,
                sub=user.get("sub"),
                actor_sub=actor_sub,
                actor_user_id=actor_user_id,
                tenant_id=user.get("tenant_id"),
                ip=client_ip,
            )
            return user
        # Support token but actor no longer admin → deny + audit.
        audit(
            "admin_access_denied_demoted_actor",
            path=str(request.url.path),
            method=request.method,
            role=user.get("role"),
            sub=user.get("sub"),
            actor_sub=actor_sub,
            actor_user_id=actor_user_id,
            tenant_id=user.get("tenant_id"),
            ip=client_ip,
        )
        raise HTTPException(
            status_code=403,
            detail="Admin access required (support actor is no longer a platform admin)",
        )

    # ── Neither branch matched → 403 ────────────────────────────────
    audit(
        "admin_access_denied",
        path=str(request.url.path),
        method=request.method,
        role=user.get("role"),
        sub=user.get("sub"),
        tenant_id=user.get("tenant_id"),
        ip=client_ip,
    )
    raise HTTPException(status_code=403, detail="Admin access required")


def require_authenticated(request: Request) -> Dict[str, Any]:
    """
    Dependency — returns the JWT payload from request.state (set by middleware).
    Never falls back to raw headers — prevents tenant escape via forged X-Tenant-ID.
    """
    payload = getattr(request.state, "jwt_payload", None)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    return payload


def get_jwt_tenant_id(request: Request) -> int:
    """Strict tenant resolver — reads tenant_id ONLY from the validated JWT."""
    payload = require_authenticated(request)
    tid = payload.get("tenant_id")
    if tid is None:
        raise HTTPException(status_code=401, detail="Token missing tenant_id claim")
    return int(tid)


def get_jwt_user_id(request: Request) -> Optional[int]:
    """
    Read user_id from the validated JWT payload stored in request.state.

    Returns None (never raises) when the claim is absent — the caller decides
    whether that is an error.  Suitable for audit fields that should not block
    the main operation if the claim is missing.
    """
    try:
        payload = require_authenticated(request)
        uid = payload.get("user_id")
        return int(uid) if uid is not None else None
    except (HTTPException, TypeError, ValueError):
        return None


def get_client_ip(request: Request) -> str:
    """Extract real client IP, honouring common reverse-proxy headers."""
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


def require_merchant_scope(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """
    Dependency that REJECTS platform-staff tokens on merchant-scoped endpoints.

    Background
    ──────────
    Platform admin/owner tokens carry ``tenant_id = 1`` by convention (see
    ``core.middleware.jwt_enforcement_middleware``). When such a token reaches
    a merchant-scoped endpoint that resolves the tenant from the JWT claim,
    the endpoint happily returns tenant 1's data to the owner UI. This is
    a tenant-isolation breach: the owner dashboard ends up rendering one
    specific merchant's conversations, orders and revenue.

    Contract
    ────────
    * If the role is in :data:`PLATFORM_ADMIN_ROLES` AND the token does NOT
      carry ``impersonation = True`` → reject with HTTP 403. Platform admins
      must use ``/admin/*`` endpoints (which return platform-wide aggregates)
      or explicitly impersonate a merchant before calling tenant-scoped APIs.
    * Support-impersonation tokens are allowed; they already encode an
      explicit, audited choice to act inside one merchant's scope.
    * Merchant tokens pass through untouched.

    Apply this dependency to every endpoint that returns merchant detail
    (``/store-sync/status``, ``/store-sync/knowledge``, ``/whatsapp/usage``, ...)
    so a misrouted owner request fails closed instead of leaking tenant data.
    """
    user = get_current_user(creds)
    role = str(user.get("role") or "").strip()
    is_impersonating = bool(user.get("impersonation"))

    if role in PLATFORM_ADMIN_ROLES and not is_impersonating:
        client_ip = get_client_ip(request)
        audit(
            "merchant_scope_denied_for_admin",
            path=str(request.url.path),
            method=request.method,
            role=role,
            sub=user.get("sub"),
            tenant_id=user.get("tenant_id"),
            ip=client_ip,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "هذه الواجهة مخصصة لبيانات تاجر محدد. "
                "حسابات المنصة لا تستطيع قراءة بيانات متجر مباشرة دون "
                "تفعيل وضع الدعم/التشخيص لمتجر محدد."
            ),
        )
    return user


def require_not_support_impersonation(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """
    Dependency that BLOCKS sensitive operations during a support session.

    Attach to any endpoint that should never be reachable by a support agent:
    password change, email change, billing edits, secret rotation, tenant deletion.
    Raises HTTP 403 with a clear explanation that logs the attempt.
    """
    user = get_current_user(creds)
    if user.get("role") == "support_impersonation" or user.get("impersonation"):
        ip = get_client_ip(request)
        _support_audit.warning(
            "SUPPORT_BLOCKED_SENSITIVE actor=%s tenant=%s path=%s ip=%s",
            user.get("actor_sub", "?"), user.get("tenant_id"), request.url.path, ip,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "هذه العملية محظورة خلال جلسة الدعم الفني. "
                "يجب على التاجر إجراء هذا التغيير بنفسه."
            ),
        )
    return user
