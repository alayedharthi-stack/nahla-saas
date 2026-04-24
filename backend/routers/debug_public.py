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
from pydantic import BaseModel, EmailStr
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
    payload = await debug_abandoned_carts_sync(
        request=request,
        tenant_id=tenant_id,
        run_sync=run_sync,
        include_dashboard=True,
        sample_raw=sample_raw,
        db=db,
        _admin=_FAKE_ADMIN,
    )
    # Splice in scheduler health so an operator gets one-stop visibility
    # on "is the auto-sync loop actually running for this tenant?".
    try:
        from core.abandoned_cart_scheduler import (  # noqa: PLC0415
            get_state_snapshot, get_last_run_for_tenant,
        )
        scheduler_snap = get_state_snapshot()
        if isinstance(payload, dict):
            payload["scheduler"] = {
                "started_at":            scheduler_snap.get("started_at"),
                "interval_seconds":      scheduler_snap.get("interval_seconds"),
                "last_cycle_at":         scheduler_snap.get("last_cycle_at"),
                "last_cycle_ok":         scheduler_snap.get("last_cycle_ok"),
                "next_cycle_at":         scheduler_snap.get("next_cycle_at"),
                "cycles_completed":      scheduler_snap.get("cycles_completed"),
                "tenants_in_last_cycle": scheduler_snap.get("tenants_in_last_cycle"),
                "this_tenant":           get_last_run_for_tenant(tenant_id),
            }
    except Exception as exc:
        # Never let a visibility add-on break the diagnostic endpoint.
        if isinstance(payload, dict):
            payload["scheduler"] = {"error": f"{type(exc).__name__}: {exc}"}
    return payload


