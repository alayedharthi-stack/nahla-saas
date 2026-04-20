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
  GET /health/tenant-isolation — authenticated: verify JWT/DB/WA tenant consistency

Dependencies: core/database, core/tenant, observability/health
"""
import logging
import os
import subprocess
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.database import get_db
from core.tenant import (
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    merge_defaults,
    resolve_tenant_id,
)
from services.whatsapp_platform.token_manager import get_token_context

_iso_logger = logging.getLogger("nahla.tenant_isolation")
router = APIRouter()

# Captured at import time — measures uptime relative to first router load.
_START_TIME = _time.monotonic()
# Captured at import time — best estimate of when this process (and therefore
# this deploy) started serving traffic. Used by /version.
_DEPLOYED_AT = datetime.now(timezone.utc)


def _read_git_head_sha() -> Optional[str]:
    """
    Fallback for environments that don't inject RAILWAY_GIT_COMMIT_SHA.

    Tries, in order:
      1. ``git rev-parse HEAD`` (works in dev / any image with git installed).
      2. Reading ``.git/HEAD`` directly (works in slim images without git).

    Returns None if neither approach succeeds — callers should treat that as
    "unknown" rather than crash.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        sha = out.decode("ascii", errors="ignore").strip()
        if sha:
            return sha
    except Exception:
        pass

    # Walk up from this file looking for a .git directory.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        git_dir = parent / ".git"
        if not git_dir.exists():
            continue
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        except Exception:
            return None
        if head.startswith("ref:"):
            ref_path = git_dir / head.split(" ", 1)[1].strip()
            try:
                return ref_path.read_text(encoding="utf-8").strip() or None
            except Exception:
                return None
        return head or None
    return None


def _resolve_deploy_metadata() -> Dict[str, Any]:
    """
    Collect deploy / build identity from the environment.

    Railway injects ``RAILWAY_GIT_*`` and ``RAILWAY_*`` vars on every build.
    We also accept generic ``GIT_SHA`` / ``GIT_BRANCH`` so non-Railway hosts
    (Render, Fly, Docker self-host) can populate the same endpoint.

    Anything truly missing falls through to a best-effort ``.git/HEAD`` read,
    and finally ``"unknown"`` so the response is always well-formed.
    """
    sha = (
        os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("GIT_SHA")
        or os.getenv("SOURCE_COMMIT")
        or os.getenv("COMMIT_SHA")
        or _read_git_head_sha()
        or "unknown"
    )
    branch = (
        os.getenv("RAILWAY_GIT_BRANCH")
        or os.getenv("GIT_BRANCH")
        or os.getenv("SOURCE_BRANCH")
        or "unknown"
    )
    return {
        "git_sha":             sha,
        "git_sha_short":       sha[:7] if sha and sha != "unknown" else "unknown",
        "git_branch":          branch,
        "git_commit_message":  os.getenv("RAILWAY_GIT_COMMIT_MESSAGE"),
        "git_author":          os.getenv("RAILWAY_GIT_AUTHOR"),
        "deployed_at":         _DEPLOYED_AT.isoformat(),
        "uptime_seconds":      round(_time.monotonic() - _START_TIME),
        "railway_environment": os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_ENVIRONMENT"),
        "railway_service":     os.getenv("RAILWAY_SERVICE_NAME"),
        "railway_project":     os.getenv("RAILWAY_PROJECT_NAME"),
        "railway_deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID"),
        "railway_replica_id":  os.getenv("RAILWAY_REPLICA_ID"),
    }


# Resolved once at import — avoids re-running git on every request and
# guarantees the SHA we report is the SHA of the binary actually running,
# even if the working tree is later mutated by a sidecar.
_DEPLOY_METADATA: Dict[str, Any] = _resolve_deploy_metadata()


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


