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


# ── Catalog-state diagnostic (Phase 4 — May 2026) ─────────────────────────────
#
# Why this endpoint exists
# ────────────────────────
# Phase 4 wired the Meta WhatsApp Catalog send path into
# ``_try_send_catalog_product`` in ``whatsapp_webhook.py``. The wire-up
# is intentionally silent on eligibility miss: when a tenant's
# ``catalog_enabled`` is False, ``meta_catalog_id`` is empty, or a
# product has no resolvable retailer id, the helper returns ``False``
# and the legacy image + CTA URL path renders the product. That keeps
# the customer experience stable, but it also makes the failure mode
# invisible from outside the box — operators can't tell whether the
# catalog actually fired or fell back without grepping Railway logs.
#
# This endpoint replaces the log-grep step with a single readonly
# call that mirrors EXACTLY the same checks ``_try_send_catalog_product``
# performs:
#
#   1. Look up the ``WhatsAppConnection`` for the tenant.
#   2. Call :func:`core.catalog.is_catalog_eligible` on it.
#   3. Sample N products from the tenant and run
#      :func:`core.catalog.effective_retailer_id` on each.
#   4. Probe ``information_schema`` for the four columns added by
#      migration 0061 so a missing migration shows up as a precise
#      ``"column X on table Y is missing"`` verdict alongside the
#      eligibility check.
#
# Output is intentionally flat JSON so it can be eyeballed in a
# browser. We deliberately do NOT expose any access tokens, phone
# numbers, or any field that could be considered customer PII; the
# catalog-state diagnostic is operational metadata only.

