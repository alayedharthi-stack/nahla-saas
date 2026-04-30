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


# ── Salla integration cleanup (preflight for fresh Easy Mode install) ────────


def _snapshot_salla_integration(intg) -> dict:
    cfg = intg.config or {}
    return {
        "id":                 intg.id,
        "provider":           intg.provider,
        "tenant_id":          intg.tenant_id,
        "enabled":            intg.enabled,
        "external_store_id":  intg.external_store_id,
        "store_id_in_config": cfg.get("store_id"),
        "store_name":         cfg.get("store_name"),
        "easy_mode": (
            (cfg.get("app_type") or "").lower() == "easy"
            or (cfg.get("api_key_source") or "").lower() == "easy_mode_webhook"
        ),
        "app_type":           cfg.get("app_type"),
        "api_key_source":     cfg.get("api_key_source"),
        "has_access_token":   bool(cfg.get("api_key")),
        "has_refresh_token":  bool(cfg.get("refresh_token")),
        "needs_reauth":       bool(cfg.get("needs_reauth")),
        "needs_reauth_reason": cfg.get("needs_reauth_reason"),
        "no_auto_refresh":    bool(cfg.get("no_auto_refresh")),
        "connected_at":       cfg.get("connected_at"),
        "last_token_refresh": cfg.get("last_token_refresh"),
    }


@router.get("/salla/cleanup")
def salla_cleanup_preview(
    tenant_id: int = Query(1, description="Tenant whose Salla rows to inspect"),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Preview only — returns the snapshot of every Salla integration row
    for this tenant WITHOUT modifying anything.  Use this first to
    confirm what would be disabled before calling the POST below.
    """
    _require_enabled(secret)
    from models import Integration  # noqa: PLC0415
    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .order_by(Integration.id.asc())
        .all()
    )
    return {
        "ok":           True,
        "preview":      True,
        "tenant_id":    tenant_id,
        "count":        len(rows),
        "integrations": [_snapshot_salla_integration(r) for r in rows],
        "next": (
            "POST /admin/debug/salla/cleanup?tenant_id=1 to disable these rows. "
            "Then reinstall the Nahla app from https://s.salla.sa/apps for the "
            "merchant — Easy Mode will deliver fresh tokens via the "
            "app.store.authorize webhook."
        ),
    }


@router.post("/salla/cleanup")
def salla_cleanup_execute(
    tenant_id: int = Query(1, description="Tenant whose Salla rows to disable"),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Soft-disable every Salla integration row for `tenant_id` so a fresh
    Easy Mode install via app.store.authorize creates a clean new row.

    What this does (per-row):
      • enabled                              = False
      • config.superseded_by_oauth_reconnect = True
      • config.disabled_reason               = "manual_cleanup_before_easy_mode_oauth"
      • config.disabled_at                   = ISO timestamp
      • Clears all needs_reauth / no_auto_refresh flags
      • external_store_id is intentionally PRESERVED — the Easy-mode
        webhook (_handle_salla_authorize) looks up by external_store_id
        to find the right tenant.  When the merchant reinstalls from
        Salla App Store, that handler finds this disabled row, scrubs
        our cleanup markers, overwrites api_key + refresh_token with
        the fresh ones from Salla, sets app_type='easy', and flips
        enabled back to True — so the row stays attached to tenant 1
        but every byte of stale state is replaced.

    Returns the BEFORE snapshot (what was disabled) so an operator can
    audit the change.
    """
    _require_enabled(secret)
    from models import Integration  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    from datetime import datetime, timezone               # noqa: PLC0415

    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .order_by(Integration.id.asc())
        .all()
    )

    if not rows:
        logger.info("[admin-debug] salla cleanup tenant=%s — nothing to do", tenant_id)
        return {
            "ok":          True,
            "tenant_id":   tenant_id,
            "disabled":    0,
            "before":      [],
            "next":        (
                "No existing Salla integration for this tenant. Install "
                "the Nahla app from https://s.salla.sa/apps to create one."
            ),
        }

    before = [_snapshot_salla_integration(r) for r in rows]
    now    = datetime.now(timezone.utc).isoformat()

    for r in rows:
        cfg = dict(r.config or {})
        cfg["superseded_by_oauth_reconnect"] = True
        cfg["disabled_reason"]               = "manual_cleanup_before_easy_mode_oauth"
        cfg["disabled_at"]                   = now
        # Clear all reauth/refresh flags so they don't pollute future state
        cfg.pop("needs_reauth",           None)
        cfg.pop("needs_reauth_reason",    None)
        cfg.pop("needs_reauth_at",        None)
        cfg.pop("no_auto_refresh",        None)
        cfg.pop("no_auto_refresh_reason", None)
        cfg.pop("no_auto_refresh_at",     None)

        r.config  = cfg
        r.enabled = False
        # external_store_id is intentionally NOT cleared so Easy-mode
        # reinstall webhook can find this row and reactivate it under
        # the same tenant_id.
        flag_modified(r, "config")

    db.commit()
    logger.warning(
        "[admin-debug] salla cleanup tenant=%s — disabled %d row(s) ids=%s",
        tenant_id, len(rows), [r.id for r in rows],
    )
    audit(
        "admin_debug_salla_cleanup",
        tenant_id  = tenant_id,
        disabled   = len(rows),
        ids        = [r.id for r in rows],
    )

    return {
        "ok":         True,
        "tenant_id":  tenant_id,
        "disabled":   len(rows),
        "before":     before,
        "next": [
            "1. Confirm Salla Partner Portal app is in Easy Mode and Application URL = https://app.nahlah.ai/app/salla",
            "2. Ask the merchant to open https://s.salla.sa/apps in their Salla account",
            "3. Find the Nahla app, click 'إلغاء التثبيت' if installed, then click 'تثبيت' again",
            "4. Salla will hit POST /webhook/salla with event=app.store.authorize and fresh access_token + refresh_token",
            "5. _handle_salla_authorize finds the disabled row by external_store_id, scrubs cleanup markers, overwrites tokens, sets app_type='easy', api_key_source='easy_mode_webhook', enabled=True — and keeps it attached to tenant 1",
            "6. Verify with GET /admin/salla/integrations?tenant_id=1 — the row should show easy_mode=true, has_refresh_token=true, needs_reauth=false, enabled=true",
        ],
        "verify": {
            "list_integrations": f"/admin/salla/integrations?tenant_id={tenant_id}",
            "poller_diag":       f"/admin/salla/orders-poller/diag?tenant_id={tenant_id}",
            "force_poll":        f"/admin/salla/orders-poller/run-once?tenant_id={tenant_id}&lookback_minutes=1440",
            "webhook_status":    "/admin/debug/salla/webhook-status",
        },
    }


