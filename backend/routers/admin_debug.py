"""
routers/admin_debug.py
──────────────────────
Temporary internal-only endpoint for production debugging.

Exposes:

    GET/POST /admin/debug/reset-admin
        Create the admin user if absent, or reset its password if it exists.
        Forces role='admin' and is_active=True.

⚠️  SECURITY
-----------
This endpoint is gated by the env var `ENABLE_ADMIN_DEBUG` and is OFF by
default.  To use it once for recovery:

    railway vars set ENABLE_ADMIN_DEBUG=true
    # call the endpoint
    railway vars set ENABLE_ADMIN_DEBUG=false   # turn it back off!

Optionally protect with a shared secret (`ADMIN_DEBUG_SECRET`) — when
set, the endpoint requires `?secret=<value>` to match.

When `ENABLE_ADMIN_DEBUG` is not exactly "true" (case-insensitive) the
endpoint returns 403 — so an accidental deploy with the flag absent
fails closed.

⚠️  Reminder
-----------
After successful login, either:
  • Set `ENABLE_ADMIN_DEBUG=false` (or unset it), OR
  • Delete this router from `main.py`.

Note: the existing `/auth/login` endpoint already accepts admin
credentials from env vars `ADMIN_EMAIL` + `ADMIN_PASSWORD` without any
DB row — that path is the recommended long-term fallback.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.auth import hash_password
from core.database import get_db
from core.audit import audit
from models import Tenant, User

logger = logging.getLogger("nahla.admin_debug")

router = APIRouter(prefix="/admin/debug", tags=["admin-debug"])


def _require_enabled(secret: Optional[str]) -> None:
    """Hard gate the endpoint on ENABLE_ADMIN_DEBUG + optional shared secret."""
    flag = (os.getenv("ENABLE_ADMIN_DEBUG", "") or "").strip().lower()
    if flag != "true":
        logger.warning(
            "[admin-debug] reset-admin called while ENABLE_ADMIN_DEBUG != 'true' "
            "(value=%r) — refused",
            flag,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "admin debug endpoints are disabled. Set "
                "ENABLE_ADMIN_DEBUG=true (and optionally ADMIN_DEBUG_SECRET) "
                "in the environment to enable for one-time recovery."
            ),
        )

    expected = (os.getenv("ADMIN_DEBUG_SECRET") or "").strip()
    if expected:
        if not secret or secret.strip() != expected:
            logger.warning("[admin-debug] reset-admin called with bad/missing secret")
            raise HTTPException(status_code=403, detail="invalid secret")


def _do_reset(
    db: Session,
    email: str,
    password: str,
    tenant_id: int,
) -> dict:
    """Create or update the admin user. Always forces role='admin' + active."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="email is required")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 chars")

    # Make sure the tenant exists (admin uses tenant_id=1 by convention; the
    # auth/login env-var fallback also uses 1).
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        tenant = Tenant(id=tenant_id, name="Nahla Admin")
        db.add(tenant)
        db.flush()
        logger.info("[admin-debug] auto-created Tenant id=%s for admin user", tenant_id)

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            tenant_id     = tenant_id,
            username      = email,
            email         = email,
            password_hash = hash_password(password),
            role          = "admin",
            is_active     = True,
        )
        db.add(user)
        action = "created"
    else:
        user.password_hash = hash_password(password)
        user.role          = "admin"
        user.is_active     = True
        # Promote tenant_id only if missing — never silently move a real
        # merchant user across tenants.
        if not user.tenant_id:
            user.tenant_id = tenant_id
        action = "updated"

    db.commit()
    logger.warning(
        "[admin-debug] reset-admin %s | email=%s tenant_id=%s user_id=%s",
        action, email, tenant_id, user.id,
    )
    audit(
        "admin_debug_reset_admin",
        action    = action,
        email     = email,
        tenant_id = tenant_id,
        user_id   = user.id,
    )
    return {
        "status":    action,
        "email":     email,
        "user_id":   user.id,
        "tenant_id": tenant_id,
        "role":      user.role,
        "is_active": user.is_active,
        "next":      "POST /auth/login with this email + password to obtain a JWT.",
        "reminder":  "Set ENABLE_ADMIN_DEBUG=false (or unset) immediately after recovery.",
    }


@router.get("/reset-admin")
def reset_admin_get(
    email: str = Query("admin@nahlah.ai"),
    password: str = Query("12345678"),
    tenant_id: int = Query(1),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Browser-friendly recovery (just hit the URL)."""
    _require_enabled(secret)
    return _do_reset(db, email, password, tenant_id)


@router.post("/reset-admin")
def reset_admin_post(
    email: str = Query("admin@nahlah.ai"),
    password: str = Query("12345678"),
    tenant_id: int = Query(1),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Same as GET, kept for callers that prefer POST for mutating actions."""
    _require_enabled(secret)
    return _do_reset(db, email, password, tenant_id)
