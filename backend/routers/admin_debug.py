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


# ── Salla integration diagnostic ────────────────────────────────────────────


@router.get("/salla/integrations")
def salla_integrations_diagnostic(
    tenant_id: int = Query(..., description="Tenant to inspect"),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Show every Salla integration for a tenant, which one picker selects,
    and why each row is accepted or rejected.

    Returns:
      selected_id          — the integration_id that get_adapter() would use
      integrations[]       — sorted by score (best first), with fields:
        id, enabled, has_access_token, has_refresh_token, needs_reauth,
        needs_reauth_reason, token_expires_at, easy_mode, api_sync,
        score, selected, reason
    """
    _require_enabled(secret)
    from store_integration.registry import describe_integrations_for_tenant
    return describe_integrations_for_tenant(db, tenant_id)


# ── Salla live connection test ───────────────────────────────────────────────


@router.post("/salla/test-connection")
async def salla_test_connection(
    tenant_id: int = Query(..., description="Tenant to test"),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Run a live Salla API call using the integration picker for this tenant.

    Selects the canonical integration, then calls GET /products?per_page=1.

    Returns:
      success             — True if Salla returned 2xx
      status_code         — HTTP status from Salla
      selected_integration_id
      store_id
      has_access_token
      has_refresh_token
      needs_reauth
      error               — error message if failed
    """
    _require_enabled(secret)

    from store_integration.registry import (
        pick_active_salla_integration,
        describe_integrations_for_tenant,
    )
    from store_adapters.salla_adapter import SallaAdapter

    intg = pick_active_salla_integration(db, tenant_id)
    if not intg:
        return {
            "success": False,
            "error": "no Salla integration found for this tenant",
            "selected_integration_id": None,
        }

    cfg = intg.config or {}
    nr  = bool(cfg.get("needs_reauth"))
    hr  = bool(cfg.get("refresh_token"))
    ha  = bool(cfg.get("api_key"))

    if nr:
        return {
            "success":               False,
            "error":                 "integration needs_reauth=True — merchant must reconnect",
            "selected_integration_id": intg.id,
            "needs_reauth":          True,
            "needs_reauth_reason":   cfg.get("needs_reauth_reason"),
            "has_access_token":      ha,
            "has_refresh_token":     hr,
        }

    adapter = SallaAdapter(
        api_key=cfg.get("api_key", ""),
        store_id=cfg.get("store_id", ""),
        refresh_token=cfg.get("refresh_token", ""),
        tenant_id=tenant_id,
        integration_id=intg.id,
    )
    logger.info(
        "[TestConnection] tenant=%s integration_id=%s has_token=%s has_refresh=%s",
        tenant_id, intg.id, ha, hr,
    )

    import httpx as _httpx
    try:
        result = await adapter._get("/products?per_page=1")
        return {
            "success":               True,
            "status_code":           200,
            "selected_integration_id": intg.id,
            "store_id":              cfg.get("store_id"),
            "has_access_token":      ha,
            "has_refresh_token":     hr,
            "needs_reauth":          nr,
            "salla_response_preview": str(result)[:300],
        }
    except Exception as exc:
        return {
            "success":               False,
            "error":                 str(exc)[:400],
            "selected_integration_id": intg.id,
            "store_id":              cfg.get("store_id"),
            "has_access_token":      ha,
            "has_refresh_token":     hr,
            "needs_reauth":          bool((intg.config or {}).get("needs_reauth")),
        }


# ── Catalog audit ──────────────────────────────────────────────────────────────

@router.get("/catalog-audit")
def catalog_audit(
    tenant_id: int = Query(1),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Audit the product catalog for a tenant.

    Returns:
      total_products          — all products in DB
      synced                  — products with external_id (orderable)
      missing_external_id     — products without external_id (cannot create Salla orders)
      in_stock_synced         — synced + in_stock
      ready_for_salla_order   — first 20 products ready to order (external_id + in_stock)
      unsynced_products       — first 20 products missing external_id
    """
    _require_enabled(secret)

    from models import Product as _Product  # noqa: PLC0415

    all_products = (
        db.query(_Product)
        .filter(_Product.tenant_id == tenant_id)
        .order_by(_Product.id)
        .all()
    )

    synced, unsynced, ready, unavailable = [], [], [], []
    for p in all_products:
        ext_id = str(p.external_id or "").strip()
        in_stock = bool(getattr(p, "in_stock", True))
        price = str(getattr(p, "price", "") or "").strip()

        row = {
            "id": p.id,
            "title": p.title,
            "sku": getattr(p, "sku", "") or "",
            "external_id": ext_id or None,
            "in_stock": in_stock,
            "stock_qty": getattr(p, "stock_quantity", None),
            "price": price,
            "status": str((getattr(p, "extra_metadata", {}) or {}).get("status", "unknown")),
        }

        if ext_id:
            synced.append(row)
            if in_stock and price:
                ready.append(row)
            else:
                unavailable.append(row)
        else:
            unsynced.append(row)

    logger.info(
        "[CATALOG AUDIT] tenant=%s total=%d synced=%d unsynced=%d ready=%d",
        tenant_id, len(all_products), len(synced), len(unsynced), len(ready),
    )

    return {
        "tenant_id": tenant_id,
        "total_products": len(all_products),
        "synced_count": len(synced),
        "missing_external_id_count": len(unsynced),
        "in_stock_synced_count": len(ready),
        "ready_for_salla_order": ready[:20],
        "unsynced_products": unsynced[:20],
        "unavailable_synced": unavailable[:10],
    }


# ── Resync products from Salla ─────────────────────────────────────────────────

@router.post("/salla/resync-products")
async def salla_resync_products(
    tenant_id: int = Query(1),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Pull ALL products from Salla and upsert into Nahla DB.

    Match strategy (in order):
      1. external_id exact match — update in place
      2. SKU match              — link and update
      3. Title match (exact)    — link by name, flag for review
      4. No match               — create new row

    Returns a summary with created/updated/skipped counts and which
    products are now ready for Salla orders.
    """
    _require_enabled(secret)

    from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415
    from store_adapters.salla_adapter import SallaAdapter  # noqa: PLC0415
    from models import Product as _Product  # noqa: PLC0415

    intg = pick_active_salla_integration(db, tenant_id)
    if not intg:
        raise HTTPException(status_code=404, detail="No active Salla integration for this tenant")

    cfg = intg.config or {}
    if cfg.get("needs_reauth"):
        raise HTTPException(status_code=403, detail="Integration needs_reauth — merchant must reconnect Salla")

    adapter = SallaAdapter(
        api_key=cfg.get("api_key", ""),
        store_id=cfg.get("store_id", ""),
        refresh_token=cfg.get("refresh_token", ""),
        tenant_id=tenant_id,
        integration_id=intg.id,
    )

    # Fetch from Salla — paginate through all pages
    logger.info("[CATALOG SYNC] tenant=%s starting product resync from Salla", tenant_id)
    try:
        salla_products = await adapter.get_products()
    except Exception as exc:
        logger.error("[CATALOG SYNC] tenant=%s Salla fetch failed: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Salla fetch failed: {exc}")

    logger.info("[CATALOG SYNC] tenant=%s fetched_from_salla=%d", tenant_id, len(salla_products))

    # Load all existing products for this tenant (keyed by external_id, sku, title)
    existing = db.query(_Product).filter(_Product.tenant_id == tenant_id).all()
    by_ext   = {str(p.external_id or "").strip(): p for p in existing if p.external_id}
    by_sku   = {str(p.sku or "").strip().lower(): p for p in existing if p.sku}
    by_title = {str(p.title or "").strip().lower(): p for p in existing}

    created = 0
    updated = 0
    skipped = 0
    ready   = 0

    for raw in salla_products:
        if hasattr(raw, "dict"):
            raw = raw.dict()

        salla_id  = str(raw.get("id") or raw.get("external_id") or "").strip()
        title     = str(raw.get("title") or raw.get("name") or "").strip()
        sku       = str(raw.get("sku") or "").strip()
        price     = str(raw.get("price") or raw.get("regular_price") or "").strip()
        in_stock  = bool(raw.get("in_stock", True))
        stock_qty_raw = raw.get("quantity") or raw.get("stock_quantity")
        try:
            stock_qty = int(float(stock_qty_raw)) if stock_qty_raw is not None else None
        except (TypeError, ValueError):
            stock_qty = None

        status_raw = raw.get("status")
        if isinstance(status_raw, dict):
            status = str(status_raw.get("slug") or status_raw.get("name") or "active")
        else:
            status = str(status_raw or "active")

        if not salla_id:
            skipped += 1
            logger.warning("[CATALOG SYNC] tenant=%s skipping product without id | title=%r", tenant_id, title)
            continue

        # Match priority: ext_id → sku → title
        product = (
            by_ext.get(salla_id)
            or (by_sku.get(sku.lower()) if sku else None)
            or (by_title.get(title.lower()) if title else None)
        )

        extra = {
            "external_id": salla_id,
            "id": salla_id,
            "title": title,
            "sku": sku,
            "price": price,
            "status": status,
            "in_stock": in_stock,
            "stock_qty": stock_qty,
            "variants": raw.get("variants") or [],
            "options": raw.get("options") or [],
        }

        if product:
            was_synced = bool(str(product.external_id or "").strip())
            product.external_id    = salla_id
            product.title          = title or product.title
            product.sku            = sku or product.sku
            product.price          = price or product.price
            product.in_stock       = in_stock
            product.stock_quantity = stock_qty
            product.extra_metadata = extra
            updated += 1
            if not was_synced:
                logger.info(
                    "[CATALOG SYNC] linked existing product | tenant=%s "
                    "nahla_id=%s title=%r external_id=%s",
                    tenant_id, product.id, title, salla_id,
                )
        else:
            product = _Product(
                tenant_id      = tenant_id,
                external_id    = salla_id,
                title          = title,
                sku            = sku,
                price          = price,
                in_stock       = in_stock,
                stock_quantity = stock_qty,
                extra_metadata = extra,
            )
            db.add(product)
            created += 1

        # Count as ready if it has a price and is in stock
        if salla_id and in_stock and price:
            ready += 1

        logger.info(
            "[CATALOG SYNC] synced product | tenant=%s external_id=%s title=%r "
            "in_stock=%s price=%s status=%s",
            tenant_id, salla_id, title, in_stock, price, status,
        )

        # Update lookup caches so subsequent iterations see the new state
        by_ext[salla_id] = product
        if sku:
            by_sku[sku.lower()] = product
        if title:
            by_title[title.lower()] = product

    db.commit()

    logger.info(
        "[CATALOG SYNC] tenant=%s done | fetched=%d created=%d updated=%d "
        "skipped=%d products_ready_for_order=%d",
        tenant_id, len(salla_products), created, updated, skipped, ready,
    )

    return {
        "tenant_id":              tenant_id,
        "fetched_from_salla":     len(salla_products),
        "created":                created,
        "updated":                updated,
        "skipped":                skipped,
        "products_ready_for_order": ready,
        "message": (
            f"Synced {len(salla_products)} products from Salla. "
            f"{ready} are now ready for orders."
        ),
    }


# ── Salla integration audit ────────────────────────────────────────────────────

@router.get("/salla/integration-audit")
def salla_integration_audit(
    tenant_id: int = Query(1),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Full audit of every Salla integration row for a tenant.

    Shows for each integration:
      id, enabled, easy_mode, api_sync, has_access_token, has_refresh_token,
      needs_reauth, token_expires_at, scopes, is_canonical, superseded_by_id,
      selected_for_orders (which row the order flow would actually use)

    Also shows the reason the winning row was selected and why each other
    row was rejected.  Use this to diagnose "no usable token" failures.
    """
    _require_enabled(secret)

    from store_integration.registry import (  # noqa: PLC0415
        describe_integrations_for_tenant,
        _score_integration,
        _is_easy_mode,
        _is_api_sync,
        _needs_reauth,
    )
    from models import Integration as _Integration  # noqa: PLC0415

    audit = describe_integrations_for_tenant(db, tenant_id)

    # Enrich with extra fields not in describe_integrations_for_tenant
    rows = (
        db.query(_Integration)
        .filter(
            _Integration.tenant_id == tenant_id,
            _Integration.provider == "salla",
        )
        .order_by(_Integration.id.asc())
        .all()
    )
    rows_by_id = {r.id: r for r in rows}

    enriched = []
    for entry in audit.get("integrations", []):
        intg = rows_by_id.get(entry["id"])
        if not intg:
            enriched.append(entry)
            continue
        cfg = intg.config or {}

        # Determine usability for orders
        ha = bool(cfg.get("api_key"))
        hr = bool(cfg.get("refresh_token"))
        nr = _needs_reauth(intg)

        if not ha:
            order_usability = "BLOCKED — no access_token"
        elif nr:
            order_usability = "BLOCKED — needs_reauth (merchant must reconnect)"
        elif not intg.enabled:
            order_usability = "BLOCKED — integration disabled"
        elif not hr:
            order_usability = "DEGRADED — access_token only, no refresh_token (will fail if token expired)"
        else:
            order_usability = "OK — has access_token + refresh_token"

        enriched.append({
            **entry,
            "api_key_source":    cfg.get("api_key_source", "unknown"),
            "app_type":          cfg.get("app_type", "unknown"),
            "scopes":            cfg.get("scopes", []),
            "external_store_id": intg.external_store_id,
            "order_usability":   order_usability,
            "token_age_note": (
                "Check token_expires_at — Salla access tokens last ~14 days"
                if ha and not hr else
                "Refresh token available — auto-renewal possible"
                if ha and hr else
                "No token at all — must reconnect"
            ),
        })

    selected_id = audit.get("selected_id")
    selected_entry = next((e for e in enriched if e["id"] == selected_id), None)

    # Actionable recommendation
    recommendation = "unknown"
    if not enriched:
        recommendation = "No Salla integrations found. Ask merchant to connect Salla from the dashboard."
    elif selected_entry:
        usability = selected_entry.get("order_usability", "")
        if "BLOCKED — needs_reauth" in usability:
            recommendation = (
                "Selected integration needs_reauth. Run POST /admin/debug/salla/cleanup "
                "then have the merchant reinstall the Nahla app from Salla App Store."
            )
        elif "BLOCKED — no access_token" in usability:
            recommendation = "Selected integration has no token. Run salla/cleanup then reconnect."
        elif "DEGRADED" in usability:
            recommendation = (
                "access_token is present but there is NO refresh_token. "
                "Orders will work if the token is still valid (< 14 days old). "
                "To fix permanently: run POST /admin/debug/salla/cleanup then have the "
                "merchant reinstall the Nahla app from Salla App Store to get fresh OAuth tokens."
            )
        elif "OK" in usability:
            recommendation = "Integration looks healthy. If orders still fail check Railway logs for 401."

    logger.info(
        "[Salla Integration Audit] tenant=%s total=%d selected_id=%s usability=%s",
        tenant_id, len(enriched), selected_id,
        selected_entry.get("order_usability") if selected_entry else "N/A",
    )

    return {
        "tenant_id":         tenant_id,
        "total_integrations": len(enriched),
        "selected_id":       selected_id,
        "recommendation":    recommendation,
        "integrations":      enriched,
    }
