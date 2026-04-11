"""
core/auth.py
────────────
JWT helpers, password hashing, and FastAPI authentication dependencies.
All auth logic lives here — routers import what they need.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import (
    JWT_ALGORITHM,
    JWT_EXPIRE_H,
    JWT_SECRET,
    INVITE_EXPIRE_H,
)
from core.audit import audit

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
    extra_claims — any additional structured claims (e.g. impersonation metadata)
    """
    payload: Dict[str, Any] = {
        "sub":       email,
        "role":      role,
        "tenant_id": tenant_id,
        "exp":       datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_H),
    }
    if user_id is not None:
        payload["user_id"] = user_id
    if extra_claims:
        payload.update(extra_claims)
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
    exp = datetime.now(timezone.utc) + timedelta(hours=actual_ttl)
    payload: Dict[str, Any] = {
        "sub":             merchant_email,
        "role":            "support_impersonation",
        "tenant_id":       tenant_id,
        "user_id":         merchant_user_id,
        "impersonation":   True,
        "actor_sub":       actor_email,
        "actor_user_id":   actor_user_id,
        "session_version": session_version,
        "exp":             exp,
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


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    if not JWT_AVAILABLE:
        return None
    try:
        return _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


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


def require_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Dependency — requires a valid JWT with a platform-staff role. Logs every access."""
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    user = get_current_user(creds)
    if not is_platform_admin_role(user.get("role")):
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
    audit(
        "admin_access_granted",
        path=str(request.url.path),
        method=request.method,
        sub=user.get("sub"),
        ip=client_ip,
    )
    return user


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


def get_client_ip(request: Request) -> str:
    """Extract real client IP, honouring common reverse-proxy headers."""
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


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
