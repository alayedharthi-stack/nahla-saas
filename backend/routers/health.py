"""
routers/health.py
─────────────────
Health and liveness endpoints.

Routes:
  GET /              — API root
  GET /health        — lightweight liveness probe (no DB)
  GET /api/health    — alias for /health
  GET /health/db     — database connectivity probe
  GET /health/whatsapp — WhatsApp configuration status
  GET /health/detailed — full readiness probe (DB + WhatsApp)

Dependencies: core/database, core/tenant, observability/health
"""
import time as _time
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import (
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    merge_defaults,
    resolve_tenant_id,
)

router = APIRouter()

# Captured at import time — measures uptime relative to first router load.
_START_TIME = _time.monotonic()


@router.get("/")
async def root():
    """API root — confirms the backend is reachable."""
    return {
        "service": "nahla-saas",
        "status":  "ok",
        "version": "1.0.0",
        "docs":    "/docs",
    }


@router.get("/health")
async def health():
    """Lightweight liveness probe — always returns fast, no DB hit."""
    return {
        "status":         "ok",
        "service":        "nahla-saas",
        "uptime_seconds": round(_time.monotonic() - _START_TIME),
        "version":        "1.0.0",
    }


@router.get("/api/health")
async def health_alias():
    """Alias: /api/health → same as /health."""
    return {
        "status":         "ok",
        "service":        "nahla-saas",
        "uptime_seconds": round(_time.monotonic() - _START_TIME),
        "version":        "1.0.0",
    }


@router.get("/health/db")
async def health_db(db: Session = Depends(get_db)):
    """Database connectivity probe."""
    from observability.health import check_database
    result = await check_database(db)
    ok = result.get("status") == "ok"
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status":   result.get("status"),
            "service":  "nahla-saas",
            "database": ok,
        },
    )


@router.get("/health/whatsapp")
async def health_whatsapp(request: Request, db: Session = Depends(get_db)):
    """WhatsApp integration readiness — reports configured/not_configured without exposing secrets."""
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    configured = bool(wa.get("phone_number_id") and wa.get("access_token"))
    return {
        "status":              "configured" if configured else "not_configured",
        "service":             "nahla-saas",
        "phone_number_set":    bool(wa.get("phone_number")),
        "phone_number_id_set": bool(wa.get("phone_number_id")),
        "access_token_set":    bool(wa.get("access_token")),
        "verify_token_set":    bool(wa.get("verify_token")),
        "auto_reply_enabled":  wa.get("auto_reply_enabled", False),
    }


@router.get("/health/detailed")
async def health_detailed(request: Request, db: Session = Depends(get_db)):
    """Full readiness probe: database + WhatsApp configuration."""
    from observability.health import check_database
    db_result = await check_database(db)
    db_ok     = db_result.get("status") == "ok"

    tenant_id = resolve_tenant_id(request)
    wa = merge_defaults(
        get_or_create_settings(db, tenant_id).whatsapp_settings,
        DEFAULT_WHATSAPP,
    )
    db.commit()
    wa_configured = bool(wa.get("phone_number_id") and wa.get("access_token"))

    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status":               "ok" if db_ok else "degraded",
            "service":              "nahla-saas",
            "database":             db_ok,
            "whatsapp_configured":  wa_configured,
            "uptime_seconds":       round(_time.monotonic() - _START_TIME),
            "timestamp":            datetime.utcnow().isoformat() + "Z",
        },
    )