@router.get("/version")
async def version():
    """
    Deploy identity probe — returns the git SHA, branch, and Railway
    environment of the **running** binary.

    Use this to confirm a redeploy actually picked up your latest commit:
        $ curl -s https://api.nahlah.ai/version | jq .git_sha

    No auth required by design — the response contains zero secrets and is
    intended to be hit by humans, CI smoke tests, and uptime probes.
    """
    meta = dict(_DEPLOY_METADATA)
    meta["uptime_seconds"] = round(_time.monotonic() - _START_TIME)
    meta["service"] = "nahla-saas"
    meta["status"] = "ok"
    meta["checked_at"] = datetime.now(timezone.utc).isoformat()
    return meta


@router.get("/api/version")
async def version_alias():
    """Alias: /api/version → same as /version."""
    return await version()


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
    from models import WhatsAppConnection  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    settings  = get_or_create_settings(db, tenant_id)
    db.commit()
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    token_ctx = get_token_context(conn)
    configured = bool((conn and conn.phone_number_id) or wa.get("phone_number_id")) and bool(token_ctx.token)
    return {
        "status":              "configured" if configured else "not_configured",
        "service":             "nahla-saas",
        "phone_number_set":    bool((conn.phone_number if conn else None) or wa.get("phone_number")),
        "phone_number_id_set": bool((conn.phone_number_id if conn else None) or wa.get("phone_number_id")),
        "access_token_set":    bool(token_ctx.token),
        "token_status":        token_ctx.token_status,
        "verify_token_set":    bool(wa.get("verify_token")),
        "auto_reply_enabled":  wa.get("auto_reply_enabled", False),
    }