@router.get("/catalog-state")
async def admin_debug_catalog_state(
    tenant_id: int = Query(..., ge=1, description="Tenant whose catalog state to inspect."),
    sample: int = Query(5, ge=1, le=25, description="Number of products to sample."),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Readonly diagnostic for Phase 4 catalog wire-up.

    Returns the exact eligibility decision the webhook makes when
    a ``[PRODUCT:...]`` marker resolves for *tenant_id*, plus a
    sample of products with their resolved retailer ids, plus a
    column-presence probe for migration 0061. No writes, no DDL.

    Auth: ``require_admin`` JWT — same gate as
    ``GET /admin/debug/db-schema-health``. No env flag required.

    Response shape (top-level keys, all stable):

    * ``tenant_id``
    * ``connection``    — basic non-secret fields of the
                          ``WhatsAppConnection`` row, plus a
                          ``found: bool``.
    * ``eligibility``   — ``{ok, reason}`` from
                          :func:`core.catalog.is_catalog_eligible`
                          (no product context).
    * ``schema``        — ``{column_name → "present"|"missing"}`` for
                          every column added by migration 0061.
    * ``products_sample`` — list of up to ``sample`` products with
                            their resolved retailer ids.
    * ``products_sample_retailer_id_coverage`` — counters
                                                 ``{with_retailer_id, without_retailer_id}``.
    * ``advice``        — short human-readable recommendation derived
                          from the eligibility reason. Operators get a
                          one-line fix to copy into the relevant env
                          var, migration, or Salla resync command.
    """
    from sqlalchemy import text as _text  # noqa: PLC0415

    from core.catalog import (  # noqa: PLC0415
        catalog_summary, effective_retailer_id, is_catalog_eligible,
    )

    # ── Connection lookup ─────────────────────────────────────────
    # We pull the row with .first() because some installs historically
    # allowed multiple connection rows per tenant. The webhook reads
    # the same way, so we mirror its semantics exactly.
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    summary = catalog_summary(conn)
    # ``WhatsAppConnection`` uses ``phone_number_id`` (Meta's
    # phone-number identifier — separate from the human phone number)
    # and ``status`` (state-machine string). Neither is customer PII,
    # but we still MASK the phone_number_id to its last 4 digits so
    # screenshots of this endpoint stay safe to share. ``status`` is
    # surfaced verbatim because it's already a closed enum.
    phone_number_id = str(getattr(conn, "phone_number_id", "") or "") if conn else ""
    conn_block: Dict[str, Any] = {
        "found":                conn is not None,
        "phone_id_tail":        phone_number_id[-4:] if phone_number_id else None,
        "status":               getattr(conn, "status", None) if conn else None,
        "catalog_enabled":      summary["catalog_enabled"],
        "meta_catalog_id":      summary["meta_catalog_id"],
    }

    # ── Eligibility check (without product context) ───────────────
    # We deliberately pass ``products=None`` so the answer reflects
    # the CONNECTION's readiness, not a particular product. The
    # per-product readiness is captured by ``products_sample`` below.
    elig = is_catalog_eligible(conn, products=None)
    eligibility_block = {"ok": elig.ok, "reason": elig.reason}

    # ── Migration-0061 column probe ───────────────────────────────
    # Mirrors the approach in ``db-schema-health`` but scoped to the
    # exact four columns Phase 4 needs. Each probe is wrapped so a
    # transient error on one column doesn't poison the others.
    schema_probes = [
        ("whatsapp_connections", "meta_catalog_id"),
        ("whatsapp_connections", "catalog_enabled"),
        ("products",             "meta_retailer_id"),
        ("products",             "meta_catalog_published_at"),
    ]
    schema_block: Dict[str, str] = {}
    schema_missing: List[str] = []
    for table_name, column_name in schema_probes:
        try:
            row = db.execute(
                _text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = :t AND column_name = :c"
                ),
                {"t": table_name, "c": column_name},
            ).first()
            present = row is not None
        except Exception:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
            present = False
        key = f"{table_name}.{column_name}"
        schema_block[key] = "present" if present else "missing"
        if not present:
            schema_missing.append(key)

    # ── Product sample with retailer-id resolution ────────────────
    # We only fetch the columns we actually need so this stays fast
    # even on tenants with 50k products. The query is index-perfect
    # on ``products.tenant_id``.
    products_sample: List[Dict[str, Any]] = []
    coverage = {"with_retailer_id": 0, "without_retailer_id": 0}
    try:
        from models import Product as _Product  # noqa: PLC0415

        rows = (
            db.query(_Product)
            .filter(_Product.tenant_id == tenant_id)
            .order_by(_Product.id)
            .limit(int(sample))
            .all()
        )
        for p in rows:
            rid = effective_retailer_id(p)
            products_sample.append({
                "id":                   p.id,
                "title":                p.title,
                "external_id":          getattr(p, "external_id", None),
                "meta_retailer_id":     getattr(p, "meta_retailer_id", None),
                "effective_retailer_id": rid or None,
            })
            if rid:
                coverage["with_retailer_id"] += 1
            else:
                coverage["without_retailer_id"] += 1
    except Exception as p_exc:  # noqa: BLE001
        # Don't fail the whole endpoint if the product probe errors.
        # The schema block above already exposes a likely root cause
        # (missing column on ``products`` table).
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[catalog-state] product sample failed for tenant=%s: %s",
            tenant_id, p_exc,
        )

    # ── Advice synthesizer ────────────────────────────────────────
    # Deterministic mapping from (eligibility.reason, schema_missing,
    # product coverage) → a single actionable sentence. Operators
    # screenshot this and act on it.
    advice = _catalog_state_advice(
        eligibility=elig,
        schema_missing=schema_missing,
        coverage=coverage,
        connection_found=conn is not None,
    )

    return {
        "tenant_id":                              tenant_id,
        "connection":                             conn_block,
        "eligibility":                            eligibility_block,
        "schema":                                 schema_block,
        "products_sample":                        products_sample,
        "products_sample_retailer_id_coverage":   coverage,
        "advice":                                 advice,
    }


def _catalog_state_advice(
    *,
    eligibility,
    schema_missing: List[str],
    coverage: Dict[str, int],
    connection_found: bool,
) -> str:
    """Translate the structured diagnostic into one actionable line."""
    if schema_missing:
        return (
            "migration 0061_meta_catalog has NOT applied — missing columns: "
            + ", ".join(schema_missing)
            + ". Call POST /admin/debug/run-migrations or check that "
            "NAHLA_SKIP_DB_BOOTSTRAP is not set, then redeploy."
        )
    if not connection_found:
        return (
            "no WhatsAppConnection row for this tenant. The bot can't "
            "send anything until the merchant completes the WhatsApp "
            "Business onboarding."
        )
    reason = eligibility.reason
    if reason == "catalog_disabled":
        return (
            "catalog_enabled=false on whatsapp_connections. Set it to "
            "true once the Meta catalog is published and the bound "
            "phone number is approved for Commerce."
        )
    if reason == "catalog_id_missing":
        return (
            "meta_catalog_id is empty on whatsapp_connections. Fetch the "
            "catalog id from Meta Commerce Manager and persist it on "
            "this row, then set catalog_enabled=true."
        )
    if coverage.get("without_retailer_id", 0) > 0 and coverage.get("with_retailer_id", 0) == 0:
        return (
            "no product in the sampled batch has a resolvable retailer "
            "id (neither meta_retailer_id nor external_id). Run "
            "POST /admin/debug/salla/resync-products so each product gets "
            "an external_id, then test again."
        )
    if reason == "ok":
        return (
            "eligibility is OK. If you still see a fallback in WhatsApp, "
            "grep Railway logs for [CATALOG_SEND_FAILED] — the failure "
            "is at the Graph payload level (most likely the "
            "product_retailer_id is not present in the Meta catalog "
            "yet, or the bound access token lacks catalog_management)."
        )
    return f"eligibility={reason}. See logs for [CATALOG_NOT_ELIGIBLE] for context."


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
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415
    from core.config import (  # noqa: PLC0415
        INBOUND_MEDIA_MAX_BYTES,
        NAHLA_STT_LANGUAGE,
        OPENAI_API_BASE,
        OPENAI_AUDIO_MODEL,
        OPENAI_MODEL,
        OPENAI_VISION_MODEL,
    )
    from services.inbound_media_storage import storage_root  # noqa: PLC0415

    # Re-read OPENAI_API_KEY from os.environ on every call instead of
    # using the module-load constant — see normalizer.py for the full
    # rationale. Short version: a process that started before the env
    # var was set in Railway captures the empty string permanently;
    # re-reading lets a process pick up a fresh value without redeploy.
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""

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

    # ── ffmpeg detection ──────────────────────────────────────────
    # We don't currently invoke ffmpeg in the inbound pipeline
    # (Whisper API accepts opus/ogg directly), but operators
    # routinely ASK whether it's installed in case we add format
    # conversion later, and the Railway base image doesn't ship
    # with it by default. We probe both presence and version so
    # they can confirm the system PATH is healthy in one call.
    ffmpeg_path: Optional[str] = shutil.which("ffmpeg")
    ffmpeg_version: Optional[str] = None
    if ffmpeg_path:
        try:
            proc = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            # First line of `ffmpeg -version` is like:
            #   ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-...
            # We strip the "Copyright" suffix for a compact label.
            first_line = (proc.stdout or proc.stderr or "").splitlines()
            if first_line:
                ffmpeg_version = first_line[0].split("Copyright")[0].strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            # Found on disk but failed to execute (rare — usually a
            # permission issue on a custom mount). Keep the path so
            # support has something concrete to investigate.
            ffmpeg_version = f"execution_failed: {type(exc).__name__}"

    audio_ready  = bool(OPENAI_API_KEY) and root_writable
    vision_ready = bool(OPENAI_API_KEY) and root_writable

    # ── Issues + hints (Arabic, dynamic per failure) ──────────────
    # `issues` is a short list of "what's broken right now". `hints`
    # is the parallel list of "how to fix it on Railway specifically".
    # Empty when everything passes — so the UI can show a green
    # checkmark instead of a stale "اضبط المفتاح" reminder.
    issues: List[str] = []
    hints:  List[str] = []
    if not OPENAI_API_KEY:
        issues.append(
            "OPENAI_API_KEY غير مضبوط — لا تفريغ صوتي ولا وصف للصور."
        )
        hints.append(
            "في Railway → Variables أضف OPENAI_API_KEY ثم اعمل Redeploy. "
            "بدونه ميزتا التفريغ والرؤية معطّلتان."
        )
    if not root_exists:
        issues.append(
            f"NAHLA_INBOUND_MEDIA_DIR غير موجود على القرص: {root_str}"
        )
        hints.append(
            "في Railway → Volumes أنشئ volume دائم واربطه على نفس المسار "
            "في NAHLA_INBOUND_MEDIA_DIR (مثلاً /data/inbound-media)."
        )
    elif not root_writable:
        issues.append(
            f"NAHLA_INBOUND_MEDIA_DIR موجود لكن غير قابل للكتابة "
            f"({write_probe_error or 'unknown'})."
        )
        hints.append(
            "الـ volume mounted لكن الـ permissions تمنع الكتابة. "
            "تحقق من ownership على /data/inbound-media — يجب أن يكون "
            "للمستخدم الذي يشغّل uvicorn."
        )
    if not audio_ready:
        issues.append(
            "ميزة التفريغ الصوتي (Whisper) معطّلة — التسجيلات الواردة "
            "ستُخزَّن لكن بدون نص مستخرج."
        )
    if not vision_ready:
        issues.append(
            "ميزة وصف الصور (Vision) معطّلة — الصور الواردة ستُخزَّن "
            "لكن بدون وصف مستخرج."
        )
    if not ffmpeg_path:
        issues.append(
            "ffmpeg غير مثبّت — لا يؤثّر على المسار الحالي (Whisper "
            "API يقبل opus/ogg مباشرة) لكنه مطلوب إذا أردنا تحويل "
            "صيغ صوتية مستقبلاً."
        )
        hints.append(
            "إن احتجته في الإنتاج: أضف ffmpeg إلى Dockerfile/Nixpacks. "
            "في Railway nixpacks اكتب: NIXPACKS_PKGS=ffmpeg في Variables."
        )

    # ── Process identity ─────────────────────────────────────────
    # Surfaces WHICH process answered this request. Crucial when a
    # multi-service deploy (web + worker + scheduler) has env-var
    # drift — e.g. web sees the OpenAI key but worker doesn't. The
    # operator runs media-env, sees `service=web` here, then greps
    # Railway logs for `[MEDIA_NORMALIZER_BOOT] service=worker` to
    # discover the worker booted with a different env snapshot.
    try:
        from modules.ai.media import normalizer as _norm  # noqa: PLC0415
        boot_pid          = getattr(_norm, "_BOOT_PID", os.getpid())
        boot_service      = getattr(_norm, "_BOOT_SERVICE", "unknown")
        boot_key_present  = getattr(_norm, "_BOOT_OPENAI_KEY_PRESENT", None)
    except Exception:
        boot_pid          = os.getpid()
        boot_service      = (
            os.environ.get("RAILWAY_SERVICE_NAME")
            or os.environ.get("NAHLA_SERVICE_ROLE")
            or "unknown"
        )
        boot_key_present  = None

    process_block: Dict[str, Any] = {
        "pid":                  os.getpid(),
        "service":              boot_service,
        "boot_pid":             boot_pid,
        # True only if the normalizer module was imported in the
        # SAME process that's answering this HTTP request. If the
        # request lands on a different replica/worker, these will
        # differ — surface that so it's obvious.
        "normalizer_loaded_in_this_process": (boot_pid == os.getpid()),
        "openai_key_present_now":   bool(OPENAI_API_KEY),
        "openai_key_present_at_boot": boot_key_present,
        "needs_restart_to_pick_up_env": (
            bool(OPENAI_API_KEY) and boot_key_present is False
        ),
        "railway_service_name":     os.environ.get("RAILWAY_SERVICE_NAME"),
        "railway_replica_id":       os.environ.get("RAILWAY_REPLICA_ID"),
        "railway_deployment_id":    os.environ.get("RAILWAY_DEPLOYMENT_ID"),
        "epoch":                    int(time.time()),
    }

    # If web sees the key NOW but the normalizer module captured an
    # empty key at boot, the merchant report of "OPENAI_API_KEY مفقود"
    # in actual conversations is explained: the worker (or this very
    # process if normalizer was loaded before env arrived) is using
    # a stale snapshot. Add a high-signal Arabic hint.
    if process_block["needs_restart_to_pick_up_env"]:
        issues.append(
            "هذا الـ process يرى OPENAI_API_KEY الآن، لكن وحدة معالجة "
            "الوسائط حُمِّلت قبل ضبط المفتاح. لا بأس بقراءة الآن، لكن "
            "أي process آخر (worker/scheduler) ربما لا يزال يرى قيمة "
            "فارغة. أعد تشغيل (Restart) كل الـ services لضمان التطابق."
        )
        hints.append(
            "في Railway، افتح كل خدمة على حدة (web + worker + scheduler) "
            "واضغط Restart. ابحث في Logs عن [MEDIA_NORMALIZER_BOOT] "
            "لتتأكد أن جميع الـ processes ترى openai_key_present_at_boot=True."
        )

    payload: Dict[str, Any] = {
        # ── Process identity (new) ────────────────────────────────
        "process": process_block,
        # ── Nested groups (legacy shape — kept stable) ────────────
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
        "ffmpeg": {
            "found":    bool(ffmpeg_path),
            "path":     ffmpeg_path,
            "version":  ffmpeg_version,
        },
        # ── Flat aliases (new, public contract) ───────────────────
        # Documented top-level names so dashboards, runbooks, and the
        # support-bundle copy-paste flow don't have to walk the
        # nested structure. These mirror the values inside the
        # groups above 1:1 — any drift between the two is a bug.
        "openai_key_present":  bool(OPENAI_API_KEY),
        "openai_key_tail":     _mask_secret_tail(OPENAI_API_KEY),
        "vision_enabled":      vision_ready,
        "stt_enabled":         audio_ready,
        "media_dir_writable":  root_writable,
        "inbound_media_dir":   root_str,
        "ffmpeg_found":        bool(ffmpeg_path),
        "ffmpeg_version":      ffmpeg_version,
        "issues":              issues,
        "hints":               hints,
    }
    return payload


# ──────────────────────────────────────────────────────────────────────
# Scheduler health — verify the campaign dispatcher loop is alive
# ──────────────────────────────────────────────────────────────────────

@router.get("/scheduler-health")
async def admin_debug_scheduler_health(
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Diagnostic snapshot of the campaign-dispatcher background loop
    that lives inside the uvicorn web process.

    Purpose
    ───────
    After a deploy you want to confirm — without scraping Railway logs —
    that:

      1. The FastAPI lifespan completed (``started_at`` is not None).
      2. The loop is alive (``alive=True`` ⇔ a tick fired within 3× the
         poll period).
      3. The F12 rescue path actually picked up stuck immediate
         campaigns (``last_rescue_at`` / ``last_rescued_campaign_ids``
         populated).
      4. The kill-switch (``NAHLA_DISABLE_SCHEDULERS``) is not flipped.
      5. The deployed git SHA matches what you just pushed.

    Hitting this endpoint with admin credentials gives you all five
    answers in one JSON document. It is read-only and process-local —
    no DB writes, no side effects.
    """
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415
    from core.scheduler import (  # noqa: PLC0415
        get_campaign_dispatcher_state,
    )

    state = get_campaign_dispatcher_state()

    # Kill switch state — the most common reason for a "dead" loop.
    raw_kill = os.environ.get("NAHLA_DISABLE_SCHEDULERS", "")
    kill_switch_set = raw_kill.strip().lower() in ("1", "true", "yes")

    # Minimal-asgi check — when set, backend.minimal_asgi:app boots
    # WITHOUT any schedulers. If a merchant deploys with this flag
    # accidentally, no rescue path runs at all.
    minimal_asgi = os.environ.get("NAHLA_MINIMAL_ASGI", "").strip().lower()
    minimal_asgi_set = minimal_asgi in ("1", "true", "yes")

    # Best-effort deployed-SHA. Railway / Nixpacks may not preserve
    # ``.git``; we try a few common sources before giving up. Never
    # raises — diagnostic, not authoritative.
    git_sha = None
    git_branch = None
    for env_key in (
        "RAILWAY_GIT_COMMIT_SHA",
        "RAILWAY_DEPLOYMENT_COMMIT_SHA",
        "GIT_COMMIT",
        "COMMIT_SHA",
        "SOURCE_VERSION",
    ):
        if os.environ.get(env_key):
            git_sha = os.environ.get(env_key)
            break
    for env_key in (
        "RAILWAY_GIT_BRANCH",
        "GIT_BRANCH",
    ):
        if os.environ.get(env_key):
            git_branch = os.environ.get(env_key)
            break
    if not git_sha:
        try:
            git_sha = (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd="/app",
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
                .decode("utf-8", errors="ignore")
                .strip()
                or None
            )
        except Exception:
            git_sha = None

    # Build human-readable diagnosis lines. The first one is the
    # most important — what action (if any) you should take.
    issues: List[str] = []
    hints:  List[str] = []
    if kill_switch_set:
        issues.append(
            "NAHLA_DISABLE_SCHEDULERS is set — every scheduler is "
            "disabled, including the campaign dispatcher. Unset this "
            "env var on Railway to re-enable the rescue path."
        )
    if minimal_asgi_set:
        issues.append(
            "NAHLA_MINIMAL_ASGI is set — backend.minimal_asgi:app is "
            "booted, which has no schedulers registered. Unset to use "
            "backend.main:app."
        )
    if not state.get("started"):
        if not kill_switch_set and not minimal_asgi_set:
            issues.append(
                "campaign_dispatcher.started_at is None — the lifespan "
                "hook never ran or the 10s startup delay has not "
                "elapsed yet. Wait ~15s after deploy then re-check."
            )
    elif not state.get("alive"):
        age = state.get("last_tick_age_seconds")
        issues.append(
            f"campaign_dispatcher last tick was {age:.0f}s ago — loop "
            f"appears stalled. Expected ≤ "
            f"{state.get('poll_seconds') * 3}s."
        )
    if (
        state.get("alive")
        and state.get("rescue_invocations_total", 0) == 0
        and state.get("ticks_total", 0) > 2
    ):
        hints.append(
            "Loop is alive but rescue path has not triggered yet. "
            "This is normal when no campaigns are stuck. If you "
            "currently have a stuck campaign with status='active', "
            "schedule_type='immediate', and zero campaign_send_logs "
            "rows older than 60s, it should appear in "
            "last_rescued_campaign_ids on the next tick."
        )

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "deployment": {
            "git_sha": git_sha,
            "git_branch": git_branch,
        },
        "kill_switches": {
            "NAHLA_DISABLE_SCHEDULERS": raw_kill or None,
            "NAHLA_DISABLE_SCHEDULERS_active": kill_switch_set,
            "NAHLA_MINIMAL_ASGI": os.environ.get("NAHLA_MINIMAL_ASGI") or None,
            "NAHLA_MINIMAL_ASGI_active": minimal_asgi_set,
        },
        "campaign_dispatcher": state,
        "issues": issues,
        "hints":  hints,
        "ok": (
            state.get("alive") is True
            and not kill_switch_set
            and not minimal_asgi_set
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# DB schema health — verify alembic head + known critical columns
# ──────────────────────────────────────────────────────────────────────

# Columns that the dispatcher / debug endpoint depend on and which
# came from a specific migration revision. If any of these are
# missing in production, the merchant sees a cryptic
# UndefinedColumn error and the campaign never enqueues. We
# enumerate them explicitly so the schema-health endpoint can
# report exactly which migration is missing.
_CRITICAL_COLUMNS: List[Dict[str, str]] = [
    # 0054_campaign_send_log_delivery_tracking
    {"table": "campaign_send_logs", "column": "delivered_at", "added_by": "0054"},
    {"table": "campaign_send_logs", "column": "read_at",      "added_by": "0054"},
    {"table": "campaign_send_logs", "column": "failed_at",    "added_by": "0054"},
    # 0053_customer_segments_manual_mode
    {"table": "customer_segments_manual", "column": "mode", "added_by": "0053"},
    # 0051_campaign_send_logs
    {"table": "campaign_send_logs", "column": "provider_message_id", "added_by": "0051"},
    # 0061_meta_catalog — Phase 4 catalog wire-up depends on all four.
    # When any of these are missing the catalog send is silently
    # short-circuited at eligibility check and the legacy image+CTA
    # path renders the product instead. Probing them here lets a
    # schema-health call diagnose the regression without grepping
    # logs.
    {"table": "whatsapp_connections", "column": "meta_catalog_id",           "added_by": "0061"},
    {"table": "whatsapp_connections", "column": "catalog_enabled",           "added_by": "0061"},
    {"table": "products",             "column": "meta_retailer_id",          "added_by": "0061"},
    {"table": "products",             "column": "meta_catalog_published_at", "added_by": "0061"},
]


def _latest_revision_in_codebase() -> Optional[str]:
    """Return the highest ``revision`` string found among the
    Alembic version files shipped in this build. Returns None on
    error — endpoint then surfaces ``codebase_head=None`` so the
    operator knows the comparison is unreliable."""
    import os  # noqa: PLC0415
    import re  # noqa: PLC0415

    repo_root = "/app"
    if not os.path.isdir(repo_root):
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
    versions_dir = os.path.join(
        repo_root, "database", "migrations", "versions"
    )
    if not os.path.isdir(versions_dir):
        return None

    rev_re = re.compile(r'^revision\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
    revisions: List[str] = []
    try:
        for fname in os.listdir(versions_dir):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(versions_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    blob = fh.read(8192)
                m = rev_re.search(blob)
                if m:
                    revisions.append(m.group(1))
            except OSError:
                continue
    except OSError:
        return None
    if not revisions:
        return None
    # Versions are 4-digit strings — natural sort suffices because
    # they're zero-padded ("0054" > "0053").
    return sorted(revisions)[-1]


@router.get("/db-schema-health")
async def admin_debug_db_schema_health(
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Compare the deployed Alembic head against the codebase's
    latest revision, and probe a list of critical columns directly
    via ``information_schema.columns`` so a missing migration shows
    up as a precise "column X on table Y is missing — added by
    revision Z" verdict.

    Why this exists
    ───────────────
    The lifespan bootstrap (``backend/main.py:_bootstrap_db_schema``)
    runs ``alembic upgrade head`` in a background thread with a
    180s timeout. If it fails / times out / is skipped by
    ``NAHLA_SKIP_DB_BOOTSTRAP=1``, the next failed-migration error
    only surfaces when an HTTP request hits a SQL query referencing
    the missing column (e.g. ``campaign_send_logs.delivered_at`` →
    psycopg2 ``UndefinedColumn`` → 500 → the campaign never enters
    the queue).

    This endpoint converts the symptom into a deterministic
    diagnostic. Read-only: no DDL, no writes. Pair with
    ``POST /admin/debug/run-migrations`` to apply pending migrations
    on demand.
    """
    import os  # noqa: PLC0415
    from sqlalchemy import text as _text  # noqa: PLC0415

    skip_bootstrap = (
        os.environ.get("NAHLA_SKIP_DB_BOOTSTRAP", "") or ""
    ).strip().lower() in ("1", "true", "yes")

    # ── Deployed Alembic head ─────────────────────────────────────
    deployed_head: Optional[str] = None
    deployed_head_error: Optional[str] = None
    try:
        row = db.execute(_text(
            "SELECT version_num FROM alembic_version LIMIT 1"
        )).first()
        deployed_head = row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        deployed_head_error = f"{type(exc).__name__}: {exc}"
    # Clear the failed transaction so subsequent queries succeed.
    try:
        db.rollback()
    except Exception:
        pass

    codebase_head = _latest_revision_in_codebase()

    # ── Critical column probe ─────────────────────────────────────
    # We probe every column listed in _CRITICAL_COLUMNS and report
    # which migrations are missing on the deployed DB.
    column_status: List[Dict[str, Any]] = []
    missing_migrations: set = set()
    for spec in _CRITICAL_COLUMNS:
        try:
            row = db.execute(
                _text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = :t AND column_name = :c"
                ),
                {"t": spec["table"], "c": spec["column"]},
            ).first()
            present = row is not None
        except Exception as exc:  # noqa: BLE001
            present = False
            try:
                db.rollback()
            except Exception:
                pass
            column_status.append({
                **spec,
                "present": False,
                "probe_error": f"{type(exc).__name__}: {exc}",
            })
            missing_migrations.add(spec["added_by"])
            continue
        column_status.append({**spec, "present": present})
        if not present:
            missing_migrations.add(spec["added_by"])

    behind = (
        deployed_head is not None
        and codebase_head is not None
        and deployed_head != codebase_head
    )

    issues: List[str] = []
    hints:  List[str] = []
    if skip_bootstrap:
        issues.append(
            "NAHLA_SKIP_DB_BOOTSTRAP=1 — alembic upgrade head is "
            "DISABLED at startup. Unset this env var on Railway and "
            "redeploy, or call POST /admin/debug/run-migrations to "
            "apply pending migrations once on demand."
        )
    if deployed_head_error:
        issues.append(
            "alembic_version table is unreachable: "
            f"{deployed_head_error}. The DB may have never been "
            "stamped — check that bootstrap Step B ran."
        )
    if behind:
        issues.append(
            f"deployed alembic head={deployed_head!r} is behind "
            f"codebase head={codebase_head!r}. Run "
            "POST /admin/debug/run-migrations to upgrade."
        )
    if missing_migrations:
        for rev in sorted(missing_migrations):
            issues.append(
                f"migration {rev} has not been applied — columns "
                "added by it are missing from the deployed schema. "
                "This is the exact cause of "
                "'UndefinedColumn ... does not exist' errors in the "
                "dispatcher."
            )
        hints.append(
            "Quick fix: POST /admin/debug/run-migrations to apply "
            "alembic upgrade head in the running container. Always "
            "back up the DB first if you have any doubt."
        )

    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "deployed_alembic_head":  deployed_head,
        "codebase_alembic_head":  codebase_head,
        "behind_by":              behind,
        "missing_migrations":     sorted(missing_migrations),
        "critical_columns":       column_status,
        "skip_bootstrap_env_set": skip_bootstrap,
        "issues": issues,
        "hints":  hints,
        "ok": (
            not issues
            and not missing_migrations
            and not behind
        ),
    }


class RunMigrationsBody(BaseModel):
    """Body for POST /admin/debug/run-migrations.

    ``confirm`` must be the literal string ``"YES_RUN_ALEMBIC_UPGRADE_HEAD"``
    to prevent accidental invocation. The endpoint streams output to the
    Railway logs and returns the alembic stdout/stderr in the response.
    """
    confirm: str = Field(
        ...,
        description=(
            "Set to 'YES_RUN_ALEMBIC_UPGRADE_HEAD' to confirm you want "
            "to apply pending migrations on the live database."
        ),
    )
    timeout_seconds: int = Field(
        default=240,
        ge=30,
        le=900,
        description="Max wall-clock seconds before the subprocess is killed.",
    )


@router.post("/run-migrations")
async def admin_debug_run_migrations(
    body: RunMigrationsBody,
    secret: Optional[str] = Query(default=None, alias="secret"),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Apply pending Alembic migrations on the running container.

    Gated by:
      1. ``require_admin`` JWT (FastAPI dependency).
      2. ``ENABLE_ADMIN_DEBUG=true`` env (matches the rest of this
         router's defaults — fail-closed when the flag is absent).
      3. Optional ``ADMIN_DEBUG_SECRET`` query param.
      4. ``body.confirm == 'YES_RUN_ALEMBIC_UPGRADE_HEAD'``.

    Why we expose this instead of "just SSH into Railway":
    Railway free / hobby plans don't expose a shell on the running
    container, and a redeploy doesn't always re-run the bootstrap
    (it may already have ``alembic_version=head_minus_one`` and the
    file watcher didn't pick up a new migration). The merchant
    needs a way to apply a missing migration without ops support.

    The subprocess runs with the SAME env / cwd as the lifespan
    bootstrap so any behavioural difference between "boot bootstrap"
    and "manual run" is impossible.
    """
    _require_enabled(secret)
    if body.confirm != "YES_RUN_ALEMBIC_UPGRADE_HEAD":
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing confirmation. POST with body "
                "{'confirm': 'YES_RUN_ALEMBIC_UPGRADE_HEAD'} to "
                "apply pending migrations."
            ),
        )

    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415

    # Resolve the database/ directory exactly the way main.py does.
    repo_root = "/app"
    if not os.path.isdir(repo_root):
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
    database_dir = os.path.join(repo_root, "database")
    if not os.path.isdir(database_dir):
        raise HTTPException(
            status_code=500,
            detail=f"database/ directory not found at {database_dir!r}",
        )

    logger.warning(
        "[admin-debug] run-migrations invoked by admin — "
        "alembic upgrade head (cwd=%s, timeout=%ds)",
        database_dir, body.timeout_seconds,
    )

    start = time.monotonic()
    try:
        result = subprocess.run(
            [_sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=database_dir,
            check=False,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=body.timeout_seconds,
        )
        elapsed_s = time.monotonic() - start
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "[admin-debug] run-migrations TIMEOUT after %ds: %s",
            body.timeout_seconds, exc,
        )
        raise HTTPException(
            status_code=504,
            detail={
                "code": "alembic_timeout",
                "timeout_seconds": body.timeout_seconds,
                "message": (
                    "alembic upgrade head did not finish within the "
                    "timeout. The migration may be holding a lock — "
                    "inspect Railway logs and consider terminating "
                    "the locking session."
                ),
            },
        )

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    audit(
        "admin_debug_run_migrations",
        rc        = result.returncode,
        elapsed_s = round(elapsed_s, 3),
        stdout_len= len(stdout),
        stderr_len= len(stderr),
    )

    if result.returncode != 0:
        logger.error(
            "[admin-debug] run-migrations FAILED rc=%d elapsed=%.1fs\n"
            "--- stderr ---\n%s\n--- stdout ---\n%s",
            result.returncode, elapsed_s, stderr, stdout,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code":       "alembic_upgrade_failed",
                "returncode": result.returncode,
                "elapsed_s":  round(elapsed_s, 3),
                "stdout":     stdout,
                "stderr":     stderr,
                "message": (
                    "alembic upgrade head returned a non-zero exit "
                    "code. Inspect 'stderr' for the offending "
                    "migration."
                ),
            },
        )

    logger.warning(
        "[admin-debug] run-migrations OK rc=0 elapsed=%.1fs\n%s",
        elapsed_s, stdout,
    )
    return {
        "ok":         True,
        "returncode": result.returncode,
        "elapsed_s":  round(elapsed_s, 3),
        "stdout":     stdout,
        "stderr":     stderr,
        "message": (
            "alembic upgrade head completed successfully. Re-call "
            "GET /admin/debug/db-schema-health to confirm the head "
            "matches the codebase and that all critical columns are "
            "present."
        ),
    }


# ════════════════════════════════════════════════════════════════════════
# F17 — Inbound AI trace
# ════════════════════════════════════════════════════════════════════════
#
# Purpose
# ───────
# After a 360dialog Coexistence connection is established the most common
# merchant complaint is "the AI isn't replying". The root cause sits in
# one of seven places along the pipeline:
#
#   1. webhook_received     — 360dialog never POSTed (URL/secret/sub off)
#   2. message_saved        — webhook arrived but the body never made it
#                             past parsing / tenant resolution / dedup
#   3. conversation_state   — the row was saved but the Conversation has
#                             a hard human-takeover flag set
#   4. ai_allowed           — every gate consulted by ai_pause_guard /
#                             billing / live-since cutoff / platform-
#                             tenant routing
#   5. ai_generated         — gates passed but the brain produced no
#                             reply (e.g. OPENAI_API_KEY missing,
#                             OpenAI 5xx, brain raised)
#   6. send_attempted       — reply text exists but 360dialog wire call
#                             never happened (token resolver failed)
#   7. send_status          — wire call attempted but the provider
#                             rejected with no wamid
#
# This endpoint walks the database (read-only — NO side effects) and
# returns a structured snapshot of which stage the most recent inbound
# message reached. The shape is deliberately designed so a dashboard
# can render one row per stage with a green check or a red Arabic
# explanation.
#
# Safety contract
# ───────────────
# * Read-only — no writes, no auto-heal, no pause/resume calls.
# * Sensitive values masked (phone numbers via `_mask_phone`, API keys
#   via `_mask_secret_tail`, secrets reported only as `_present`
#   booleans).
# * Admin-gated via `require_admin` only — no env flag — because
#   support uses it routinely during merchant onboarding.

@router.get("/inbound-trace")
async def admin_debug_inbound_trace(
    tenant_id: int = Query(..., description="Merchant tenant id to trace."),
    phone: Optional[str] = Query(
        None,
        description=(
            "Optional customer phone (E.164 or bare digits). When set, "
            "the trace targets the most recent inbound from this number."
        ),
    ),
    wa_message_id: Optional[str] = Query(
        None,
        description=(
            "Optional WhatsApp message id (wamid). When set, the trace "
            "targets the specific message; takes precedence over `phone`."
        ),
    ),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Trace a single inbound message through the full AI pipeline.

    Resolution order for the target message:
      1. ``wa_message_id`` (exact match on
         ``MessageEvent.extra_metadata->>'wa_message_id'``).
      2. ``phone`` (latest inbound on
         ``MessageEvent.extra_metadata->>'phone'``).
      3. Otherwise: the latest inbound for the tenant.

    Returns a structured `pipeline` block with one entry per stage.
    Each stage carries ``ok: bool``, free-form ``details`` and (when
    blocked) a ``blocked_by`` list of stable string codes the
    dashboard can localise.

    Read-only diagnostic. Does NOT pause, resume, dispatch, or
    trigger any send. Does NOT change conversation state.
    """
    from datetime import datetime, timezone, timedelta  # noqa: PLC0415
    from sqlalchemy import desc  # noqa: PLC0415
    from models import Conversation, Customer, MessageEvent  # noqa: PLC0415
    from core.billing import has_billing_access  # noqa: PLC0415
    from core.ai_pause_guard import (  # noqa: PLC0415
        is_internal_or_blocked,
    )
    from core.whatsapp_ai_live import (  # noqa: PLC0415
        is_inbound_before_ai_live_since,
    )
    from services.whatsapp_platform.provider_utils import (  # noqa: PLC0415
        WHATSAPP_PROVIDER_360DIALOG,
        wa_provider,
    )

    def _aware(dt: Optional[datetime]) -> Optional[datetime]:
        """Coerce a possibly-naive datetime to UTC-aware. SQLite
        round-trips DateTime(timezone=True) as naive, so a direct
        subtraction with ``datetime.now(timezone.utc)`` raises
        ``TypeError: can't subtract offset-naive and offset-aware``.
        We never assume a non-UTC zone — naive values in this codebase
        are always UTC by convention."""
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    admin_sub = _admin.get("sub") or "?"
    logger.info(
        "[ADMIN/INBOUND_TRACE] start admin=%s tenant=%s phone=%s wa_message_id=%s",
        admin_sub, tenant_id, _mask_phone(phone or ""), wa_message_id or "—",
    )

    issues: List[str] = []
    hints:  List[str] = []
    now_utc = datetime.now(timezone.utc)

    # ── Tenant existence ────────────────────────────────────────
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=f"tenant {tenant_id} not found",
        )

    # ── Connection snapshot ─────────────────────────────────────
    # We treat the connection as the source of truth for "is this
    # merchant actually wired to 360dialog?". A missing or
    # mis-configured row is the #1 reason webhooks never arrive.
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .order_by(WhatsAppConnection.id.desc())
        .first()
    )

    connection_block: Dict[str, Any] = {"found": False}
    if wa_conn is not None:
        extra_meta = wa_conn.extra_metadata or {}
        coex_meta  = extra_meta.get("coexistence") if isinstance(extra_meta, dict) else {}
        coex_meta  = coex_meta if isinstance(coex_meta, dict) else {}
        coex_secret = str(extra_meta.get("coexistence_internal_secret") or "") \
            if isinstance(extra_meta, dict) else ""

        provider_kind = wa_provider(wa_conn)
        last_webhook_at = _aware(getattr(wa_conn, "last_webhook_received_at", None))
        last_coex_at    = _aware(getattr(wa_conn, "webhook_coexistence_received_at", None))
        last_status_at  = _aware(getattr(wa_conn, "webhook_status_received_at", None))
        ai_live_since   = _aware(getattr(wa_conn, "whatsapp_ai_live_since", None))
        connected_at    = _aware(getattr(wa_conn, "connected_at", None))
        last_verified   = _aware(getattr(wa_conn, "last_verified_at", None))

        connection_block = {
            "found":              True,
            "provider":           provider_kind,
            "connection_type":    getattr(wa_conn, "connection_type", None),
            "status":             getattr(wa_conn, "status", None),
            "phone_number_id":    getattr(wa_conn, "phone_number_id", None),
            "waba_id":            getattr(wa_conn, "whatsapp_business_account_id", None),
            "phone_number":       getattr(wa_conn, "phone_number", None),
            "access_token_present": bool((getattr(wa_conn, "access_token", "") or "").strip()),
            "access_token_tail":  _mask_secret_tail(getattr(wa_conn, "access_token", None)),
            "coexistence_secret_present": bool(coex_secret),
            "webhook_verified":   bool(getattr(wa_conn, "webhook_verified", False)),
            "sending_enabled":    bool(getattr(wa_conn, "sending_enabled", False)),
            "whatsapp_ai_live_since":   ai_live_since.isoformat() if ai_live_since else None,
            "last_webhook_received_at": last_webhook_at.isoformat() if last_webhook_at else None,
            "last_coexistence_event_at": last_coex_at.isoformat() if last_coex_at else None,
            "last_status_event_at":     last_status_at.isoformat() if last_status_at else None,
            "coexistence_last_event": coex_meta.get("last_event"),
            "connected_at":  connected_at.isoformat() if connected_at else None,
            "last_verified_at": last_verified.isoformat() if last_verified else None,
            "last_error": getattr(wa_conn, "last_error", None),
        }

        # Connection-level findings — surface in the top-level issues
        # block so support sees the actionable items first.
        if provider_kind != WHATSAPP_PROVIDER_360DIALOG:
            issues.append(
                f"الاتصال الحالي ليس 360dialog (provider={provider_kind}). "
                f"هذا التشخيص مخصص لـ 360dialog Coexistence."
            )
        if getattr(wa_conn, "connection_type", None) != "coexistence":
            hints.append(
                f"connection_type={getattr(wa_conn, 'connection_type', None) or '—'} "
                f"— ليس coexistence. التشخيص يعمل لكن بعض الفحوصات قد لا تنطبق."
            )
        if getattr(wa_conn, "status", None) != "connected":
            issues.append(
                f"حالة الاتصال = {getattr(wa_conn, 'status', None) or '—'} "
                f"(يجب أن تكون 'connected')."
            )
        if not (getattr(wa_conn, "access_token", "") or "").strip():
            issues.append("D360-API-KEY مفقود على الاتصال — لن ينجح أي إرسال.")
        if not coex_secret:
            hints.append(
                "coexistence_internal_secret غير مضبوط — webhook 360dialog "
                "سيُسقَط بهذا السبب: '[Webhook360] Invalid internal secret'."
            )
        if last_webhook_at is None:
            issues.append(
                "last_webhook_received_at = NULL — لم يصل أي webhook من 360dialog "
                "إلى هذا الـ process أبداً. تأكد من اشتراك الـ URL في لوحة 360dialog."
            )
        elif now_utc - last_webhook_at > timedelta(hours=24):
            hints.append(
                "آخر webhook منذ أكثر من 24 ساعة — قد تكون القناة صامتة فعلاً، "
                "أو يكون الاشتراك انتهى صلاحيته."
            )

    else:
        issues.append(
            "لا يوجد سجل WhatsAppConnection لهذا التاجر — يجب ربط القناة أولاً."
        )

    # ── Locate the target inbound MessageEvent ──────────────────
    # We never load the entire body for a giant message — it's
    # truncated to 600 chars in the response.
    def _truncate(text: Optional[str], n: int = 600) -> Optional[str]:
        if text is None:
            return None
        s = str(text)
        if len(s) <= n:
            return s
        return s[:n] + "…"

    target_message: Optional[MessageEvent] = None
    target_query = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.direction == "inbound",
        )
    )
    if wa_message_id:
        target_message = (
            target_query
            .filter(MessageEvent.extra_metadata["wa_message_id"].astext == wa_message_id)
            .order_by(desc(MessageEvent.id))
            .first()
        )
    elif phone:
        target_message = (
            target_query
            .filter(MessageEvent.extra_metadata["phone"].astext == phone.strip())
            .order_by(desc(MessageEvent.id))
            .first()
        )
    else:
        target_message = target_query.order_by(desc(MessageEvent.id)).first()

    # ── Stage 1: webhook_received ───────────────────────────────
    # Evidence the 360dialog POST reached this process. Two signals:
    #   • last_webhook_received_at on the connection row (set by
    #     `_stamp_webhook_received` for every accepted payload);
    #   • OR a target MessageEvent for this tenant exists (proves a
    #     prior webhook walked the full ingestion path).
    has_webhook_evidence = (
        connection_block.get("last_webhook_received_at") is not None
        or target_message is not None
    )
    step_1 = {
        "ok": bool(has_webhook_evidence),
        "details": {
            "last_webhook_received_at":  connection_block.get("last_webhook_received_at"),
            "last_coexistence_event_at": connection_block.get("last_coexistence_event_at"),
            "any_inbound_persisted":     target_message is not None,
        },
        "blocked_by": [] if has_webhook_evidence else ["no_webhook_evidence"],
        "reason": None if has_webhook_evidence else (
            "لم يصل أي webhook من 360dialog إلى هذا الـ process. تحقق من URL الاشتراك "
            "في لوحة 360dialog، وتحقق من X-Nahla-Coexistence-Secret."
        ),
    }

    # ── Stage 2: message_saved ──────────────────────────────────
    step_2: Dict[str, Any] = {"ok": False, "details": None, "blocked_by": [], "reason": None}
    inbound_extra: Dict[str, Any] = {}
    if target_message is None:
        step_2["blocked_by"].append("no_inbound_message_found")
        step_2["reason"] = (
            "لم يُعثر على أي رسالة inbound مطابقة. إن كنت متأكداً أنها أُرسلت، "
            "السبب الأرجح: webhook secret خاطئ، أو phone_number_id لا يطابق "
            "أي WhatsAppConnection."
        )
    else:
        inbound_extra = target_message.extra_metadata or {}
        step_2["ok"] = True
        step_2["details"] = {
            "message_event_id": int(target_message.id),
            "conversation_id":  int(target_message.conversation_id) if target_message.conversation_id else None,
            "event_type":       target_message.event_type,
            "direction":        target_message.direction,
            "created_at":       _aware(target_message.created_at).isoformat() if target_message.created_at else None,
            "body_preview":     _truncate(target_message.body, 600),
            "wa_message_id":    inbound_extra.get("wa_message_id"),
            "phone":            _mask_phone(str(inbound_extra.get("phone") or "")),
            "whatsapp_timestamp": inbound_extra.get("whatsapp_timestamp"),
            "message_origin":   inbound_extra.get("message_origin"),
            "historical_import": bool(inbound_extra.get("historical_import")),
            "phone_number_id":  inbound_extra.get("phone_number_id"),
            "provider":         inbound_extra.get("provider"),
            "normalized_inbound_status": (
                (inbound_extra.get("normalized_inbound") or {}).get("status")
                if isinstance(inbound_extra.get("normalized_inbound"), dict) else None
            ),
        }

    # ── Stage 3: conversation_state ─────────────────────────────
    convo: Optional[Conversation] = None
    if target_message and target_message.conversation_id:
        convo = (
            db.query(Conversation)
            .filter(Conversation.id == target_message.conversation_id)
            .first()
        )

    step_3: Dict[str, Any] = {"ok": False, "details": None, "blocked_by": [], "reason": None}
    if convo is None:
        # An inbound without a conversation_id is an unusual state —
        # the dispatcher creates the conversation before persisting.
        if target_message is not None:
            step_3["blocked_by"].append("conversation_missing")
            step_3["reason"] = (
                "الرسالة محفوظة لكن بدون conversation_id — حالة غير متوقعة، "
                "تحقق من _get_or_create_conversation في routers/conversations.py."
            )
    else:
        convo_extra = convo.extra_metadata or {}
        blockers: List[str] = []
        # Note: these are the CURRENT flags, not necessarily what they
        # were at the moment the inbound landed. Still the best signal
        # we have for "is the AI free to reply right now?".
        if bool(getattr(convo, "ai_paused", False)):
            blockers.append("ai_paused")
        if bool(getattr(convo, "needs_human", False)):
            blockers.append("needs_human")
        if bool(getattr(convo, "handoff_active", False)):
            blockers.append("handoff_active")
        if bool(getattr(convo, "is_human_handoff", False)):
            blockers.append("is_human_handoff")
        if bool(getattr(convo, "paused_by_human", False)):
            blockers.append("paused_by_human")

        step_3["ok"] = (len(blockers) == 0)
        step_3["details"] = {
            "conversation_id": int(convo.id),
            "status":          convo.status,
            "ai_paused":       bool(getattr(convo, "ai_paused", False)),
            "ai_paused_reason": getattr(convo, "ai_paused_reason", None),
            "ai_paused_at":    _aware(convo.ai_paused_at).isoformat() if getattr(convo, "ai_paused_at", None) else None,
            "ai_paused_by":    getattr(convo, "ai_paused_by", None),
            "needs_human":     bool(getattr(convo, "needs_human", False)),
            "handoff_active":  bool(getattr(convo, "handoff_active", False)),
            "is_human_handoff": bool(getattr(convo, "is_human_handoff", False)),
            "paused_by_human": bool(getattr(convo, "paused_by_human", False)),
            "taken_over_at":   _aware(convo.taken_over_at).isoformat() if getattr(convo, "taken_over_at", None) else None,
            "taken_over_by":   getattr(convo, "taken_over_by", None),
            "last_read_at":    _aware(convo.last_read_at).isoformat() if getattr(convo, "last_read_at", None) else None,
            "brain_state_keys": (
                list((convo_extra.get("brain_state") or {}).keys())
                if isinstance(convo_extra.get("brain_state"), dict) else []
            ),
        }
        step_3["blocked_by"] = blockers
        if blockers:
            step_3["reason"] = (
                f"الذكاء معطّل على هذه المحادثة. الأسباب: {', '.join(blockers)} "
                f"(reason={getattr(convo, 'ai_paused_reason', None) or '—'})."
            )

    # ── Stage 4: ai_allowed ─────────────────────────────────────
    # Re-evaluate every gate the live inbound dispatcher consults,
    # using READ-ONLY versions of the helpers. We deliberately
    # don't invoke `should_skip_ai` because it has side effects
    # (calls `pause_ai`) on the bot-loop branch.
    step_4_blockers: List[str] = []
    step_4_details: Dict[str, Any] = {}

    # 4.a — Platform tenant routing (skips merchant brain entirely).
    is_platform_tenant = bool(getattr(tenant, "is_platform_tenant", False))
    step_4_details["is_platform_tenant"] = is_platform_tenant
    if is_platform_tenant:
        step_4_blockers.append("platform_tenant_routing")

    # 4.b — Customer phone on tenant blocklist or internal numbers.
    customer_phone_raw = str(inbound_extra.get("phone") or phone or "").strip()
    if customer_phone_raw:
        blocked_match, block_reason = is_internal_or_blocked(
            db, int(tenant_id), customer_phone_raw,
        )
    else:
        blocked_match, block_reason = (False, None)
    step_4_details["blocklist_match"] = bool(blocked_match)
    step_4_details["blocklist_reason"] = block_reason
    if blocked_match:
        step_4_blockers.append("blocklist")

    # 4.c — Conversation-level ai_paused (already reported in step 3
    # but we surface here too so the gate map is self-contained).
    convo_paused = bool(getattr(convo, "ai_paused", False)) if convo else False
    step_4_details["conversation_ai_paused"] = convo_paused
    if convo_paused:
        step_4_blockers.append("ai_paused")

    # 4.d — Billing gate.
    try:
        billing_ok = bool(has_billing_access(db, int(tenant_id)))
    except Exception as exc:  # noqa: BLE001
        billing_ok = False
        step_4_details["billing_lookup_error"] = f"{type(exc).__name__}: {exc}"
    step_4_details["billing_access"] = billing_ok
    if not billing_ok:
        step_4_blockers.append("billing_access_denied")

    # 4.e — whatsapp_ai_live_since cutoff (only relevant when the
    # row has a wa timestamp; historical sync writes
    # historical_import=True).
    wa_ts_raw = inbound_extra.get("whatsapp_timestamp")
    wa_ts_dt: Optional[datetime] = None
    if isinstance(wa_ts_raw, (int, float)):
        try:
            wa_ts_dt = datetime.fromtimestamp(int(wa_ts_raw), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            wa_ts_dt = None
    elif isinstance(wa_ts_raw, str):
        try:
            wa_ts_dt = datetime.fromisoformat(wa_ts_raw.replace("Z", "+00:00"))
        except ValueError:
            wa_ts_dt = None

    historical_skip = False
    if wa_conn is not None and wa_ts_dt is not None:
        historical_skip = bool(is_inbound_before_ai_live_since(wa_conn, wa_ts_dt))
    historical_import_flag = bool(inbound_extra.get("historical_import"))
    step_4_details["whatsapp_timestamp"] = wa_ts_dt.isoformat() if wa_ts_dt else None
    step_4_details["whatsapp_ai_live_since"] = connection_block.get("whatsapp_ai_live_since")
    step_4_details["before_ai_live_since"] = historical_skip
    step_4_details["historical_import_flag"] = historical_import_flag
    if historical_skip or historical_import_flag:
        step_4_blockers.append("historical_before_ai_live_since")

    step_4 = {
        "ok": (len(step_4_blockers) == 0),
        "blocked_by": step_4_blockers,
        "details": step_4_details,
        "reason": (
            "AI gate blocks: " + ", ".join(step_4_blockers)
            if step_4_blockers else None
        ),
    }

    # ── Stage 5: ai_generated ───────────────────────────────────
    # We define "AI generated a reply" as: there exists an outbound
    # MessageEvent in the same conversation with id > target.id.
    # `record_outbound_message` and the brain happy-path both write
    # this row BEFORE the wire call, so this is the right signal
    # for "the AI produced a string".
    step_5: Dict[str, Any] = {"ok": False, "details": None, "blocked_by": [], "reason": None}
    outbound_after: Optional[MessageEvent] = None
    if target_message is not None and target_message.conversation_id is not None:
        outbound_after = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id == target_message.conversation_id,
                MessageEvent.direction == "outbound",
                MessageEvent.id > target_message.id,
            )
            .order_by(MessageEvent.id.asc())
            .first()
        )
    if outbound_after is None:
        step_5["blocked_by"].append("no_outbound_after_inbound")
        if step_4["ok"]:
            step_5["reason"] = (
                "كل البوّابات سمحت لكن لم يُسجَّل أي رد. غالباً brain أعاد "
                "{reply: None} أو رفع استثناء (انظر [Merchant/Brain] / [BRAIN_RESULT] في logs). "
                "تحقق من توفر OPENAI_API_KEY / ANTHROPIC_API_KEY."
            )
        else:
            step_5["reason"] = (
                "لم يُسجَّل أي رد لأن إحدى البوابات منعت الذكاء (راجع ai_allowed)."
            )
    else:
        out_extra = outbound_after.extra_metadata or {}
        step_5["ok"] = True
        delta_seconds: Optional[float] = None
        if target_message.created_at and outbound_after.created_at:
            # Coerce both sides to aware to keep the subtraction
            # legal regardless of how the DB driver returns them.
            delta_seconds = (
                _aware(outbound_after.created_at) - _aware(target_message.created_at)
            ).total_seconds()
        step_5["details"] = {
            "outbound_message_event_id": int(outbound_after.id),
            "outbound_event_type":       outbound_after.event_type,
            "outbound_created_at":       _aware(outbound_after.created_at).isoformat() if outbound_after.created_at else None,
            "seconds_after_inbound":     round(delta_seconds, 3) if delta_seconds is not None else None,
            "body_preview":              _truncate(outbound_after.body, 600),
            "wa_message_id":             out_extra.get("wa_message_id"),
            "source":                    out_extra.get("source"),
            "media_fallback":            out_extra.get("media_fallback"),
        }

    # ── Stage 6: send_attempted ─────────────────────────────────
    # In the current architecture, persisting an outbound
    # MessageEvent IS the send attempt — `_post_wa` is called
    # immediately after, and we only see a row when the brain
    # produced a string. The distinguishing signal between
    # "attempted" and "delivered to provider" lives in
    # `extra_metadata.wa_message_id`: only set on a 200 response
    # that carried a wamid (see `_extract_provider_message_id`).
    step_6: Dict[str, Any] = {"ok": False, "details": None, "blocked_by": [], "reason": None}
    if outbound_after is None:
        step_6["blocked_by"].append("no_outbound_attempt")
        step_6["reason"] = "لم يتم استدعاء الموزّع لأن AI لم يولّد رد (راجع ai_generated)."
    else:
        out_extra = outbound_after.extra_metadata or {}
        provider_msg_id = out_extra.get("wa_message_id")
        step_6["ok"] = True
        step_6["details"] = {
            "attempted_at": _aware(outbound_after.created_at).isoformat() if outbound_after.created_at else None,
            "provider_message_id_recorded": bool(provider_msg_id),
            "provider_message_id_tail": _mask_secret_tail(provider_msg_id) if provider_msg_id else None,
        }

    # ── Stage 7: send_status ────────────────────────────────────
    # We can't replay the wire call read-only; the closest evidence
    # is the presence of a wamid in the outbound's metadata
    # (provider accepted the message) and any subsequent inbound
    # statuses persisted on the conversation. For conversational
    # replies the WhatsApp status webhook updates the campaign log
    # only when the wamid happens to match a campaign row — so for
    # most conversational replies the status remains "unknown".
    step_7: Dict[str, Any] = {"ok": None, "details": None, "blocked_by": [], "reason": None}
    if outbound_after is None:
        step_7["ok"] = False
        step_7["blocked_by"].append("no_outbound_to_evaluate")
        step_7["reason"] = "لا يوجد رد محفوظ يمكن تتبّع حالة تسليمه."
    else:
        out_extra = outbound_after.extra_metadata or {}
        wamid = out_extra.get("wa_message_id")
        if wamid:
            step_7["ok"] = True
            step_7["details"] = {
                "provider_message_id_present": True,
                "note": (
                    "تم استلام الرد بنجاح من 360dialog ولديه wamid. "
                    "حالة التسليم/القراءة الفعلية تأتي عبر status webhook "
                    "(غير مسجلة على message_events للمحادثات اللحظية)."
                ),
            }
        else:
            step_7["ok"] = False
            step_7["blocked_by"].append("missing_wamid")
            step_7["reason"] = (
                "تم حفظ رد AI لكن 360dialog لم يُعِد wamid — يعني الإرسال فشل "
                "(token غير صحيح، D360-API-KEY خاطئ، quota مستنفد، أو خطأ provider). "
                "راجع [SEND_DEBUG] / [WA provider_post] في logs."
            )

    # ── Final verdict ───────────────────────────────────────────
    pipeline_blocks = {
        "step_1_webhook_received":  step_1,
        "step_2_message_saved":     step_2,
        "step_3_conversation_state": step_3,
        "step_4_ai_allowed":        step_4,
        "step_5_ai_generated":      step_5,
        "step_6_send_attempted":    step_6,
        "step_7_send_status":       step_7,
    }

    # Pick the first stage that's not green and surface it as the
    # verdict. Stage 7 with ok=None counts as "indeterminate" not
    # "failed" — we promote it only when no earlier stage failed.
    failed_stage: Optional[str] = None
    failed_codes: List[str] = []
    for stage_name, stage in pipeline_blocks.items():
        ok = stage.get("ok")
        if ok is False:
            failed_stage = stage_name
            failed_codes = list(stage.get("blocked_by") or [])
            break

    if failed_stage is None:
        verdict_ar = "الذكاء رد بنجاح على هذه الرسالة وتم تسليم الرد لـ 360dialog."
        verdict_code = "ok"
        overall_ok = True
    else:
        verdict_ar = pipeline_blocks[failed_stage].get("reason") or f"المرحلة المتعثرة: {failed_stage}"
        verdict_code = failed_stage
        overall_ok = False

    response = {
        "ts":                  now_utc.isoformat(),
        "tenant_id":           int(tenant_id),
        "tenant_name":         tenant.name,
        "input": {
            "phone_masked":    _mask_phone(phone or "") if phone else None,
            "wa_message_id":   wa_message_id,
        },
        "connection":          connection_block,
        "inbound_message_found": target_message is not None,
        "pipeline":            pipeline_blocks,
        "verdict": {
            "code":            verdict_code,
            "failed_stage":    failed_stage,
            "blocked_by":      failed_codes,
            "reason_ar":       verdict_ar,
        },
        "issues":              issues,
        "hints":               hints,
        "ok":                  overall_ok,
    }

    audit(
        "admin_debug_inbound_trace",
        admin_sub=admin_sub,
        tenant_id=int(tenant_id),
        phone_masked=_mask_phone(phone or "") if phone else "",
        wa_message_id=wa_message_id or "",
        verdict_code=verdict_code,
        overall_ok=overall_ok,
    )
    logger.info(
        "[ADMIN/INBOUND_TRACE] done admin=%s tenant=%s ok=%s verdict=%s",
        admin_sub, tenant_id, overall_ok, verdict_code,
    )

    return response


# ════════════════════════════════════════════════════════════════════════
# F18 — Last provider send (raw wire activity for one tenant)
# ════════════════════════════════════════════════════════════════════════
#
# Pairs with F17. F17 tells you "AI did or didn't reply" — F18 tells
# you "the AI tried, here's exactly what 360dialog/Meta said back."
# Both are needed because the failure F18 is built for catches:
#
#     status_code == 200
#     response body has no `error` envelope
#     BUT no `messages[0].id` either
#
# Pre-F18 the wire layer logged this as a success. The campaign
# dispatcher / inbound dispatcher recorded "delivered", and the
# merchant was left wondering why nothing arrived. F18 now stamps
# these as ``classification: "missing_wamid"`` in the ring buffer,
# injects an ``error`` envelope into the response so downstream
# treats it as a failed send, and surfaces the raw provider body
# through this endpoint so support can read the actual error.
#
# Security:
#   * ``require_admin`` only — no env flag. Support uses this
#     routinely.
#   * The recipient phone in each request payload is pre-masked
#     by the ring buffer's ``_scrub_payload``.
#   * The API key never enters the ring buffer — only
#     ``token_source`` and the last 4 chars via
#     ``summarize_headers``.
#   * Process-local state — wiped on every Railway redeploy.

@router.get("/last-provider-send")
async def admin_debug_last_provider_send(
    tenant_id: int = Query(..., description="Merchant tenant id."),
    limit:     int = Query(
        10,
        ge=1, le=50,
        description="How many recent attempts to return (newest first).",
    ),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Return the last N WhatsApp provider POST attempts for a tenant,
    newest first.

    Each entry is the raw wire-layer record that
    ``provider_post_with_context`` captured: full URL, sanitised
    request payload, response status + body, parsed wamid (or
    ``null``), and a ``classification`` string the dashboard can
    switch on:

      * ``ok``                   — 2xx + wamid (or non-send call that
                                    succeeded)
      * ``non_2xx``              — HTTP error
      * ``provider_error_field`` — 2xx body carries ``error`` envelope
      * ``missing_wamid``        — 2xx, no error envelope, BUT no
                                    ``messages[0].id`` either — the
                                    silent failure F18 was created for
      * ``exception``            — transport-level failure
                                    (network/timeout)

    Resilience notes
    ────────────────
    * Process-local ring buffer — every Railway redeploy resets it.
      Hit this endpoint within minutes of seeing the symptom.
    * Empty buffer is NOT an error — it just means no provider POST
      has fired for this tenant in this process.
    * The endpoint never raises 5xx on observability failures; if
      the ring buffer can't be read for any reason, it returns
      ``attempts: []`` with the explanation in ``issues``.

    Cross-references
    ────────────────
    Read alongside ``GET /admin/debug/inbound-trace`` — F17 reports
    on persisted state, F18 reports on wire activity.
    """
    from core.wa_provider_observability import (  # noqa: PLC0415
        get_recent_attempts,
    )
    from datetime import datetime, timezone  # noqa: PLC0415

    admin_sub = _admin.get("sub") or "?"
    logger.info(
        "[ADMIN/LAST_PROVIDER_SEND] start admin=%s tenant=%s limit=%s",
        admin_sub, tenant_id, limit,
    )

    issues: List[str] = []
    hints:  List[str] = []

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=f"tenant {tenant_id} not found",
        )

    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .order_by(WhatsAppConnection.id.desc())
        .first()
    )
    connection_phone_id = getattr(wa_conn, "phone_number_id", None) if wa_conn else None
    connection_provider = (
        # Best-effort: we read the column rather than calling
        # ``wa_provider`` to avoid pulling the services module here.
        getattr(wa_conn, "provider", None) if wa_conn else None
    )

    try:
        attempts = get_recent_attempts(tenant_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[ADMIN/LAST_PROVIDER_SEND] ring buffer read failed admin=%s tenant=%s err=%s",
            admin_sub, tenant_id, exc,
        )
        attempts = []
        issues.append(
            "تعذّر قراءة سجل الإرسال (ring buffer). راجع logs."
        )

    # Convenience: count classifications so the dashboard can show
    # a "3/10 missing wamid in last 10 sends" summary.
    classification_counts: Dict[str, int] = {}
    last_send_missing_wamid: Optional[Dict[str, Any]] = None
    mismatch_phone_id_attempts: List[int] = []  # indices into `attempts`
    for idx, att in enumerate(attempts):
        cls = att.get("classification") or "unknown"
        classification_counts[cls] = classification_counts.get(cls, 0) + 1

        if cls == "missing_wamid" and last_send_missing_wamid is None:
            last_send_missing_wamid = att

        # Cross-check: the wire layer captured the connection's
        # phone_number_id. If the *current* connection.phone_number_id
        # disagrees with what was recorded, the merchant most likely
        # reconnected the channel under a different phone_id —
        # outbound traffic still aims at the OLD phone_id until the
        # process restarts.
        recorded_phone_id = att.get("connection_phone_number_id")
        if (
            connection_phone_id
            and recorded_phone_id
            and str(connection_phone_id) != str(recorded_phone_id)
        ):
            mismatch_phone_id_attempts.append(idx)

    if mismatch_phone_id_attempts:
        issues.append(
            "تم اكتشاف اختلاف بين phone_number_id الحالي على الاتصال "
            f"({connection_phone_id}) وما تم استخدامه فعلياً في بعض "
            f"المحاولات ({len(mismatch_phone_id_attempts)} من {len(attempts)}). "
            "غالباً أُعيد ربط القناة برقم مختلف — أعد تشغيل الـ process لتحديث "
            "المرجع المُخزَّن."
        )

    if classification_counts.get("missing_wamid", 0) > 0:
        issues.append(
            f"{classification_counts['missing_wamid']} من آخر "
            f"{len(attempts)} محاولات إرسال أرجعت 2xx بدون wamid — "
            "الإرسال فشل صامتاً قبل F18. تحقّق من D360-API-KEY و "
            "صلاحية قناة phone_number_id."
        )
    if classification_counts.get("non_2xx", 0) > 0:
        issues.append(
            f"{classification_counts['non_2xx']} محاولة فشلت بـ HTTP غير 2xx. "
            "افتح أحدث attempt واقرأ response_body."
        )
    if classification_counts.get("provider_error_field", 0) > 0:
        issues.append(
            f"{classification_counts['provider_error_field']} محاولة 2xx لكنها "
            "تحمل error envelope. اقرأ response_body.error لمعرفة code/subcode."
        )

    if not attempts:
        hints.append(
            "لم يُسجَّل أي POST لهذا التاجر في هذا الـ process. سبب محتمل: "
            "الـ process أُعيد تشغيله للتو، أو لم يحاول إرسال أي رسالة "
            "خارجة بعد آخر deploy."
        )

    response = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "tenant_id":     int(tenant_id),
        "tenant_name":   tenant.name,
        "current_connection": {
            "found":           wa_conn is not None,
            "id":              int(wa_conn.id) if wa_conn else None,
            "provider":        connection_provider,
            "connection_type": getattr(wa_conn, "connection_type", None) if wa_conn else None,
            "status":          getattr(wa_conn, "status", None) if wa_conn else None,
            "phone_number_id": connection_phone_id,
            "access_token_tail": _mask_secret_tail(
                getattr(wa_conn, "access_token", None) if wa_conn else None
            ),
        },
        "limit":         int(limit),
        "attempts_returned": len(attempts),
        "classification_counts": classification_counts,
        "last_missing_wamid_attempt": last_send_missing_wamid,
        "mismatch_phone_id_count":  len(mismatch_phone_id_attempts),
        "attempts":      attempts,
        "issues":        issues,
        "hints":         hints,
        "ok":            len(issues) == 0,
    }

    audit(
        "admin_debug_last_provider_send",
        admin_sub=admin_sub,
        tenant_id=int(tenant_id),
        attempts_returned=len(attempts),
        missing_wamid_count=classification_counts.get("missing_wamid", 0),
        non_2xx_count=classification_counts.get("non_2xx", 0),
    )
    logger.info(
        "[ADMIN/LAST_PROVIDER_SEND] done admin=%s tenant=%s attempts=%d missing_wamid=%d",
        admin_sub, tenant_id, len(attempts),
        classification_counts.get("missing_wamid", 0),
    )

    return response


