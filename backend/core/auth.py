"""
core/auth.py
────────────
JWT helpers, password hashing, and FastAPI authentication dependencies.
All auth logic lives here — routers import what they need.
"""
from __future__ import annotations

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
) -> str:
    """
    Issue a signed JWT for a user session.

    Claims
    ------
    sub        — user email (standard JWT subject)
    role       — merchant | admin | staff | owner
    tenant_id  — immutable tenant scope (every merchant call must be scoped to this)
    user_id    — database user.id (present for all real accounts; absent only for
                 legacy admin tokens issued before this was added)
    exp        — expiry timestamp
    """
    payload: Dict[str, Any] = {
        "sub":       email,
        "role":      role,
        "tenant_id": tenant_id,
        "exp":       datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_H),
    }
    if user_id is not None:
        payload["user_id"] = user_id
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


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


def require_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Dependency — requires a valid JWT with role=admin. Logs every access."""
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    user = get_current_user(creds)
    if user.get("role") != "admin":
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