# ── /debug/salla-direct ─────────────────────────────────────────────────
@router.get("/debug/salla-direct")
async def debug_salla_direct(
    debug_token: str = Query(..., description="Shared secret from env"),
    tenant_id: Optional[int] = Query(None, description="Defaults to most-recent Salla tenant"),
    path: str = Query("/carts/abandoned", description="Salla path (relative to /admin/v2)"),
    per_page: int = Query(10, ge=1, le=50),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Raw, un-wrapped HTTP call to Salla — bypasses the adapter entirely.

    This endpoint exists because the adapter's ``_get_all_pages`` has a
    broad ``except Exception: break`` that converts auth failures
    (401/403) and server errors (5xx) into a silent empty list — making
    the symptom indistinguishable from "Salla genuinely returned 0".

    What this returns
    ─────────────────
      • The exact HTTP status Salla replied with
      • Response headers (rate-limit, request-id) and a body preview
        so you can read Salla's own error message verbatim
      • The token's first 12 chars + length (so we can confirm a token
        is actually being sent without leaking the secret)
      • Hints based on the status code (401 → token; 403 → scope; 200
        + empty data → genuinely no abandoned carts)

    Default ``path`` hits the abandoned-cart endpoint, but you can
    pass any other admin path (e.g. ``/store/info`` to confirm basic
    auth works, or ``/orders?per_page=1`` to confirm orders.read scope).
    """
    _check_token(debug_token)
    if tenant_id is None:
        tenant_id = _resolve_default_tenant(db)

    from datetime import datetime, timezone  # noqa: PLC0415
    import httpx  # noqa: PLC0415
    from models import Integration, Tenant  # noqa: PLC0415

    out: Dict[str, Any] = {
        "tenant_id":     tenant_id,
        "tenant_name":   None,
        "checked_at":    datetime.now(timezone.utc).isoformat(),
        "request": {
            "url":      None,
            "params":   {"per_page": per_page, "page": page},
            "has_auth": False,
            "token_preview": None,
            "token_length":  0,
        },
        "response": {
            "status":         None,
            "headers":        None,
            "body_keys":      None,
            "data_count":     None,
            "pagination":     None,
            "body_preview":   None,
            "elapsed_ms":     None,
        },
        "integration": None,
        "hint":        None,
        "error":       None,
    }

    t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        out["error"] = f"tenant {tenant_id} not found"
        return out
    out["tenant_name"] = t.name

    integ = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .order_by(Integration.id.desc())
        .first()
    )
    if not integ:
        out["error"] = "no_salla_integration_for_tenant"
        out["hint"]  = "This tenant has no Integration row with provider='salla'."
        return out

    cfg: Dict[str, Any] = integ.config or {}
    out["integration"] = {
        "id":                integ.id,
        "enabled":           bool(integ.enabled),
        "external_store_id": integ.external_store_id,
        "config_keys":       sorted(list(cfg.keys())),
        "no_auto_refresh":   bool(cfg.get("no_auto_refresh")),
        "needs_reauth":      bool(cfg.get("needs_reauth")),
        "uninstalled_at":    cfg.get("uninstalled_at"),
        "revoked_reason":    cfg.get("revoked_reason"),
    }

    # Salla's stored "api_key" IS the OAuth access_token in this codebase
    # (see SallaAdapter._persist_refreshed_tokens which writes the
    # refreshed access_token back into config["api_key"]).
    token = cfg.get("api_key") or cfg.get("access_token") or ""
    if not token:
        out["error"] = "no_token_in_config"
        out["hint"]  = (
            "Integration row exists but config has no api_key / access_token. "
            "Merchant must reconnect Salla or paste a fresh Account Token."
        )
        return out

    out["request"]["has_auth"]      = True
    out["request"]["token_preview"] = token[:12] + "..."
    out["request"]["token_length"]  = len(token)

    base = "https://api.salla.dev/admin/v2"
    url  = f"{base}{path if path.startswith('/') else '/' + path}"
    out["request"]["url"] = url

    import time as _time  # noqa: PLC0415
    started = _time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept":        "application/json",
                },
                params={"per_page": per_page, "page": page},
            )
        elapsed = round((_time.monotonic() - started) * 1000)
        out["response"]["status"]     = resp.status_code
        out["response"]["elapsed_ms"] = elapsed
        # Only safe response headers — never echo Authorization back.
        out["response"]["headers"] = {
            k: v for k, v in resp.headers.items()
            if k.lower() in {
                "content-type", "x-request-id", "x-ratelimit-limit",
                "x-ratelimit-remaining", "x-ratelimit-reset",
                "retry-after", "date",
            }
        }
        body_text = resp.text or ""
        out["response"]["body_preview"] = body_text[:600]
        try:
            body_json = resp.json()
            if isinstance(body_json, dict):
                out["response"]["body_keys"]   = sorted(list(body_json.keys()))
                data = body_json.get("data")
                if isinstance(data, list):
                    out["response"]["data_count"] = len(data)
                pag = body_json.get("pagination") or body_json.get("meta")
                if isinstance(pag, dict):
                    out["response"]["pagination"] = {
                        k: pag.get(k) for k in
                        ("count", "total", "perPage", "currentPage",
                         "totalPages", "last_page", "current_page", "per_page")
                        if k in pag
                    }
        except Exception:
            pass

        # ── Diagnostic hints ─────────────────────────────────────────
        sc = resp.status_code
        if sc == 401:
            out["hint"] = (
                "401 Unauthorized — token is invalid or expired. "
                "Salla's refresh_token rotation likely needs to run, "
                "OR the merchant needs to paste a fresh Account Token "
                "from Salla Partners → API credentials."
            )
        elif sc == 403:
            out["hint"] = (
                "403 Forbidden — token is valid but missing the required "
                "scope. /carts/abandoned needs the 'carts.read' scope. "
                "Re-install the Salla app with all required scopes."
            )
        elif sc == 404:
            out["hint"] = (
                "404 Not Found — wrong URL or this Salla plan does not "
                "expose this endpoint. Confirm the path is correct."
            )
        elif sc == 429:
            out["hint"] = "429 — rate limited. Try again in a few seconds."
        elif sc >= 500:
            out["hint"] = f"{sc} — Salla server error. Check Salla status page."
        elif sc == 200:
            dc = out["response"]["data_count"]
            if dc == 0:
                out["hint"] = (
                    "200 OK with empty data array — Salla genuinely has "
                    "0 abandoned carts for this store right now. The "
                    "carts shown in the merchant's Salla dashboard "
                    "may be live (not yet abandoned by Salla's "
                    "definition; Salla typically waits 30+ minutes "
                    "before classifying a cart as 'abandoned'). "
                    "Try /debug/salla-direct?path=/orders to confirm "
                    "the token works for other endpoints."
                )
            else:
                out["hint"] = f"200 OK with {dc} cart(s) — adapter pipeline should pick these up."
    except httpx.TimeoutException:
        out["error"] = "salla_timeout_after_20s"
        out["hint"]  = "Salla didn't respond within 20s. Try again."
    except Exception as exc:
        out["error"] = f"network_error: {type(exc).__name__}: {exc}"

    return out


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


# ── /debug/email-config ─────────────────────────────────────────────────
@router.get("/debug/email-config")
async def debug_email_config(
    debug_token: str = Query(..., description="Shared secret from env"),
) -> Dict[str, Any]:
    """Show SMTP config + TCP reachability test (no email sent)."""
    _check_token(debug_token)
    import asyncio as _aio  # noqa: PLC0415
    import socket as _socket  # noqa: PLC0415
    import time as _time  # noqa: PLC0415
    from core.config import (  # noqa: PLC0415
        EMAIL_ENABLED, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
        EMAIL_FROM, SMTP_USE_TLS, RESEND_API_KEY,
    )

    # Quick TCP reachability check (5s timeout)
    tcp_ok = False
    tcp_error = None
    tcp_ms = None
    try:
        t0 = _time.monotonic()
        loop = _aio.get_event_loop()
        conn = await _aio.wait_for(
            loop.run_in_executor(
                None,
                lambda: _socket.create_connection((SMTP_HOST, SMTP_PORT), timeout=5),
            ),
            timeout=6.0,
        )
        conn.close()
        tcp_ms = round((_time.monotonic() - t0) * 1000)
        tcp_ok = True
    except _aio.TimeoutError:
        tcp_error = f"TCP timeout after 5s — port {SMTP_PORT} unreachable from Railway"
    except OSError as e:
        tcp_error = str(e)

    return {
        "email_enabled":    EMAIL_ENABLED,
        "method":           "resend" if RESEND_API_KEY else "smtp",
        "resend_key_set":   bool(RESEND_API_KEY),
        "smtp_host":        SMTP_HOST,
        "smtp_port":        SMTP_PORT,
        "smtp_user":        SMTP_USER or "(not set)",
        "smtp_pass_set":    bool(SMTP_PASS),
        "smtp_use_tls":     SMTP_USE_TLS,
        "email_from":       EMAIL_FROM,
        "tcp_reachable":    tcp_ok,
        "tcp_latency_ms":   tcp_ms,
        "tcp_error":        tcp_error,
    }


# ── /debug/test-resend ──────────────────────────────────────────────────
@router.post("/debug/test-resend")
async def debug_test_resend(
    debug_token: str = Query(...),
    to: str = Query(...),
) -> Dict[str, Any]:
    """Raw Resend API call — no template, no SMTP, just HTTP POST to Resend."""
    _check_token(debug_token)
    import httpx as _httpx  # noqa: PLC0415
    from core.config import RESEND_API_KEY, EMAIL_FROM  # noqa: PLC0415

    if not RESEND_API_KEY:
        return {"success": False, "error": "RESEND_API_KEY not set in Railway Variables"}

    payload = {
        "from":    EMAIL_FROM if EMAIL_FROM else "نحلة <support@nahlah.ai>",
        "to":      [to],
        "subject": "نحلة — اختبار مباشر",
        "html":    "<h1>🐝 نحلة</h1><p>اختبار مباشر — Resend API يعمل!</p>",
    }
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
        return {
            "success":     resp.status_code in (200, 201),
            "status_code": resp.status_code,
            "body":        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:500],
            "from":        payload["from"],
            "to":          to,
        }
    except _httpx.TimeoutException:
        return {"success": False, "error": "Resend API timeout (10s)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ── /debug/test-email ───────────────────────────────────────────────────
@router.post("/debug/test-email")
async def debug_test_email(
    debug_token: str = Query(..., description="Shared secret from env (DEBUG_ADMIN_TOKEN)"),
    to: str = Query(..., description="Recipient email address"),
    template: str = Query("welcome_email", description="Template name without .html"),
) -> Dict[str, Any]:
    """
    **Temporary** — send a test email without requiring a JWT.

    Protected by ``DEBUG_ADMIN_TOKEN`` query param (same as other /debug/* routes).
    Remove once Zoho SMTP is confirmed working in production.

    Example::

        POST /debug/test-email?debug_token=XXX&to=you@example.com
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import traceback as _tb  # noqa: PLC0415

    _check_token(debug_token)

    try:
        from core.config import EMAIL_ENABLED, SMTP_HOST, SMTP_PORT, SMTP_USER  # noqa: PLC0415
    except Exception as exc:
        return {"success": False, "error": f"config import error: {exc}"}

    if not EMAIL_ENABLED:
        return {
            "success":  False,
            "error":    "SMTP not configured — set SMTP_USER and SMTP_PASS in Railway variables",
            "smtp_host": SMTP_HOST,
            "smtp_port": SMTP_PORT,
            "smtp_user": SMTP_USER or "(not set)",
        }

    # Single-attempt send with a 25-second hard timeout so this endpoint
    # never hangs the server even if Zoho is unreachable.
    try:
        from services.email_service import send_email as _send  # noqa: PLC0415
    except Exception as exc:
        return {"success": False, "error": f"email_service import error: {exc}",
                "traceback": _tb.format_exc()}

    try:
        from core.config import RESEND_API_KEY, SMTP_HOST, SMTP_PORT, SMTP_USER  # noqa: PLC0415
        ok = await _send(
            to=to,
            subject=f"نحلة — اختبار قالب {template}",
            template=template,
            variables={
                "merchant_name": "مدير نحلة",
                "store_name":    "متجر الاختبار",
                "report_date":   "اليوم",
            },
        )
        if ok:
            return {
                "success":  True,
                "message":  f"تم إرسال الإيميل إلى {to}",
                "template": template,
                "method":   "resend" if RESEND_API_KEY else "smtp",
            }
        return {
            "success": False,
            "error":   "فشل الإرسال — راجع logs السيرفر",
            "method":  "resend" if RESEND_API_KEY else "smtp",
            "smtp_host": SMTP_HOST,
            "smtp_port": SMTP_PORT,
        }
    except Exception as exc:
        return {
            "success":   False,
            "error":     str(exc),
            "traceback": _tb.format_exc(),
        }


# ── /debug/scheduler-status ─────────────────────────────────────────────
@router.get("/debug/scheduler-status")
async def debug_scheduler_status(
    debug_token: str = Query(..., description="Shared secret from env"),
) -> Dict[str, Any]:
    """Health probe for the dedicated abandoned-cart reconciliation loop.

    Returns the in-memory state of
    :mod:`backend.core.abandoned_cart_scheduler` — when it last ran,
    how often it runs, when the next tick is due, and per-tenant
    success/failure for the most recent attempt.

    This is the canonical place to look when a merchant says
    "carts only show after manual sync": if ``last_cycle_at`` is
    older than ``interval_seconds`` ago, the scheduler is dead and
    Railway needs a restart. If ``last_runs[tenant_id].status`` is
    ``"error"`` and ``consecutive_failures`` is climbing, the
    integration itself is broken (token, scope, network).
    """
    _check_token(debug_token)
    from core.abandoned_cart_scheduler import get_state_snapshot  # noqa: PLC0415
    snap = get_state_snapshot()

    # Compute a human-readable "is the scheduler alive?" verdict so an
    # operator doesn't have to do timestamp math in their head.
    from datetime import datetime, timezone  # noqa: PLC0415
    verdict = "unknown"
    seconds_since_last = None
    if snap.get("last_cycle_at"):
        try:
            last = datetime.fromisoformat(snap["last_cycle_at"])
            seconds_since_last = int(
                (datetime.now(timezone.utc) - last).total_seconds()
            )
            # We expect a tick at least every interval+grace; be lenient
            # for the first cycle delay.
            grace = int(snap.get("interval_seconds", 300)) * 2
            verdict = "alive" if seconds_since_last <= grace else "stalled"
        except Exception:
            verdict = "unknown"
    elif snap.get("started_at"):
        verdict = "starting"

    return {
        "verdict":              verdict,
        "seconds_since_last":   seconds_since_last,
        "scheduler":            snap,
    }


# ── /debug/send-email (public JSON endpoint) ─────────────────────────────────

class _SendEmailBody(BaseModel):
    to:          str
    template:    str = "welcome_email"
    sender_type: Optional[str] = None


@router.post("/debug/send-email")
async def debug_send_email(body: _SendEmailBody) -> Dict[str, Any]:
    """
    **Temporary public endpoint** — send a test email via email_service.

    No auth required. Read-only (no DB writes).
    Remove or re-gate after SMTP/Resend is confirmed working.

    Body::

        {
            "to":          "you@example.com",
            "template":    "welcome_email",
            "sender_type": "welcome"          // optional
        }
    """
    from services.email_service import send_email, SENDER_MAP, TEMPLATE_SENDER  # noqa: PLC0415
    from core.config import EMAIL_ENABLED, RESEND_API_KEY  # noqa: PLC0415

    if not EMAIL_ENABLED:
        return {
            "success": False,
            "error":   "البريد الإلكتروني غير مفعّل — أضف RESEND_API_KEY في Railway Variables",
        }

    resolved_sender = body.sender_type or TEMPLATE_SENDER.get(body.template)

    ok = await send_email(
        to=body.to,
        subject=f"🐝 نحلة — اختبار قالب «{body.template}»",
        template=body.template,
        sender_type=body.sender_type,
        variables={
            "merchant_name": "مدير نحلة",
            "store_name":    "متجر الاختبار",
            "report_date":   "اليوم",
        },
    )

    if ok:
        return {
            "success":      True,
            "message":      f"✅ أُرسل إلى {body.to}",
            "template":     body.template,
            "sender_type":  resolved_sender or "default",
            "from":         SENDER_MAP.get(resolved_sender, SENDER_MAP[None]),
            "method":       "resend" if RESEND_API_KEY else "smtp",
        }
    return {
        "success":  False,
        "error":    "فشل الإرسال — راجع logs السيرفر",
        "template": body.template,
        "method":   "resend" if RESEND_API_KEY else "smtp",
    }