@router.get("/health/tenant-isolation")
async def tenant_isolation_check(
    request: Request,
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Authenticated tenant-isolation verification endpoint.

    Runs all critical checks for the currently logged-in user:
      ✅ JWT claims match the DB record
      ✅ tenant_id from JWT == tenant_id from DB user row
      ✅ WhatsApp connection belongs to the correct tenant
      ✅ No CRITICAL fallback to tenant_id=1 occurred

    A passing response looks like:
      { "all_checks_passed": true, "issues": [] }

    Any failing check is a HIGH-SEVERITY data-isolation bug.
    """
    from models import Tenant, User, WhatsAppConnection  # noqa: PLC0415

    issues: list[str] = []
    details: dict = {}

    # ── JWT claims ──────────────────────────────────────────────────────────────
    jwt_email     = user.get("sub")
    jwt_role      = user.get("role")
    jwt_tenant_id = user.get("tenant_id")
    jwt_user_id   = user.get("user_id")

    details["jwt"] = {
        "sub":       jwt_email,
        "role":      jwt_role,
        "tenant_id": jwt_tenant_id,
        "user_id":   jwt_user_id,
    }

    if jwt_tenant_id is None:
        issues.append("CRITICAL: JWT missing tenant_id claim")
    if jwt_user_id is None:
        issues.append("WARNING: JWT missing user_id claim — token is from before multi-tenant fix")

    # ── DB user record ──────────────────────────────────────────────────────────
    db_user = db.query(User).filter_by(email=jwt_email).first() if jwt_email else None
    if db_user:
        details["db_user"] = {
            "id":        db_user.id,
            "email":     db_user.email,
            "role":      db_user.role,
            "tenant_id": db_user.tenant_id,
            "is_active": db_user.is_active,
        }
        if jwt_tenant_id is not None and int(jwt_tenant_id) != db_user.tenant_id:
            issues.append(
                f"CRITICAL: tenant_id mismatch — JWT={jwt_tenant_id} vs DB={db_user.tenant_id}"
            )
        if jwt_user_id is not None and int(jwt_user_id) != db_user.id:
            issues.append(
                f"CRITICAL: user_id mismatch — JWT={jwt_user_id} vs DB={db_user.id}"
            )
    else:
        details["db_user"] = None
        if jwt_role != "admin":
            issues.append(f"CRITICAL: No User record found in DB for email={jwt_email}")

    # ── Tenant record ───────────────────────────────────────────────────────────
    resolved_tid = resolve_tenant_id(request)
    tenant = db.query(Tenant).filter_by(id=resolved_tid).first() if resolved_tid else None
    details["tenant"] = {
        "resolved_id": resolved_tid,
        "name":        tenant.name if tenant else None,
        "exists":      tenant is not None,
    }
    if resolved_tid == 1 and jwt_role != "admin":
        issues.append("CRITICAL: resolved tenant_id=1 for non-admin — fallback triggered")

    # ── WhatsApp connection ─────────────────────────────────────────────────────
    wa = db.query(WhatsAppConnection).filter_by(tenant_id=resolved_tid).first() if resolved_tid else None
    details["whatsapp"] = {
        "connected":    bool(wa and wa.status == "connected" and wa.sending_enabled),
        "status":       wa.status if wa else "none",
        "phone_number": wa.phone_number if wa else None,
        "tenant_id":    wa.tenant_id if wa else None,
    }
    if wa and wa.tenant_id != resolved_tid:
        issues.append(
            f"CRITICAL: WhatsApp connection belongs to tenant={wa.tenant_id} "
            f"but request resolved to tenant={resolved_tid}"
        )

    # ── Role isolation: merchant should never see admin panel ───────────────────
    details["role_isolation"] = {
        "role":               jwt_role,
        "is_merchant":        jwt_role in ("merchant", "merchant_admin", "merchant_user"),
        "is_admin":           jwt_role in ("admin", "owner", "super_admin"),
        "correct_dashboard":  "/admin" if jwt_role == "admin" else "/overview",
    }

    all_passed = len(issues) == 0

    # Log any issues found
    if not all_passed:
        for issue in issues:
            _iso_logger.critical(
                "[tenant-isolation-check] %s | user=%s jwt_tenant=%s resolved_tenant=%s",
                issue, jwt_email, jwt_tenant_id, resolved_tid,
            )
    else:
        _iso_logger.info(
            "[tenant-isolation-check] ✅ ALL CHECKS PASSED | user=%s role=%s tenant=%s",
            jwt_email, jwt_role, resolved_tid,
        )

    return JSONResponse(
        status_code=200 if all_passed else 422,
        content={
            "all_checks_passed": all_passed,
            "issues":            issues,
            "details":           details,
            "checked_at":        datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


@router.get("/health/tables")
async def health_tables(db: Session = Depends(get_db)):
    """Check if key DB tables exist and have data — for diagnosing startup issues."""
    from sqlalchemy import text, inspect
    results = {}
    tables_to_check = [
        "billing_plans", "billing_subscriptions", "smart_automations",
        "customer_profiles", "tenants", "users",
    ]
    for table in tables_to_check:
        try:
            count = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            results[table] = {"exists": True, "rows": count}
        except Exception as exc:
            results[table] = {"exists": False, "error": str(exc)[:100]}
    return {"status": "ok", "tables": results}


@router.get("/health/detailed")
async def health_detailed(request: Request, db: Session = Depends(get_db)):
    """Full readiness probe: database + WhatsApp configuration."""
    from observability.health import check_database
    db_result = await check_database(db)
    db_ok     = db_result.get("status") == "ok"

    from models import WhatsAppConnection  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    wa = merge_defaults(
        get_or_create_settings(db, tenant_id).whatsapp_settings,
        DEFAULT_WHATSAPP,
    )
    db.commit()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    token_ctx = get_token_context(conn)
    wa_configured = bool((conn.phone_number_id if conn else None) or wa.get("phone_number_id")) and bool(token_ctx.token)

    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status":               "ok" if db_ok else "degraded",
            "service":              "nahla-saas",
            "database":             db_ok,
            "whatsapp_configured":  wa_configured,
            "whatsapp_token_status": token_ctx.token_status,
            "uptime_seconds":       round(_time.monotonic() - _START_TIME),
            "timestamp":            datetime.now(timezone.utc).isoformat() + "Z",
        },
    )