# ── Webhook delivery diagnostic ─────────────────────────────────────────────


@router.get("/salla/webhook-status")
def salla_webhook_status(
    limit: int = Query(20, ge=1, le=100),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Returns proof of whether Salla is actually hitting POST /webhook/salla.

    Includes:
      • route_registered    — sanity check (always True if this endpoint
                              responds, since both routes live in the same app)
      • webhook_url         — the URL that MUST be set in Salla Partner
                              Dashboard → Webhooks for the Nahla app
      • event_counts        — last 24h counts grouped by event_type
      • status_counts       — last 24h counts grouped by status
      • recent_events       — newest `limit` rows from webhook_events
                              (provider='salla') with the fields most useful
                              for diagnosing missing app.store.authorize

    If `recent_events` is empty (or none in the last 24h) and the merchant
    has just reinstalled the app, the problem is on the Salla side — the
    Webhook URL in https://salla.dev/dashboard is wrong, or the Nahla
    Partner App is not the one the merchant installed.
    """
    _require_enabled(secret)
    from models import WebhookEvent  # noqa: PLC0415
    from sqlalchemy import func        # noqa: PLC0415
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    event_counts_q = (
        db.query(WebhookEvent.event_type, func.count(WebhookEvent.id))
        .filter(WebhookEvent.provider == "salla")
        .filter(WebhookEvent.received_at >= cutoff)
        .group_by(WebhookEvent.event_type)
        .all()
    )
    event_counts = {
        (et or "<none>"): int(c) for et, c in event_counts_q
    }

    status_counts_q = (
        db.query(WebhookEvent.status, func.count(WebhookEvent.id))
        .filter(WebhookEvent.provider == "salla")
        .filter(WebhookEvent.received_at >= cutoff)
        .group_by(WebhookEvent.status)
        .all()
    )
    status_counts = {
        (st or "<none>"): int(c) for st, c in status_counts_q
    }

    recent = (
        db.query(WebhookEvent)
        .filter(WebhookEvent.provider == "salla")
        .order_by(WebhookEvent.id.desc())
        .limit(limit)
        .all()
    )

    def _row(ev) -> dict:
        payload = ev.parsed_payload or {}
        data    = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}
        return {
            "id":                 ev.id,
            "received_at":        ev.received_at.isoformat() if ev.received_at else None,
            "event_type":         ev.event_type,
            "store_id":           ev.store_id,
            "status":             ev.status,
            "attempts":           ev.attempts,
            "signature_valid":    ev.signature_valid,
            "tenant_id":          ev.tenant_id,
            "external_event_id":  ev.external_event_id,
            "has_access_token":   bool(data.get("access_token")  or payload.get("access_token")),
            "has_refresh_token":  bool(data.get("refresh_token") or payload.get("refresh_token")),
            "last_error":         (ev.last_error or "")[:300] if ev.last_error else None,
            "processed_at":       ev.processed_at.isoformat() if ev.processed_at else None,
        }

    total = (
        db.query(func.count(WebhookEvent.id))
        .filter(WebhookEvent.provider == "salla")
        .scalar()
    ) or 0
    last_24h = (
        db.query(func.count(WebhookEvent.id))
        .filter(WebhookEvent.provider == "salla")
        .filter(WebhookEvent.received_at >= cutoff)
        .scalar()
    ) or 0

    return {
        "ok":               True,
        "route_registered": True,
        "webhook_url":      "https://api.nahlah.ai/webhook/salla",
        "totals": {
            "all_time":    int(total),
            "last_24h":    int(last_24h),
            "shown":       len(recent),
        },
        "event_counts_24h":  event_counts,
        "status_counts_24h": status_counts,
        "recent_events":     [_row(r) for r in recent],
        "hint": (
            "If recent_events is empty after a fresh reinstall, the Webhook "
            "URL in Salla Partner Dashboard is not pointing at "
            "https://api.nahlah.ai/webhook/salla — fix it there and reinstall again."
        ),
    }
