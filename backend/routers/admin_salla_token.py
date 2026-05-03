"""
routers/admin_salla_token.py
─────────────────────────────
Admin endpoint for monitoring Salla Easy Mode token health across all tenants.

GET /admin/salla/integrations/token-status

Returns per-tenant token status including expiry, last refresh, and failure history.
Gated by ENABLE_ADMIN_DEBUG=true (same guard as admin_debug.py).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.database import get_db

logger = logging.getLogger("nahla.admin_salla_token")

router = APIRouter(prefix="/admin/salla", tags=["admin-salla-token"])


def _require_enabled(secret: Optional[str]) -> None:
    """Gate behind ENABLE_ADMIN_DEBUG + optional shared secret."""
    flag = (os.getenv("ENABLE_ADMIN_DEBUG", "") or "").strip().lower()
    if flag != "true":
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail="Set ENABLE_ADMIN_DEBUG=true to use admin endpoints.",
        )
    expected = (os.getenv("ADMIN_DEBUG_SECRET") or "").strip()
    if expected:
        if not secret or secret.strip() != expected:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="invalid secret")


def _compute_days_until(expires_at_str: Optional[str]) -> Optional[float]:
    if not expires_at_str:
        return None
    try:
        _dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=timezone.utc)
        return (_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return None


def _build_token_row(intg) -> dict:
    cfg = intg.config or {}

    # Resolve expires_at (try both field names for backward compat)
    expires_at = cfg.get("expires_at") or cfg.get("token_expires_at")
    days_until_expiry = _compute_days_until(expires_at)

    # Resolve last refresh timestamp
    last_refresh = cfg.get("last_token_refresh_at") or cfg.get("last_token_refresh")

    easy_mode = bool(
        cfg.get("easy_mode")
        or (cfg.get("app_type") or "").lower() == "easy"
        or (cfg.get("api_key_source") or "").lower() == "easy_mode_webhook"
    )

    return {
        "tenant_id":            intg.tenant_id,
        "integration_id":       intg.id,
        "store_id":             cfg.get("store_id") or intg.external_store_id,
        "store_name":           cfg.get("store_name"),
        "enabled":              intg.enabled,
        "easy_mode":            easy_mode,
        "token_source":         cfg.get("token_source") or cfg.get("api_key_source"),
        "has_access_token":     bool(cfg.get("api_key")),
        "has_refresh_token":    bool(cfg.get("refresh_token")),
        "expires_at":           expires_at,
        "days_until_expiry":    round(days_until_expiry, 2) if days_until_expiry is not None else None,
        "expiry_health": (
            "unknown"        if days_until_expiry is None else
            "expired"        if days_until_expiry < 0 else
            "critical"       if days_until_expiry < 2 else
            "warning"        if days_until_expiry < 5 else
            "ok"
        ),
        "refresh_token_received_at": cfg.get("refresh_token_received_at"),
        "last_token_refresh_at":     last_refresh,
        "token_refresh_status":      cfg.get("token_refresh_status"),
        "token_refresh_error":       cfg.get("token_refresh_error"),
        "token_refresh_failed_at":   cfg.get("token_refresh_failed_at"),
        "token_refresh_attempts":        cfg.get("token_refresh_attempts", 0),
        "token_refresh_first_failed_at": cfg.get("token_refresh_first_failed_at"),
        "needs_reauth":                  bool(cfg.get("needs_reauth")),
        "needs_reauth_reason":           cfg.get("needs_reauth_reason"),
        "reauth_reason":                 cfg.get("needs_reauth_reason"),  # friendly alias
        "token_reauth_alert_sent_at":    cfg.get("token_reauth_alert_sent_at"),
        "no_auto_refresh":               bool(cfg.get("no_auto_refresh")),
        "connected_at":                  cfg.get("connected_at"),
    }


@router.get("/integrations/token-status")
def salla_token_status(
    tenant_id: Optional[int] = Query(None, description="Filter by tenant (omit for all tenants)"),
    enabled_only: bool = Query(False, description="Only show enabled integrations"),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Token health dashboard for all (or one) Salla Easy Mode integrations.

    For each integration returns:
      tenant_id, integration_id, store_id, store_name,
      enabled, easy_mode, token_source,
      has_access_token, has_refresh_token,
      expires_at, days_until_expiry, expiry_health (ok/warning/critical/expired/unknown),
      refresh_token_received_at, last_token_refresh_at,
      token_refresh_status, token_refresh_error, token_refresh_failed_at,
      token_refresh_attempts, token_refresh_first_failed_at,
      needs_reauth, needs_reauth_reason, reauth_reason,
      token_reauth_alert_sent_at, no_auto_refresh, connected_at

    expiry_health values:
      ok        → expires in 5+ days
      warning   → expires in 2–5 days
      critical  → expires in < 2 days
      expired   → already expired
      unknown   → no expiry info stored yet
    """
    _require_enabled(secret)

    from models import Integration  # noqa: PLC0415

    q = db.query(Integration).filter(Integration.provider == "salla")
    if tenant_id is not None:
        q = q.filter(Integration.tenant_id == tenant_id)
    if enabled_only:
        q = q.filter(Integration.enabled.is_(True))
    integrations = q.order_by(Integration.tenant_id.asc(), Integration.id.asc()).all()

    rows = [_build_token_row(i) for i in integrations]

    # Aggregate stats
    total       = len(rows)
    ok_count    = sum(1 for r in rows if r["expiry_health"] == "ok")
    warning     = sum(1 for r in rows if r["expiry_health"] == "warning")
    critical    = sum(1 for r in rows if r["expiry_health"] == "critical")
    expired     = sum(1 for r in rows if r["expiry_health"] == "expired")
    unknown     = sum(1 for r in rows if r["expiry_health"] == "unknown")
    needs_reauth_count = sum(1 for r in rows if r["needs_reauth"])
    failed_refresh     = sum(1 for r in rows if r["token_refresh_status"] == "failed")
    no_refresh_token   = sum(1 for r in rows if not r["has_refresh_token"])

    logger.info(
        "[admin] salla token-status called | total=%d ok=%d warning=%d critical=%d "
        "expired=%d needs_reauth=%d failed_refresh=%d",
        total, ok_count, warning, critical, expired, needs_reauth_count, failed_refresh,
    )

    return {
        "ok":      True,
        "summary": {
            "total":             total,
            "expiry_ok":         ok_count,
            "expiry_warning":    warning,
            "expiry_critical":   critical,
            "expiry_expired":    expired,
            "expiry_unknown":    unknown,
            "needs_reauth":      needs_reauth_count,
            "failed_last_refresh": failed_refresh,
            "no_refresh_token":  no_refresh_token,
        },
        "integrations": rows,
        "hint": (
            "Merchants in 'critical' or 'expired' state will lose API access soon. "
            "The daily scheduler will attempt to refresh tokens automatically. "
            "If token_refresh_status='failed' persists, check Railway logs for "
            "[SALLA TOKEN] entries. needs_reauth=true requires the merchant to "
            "reinstall the Nahla app from the Salla App Store."
        ),
    }