# ════════════════════════════════════════════════════════════════════════
# F20 — Outbound trace (MessageEvent ↔ wire-layer cross-join)
# ════════════════════════════════════════════════════════════════════════
#
# F18 (``/last-provider-send``) reports what the wire layer SAW.
# F17 (``/inbound-trace``) reports what the pipeline DID with an inbound.
# Neither one closes the loop between "this row appeared in the dashboard
# as an AI reply" and "did the customer actually receive it?".
#
# F20 is that bridge. Given a tenant_id (and optional phone), it returns
# the latest outbound ``MessageEvent`` rows annotated with the
# wire-layer outcome that ``core.outbound_send_status`` stamped into
# ``extra_metadata.provider_send``:
#
#   * ``queued``  — row persisted, send hasn't returned yet (very brief
#                   window unless something is hung).
#   * ``sent``    — Meta / 360dialog returned 2xx + wamid.
#   * ``failed``  — non-2xx / provider error envelope / missing wamid /
#                   transport exception. The ``error`` block carries
#                   the Meta code+subcode and the Arabic merchant
#                   label from ``services.meta_errors``.
#   * ``null``    — historical row written before this fix (no stamp).
#
# Useful for: "the merchant says the AI replied but the customer never
# got the message" — call this endpoint with the customer's phone and
# see the exact failure code per outbound row.

