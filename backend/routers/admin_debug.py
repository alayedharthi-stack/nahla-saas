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
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import hash_password, require_admin
from core.database import get_db
from core.audit import audit
from models import Tenant, User, WhatsAppConnection, WhatsAppTemplate

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
    preview: bool = Query(
        True,
        description="True (default) returns a dry-run preview without DB changes. "
                    "Pass preview=false to actually disable the integrations.",
    ),
    db: Session = Depends(get_db),
):
    """
    Disable every Salla integration row for ``tenant_id`` so a fresh Easy
    Mode reinstall via app.store.authorize creates valid tokens.

    Modes:
      • ``preview=true``  (default) — returns the snapshot, NO DB changes.
      • ``preview=false``           — actually disables every row:
            enabled                       = False
            config.needs_reauth           = True
            config.needs_reauth_reason    = "manual_cleanup_for_reinstall"
            config.cleanup_at             = <ISO timestamp>
            external_store_id is PRESERVED (Easy-mode webhook keys on it).

    Response shape::

        {
          "ok": true,
          "preview": false,
          "disabled_count": 1,
          "integrations": [
            {"id": 3, "enabled_before": true, "enabled_after": false,
             "needs_reauth": true, "needs_reauth_reason": "manual_cleanup_for_reinstall"}
          ],
          "next": "Reinstall Nahla app from Salla to receive fresh tokens with refresh_token."
        }
    """
    _require_enabled(secret)
    from models import Integration                          # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified     # noqa: PLC0415
    from datetime import datetime, timezone                 # noqa: PLC0415

    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .order_by(Integration.id.asc())
        .all()
    )

    # ── Build the integrations[] array showing before/after for each row ─────
    integrations_summary = []
    for r in rows:
        cfg_before = r.config or {}
        integrations_summary.append({
            "id":                  r.id,
            "enabled_before":      bool(r.enabled),
            "enabled_after":       bool(r.enabled) if preview else False,
            "needs_reauth":        cfg_before.get("needs_reauth") if preview else True,
            "needs_reauth_reason": (
                cfg_before.get("needs_reauth_reason") if preview
                else "manual_cleanup_for_reinstall"
            ),
            "external_store_id":   r.external_store_id,
            "had_refresh_token":   bool(cfg_before.get("refresh_token")),
        })

    # ── Preview mode: return without modifying anything ──────────────────────
    if preview:
        logger.info(
            "[Salla Cleanup] preview only | tenant=%s count=%d",
            tenant_id, len(rows),
        )
        return {
            "ok":             True,
            "preview":        True,
            "tenant_id":      tenant_id,
            "disabled_count": 0,
            "integrations":   integrations_summary,
            "next":           "Run again with preview=false to apply cleanup.",
        }

    # ── Execute: disable rows + mark needs_reauth ────────────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()
    disabled_count = 0
    for r in rows:
        cfg = dict(r.config or {})
        cfg["needs_reauth"]        = True
        cfg["needs_reauth_reason"] = "manual_cleanup_for_reinstall"
        cfg["cleanup_at"]          = now_iso
        # Preserve diagnostic markers in case operators need history
        cfg["disabled_at"]         = now_iso
        cfg["disabled_reason"]     = "manual_cleanup_for_reinstall"

        r.config  = cfg
        r.enabled = False
        flag_modified(r, "config")
        disabled_count += 1

        logger.warning(
            "[Salla Cleanup] disabled integration | tenant=%s integration_id=%s "
            "reason=manual_cleanup_for_reinstall",
            tenant_id, r.id,
        )

    db.commit()
    audit(
        "admin_debug_salla_cleanup",
        tenant_id      = tenant_id,
        preview        = False,
        disabled_count = disabled_count,
        ids            = [r.id for r in rows],
    )

    return {
        "ok":             True,
        "preview":        False,
        "tenant_id":      tenant_id,
        "disabled_count": disabled_count,
        "integrations":   integrations_summary,
        "next": (
            "Reinstall Nahla app from Salla to receive fresh tokens with "
            "refresh_token."
        ),
        "verify": {
            "list_integrations": f"/admin/salla/integrations?tenant_id={tenant_id}",
            "integration_audit": f"/admin/debug/salla/integration-audit?tenant_id={tenant_id}",
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


# ── Store Checkout Profile ────────────────────────────────────────────────────


@router.post("/salla/sync-checkout-profile")
async def salla_sync_checkout_profile(
    tenant_id: int = Query(..., description="Tenant to sync"),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Fetch shipping companies, zones, delivery methods, and payment methods
    from Salla and persist the result into ``Integration.config['checkout_profile']``.

    This endpoint populates the ``store_checkout_profile`` so that order
    creation no longer relies on hardcoded values.

    Returns the full profile as stored.
    """
    _require_enabled(secret)

    from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415
    from store_adapters.salla_adapter import SallaAdapter                 # noqa: PLC0415
    from models import Integration as _Integration                        # noqa: PLC0415
    import sqlalchemy as _sa                                              # noqa: PLC0415

    intg = pick_active_salla_integration(db, tenant_id)
    if not intg:
        return {"success": False, "error": "no Salla integration found for this tenant"}

    cfg = intg.config or {}
    adapter = SallaAdapter(
        api_key=cfg.get("api_key", ""),
        store_id=cfg.get("store_id", ""),
        refresh_token=cfg.get("refresh_token", ""),
        tenant_id=tenant_id,
        integration_id=intg.id,
    )

    try:
        profile = await adapter.sync_store_checkout_profile()
    except Exception as exc:
        logger.error(
            "[CheckoutProfile] sync failed | tenant=%s err=%s", tenant_id, exc, exc_info=True,
        )
        return {"success": False, "error": str(exc)}

    # Persist into Integration.config so it survives process restarts
    new_config = dict(cfg)
    new_config["checkout_profile"] = profile
    db.execute(
        _sa.update(_Integration)
        .where(_Integration.id == intg.id)
        .values(config=new_config),
    )
    db.commit()

    logger.error(
        "[CHECKOUT PROFILE] synced and saved | tenant=%s integration_id=%s "
        "delivery_methods=%s default_delivery_method=%s default_company=%s",
        tenant_id, intg.id,
        profile.get("delivery_methods"),
        profile.get("default_delivery_method"),
        profile.get("default_shipping_company_id"),
    )

    return {
        "success":        True,
        "integration_id": intg.id,
        "profile":        profile,
    }


@router.get("/salla/checkout-profile")
def salla_get_checkout_profile(
    tenant_id: int = Query(..., description="Tenant to inspect"),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Return the last-synced ``store_checkout_profile`` for a tenant.

    Also shows the in-memory cached delivery_method so you can compare what
    is saved in the DB vs what the current process is using.
    """
    _require_enabled(secret)

    from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415
    from store_adapters.salla_adapter import (                            # noqa: PLC0415
        _CHECKOUT_PROFILE_CACHE,
        _DELIVERY_METHOD_CACHE,
        _SUPPORTS_DELIVERY_METHOD_CACHE,
    )

    intg = pick_active_salla_integration(db, tenant_id)
    if not intg:
        return {"success": False, "error": "no Salla integration found for this tenant"}

    cfg = intg.config or {}
    db_profile = cfg.get("checkout_profile")
    mem_profile = _CHECKOUT_PROFILE_CACHE.get(tenant_id)
    mem_dm      = _DELIVERY_METHOD_CACHE.get(tenant_id)

    readiness: dict = {"ok": True, "issues": []}
    if not db_profile:
        readiness["ok"] = False
        readiness["issues"].append("checkout_profile not synced — run POST /salla/sync-checkout-profile")
    else:
        if not db_profile.get("delivery_methods"):
            readiness["ok"] = False
            readiness["issues"].append("no delivery_methods in profile")
        if not db_profile.get("shipping_companies"):
            readiness["issues"].append("no shipping_companies — orders may lack shipping block")
        if not db_profile.get("default_delivery_method"):
            readiness["ok"] = False
            readiness["issues"].append("default_delivery_method is null")

    mem_supports = _SUPPORTS_DELIVERY_METHOD_CACHE.get(tenant_id)

    return {
        "tenant_id":                        tenant_id,
        "integration_id":                   intg.id,
        "readiness":                        readiness,
        "db_profile":                       db_profile,
        "in_memory_profile":                mem_profile,
        "in_memory_delivery_method":        mem_dm,
        "in_memory_supports_delivery_method": mem_supports,
        "hint": (
            "Run POST /admin/debug/salla/sync-checkout-profile to refresh the profile."
            if not db_profile else
            f"Profile looks healthy. Default delivery_method={db_profile.get('default_delivery_method')}"
        ),
    }


# ════════════════════════════════════════════════════════════════════════
# WhatsApp DIRECT-SEND DEBUG
# ════════════════════════════════════════════════════════════════════════
#
# Purpose: let platform support staff fire a single template message
# through the live 360dialog/Meta connection of any tenant WITHOUT going
# anywhere near the campaign engine. This bypasses:
#
#   * campaign_send_logs persistence
#   * frequency caps
#   * retries (single try, raw result)
#   * idempotency / dedup
#   * snapshotting / audience filters
#
# It exists because the campaign pipeline is now hardened enough that
# when a merchant says "the template isn't arriving", we need to be able
# to ask "does the connection itself work, in isolation, right now?".
# If THIS endpoint succeeds and the campaign still fails, the bug is in
# the campaign layer; if THIS endpoint also fails we have an isolated
# reproduction we can hand to 360dialog support.
#
# Security: gated by `require_admin` only. The endpoint does NOT use the
# extra ENABLE_ADMIN_DEBUG flag because (a) admins are already platform
# staff with explicit JWT roles, and (b) we want this available in
# production at all times — that's the whole point of having it.
#
# Masking: the response masks the recipient phone (keeps first 4 + last
# 3 digits) and never returns the bearer token. Logs are masked the
# same way.

_DIRECT_SEND_PATH_WHITELIST_DOC = """
POST body shape (admin-only):

    {
      "phone_number_id": "100543193146977",   // required
      "to":              "+966537970430",     // required, E.164
      "template":        "nahla_special_offer_c874",
      "language":        "ar",                // optional, default 'ar'
      "merchant_vars":   { "1": "Hisham", "2": "499" }  // optional
    }
"""


class _DirectSendBody(BaseModel):
    """Request schema for the admin direct-template-send debug route.

    ``tenant_id`` is intentionally not in the body: we resolve it from
    ``phone_number_id`` so admins can target any tenant without first
    looking up its numeric id. This also matches `_post_wa`'s reverse
    resolution path (so any tenant a merchant connected to today is
    reachable here even if its name changed).
    """
    phone_number_id: str   = Field(..., min_length=4, max_length=64)
    to:              str   = Field(..., min_length=4, max_length=32)
    template:        str   = Field(..., min_length=2, max_length=128)
    language:        str   = Field("ar", min_length=2, max_length=12)
    merchant_vars:   Optional[Dict[str, str]] = Field(
        default=None,
        description="Optional placeholder vars keyed by '1', '2', ...",
    )


def _mask_phone(raw: str) -> str:
    """Return a masked, log-safe form of a phone number.

    Strategy: keep first 4 digits (country code) and last 3 digits
    (so support can match an arriving ticket against the right
    customer), redact the middle. Handles E.164 (+966...) and
    bare digits. Never returns more than 11 characters.

      "+966537970430" → "+9665***430"
      "966537970430"  → "9665***430"
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if len(s) <= 7:
        return "***"
    head = s[:4]
    tail = s[-3:]
    return f"{head}***{tail}"


def _mask_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-mask a Meta API payload for safe logging/return.

    Currently only masks the top-level `to` field; templates do not
    carry the bearer token or any other sensitive merchant data. We
    don't mask placeholder values inside `template.components` —
    those are merchant-provided test inputs and showing them is the
    whole point of a debug endpoint.
    """
    if not isinstance(payload, dict):
        return payload
    masked = dict(payload)
    if "to" in masked:
        masked["to"] = _mask_phone(str(masked.get("to") or ""))
    return masked


def _extract_provider_message_id(resp_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Pull ``messages[0].id`` out of a Meta / 360dialog response.

    Both providers wrap the wamid in ``{"messages": [{"id": "<wamid>"}]}``
    on success. Returns None on any structural mismatch — callers should
    treat ``None`` as "the send did NOT succeed" even if HTTP 200.
    """
    if not isinstance(resp_data, dict):
        return None
    msgs = resp_data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    first = msgs[0]
    if not isinstance(first, dict):
        return None
    mid = first.get("id")
    return str(mid).strip() if mid else None


@router.post("/whatsapp/send-template")
async def admin_debug_send_template(
    body: _DirectSendBody,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Fire a single WhatsApp template message synchronously, bypassing
    every safety net the campaign pipeline adds.

    Returns the raw provider response (Meta or 360dialog) so support
    can read the exact error code/subcode/fbtrace_id without scraping
    logs. Phone numbers are masked on the way in and the way out.

    Errors caught:
      * 404 — phone_number_id not connected to any tenant
      * 404 — template not found / not approved for the tenant
      * 502 — provider call exploded (network, auth, etc.)

    Success shape:
      {
        "ok": bool,
        "http_status": 200,
        "provider": "meta_cloud" | "360dialog",
        "phone_number_id": "100543193146977",
        "tenant_id": 33,
        "template": "nahla_special_offer_c874",
        "language": "ar",
        "to_masked": "+9665***430",
        "raw_request_masked": { ... full payload, `to` redacted ... },
        "raw_response": { ... unmodified provider body ... },
        "provider_message_id": "wamid.HBgNOTY..."
      }
    """
    from services.whatsapp_platform.service import provider_send_message, wa_provider  # noqa: PLC0415
    from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_360DIALOG  # noqa: PLC0415
    from services.campaign_wizard.test_send import build_test_payload  # noqa: PLC0415

    started_at = time.time()
    admin_sub = _admin.get("sub") or "?"

    # ── 1. Resolve tenant via phone_number_id ────────────────────
    # We deliberately key by phone_number_id (not tenant_id from the
    # admin JWT) so support can target any connected merchant by
    # quoting the number from their conversation tab.
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.phone_number_id == body.phone_number_id)
        .order_by(WhatsAppConnection.id.desc())
        .first()
    )
    if not wa_conn:
        logger.warning(
            "[ADMIN/WA_SEND_TEMPLATE_DEBUG] unknown phone_number_id=%s admin=%s",
            body.phone_number_id, admin_sub,
        )
        raise HTTPException(
            status_code=404,
            detail=f"no WhatsAppConnection found for phone_number_id={body.phone_number_id!r}",
        )
    tenant_id = int(wa_conn.tenant_id)

    # ── 2. Resolve template on that tenant ───────────────────────
    template = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == tenant_id,
            WhatsAppTemplate.name == body.template,
        )
        .order_by(WhatsAppTemplate.id.desc())
        .first()
    )
    if not template:
        logger.warning(
            "[ADMIN/WA_SEND_TEMPLATE_DEBUG] unknown template tenant=%s name=%s admin=%s",
            tenant_id, body.template, admin_sub,
        )
        raise HTTPException(
            status_code=404,
            detail=(
                f"template {body.template!r} not found on tenant {tenant_id}. "
                f"check that the merchant has synced their templates."
            ),
        )

    # We honour the request's language code by overriding template
    # locally — saves admins having to maintain ar/en variants when
    # they're testing the same template on a different language code.
    effective_template = template
    if (body.language or "").strip() and body.language.strip() != (template.language or ""):
        # Shallow copy via the ORM (without committing) so the payload
        # builder sees the requested language without touching the DB.
        from types import SimpleNamespace  # noqa: PLC0415
        effective_template = SimpleNamespace(
            name=template.name,
            language=body.language.strip(),
            components=getattr(template, "components", None),
        )

    # ── 3. Build the Meta payload ────────────────────────────────
    try:
        payload = build_test_payload(
            effective_template,
            to_phone_e164=body.to.strip(),
            merchant_vars=body.merchant_vars or {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    masked_request = _mask_payload(payload)
    provider_kind = wa_provider(wa_conn)
    provider_label = (
        "360dialog" if provider_kind == WHATSAPP_PROVIDER_360DIALOG else "meta_cloud"
    )

    logger.info(
        "[ADMIN/WA_SEND_TEMPLATE_DEBUG] start admin=%s tenant=%s phone_number_id=%s "
        "provider=%s template=%s language=%s to_masked=%s",
        admin_sub, tenant_id, body.phone_number_id, provider_label,
        body.template, body.language, _mask_phone(body.to),
    )

    # ── 4. Single-shot provider call (NO retries, NO campaign log) ───
    resp_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    http_status = 200
    try:
        resp_data, ctx = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="admin_debug_send_template",
            phone_id=body.phone_number_id,
            payload=payload,
            prefer_platform=bool(
                getattr(wa_conn, "connection_type", None) == "direct"
            ),
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        error_message = f"{type(exc).__name__}: {exc}"
        http_status = 502
        logger.warning(
            "[ADMIN/WA_SEND_TEMPLATE_DEBUG] provider call exploded admin=%s tenant=%s "
            "err=%s",
            admin_sub, tenant_id, error_message,
        )
        resp_data = {"error": {"message": error_message, "type": "provider_exception"}}

    duration_ms = int((time.time() - started_at) * 1000)
    provider_message_id = _extract_provider_message_id(resp_data)
    has_error = isinstance(resp_data, dict) and "error" in resp_data
    ok = (http_status == 200) and (provider_message_id is not None) and (not has_error)

    logger.info(
        "[ADMIN/WA_SEND_TEMPLATE_DEBUG] done admin=%s tenant=%s provider=%s "
        "ok=%s provider_message_id=%s has_error=%s duration_ms=%d",
        admin_sub, tenant_id, provider_label, ok,
        provider_message_id or "—", has_error, duration_ms,
    )

    audit(
        "admin_debug_send_template",
        admin_sub=admin_sub,
        tenant_id=tenant_id,
        phone_number_id=body.phone_number_id,
        template=body.template,
        language=body.language,
        to_masked=_mask_phone(body.to),
        provider=provider_label,
        ok=ok,
        provider_message_id=provider_message_id or "",
        has_error=has_error,
        duration_ms=duration_ms,
    )

    return {
        "ok":                  bool(ok),
        "http_status":         http_status,
        "provider":            provider_label,
        "phone_number_id":     body.phone_number_id,
        "tenant_id":           tenant_id,
        "template":            body.template,
        "language":            body.language,
        "to_masked":           _mask_phone(body.to),
        "raw_request_masked":  masked_request,
        "raw_response":        resp_data,
        "provider_message_id": provider_message_id,
        "duration_ms":         duration_ms,
        "error_message":       error_message,
    }


# ════════════════════════════════════════════════════════════════════════
# MEDIA PIPELINE ENV DIAGNOSTICS
# ════════════════════════════════════════════════════════════════════════
#
# When the conversation drawer shows "تعذر عرض الصورة" / "لم يتم تفريغ
# التسجيل (الميزة غير مفعّلة)" — the merchant has no way to tell whether
# the problem is:
#
#   * OPENAI_API_KEY missing on Railway
#   * NAHLA_INBOUND_MEDIA_DIR pointing at a non-existent / read-only
#     volume (so storage writes silently fail)
#   * Volume mounted but empty after a redeploy
#   * Vision / STT model env var set to something nonsensical
#
# This endpoint returns a structured snapshot platform support can
# read in one HTTP call. It NEVER returns API key values — only
# "present: true|false" and the last 4 characters when present (so we
# can confirm the key isn't truncated without leaking it).


def _mask_secret_tail(value: Optional[str]) -> Optional[str]:
    """Return a hint we can show in a debug response. We expose only
    the last 4 characters so support can verify the secret matches
    what they set, without ever transmitting the full value."""
    s = (value or "").strip()
    if not s:
        return None
    if len(s) <= 4:
        return "***"
    return f"***{s[-4:]}"


@router.get("/media-env")
async def admin_debug_media_env(
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Diagnostic snapshot of the inbound-media pipeline configuration.

    Reads:
      * OPENAI_API_KEY                (presence + last 4)
      * OPENAI_API_BASE               (full — public)
      * OPENAI_MODEL / OPENAI_AUDIO_MODEL / OPENAI_VISION_MODEL
      * NAHLA_STT_LANGUAGE
      * INBOUND_MEDIA_MAX_BYTES       (parsed integer)
      * NAHLA_INBOUND_MEDIA_DIR       (full path)
      * Storage root existence + writability + free space

    The writability probe creates and deletes a tiny temp file in the
    storage root — the same operation `save_inbound_media` performs.
    A failure here is the single best indicator of why audio / image
    persistence is silently dropping.
    """
    import shutil  # noqa: PLC0415
    from core.config import (  # noqa: PLC0415
        INBOUND_MEDIA_MAX_BYTES,
        NAHLA_STT_LANGUAGE,
        OPENAI_API_BASE,
        OPENAI_API_KEY,
        OPENAI_AUDIO_MODEL,
        OPENAI_MODEL,
        OPENAI_VISION_MODEL,
    )
    from services.inbound_media_storage import storage_root  # noqa: PLC0415

    root = storage_root()
    root_str = str(root)
    root_exists = root.exists()
    root_writable = False
    write_probe_error: Optional[str] = None
    free_bytes: Optional[int] = None
    if root_exists:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".__nahla_writable_probe"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
            root_writable = True
        except Exception as exc:  # noqa: BLE001
            write_probe_error = f"{type(exc).__name__}: {exc}"
        try:
            free_bytes = shutil.disk_usage(root_str).free
        except Exception:
            free_bytes = None
    else:
        # If the directory doesn't exist, attempt to create it (matches
        # what `save_inbound_media` does on first write).
        try:
            root.mkdir(parents=True, exist_ok=True)
            root_exists = True
            probe = root / ".__nahla_writable_probe"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
            root_writable = True
        except Exception as exc:  # noqa: BLE001
            write_probe_error = f"create_failed: {type(exc).__name__}: {exc}"

    audio_ready  = bool(OPENAI_API_KEY) and root_writable
    vision_ready = bool(OPENAI_API_KEY) and root_writable

    issues: List[str] = []
    if not OPENAI_API_KEY:
        issues.append(
            "OPENAI_API_KEY غير مضبوط — لا تفريغ صوتي ولا وصف للصور"
        )
    if not root_exists:
        issues.append(
            f"NAHLA_INBOUND_MEDIA_DIR غير موجود: {root_str}"
        )
    elif not root_writable:
        issues.append(
            f"NAHLA_INBOUND_MEDIA_DIR غير قابل للكتابة "
            f"({write_probe_error or 'unknown'})"
        )

    return {
        "openai": {
            "api_key_present":   bool(OPENAI_API_KEY),
            "api_key_tail":      _mask_secret_tail(OPENAI_API_KEY),
            "api_base":          OPENAI_API_BASE,
            "chat_model":        OPENAI_MODEL,
            "audio_model":       OPENAI_AUDIO_MODEL,
            "vision_model":      OPENAI_VISION_MODEL,
            "stt_language":      NAHLA_STT_LANGUAGE,
        },
        "storage": {
            "root":              root_str,
            "exists":            root_exists,
            "writable":          root_writable,
            "write_probe_error": write_probe_error,
            "free_bytes":        free_bytes,
            "max_inbound_bytes": INBOUND_MEDIA_MAX_BYTES,
        },
        "ready": {
            "audio":  audio_ready,
            "vision": vision_ready,
        },
        "issues": issues,
        "hints": [
            "في Railway اضبط OPENAI_API_KEY ثم اعمل redeploy.",
            "اربط volume دائم على NAHLA_INBOUND_MEDIA_DIR (مثلاً /data/inbound-media) "
            "وإلا ستضيع الملفات في كل deploy.",
            "OPENAI_VISION_MODEL=gpt-4o-mini و OPENAI_AUDIO_MODEL=whisper-1 (الافتراضي).",
        ],
    }
