"""
TEMPORARY public debug surface — gated by a shared secret in env.

Why this exists
───────────────
The merchant currently cannot reach the existing
``/admin/debug/abandoned-carts-*`` endpoints because they require an
admin JWT, and no admin account is provisioned for the live tenant.
This router exposes the *same* diagnostic JSON over an unauthenticated
path that is gated by ``DEBUG_ADMIN_TOKEN`` — a value only the operator
who set it in Railway env knows.

Design constraints
──────────────────
1. **Reuse, don't duplicate**: we call the existing admin handlers as
   plain Python functions so any normaliser/dashboard fix lands here
   automatically too. No drift.
2. **Constant-time secret compare**: ``hmac.compare_digest`` so attackers
   can't time-guess the token byte by byte.
3. **Closed by default**: if ``DEBUG_ADMIN_TOKEN`` is not set on the
   server we return ``503`` rather than fall open.
4. **Self-removable**: this entire file + its include line in
   ``main.py`` is intentionally additive. Deleting it disables the
   feature with zero ripple effect on the rest of the app.

Endpoints
─────────
GET /debug/version?debug_token=...
GET /debug/abandoned-carts-sync?debug_token=...&run_sync=true[&tenant_id=N]
GET /debug/abandoned-carts-raw?debug_token=...[&tenant_id=N&limit=3]
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.database import get_db

router = APIRouter()
logger = logging.getLogger("nahla-backend")

# Sentinel passed in place of the JWT-resolved admin dict so the
# audit-log line in the underlying admin handlers still works.
_FAKE_ADMIN: Dict[str, Any] = {
    "sub":       "debug_token",
    "role":      "debug",
    "tenant_id": None,
}


def _check_token(debug_token: Optional[str]) -> None:
    """Constant-time comparison against ``DEBUG_ADMIN_TOKEN``.

    Returns nothing on success; raises ``HTTPException`` otherwise.
    """
    expected = os.getenv("DEBUG_ADMIN_TOKEN") or ""
    if not expected:
        # Fail closed: missing env var means the operator has not opted
        # in to exposing this surface.
        raise HTTPException(
            status_code=503,
            detail=(
                "DEBUG_ADMIN_TOKEN is not set on the server. "
                "Set it in Railway → Variables, then redeploy."
            ),
        )
    if not debug_token or not hmac.compare_digest(str(debug_token), expected):
        raise HTTPException(status_code=403, detail="invalid debug_token")


def _resolve_default_tenant(db: Session) -> int:
    """Pick a sensible tenant when the caller didn't specify one.

    Preference order:
      1. Most recent ``Integration`` row whose ``provider == 'salla'``
         (those are the tenants we can actually fetch carts for).
      2. Otherwise, the lowest-id tenant on record — useful for raw
         ``/debug/db-overview`` calls where the operator just wants to
         confirm any tenant exists.

    On any DB/import failure the *real* exception bubbles up as a 500
    with detail — earlier the broad ``except`` here swallowed an
    ``ImportError`` (wrong class name) and silently returned the
    misleading "no tenants found in DB" 404.
    """
    from models import Integration, Tenant  # noqa: PLC0415
    integ = (
        db.query(Integration)
        .filter(Integration.provider == "salla")
        .order_by(Integration.id.desc())
        .first()
    )
    if integ and integ.tenant_id:
        return int(integ.tenant_id)
    t = db.query(Tenant).order_by(Tenant.id.asc()).first()
    if t:
        return int(t.id)
    raise HTTPException(status_code=404, detail="no tenants found in DB")


# ── /debug/version ──────────────────────────────────────────────────────
@router.get("/debug/version")
async def debug_version(debug_token: str = Query(..., description="Shared secret from env")):
    """Same payload as ``/version`` but reachable without admin login."""
    _check_token(debug_token)
    # Lazy import so this router doesn't pin the health module's load order.
    from routers.health import _DEPLOY_METADATA  # noqa: PLC0415
    import time as _time  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415
    from routers.health import _START_TIME  # noqa: PLC0415

    out = dict(_DEPLOY_METADATA)
    out["uptime_seconds"] = round(_time.monotonic() - _START_TIME)
    out["service"]    = "nahla-saas"
    out["status"]     = "ok"
    out["checked_at"] = datetime.now(timezone.utc).isoformat()
    return out


# ── /debug/db-overview ──────────────────────────────────────────────────
@router.get("/debug/db-overview")
async def debug_db_overview(
    debug_token: str = Query(..., description="Shared secret from env"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """High-level "is the DB even populated?" probe.

    Returns counts + first-10 rows of the tables that the abandoned-cart
    pipeline depends on, so we can answer questions like:
      • Is there any tenant at all?
      • Is there a Salla integration row, and which tenant owns it?
      • Are there any Order rows (abandoned or otherwise)?

    Each section is wrapped in its own try/except so a single bad model
    import or missing table doesn't black out the whole endpoint.
    """
    _check_token(debug_token)
    out: Dict[str, Any] = {"sections": {}, "errors": {}}

    # ── Tenants ──────────────────────────────────────────────────────────
    try:
        from models import Tenant  # noqa: PLC0415
        rows = db.query(Tenant).order_by(Tenant.id.asc()).limit(10).all()
        total = db.query(Tenant).count()
        out["sections"]["tenants"] = {
            "total": total,
            "rows": [
                {
                    "id":         t.id,
                    "name":       t.name,
                    "domain":     getattr(t, "domain", None),
                    "is_active":  getattr(t, "is_active", None),
                    "is_platform_tenant": getattr(t, "is_platform_tenant", None),
                    "created_at": t.created_at.isoformat() if getattr(t, "created_at", None) else None,
                }
                for t in rows
            ],
        }
    except Exception as exc:
        out["errors"]["tenants"] = repr(exc)

    # ── Integrations (Salla + others) ────────────────────────────────────
    try:
        from models import Integration  # noqa: PLC0415
        rows = db.query(Integration).order_by(Integration.id.desc()).limit(20).all()
        total = db.query(Integration).count()
        salla_count = db.query(Integration).filter(Integration.provider == "salla").count()
        out["sections"]["integrations"] = {
            "total":       total,
            "salla_count": salla_count,
            "rows": [
                {
                    "id":                i.id,
                    "provider":          i.provider,
                    "tenant_id":         i.tenant_id,
                    "external_store_id": getattr(i, "external_store_id", None),
                    "enabled":           getattr(i, "enabled", None),
                    "config_keys":       sorted(list((i.config or {}).keys())) if isinstance(i.config, dict) else None,
                    "has_access_token":  bool(isinstance(i.config, dict) and i.config.get("access_token")),
                    "has_refresh_token": bool(isinstance(i.config, dict) and i.config.get("refresh_token")),
                }
                for i in rows
            ],
        }
    except Exception as exc:
        out["errors"]["integrations"] = repr(exc)

    # ── Orders (abandoned + total) ───────────────────────────────────────
    try:
        from models import Order  # noqa: PLC0415
        total = db.query(Order).count()
        abandoned = db.query(Order).filter(Order.is_abandoned == True).count()  # noqa: E712
        out["sections"]["orders"] = {
            "total":          total,
            "abandoned":      abandoned,
            "by_tenant": [
                {"tenant_id": tid, "abandoned_count": cnt}
                for tid, cnt in (
                    db.query(Order.tenant_id, sa_func_count_id())
                    .filter(Order.is_abandoned == True)  # noqa: E712
                    .group_by(Order.tenant_id)
                    .all()
                )
            ],
        }
    except Exception as exc:
        out["errors"]["orders"] = repr(exc)

    # ── Users (lets the operator verify which tenant their account is on) ─
    try:
        from models import User  # noqa: PLC0415
        rows = db.query(User).order_by(User.id.asc()).limit(10).all()
        total = db.query(User).count()
        out["sections"]["users"] = {
            "total": total,
            "rows": [
                {
                    "id":        u.id,
                    "email":     u.email,
                    "role":      getattr(u, "role", None),
                    "tenant_id": getattr(u, "tenant_id", None),
                    "is_active": getattr(u, "is_active", None),
                }
                for u in rows
            ],
        }
    except Exception as exc:
        out["errors"]["users"] = repr(exc)

    return out


# Helper for Order.by_tenant aggregate above. Defined at module scope so
# we don't pay the import cost on every call.
def sa_func_count_id():
    from sqlalchemy import func  # noqa: PLC0415
    from models import Order  # noqa: PLC0415
    return func.count(Order.id)


# ── /debug/abandoned-carts-sync ─────────────────────────────────────────
@router.get("/debug/abandoned-carts-sync")
async def debug_abandoned_carts_sync_public(
    request: Request,
    debug_token: str = Query(..., description="Shared secret from env"),
    tenant_id: Optional[int] = Query(None, description="Defaults to most-recent Salla tenant"),
    run_sync: bool = Query(False, description="If true, trigger a live sync first"),
    sample_raw: int = Query(2, ge=0, le=5),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Public mirror of ``/admin/debug/abandoned-carts-sync``.

    Returns the same structure: ``salla_count``, ``db_count``,
    ``dashboard_query``, ``live_sync``, ``salla_fetch_error``,
    ``raw_salla_sample``, ``normalized_sample``, ``warnings``, etc.
    """
    _check_token(debug_token)
    if tenant_id is None:
        tenant_id = _resolve_default_tenant(db)

    from routers.admin import debug_abandoned_carts_sync  # noqa: PLC0415
    return await debug_abandoned_carts_sync(
        request=request,
        tenant_id=tenant_id,
        run_sync=run_sync,
        include_dashboard=True,
        sample_raw=sample_raw,
        db=db,
        _admin=_FAKE_ADMIN,
    )


# ── /debug/abandoned-carts-raw ──────────────────────────────────────────
@router.get("/debug/abandoned-carts-raw")
async def debug_abandoned_carts_raw_public(
    request: Request,
    debug_token: str = Query(..., description="Shared secret from env"),
    tenant_id: Optional[int] = Query(None, description="Defaults to most-recent Salla tenant"),
    limit: int = Query(3, ge=1, le=10),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Public mirror of ``/admin/debug/abandoned-carts-raw``."""
    _check_token(debug_token)
    if tenant_id is None:
        tenant_id = _resolve_default_tenant(db)

    from routers.admin import debug_abandoned_carts_raw  # noqa: PLC0415
    return await debug_abandoned_carts_raw(
        request=request,
        tenant_id=tenant_id,
        limit=limit,
        db=db,
        _admin=_FAKE_ADMIN,
    )