@router.get("/outbound-trace")
async def admin_debug_outbound_trace(
    tenant_id: int = Query(..., description="Merchant tenant id to trace."),
    phone: Optional[str] = Query(
        None,
        description=(
            "Optional customer phone (E.164 or bare digits). When set, "
            "only outbound rows targeting this phone are returned."
        ),
    ),
    minutes: int = Query(
        60,
        ge=1, le=1440,
        description=(
            "Sliding time window in minutes (default 1h, max 24h). "
            "Rows older than the window are excluded so big inboxes "
            "stay responsive."
        ),
    ),
    limit: int = Query(
        30,
        ge=1, le=200,
        description="Max number of MessageEvent rows to return (newest first).",
    ),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Return the last N outbound MessageEvent rows for a tenant,
    each annotated with the WhatsApp wire-layer outcome.

    Response shape
    ──────────────
      {
        "ts":          ISO,
        "tenant_id":   int,
        "filter":      { "phone": str|null, "minutes": int, "limit": int },
        "summary": {
            "rows":          int,
            "sent":          int,
            "failed":        int,
            "queued":        int,
            "unstamped":     int,        # rows with no provider_send
            "by_error_key":  { key: count, ... }
        },
        "current_connection": {
            "found":           bool,
            "provider":        "meta" | "dialog360",
            "phone_number_id": str|null,
            "status":          str|null,
            "sending_enabled": bool|null,
        },
        "rows": [
          {
            "message_event_id": int,
            "conversation_id":  int|null,
            "created_at":       ISO,
            "event_type":       str|null,
            "body_preview":     str (≤200 chars),
            "to":               str (masked),
            "send_status":      "queued" | "sent" | "failed" | null,
            "wamid":            str|null,
            "operation":        str|null,
            "duration_ms":      number|null,
            "queued_at":        ISO|null,
            "completed_at":     ISO|null,
            "error": {
                "key":            str,
                "label_ar":       str,
                "advice_ar":      str|null,
                "code":           int|str|null,
                "subcode":        int|str|null,
                "is_recoverable": bool,
            } | null
          },
          ...
        ],
        "issues":      [arabic strings],
        "hints":       [arabic strings],
        "ok":          bool
      }
    """
    from datetime import datetime, timezone, timedelta  # noqa: PLC0415
    from sqlalchemy import or_, func  # noqa: PLC0415
    from models import MessageEvent  # noqa: PLC0415

    admin_sub = _admin.get("sub") or "?"
    logger.info(
        "[ADMIN/OUTBOUND_TRACE] start admin=%s tenant=%s phone=%s minutes=%s limit=%s",
        admin_sub, tenant_id, _mask_phone(phone or ""), minutes, limit,
    )

    issues: List[str] = []
    hints:  List[str] = []

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id} not found")

    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .order_by(WhatsAppConnection.id.desc())
        .first()
    )
    connection_block = {
        "found":           wa_conn is not None,
        "provider":        getattr(wa_conn, "provider", None) if wa_conn else None,
        "phone_number_id": getattr(wa_conn, "phone_number_id", None) if wa_conn else None,
        "status":          getattr(wa_conn, "status", None) if wa_conn else None,
        "sending_enabled": (
            bool(getattr(wa_conn, "sending_enabled", False)) if wa_conn else None
        ),
        "connection_type": getattr(wa_conn, "connection_type", None) if wa_conn else None,
    }

    # Surface obvious mis-configurations up front so the operator
    # doesn't have to read each row.
    if wa_conn is None:
        issues.append(
            "لا يوجد سجل WhatsApp connection لهذا التاجر — لا يمكن الإرسال أصلاً."
        )
    elif not getattr(wa_conn, "sending_enabled", True):
        issues.append(
            "sending_enabled=false على سجل الاتصال — كل الإرساليات سترفض. "
            "تحقق من إعدادات القناة وأعد تشغيل المعالج إن لزم."
        )
    elif getattr(wa_conn, "status", None) and str(wa_conn.status).lower() != "connected":
        issues.append(
            f"حالة الاتصال = {wa_conn.status!r} (ليست connected) — قد يفسر فشل الإرسال."
        )

    cutoff = datetime.utcnow() - timedelta(minutes=minutes)

    q = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            func.lower(MessageEvent.direction) == "outbound",
            MessageEvent.created_at >= cutoff,
        )
    )

    if phone:
        # Match by suffix of either ``phone`` or ``customer_phone`` in
        # extra_metadata. Same logic the wire-layer stamp uses.
        digits = "".join(ch for ch in phone if ch.isdigit())
        suffix = digits[-9:] if len(digits) >= 9 else digits
        if suffix:
            phone_text   = MessageEvent.extra_metadata["phone"].astext
            customer_txt = MessageEvent.extra_metadata["customer_phone"].astext
            q = q.filter(
                or_(
                    func.right(phone_text, len(suffix)) == suffix,
                    func.right(customer_txt, len(suffix)) == suffix,
                )
            )

    me_rows = q.order_by(MessageEvent.id.desc()).limit(limit).all()

    def _preview(s: Optional[str], n: int = 200) -> str:
        if not s:
            return ""
        s2 = str(s)
        return s2 if len(s2) <= n else s2[:n] + "…"

    counts = {"sent": 0, "failed": 0, "queued": 0, "unstamped": 0}
    by_error_key: Dict[str, int] = {}
    rows_out: List[Dict[str, Any]] = []
    for r in me_rows:
        meta = r.extra_metadata or {}
        ps   = meta.get("provider_send") if isinstance(meta.get("provider_send"), dict) else None
        send_status = (ps or {}).get("status") if ps else None
        if send_status == "sent":
            counts["sent"] += 1
        elif send_status == "failed":
            counts["failed"] += 1
            key = ((ps or {}).get("error") or {}).get("key") or "unknown"
            by_error_key[key] = by_error_key.get(key, 0) + 1
        elif send_status == "queued":
            counts["queued"] += 1
        else:
            counts["unstamped"] += 1

        to_masked = _mask_phone(meta.get("phone") or meta.get("customer_phone") or "")
        err = (ps or {}).get("error") if ps else None
        err_out: Optional[Dict[str, Any]] = None
        if isinstance(err, dict):
            err_out = {
                "key":             err.get("key"),
                "label_ar":        err.get("label_ar"),
                "advice_ar":       err.get("advice_ar"),
                "code":            err.get("code"),
                "subcode":         err.get("subcode"),
                "is_recoverable":  bool(err.get("is_recoverable")),
                "fbtrace_id":      err.get("fbtrace_id"),
            }

        rows_out.append({
            "message_event_id": int(r.id),
            "conversation_id":  int(r.conversation_id) if r.conversation_id else None,
            "created_at":       r.created_at.isoformat() if r.created_at else None,
            "event_type":       r.event_type,
            "body_preview":     _preview(r.body),
            "to":               to_masked,
            "send_status":      send_status,
            "wamid":            (ps or {}).get("wamid"),
            "operation":        (ps or {}).get("operation"),
            "duration_ms":      (ps or {}).get("duration_ms"),
            "queued_at":        (ps or {}).get("queued_at"),
            "completed_at":     (ps or {}).get("completed_at"),
            "error":            err_out,
        })

    summary = {
        "rows":          len(rows_out),
        **counts,
        "by_error_key":  by_error_key,
    }

    if counts["failed"] > 0:
        issues.append(
            f"{counts['failed']} من آخر {len(rows_out)} رسائل صادرة فشلت في الإرسال. "
            "اقرأ كل صف وعالج كل error.key على حدة."
        )
    if counts["queued"] > 0:
        # Anything still queued after the window cutoff is a leak — either
        # the wire layer never returned, or the worker died mid-send.
        issues.append(
            f"{counts['queued']} رسائل بقيت بحالة 'queued' — الإرسال لم يكتمل. "
            "تحقق من logs /admin/debug/last-provider-send وأعد تشغيل الـ worker إن لزم."
        )
    if counts["unstamped"] > 0:
        hints.append(
            f"{counts['unstamped']} صف بدون provider_send — رسائل قديمة من قبل تطبيق "
            "هذا الإصلاح، أو من مسار لم يمر عبر _post_wa (مثل campaign dispatcher "
            "الذي يستخدم سجلاته الخاصة)."
        )
    if not rows_out:
        hints.append(
            "لا توجد رسائل صادرة في الفترة المحددة. جرّب توسيع `minutes` أو "
            "إزالة فلتر `phone`."
        )

    response = {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "tenant_id":    int(tenant_id),
        "tenant_name":  tenant.name,
        "filter": {
            "phone":   _mask_phone(phone or "") if phone else None,
            "minutes": int(minutes),
            "limit":   int(limit),
        },
        "current_connection": connection_block,
        "summary":      summary,
        "rows":         rows_out,
        "issues":       issues,
        "hints":        hints,
        "ok":           len(issues) == 0,
    }

    audit(
        "admin_debug_outbound_trace",
        admin_sub=admin_sub,
        tenant_id=int(tenant_id),
        rows=len(rows_out),
        failed=counts["failed"],
        queued=counts["queued"],
    )
    logger.info(
        "[ADMIN/OUTBOUND_TRACE] done admin=%s tenant=%s rows=%d sent=%d failed=%d queued=%d",
        admin_sub, tenant_id, len(rows_out),
        counts["sent"], counts["failed"], counts["queued"],
    )
    return response


# ════════════════════════════════════════════════════════════════════════
# F21 — AI outbound success rate (regression sentinel)
# ════════════════════════════════════════════════════════════════════════
#
# What this catches
# -----------------
# F18 (outbound-trace) is per-tenant + per-row: it answers
# "did MY merchant's last 30 messages reach Meta?". F21 is the
# fleet-wide, AI-only version: it answers "across ALL tenants in
# the last N minutes, what fraction of AI-generated outbound
# messages actually reached WhatsApp?".
#
# Why a separate endpoint
# -----------------------
# AI is the only outbound path that's fully automated end-to-end
# (no merchant click between generate and send). A regression in
# the send path — a token rotation that left tenants un-registered,
# a new burst throttle that bites real traffic, a Meta API change
# we missed — shows up here FIRST as a sudden drop in
# ``ai.sent_rate`` and a spike in ``failure_reasons``.
#
# The endpoint is deliberately read-only and aggregated. It does
# NOT surface individual customer phones or message bodies — admins
# get counts, error keys, and merchant-facing labels only.
@router.get("/ai-outbound-stats")
async def admin_debug_ai_outbound_stats(
    minutes: int = Query(
        60,
        ge=1, le=10080,
        description=(
            "Sliding time window in minutes (default 1h, max 7 days). "
            "Aggregation is in-memory so larger windows pay a bigger "
            "scan cost; keep <= 1440 for snappy dashboards."
        ),
    ),
    tenant_id: Optional[int] = Query(
        None,
        description=(
            "Optional tenant filter. Omit to aggregate across the whole "
            "fleet (regression-sentinel mode). With a value, the stats "
            "narrow to a single merchant for support investigations."
        ),
    ),
    top_n: int = Query(
        10,
        ge=1, le=50,
        description="How many distinct failure reasons to surface.",
    ),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """AI outbound success-rate funnel + top failure reasons.

    Response shape
    ──────────────
      {
        "ts":          ISO,
        "window_minutes": int,
        "tenant_id":   int | null,
        "funnel": {
            "generated":  int,   # AI rows persisted (queued+sent+failed+unstamped)
            "attempted":  int,   # AI rows that left the queued state
            "sent":       int,   # provider_send.status == sent
            "failed":     int,   # provider_send.status == failed
            "queued":     int,   # still queued (in-flight or stuck)
            "unstamped":  int    # historical / non-_post_wa rows
        },
        "rates": {
            "attempt_rate":  float,   # attempted / generated
            "sent_rate":     float,   # sent / generated
            "failure_rate":  float,   # failed / attempted
            "stuck_rate":    float,   # queued / generated
        },
        "failure_reasons": [   # top_n entries, descending
            {
                "key":      str,    # e.g. "out_of_24h_window"
                "label_ar": str,    # merchant-facing label
                "count":    int,
                "share":    float,  # count / failed
                "is_recoverable": bool|null,
            }, ...
        ],
        "alerts": [str, ...]   # heuristic regression flags
      }

    What "AI" means here
    --------------------
    We filter MessageEvent rows to those whose
    ``extra_metadata.is_ai`` is truthy. ``StateManager.save_message``
    sets this on every AI-generated outbound. Manual replies from
    ``/conversations/reply`` and campaign sends are EXCLUDED — they
    have their own audit trails.

    Heuristic alerts
    ----------------
    The endpoint flags suspicious patterns so an admin polling this
    can spot regressions without staring at the numbers:

      * ``sent_rate < 0.85``        — system-wide send health is bad
      * ``stuck_rate > 0.05``       — queued rows aren't progressing
                                      (worker stuck, stamping broken)
      * single error_key > 30%      — one failure mode is dominant
                                      (token rotation, Meta outage)
    """
    from datetime import datetime, timedelta, timezone as _tz  # noqa: PLC0415
    from sqlalchemy import func as _func  # noqa: PLC0415
    from models import MessageEvent  # noqa: PLC0415

    admin_sub = _admin.get("sub") if isinstance(_admin, dict) else None
    started   = time.time()
    window    = timedelta(minutes=int(minutes))
    cutoff    = datetime.utcnow() - window

    # ── Base query ────────────────────────────────────────────────────
    # Filter to OUTBOUND rows in the window. We then filter by
    # ``is_ai`` in Python because the JSONB ``->`` operator on
    # boolean values doesn't index well across the fleet — for
    # 1-week aggregates we'd want a column, but for the F21 use
    # case the row count is bounded enough that the post-filter
    # is cheap (and we keep the SQL portable).
    q = (
        db.query(MessageEvent.id, MessageEvent.tenant_id,
                 MessageEvent.created_at, MessageEvent.extra_metadata)
        .filter(_func.lower(MessageEvent.direction) == "outbound")
        .filter(MessageEvent.created_at >= cutoff)
    )
    if tenant_id is not None:
        q = q.filter(MessageEvent.tenant_id == int(tenant_id))

    rows = q.order_by(MessageEvent.id.desc()).limit(50000).all()

    generated = 0
    attempted = 0
    sent      = 0
    failed    = 0
    queued    = 0
    unstamped = 0

    # error_key → (count, label_ar, is_recoverable)
    reasons: Dict[str, Dict[str, Any]] = {}

    for _id, _tid, _created, meta in rows:
        m = meta or {}
        # AI filter: only count rows the AI pipeline produced.
        if not bool(m.get("is_ai")):
            continue
        generated += 1
        ps = m.get("provider_send") if isinstance(m.get("provider_send"), dict) else None
        if not ps:
            unstamped += 1
            continue
        status = (ps.get("status") or "").lower()
        if status == "sent":
            sent += 1
            attempted += 1
        elif status == "failed":
            failed += 1
            attempted += 1
            err = ps.get("error") if isinstance(ps.get("error"), dict) else {}
            key = (err.get("key") or "unknown") or "unknown"
            entry = reasons.setdefault(key, {
                "key":            key,
                "label_ar":       err.get("label_ar") or key,
                "is_recoverable": err.get("is_recoverable"),
                "count":          0,
            })
            entry["count"] += 1
            # Prefer a non-empty label if a later row carries one
            if not entry.get("label_ar") or entry["label_ar"] == key:
                if err.get("label_ar"):
                    entry["label_ar"] = err["label_ar"]
        elif status == "queued":
            queued += 1
        else:
            # Unknown status string — treat as unstamped so the row
            # still surfaces in the funnel without skewing the
            # success rate.
            unstamped += 1

    def _safe_rate(numer: int, denom: int) -> float:
        return round(numer / denom, 4) if denom > 0 else 0.0

    rates = {
        "attempt_rate": _safe_rate(attempted, generated),
        "sent_rate":    _safe_rate(sent,      generated),
        "failure_rate": _safe_rate(failed,    attempted),
        "stuck_rate":   _safe_rate(queued,    generated),
    }

    failure_reasons: List[Dict[str, Any]] = sorted(
        (
            {
                **r,
                "share": _safe_rate(r["count"], failed),
            }
            for r in reasons.values()
        ),
        key=lambda e: e["count"],
        reverse=True,
    )[: int(top_n)]

    # ── Heuristic alerts ─────────────────────────────────────────────
    alerts: List[str] = []
    if generated >= 20 and rates["sent_rate"] < 0.85:
        alerts.append(
            f"AI sent_rate={rates['sent_rate']*100:.1f}% < 85% — احتمال "
            "regression في مسار الإرسال. تحقق من logs أو من "
            "/admin/debug/last-provider-send."
        )
    if generated >= 50 and rates["stuck_rate"] > 0.05:
        alerts.append(
            f"AI stuck_rate={rates['stuck_rate']*100:.1f}% > 5% — صفوف "
            "queued لا تتقدّم. تحقق من worker أو من "
            "core.outbound_send_status."
        )
    if failed >= 10 and failure_reasons:
        top = failure_reasons[0]
        if top["share"] >= 0.30:
            alerts.append(
                f"سبب فشل مهيمن: {top['label_ar']} ({top['key']}) "
                f"يمثّل {top['share']*100:.1f}% من حالات الفشل — "
                "تحقق من تكوين هذا المسار."
            )

    response = {
        "ts":              datetime.now(_tz.utc).isoformat(),
        "window_minutes":  int(minutes),
        "tenant_id":       int(tenant_id) if tenant_id is not None else None,
        "funnel": {
            "generated":  generated,
            "attempted":  attempted,
            "sent":       sent,
            "failed":     failed,
            "queued":     queued,
            "unstamped":  unstamped,
        },
        "rates":           rates,
        "failure_reasons": failure_reasons,
        "alerts":          alerts,
        "elapsed_ms":      int((time.time() - started) * 1000),
    }
    audit(
        "admin_debug_ai_outbound_stats",
        admin_sub=admin_sub,
        tenant_id=int(tenant_id) if tenant_id is not None else None,
        window_minutes=int(minutes),
        generated=generated,
        sent=sent,
        failed=failed,
    )
    logger.info(
        "[ADMIN/AI_OUTBOUND_STATS] admin=%s tenant=%s window=%dm "
        "generated=%d sent=%d failed=%d queued=%d stuck=%.1f%% "
        "sent_rate=%.1f%%",
        admin_sub, tenant_id, minutes,
        generated, sent, failed, queued,
        rates["stuck_rate"] * 100,
        rates["sent_rate"] * 100,
    )
    return response


# ════════════════════════════════════════════════════════════════════════
# F19 — Recent webhook events (inbound routing audit)
# ════════════════════════════════════════════════════════════════════════
#
# F17 traces a SINGLE message through the pipeline. F18 traces every
# OUTBOUND wire call. F19 closes the loop on the third axis: every
# INCOMING 360dialog webhook, routed or not.
#
# The failure F19 catches:
#
#   The merchant says "the customer sent me a message but it doesn't
#   appear in Nahla." 360dialog's dashboard confirms the webhook
#   fired. F17's inbound-trace finds nothing recent because the
#   webhook hit one of six silent drop points inside
#   `_handle_360dialog_body`:
#
#     1. value.metadata.phone_number_id missing
#     2. no WhatsAppConnection row with that phone_number_id
#     3. multiple connections share the phone_number_id (ambiguous)
#     4. the matched connection's provider is not 'dialog360'
#     5. X-Nahla-Coexistence-Secret header mismatch
#     6. scope mismatch (channel event on coexistence URL etc.)
#
# All six return HTTP 200 to 360dialog, so the merchant has no
# visibility. F19 surfaces the raw routing decision per event from a
# process-local ring buffer and tells you exactly WHICH bucket the
# missing message landed in.
#
# The endpoint is read-only, admin-gated, and process-local — the
# buffer wipes on every Railway redeploy, so hit this within minutes
# of seeing the symptom.

@router.get("/recent-webhook-events")
async def admin_debug_recent_webhook_events(
    tenant_id: Optional[int] = Query(
        None,
        description=(
            "Filter to events whose matched_tenant_id equals this. "
            "Unrouted events (no tenant match) are always included "
            "alongside the matched ones — the operator must see "
            "'webhooks arrived but landed nowhere' for the same "
            "phone_number_id."
        ),
    ),
    phone_number_id: Optional[str] = Query(
        None,
        description=(
            "Filter to events whose payload OR matched-connection "
            "phone_number_id equals this. Useful when the operator "
            "only knows the WABA phone_number_id from the 360dialog "
            "dashboard."
        ),
    ),
    minutes: int = Query(
        30,
        ge=1, le=720,
        description="Sliding time window in minutes (max 12h).",
    ),
    limit: int = Query(
        100,
        ge=1, le=500,
        description="Maximum number of events to return (newest first).",
    ),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Return recent 360dialog webhook deliveries with routing
    outcomes — matched, unrouted_unknown_phone_id,
    unrouted_ambiguous, unrouted_wrong_provider, unrouted_bad_secret,
    unrouted_missing_phone_id, scope_mismatch, exception.

    Why this matters
    ────────────────
    Without F19 there is no way to tell whether a customer's message
    reached your process at all. ``last_webhook_received_at`` on
    ``WhatsAppConnection`` only updates when routing SUCCEEDS — every
    drop case leaves it untouched and 360dialog still records a 200.

    Response shape
    ──────────────
      {
        "ts":  ISO,
        "filter": {
          "tenant_id": ..., "phone_number_id": ..., "minutes": 30,
          "limit": 100
        },
        "route_status_counts": {
          "matched": 12, "unrouted_unknown_phone_id": 3, ...
        },
        "distinct_payload_phone_ids": {
          "100543193146977": 12,
          "1061057720431678": 3   # ← drift signal
        },
        "phone_id_drift_detected": true,
        "events": [ {full event}, ... ],
        "issues":  [arabic],
        "hints":   [arabic],
        "ok":      bool
      }
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    from core.wa_webhook_observability import (  # noqa: PLC0415
        get_distinct_payload_phone_ids,
        get_recent_events,
        get_route_status_counts,
    )

    admin_sub = _admin.get("sub") or "?"
    logger.info(
        "[ADMIN/RECENT_WEBHOOK_EVENTS] start admin=%s tenant=%s phone_id=%s "
        "minutes=%s limit=%s",
        admin_sub, tenant_id, phone_number_id, minutes, limit,
    )

    issues: List[str] = []
    hints:  List[str] = []

    try:
        events  = get_recent_events(
            tenant_id=tenant_id,
            phone_number_id=phone_number_id,
            minutes=minutes,
            include_unrouted=True,
            limit=limit,
        )
        rs_counts   = get_route_status_counts(
            tenant_id=tenant_id,
            phone_number_id=phone_number_id,
            minutes=minutes,
        )
        distinct_pids = get_distinct_payload_phone_ids(minutes=minutes)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[ADMIN/RECENT_WEBHOOK_EVENTS] buffer read failed admin=%s err=%s",
            admin_sub, exc,
        )
        events, rs_counts, distinct_pids = [], {}, {}
        issues.append(
            "تعذّر قراءة سجل webhook events (ring buffer). راجع logs."
        )

    # ── Diagnostic flags ────────────────────────────────────────
    phone_id_drift = len(distinct_pids) > 1
    bad_secret_count       = rs_counts.get("unrouted_bad_secret", 0)
    unknown_phone_count    = rs_counts.get("unrouted_unknown_phone_id", 0)
    ambiguous_count        = rs_counts.get("unrouted_ambiguous", 0)
    missing_phone_count    = rs_counts.get("unrouted_missing_phone_id", 0)
    wrong_provider_count   = rs_counts.get("unrouted_wrong_provider", 0)
    scope_mismatch_count   = rs_counts.get("scope_mismatch", 0)
    exception_count        = rs_counts.get("exception", 0)
    matched_count          = rs_counts.get("matched", 0)
    total_count            = sum(rs_counts.values())

    if phone_id_drift:
        sorted_pids = sorted(distinct_pids.items(), key=lambda kv: -kv[1])
        pretty = ", ".join(f"{pid}({count})" for pid, count in sorted_pids)
        issues.append(
            f"تم استقبال webhooks لأكثر من phone_number_id واحد خلال الـ "
            f"{minutes} دقيقة الأخيرة: {pretty}. غالباً أُعيد ربط القناة برقم "
            "مختلف لكن WhatsAppConnection ما زال يحمل الرقم القديم — "
            "الرسائل التي تصل بالرقم الجديد تُسقَط."
        )
    if unknown_phone_count:
        issues.append(
            f"{unknown_phone_count} webhook(s) وصلت بـ phone_number_id لا "
            "يطابق أي WhatsAppConnection — حدّث الـ phone_number_id على صف "
            "الاتصال (أو أعد ربط القناة من جهة 360dialog)."
        )
    if ambiguous_count:
        issues.append(
            f"{ambiguous_count} webhook(s) أُسقطت بسبب تطابق أكثر من اتصال "
            "بنفس phone_number_id — احذف الاتصالات المكررة."
        )
    if bad_secret_count:
        issues.append(
            f"{bad_secret_count} webhook(s) رُفضت بسبب اختلاف "
            "X-Nahla-Coexistence-Secret — تأكد أن 360dialog dashboard "
            "يحمل نفس السر المخزّن في extra_metadata.coexistence_internal_secret."
        )
    if missing_phone_count:
        hints.append(
            f"{missing_phone_count} webhook(s) جاءت بدون "
            "metadata.phone_number_id — عادة coexistence lifecycle "
            "events غير حرجة."
        )
    if wrong_provider_count:
        issues.append(
            f"{wrong_provider_count} webhook(s) طابقت اتصالاً ليس "
            "dialog360 — صف WhatsAppConnection يحمل provider خاطئ."
        )
    if scope_mismatch_count:
        hints.append(
            f"{scope_mismatch_count} webhook(s) سُجّلت لكن لم تُعالج بسبب "
            "scope mismatch (مثل channel event على coexistence URL). "
            "أكدّ ربط الـ URL الصحيح لكل scope في 360dialog dashboard."
        )
    if exception_count:
        issues.append(
            f"{exception_count} webhook(s) رُفعت داخل routing — راجع logs "
            "للبحث عن [Webhook360] batch failed."
        )

    if total_count == 0:
        hints.append(
            "لم تُسجَّل أي webhooks خلال هذه النافذة — السبب الأرجح أن "
            "الـ process أُعيد تشغيله بعد آخر webhook، أو 360dialog لم "
            "يُرسل أي تسليم لهذا الـ scope. أرسل رسالة اختبار وأعد "
            "الاستعلام."
        )
    elif matched_count == 0 and total_count > 0:
        issues.append(
            f"جميع webhooks الأخيرة ({total_count}) فشل routing لها — "
            "لا تصل أي رسالة inbound فعلاً للذكاء."
        )

    response = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "filter": {
            "tenant_id":       tenant_id,
            "phone_number_id": phone_number_id,
            "minutes":         minutes,
            "limit":           limit,
        },
        "events_returned":              len(events),
        "route_status_counts":          rs_counts,
        "distinct_payload_phone_ids":   distinct_pids,
        "phone_id_drift_detected":      phone_id_drift,
        "events":                       events,
        "issues":                       issues,
        "hints":                        hints,
        "ok": (len(issues) == 0) and (matched_count > 0 or total_count == 0),
    }

    audit(
        "admin_debug_recent_webhook_events",
        admin_sub=admin_sub,
        tenant_id=tenant_id or 0,
        phone_number_id=phone_number_id or "",
        events_returned=len(events),
        unknown_phone_id_count=unknown_phone_count,
        phone_id_drift_detected=phone_id_drift,
    )
    logger.info(
        "[ADMIN/RECENT_WEBHOOK_EVENTS] done admin=%s events=%d drift=%s issues=%d",
        admin_sub, len(events), phone_id_drift, len(issues),
    )

    return response


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  CATALOG CONFIG + DIAGNOSTIC SEND — May 2026 #10                     ║
# ║                                                                       ║
# ║  Two endpoints that close the gap "catalog exists in Meta but the    ║
# ║  webhook can't use it":                                              ║
# ║                                                                       ║
# ║    POST /admin/debug/catalog-config                                  ║
# ║      Set ``meta_catalog_id`` and ``catalog_enabled`` on              ║
# ║      WhatsAppConnection. Until now there was NO write path for       ║
# ║      these columns outside hand-running SQL on the production DB.    ║
# ║                                                                       ║
# ║    POST /admin/debug/whatsapp/send-product                           ║
# ║      Exercise the EXACT same dispatch chain a brain reply would      ║
# ║      use — catalog first, fall back to image+CTA, fall back to       ║
# ║      CTA-only — and return a structured audit so support can         ║
# ║      isolate the failing stage (config / eligibility / payload /    ║
# ║      provider / AI trigger).                                         ║
# ╚════════════════════════════════════════════════════════════════════════╝


class _CatalogConfigBody(BaseModel):
    """Body for ``POST /admin/debug/catalog-config``."""

    tenant_id: int = Field(..., ge=1)
    meta_catalog_id: Optional[str] = Field(
        default=None,
        description=(
            "Meta Commerce Manager catalog id (numeric string). Pass "
            "an empty string to CLEAR the binding."
        ),
    )
    catalog_enabled: Optional[bool] = Field(
        default=None,
        description="Toggle the per-connection kill switch.",
    )


@router.post("/catalog-config")
async def admin_debug_set_catalog_config(
    body: _CatalogConfigBody,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Persist ``meta_catalog_id`` / ``catalog_enabled`` on the
    ``WhatsAppConnection`` row for *tenant_id*.

    The merchant dashboard does not currently surface these fields, so
    operators were hand-editing the DB whenever a catalog needed to be
    wired up. This endpoint is the supported writeable path —
    idempotent, audited, and returns the resulting catalog summary so
    the caller can confirm the binding without a follow-up
    ``GET /admin/debug/catalog-state``.

    Either ``meta_catalog_id`` or ``catalog_enabled`` may be omitted
    in the body; only the supplied fields are written, the others
    keep their current values. Passing ``meta_catalog_id=""`` clears
    the binding (sets the column to NULL).

    Auth: ``require_admin`` — same gate as ``/catalog-state``.
    """
    from core.catalog import catalog_summary  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == body.tenant_id)
        .first()
    )
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail=f"WhatsAppConnection not found for tenant_id={body.tenant_id}",
        )

    before = catalog_summary(conn)
    changes: Dict[str, Any] = {}

    if body.meta_catalog_id is not None:
        new_val = body.meta_catalog_id.strip() or None
        if (conn.meta_catalog_id or None) != new_val:
            conn.meta_catalog_id = new_val
            changes["meta_catalog_id"] = {
                "before": before["meta_catalog_id"],
                "after":  new_val,
            }

    if body.catalog_enabled is not None:
        new_flag = bool(body.catalog_enabled)
        if bool(conn.catalog_enabled) != new_flag:
            conn.catalog_enabled = new_flag
            changes["catalog_enabled"] = {
                "before": bool(before["catalog_enabled"]),
                "after":  new_flag,
            }

    if changes:
        db.commit()
        db.refresh(conn)

    after = catalog_summary(conn)

    admin_sub = _admin.get("sub") or "?"
    audit(
        "admin_debug_set_catalog_config",
        admin_sub=admin_sub,
        tenant_id=body.tenant_id,
        changes=changes,
        catalog_summary=after,
    )
    logger.info(
        "[CATALOG_CONFIG] admin=%s tenant=%s changes=%s summary=%s",
        admin_sub, body.tenant_id, changes, after,
    )

    return {
        "ok":               True,
        "tenant_id":        body.tenant_id,
        "before":           before,
        "after":            after,
        "applied_changes":  changes,
    }


class _SendProductBody(BaseModel):
    """Body for ``POST /admin/debug/whatsapp/send-product``."""

    tenant_id: int = Field(..., ge=1)
    to: str = Field(
        ...,
        description=(
            "Recipient MSISDN in E.164 without the leading + "
            "(e.g. 9665XXXXXXXX)."
        ),
    )
    product_id: Optional[int] = Field(
        default=None,
        description="Resolve by Nahla Product.id (preferred when known).",
    )
    product_title: Optional[str] = Field(
        default=None,
        description=(
            "Resolve by fuzzy title match — same path the visual "
            "enforcer uses. Required when ``product_id`` is absent."
        ),
    )
    mode: str = Field(
        default="auto",
        description=(
            "Send strategy: ``auto`` (catalog → image+CTA → CTA only, "
            "matching the webhook), ``catalog`` (force catalog only), "
            "``image`` (force legacy image+CTA), ``cta`` (force "
            "CTA-only with the buy URL). Any other value → ``auto``."
        ),
    )


@router.post("/whatsapp/send-product")
async def admin_debug_send_product(
    body: _SendProductBody,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Exercise the FULL product send chain for a single test
    recipient and return a structured audit.

    The endpoint runs the same helpers the WhatsApp webhook uses, in
    the same order:

      1. Resolve the product (by id or fuzzy title).
      2. Look up the ``WhatsAppConnection`` for the tenant.
      3. Run ``is_catalog_eligible`` and log the verdict.
      4. Attempt ``send_single_product_message`` (catalog) when
         eligible and ``mode`` allows it.
      5. Attempt legacy ``_send_media_message`` + ``_send_cta_url``
         when the catalog send fell through and ``mode`` allows it.
      6. Attempt ``_send_cta_url`` ONLY as a last resort or when
         ``mode='cta'`` is forced.

    The response shape mirrors what the webhook stamps onto the
    delivery audit:

      {
        "ok":                bool,
        "tenant_id":         int,
        "to_masked":         "9665***430",
        "product": {
          "id":         int,
          "title":      str,
          "external_id": str | None,
          "retailer_id": str | None,
          "image_url":   str | None,
          "product_url": str | None,
        },
        "catalog": {
          "eligible":   bool,
          "reason":     str,
          "attempted":  bool,
          "succeeded":  bool,
          "raw_error":  any | None,
        },
        "image_cta": {
          "attempted":      bool,
          "image_ok":       bool,
          "cta_ok":         bool,
        },
        "cta_only": {
          "attempted":  bool,
          "ok":         bool,
        },
        "final_mode": "catalog" | "image_cta" | "media_only" | "cta_only" | "text_only" | "failed",
        "mode_requested": str,
      }
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415, F401

    from core.catalog import (  # noqa: PLC0415
        effective_retailer_id, is_catalog_eligible,
    )
    from modules.observability.delivery_mode import (  # noqa: PLC0415
        compute_final_delivery_mode, new_delivery_audit,
    )
    from routers.whatsapp_webhook import (  # noqa: PLC0415
        _send_cta_url, _send_media_message, _try_send_catalog_product,
    )
    from services.product_resolver import (  # noqa: PLC0415
        format_product_card_caption, resolve_by_external_id, resolve_by_query,
    )

    admin_sub = _admin.get("sub") or "?"
    requested_mode = (body.mode or "auto").strip().lower()
    if requested_mode not in ("auto", "catalog", "image", "cta"):
        requested_mode = "auto"

    to = (body.to or "").strip().lstrip("+")
    if not to.isdigit() or len(to) < 8:
        raise HTTPException(status_code=400, detail="invalid recipient phone")
    to_masked = f"{to[:4]}***{to[-3:]}" if len(to) >= 7 else f"***{to[-2:]}"

    # ── 1. Resolve product ──────────────────────────────────────
    resolution = None
    if body.product_id is not None:
        try:
            resolution = resolve_by_external_id(db, body.tenant_id, str(body.product_id))
        except Exception:
            resolution = None
        if resolution is None:
            # Fallback: load Product by id and synthesise resolution
            from database.models import Product as _Product  # noqa: PLC0415
            product_row = (
                db.query(_Product)
                .filter(_Product.id == body.product_id,
                        _Product.tenant_id == body.tenant_id)
                .first()
            )
            if product_row is not None:
                try:
                    resolution = resolve_by_external_id(
                        db, body.tenant_id, product_row.external_id or "",
                    )
                except Exception:
                    resolution = None
    if resolution is None and body.product_title:
        try:
            resolution = resolve_by_query(
                db, body.tenant_id, body.product_title.strip(),
            )
        except Exception:
            resolution = None
    if resolution is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "could not resolve product — pass product_id (Nahla "
                "Product.id) or a more specific product_title."
            ),
        )

    # ── 2. Resolve connection + retailer id + eligibility ──────
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == body.tenant_id)
        .first()
    )
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail=f"WhatsAppConnection not found for tenant_id={body.tenant_id}",
        )
    phone_id = str(getattr(conn, "phone_number_id", "") or "").strip()
    if not phone_id:
        raise HTTPException(
            status_code=409,
            detail="WhatsAppConnection missing phone_number_id — provision the connection first.",
        )

    attachment: Dict[str, Any] = {
        "kind":         "product_card",
        "id":           resolution.id,
        "title":        resolution.title,
        "media_type":   "image",
        "file_url":     resolution.image_url or "",
        "caption":      format_product_card_caption(resolution),
        "product_url":  resolution.product_url or "",
        "price":        resolution.price,
        "in_stock":     resolution.in_stock,
        "external_id":  resolution.external_id,
        "confidence":   resolution.confidence,
    }
    retailer_id = effective_retailer_id(attachment)
    elig = is_catalog_eligible(conn, products=[attachment])

    # ── 3. Build audit + run sender chain ───────────────────────
    audit_doc = new_delivery_audit()
    audit_doc["text_sent"] = True  # mirrors webhook — admin tests outbound only
    catalog_block: Dict[str, Any] = {
        "eligible":  elig.ok,
        "reason":    elig.reason,
        "attempted": False,
        "succeeded": False,
        "raw_error": None,
    }
    image_cta_block: Dict[str, Any] = {
        "attempted": False, "image_ok": False, "cta_ok": False,
    }
    cta_only_block: Dict[str, Any] = {"attempted": False, "ok": False}

    async def _try_image_cta() -> None:
        if not attachment.get("file_url"):
            return
        image_cta_block["attempted"] = True
        try:
            image_cta_block["image_ok"] = await _send_media_message(
                phone_id=phone_id, to=to,
                media_type="image",
                media_url=attachment["file_url"],
                filename=None,
                caption=attachment.get("caption"),
                _tenant_id=body.tenant_id, _db=db,
            )
            if image_cta_block["image_ok"]:
                audit_doc["legacy_media_sent_count"] = 1
        except Exception as exc:  # noqa: BLE001
            image_cta_block["image_ok"] = False
            image_cta_block["raw_error"] = repr(exc)
        if image_cta_block["image_ok"] and attachment.get("product_url"):
            try:
                image_cta_block["cta_ok"] = await _send_cta_url(
                    phone_id=phone_id, to=to,
                    body_text="اضغط زر «عرض المنتج» للمتابعة.",
                    btn_label="عرض المنتج",
                    btn_url=attachment["product_url"],
                    _tenant_id=body.tenant_id, _db=db,
                )
                if image_cta_block["cta_ok"]:
                    audit_doc["cta_url_sent_count"] = (
                        int(audit_doc.get("cta_url_sent_count", 0)) + 1
                    )
            except Exception as exc:  # noqa: BLE001
                image_cta_block["cta_ok"] = False
                image_cta_block["raw_error"] = repr(exc)

    async def _try_cta_only() -> None:
        if not attachment.get("product_url"):
            return
        cta_only_block["attempted"] = True
        try:
            cta_only_block["ok"] = await _send_cta_url(
                phone_id=phone_id, to=to,
                body_text=attachment.get("title") or "عرض المنتج",
                btn_label="عرض المنتج",
                btn_url=attachment["product_url"],
                _tenant_id=body.tenant_id, _db=db,
            )
            if cta_only_block["ok"]:
                audit_doc["cta_url_sent_count"] = (
                    int(audit_doc.get("cta_url_sent_count", 0)) + 1
                )
        except Exception as exc:  # noqa: BLE001
            cta_only_block["ok"] = False
            cta_only_block["raw_error"] = repr(exc)

    if requested_mode in ("auto", "catalog"):
        catalog_block["attempted"] = True
        try:
            catalog_block["succeeded"] = await _try_send_catalog_product(
                db=db, connection=conn,
                tenant_id=body.tenant_id,
                phone_id=phone_id, to=to,
                attachment=attachment,
            )
            if catalog_block["succeeded"]:
                audit_doc["catalog_card_sent_count"] = 1
        except Exception as exc:  # noqa: BLE001
            catalog_block["raw_error"] = repr(exc)
            catalog_block["succeeded"] = False

    if (
        requested_mode == "image"
        or (requested_mode == "auto" and not catalog_block["succeeded"])
    ):
        await _try_image_cta()

    if (
        requested_mode == "cta"
        or (
            requested_mode == "auto"
            and not catalog_block["succeeded"]
            and not image_cta_block.get("image_ok")
        )
    ):
        await _try_cta_only()

    final_mode = compute_final_delivery_mode(audit_doc)
    audit(
        "admin_debug_send_product",
        admin_sub=admin_sub,
        tenant_id=body.tenant_id,
        product_id=resolution.id,
        retailer_id=retailer_id or "",
        catalog_eligible=elig.ok,
        catalog_succeeded=catalog_block["succeeded"],
        image_ok=image_cta_block.get("image_ok"),
        cta_ok=image_cta_block.get("cta_ok") or cta_only_block.get("ok"),
        final_mode=final_mode,
    )
    logger.info(
        "[ADMIN/SEND_PRODUCT] admin=%s tenant=%s product=%s ext=%s "
        "retailer=%s mode_req=%s catalog_elig=%s catalog_ok=%s "
        "image_ok=%s cta_ok=%s final=%s",
        admin_sub, body.tenant_id, resolution.id, resolution.external_id,
        retailer_id, requested_mode, elig.ok, catalog_block["succeeded"],
        image_cta_block.get("image_ok"),
        image_cta_block.get("cta_ok") or cta_only_block.get("ok"),
        final_mode,
    )

    return {
        "ok": (
            catalog_block["succeeded"]
            or image_cta_block.get("image_ok")
            or cta_only_block.get("ok")
        ),
        "tenant_id":      body.tenant_id,
        "to_masked":      to_masked,
        "mode_requested": requested_mode,
        "product": {
            "id":          resolution.id,
            "title":       resolution.title,
            "external_id": resolution.external_id,
            "retailer_id": retailer_id or None,
            "image_url":   bool(resolution.image_url),
            "product_url": bool(resolution.product_url),
        },
        "catalog":      catalog_block,
        "image_cta":    image_cta_block,
        "cta_only":     cta_only_block,
        "final_mode":   final_mode,
    }
