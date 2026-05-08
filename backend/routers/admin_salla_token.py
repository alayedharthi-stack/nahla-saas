"""
routers/admin_salla_token.py
─────────────────────────────
Admin endpoints for monitoring & repairing Salla Easy Mode token health.

Endpoints
─────────
GET  /admin/salla/integrations/token-status
       Aggregate token health across all tenants (or one).

GET  /admin/salla/diagnose/{tenant_id}
       Deep diagnosis: every Salla integration record for a tenant + per-store
       sibling grouping, used to spot orphans / superseded records driving
       alerts.

POST /admin/salla/integrations/{integration_id}/refresh
       Manually trigger a Salla token refresh for a single record. Returns the
       cfg state before + after + Salla's HTTP response so ops can verify the
       refresh-attempts counter, first_failure_at, last_error and needs_reauth
       all behave correctly without waiting for the scheduler.

All endpoints are gated by ENABLE_ADMIN_DEBUG=true (same guard as admin_debug.py).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
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
        "app_type":             cfg.get("app_type"),
        "token_source":         cfg.get("token_source") or cfg.get("api_key_source"),
        "has_access_token":     bool(cfg.get("api_key")),
        "has_refresh_token":    bool(cfg.get("refresh_token")),
        "created_at":           getattr(intg, "created_at", None).isoformat() if getattr(intg, "created_at", None) else None,
        "updated_at":           getattr(intg, "updated_at", None).isoformat() if getattr(intg, "updated_at", None) else None,
        "expires_at":           expires_at,
        "token_expires_at":     expires_at,
        "days_until_expiry":    round(days_until_expiry, 2) if days_until_expiry is not None else None,
        "expiry_health": (
            "unknown"        if days_until_expiry is None else
            "expired"        if days_until_expiry < 0 else
            "critical"       if days_until_expiry < 2 else
            "warning"        if days_until_expiry < 5 else
            "ok"
        ),
        "refresh_token_received_at":     cfg.get("refresh_token_received_at"),
        "last_successful_refresh":       last_refresh,
        "last_token_refresh_at":         last_refresh,
        "last_failed_refresh":           cfg.get("token_refresh_failed_at"),
        "token_refresh_status":          cfg.get("token_refresh_status"),
        "token_refresh_error":           cfg.get("token_refresh_error"),
        "token_refresh_failed_at":       cfg.get("token_refresh_failed_at"),
        "first_failure_at":              cfg.get("token_refresh_first_failed_at"),
        "token_refresh_attempts":        int(cfg.get("token_refresh_attempts", 0) or 0),
        "refresh_attempts":              int(cfg.get("token_refresh_attempts", 0) or 0),
        "token_refresh_first_failed_at": cfg.get("token_refresh_first_failed_at"),
        "needs_reauth":                  bool(cfg.get("needs_reauth")),
        "needs_reauth_reason":           cfg.get("needs_reauth_reason"),
        "needs_reauth_at":               cfg.get("needs_reauth_at"),
        "reauth_reason":                 cfg.get("needs_reauth_reason"),  # friendly alias
        "token_reauth_alert_sent_at":    cfg.get("token_reauth_alert_sent_at"),
        "alert_suppressed":              bool(cfg.get("alert_suppressed")),
        "alert_suppressed_reason":       cfg.get("alert_suppressed_reason"),
        "alert_suppressed_by":           cfg.get("alert_suppressed_by_integration_id"),
        "superseded":                    bool(cfg.get("superseded")),
        "superseded_by_integration_id":  cfg.get("superseded_by_integration_id"),
        "superseded_at":                 cfg.get("superseded_at"),
        "no_auto_refresh":               bool(cfg.get("no_auto_refresh")),
        "no_auto_refresh_reason":        cfg.get("no_auto_refresh_reason"),
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


# ── Deep diagnostic endpoint ──────────────────────────────────────────────────

@router.get("/diagnose/{tenant_id}")
def salla_diagnose_tenant(
    tenant_id: int = Path(..., ge=1),
    secret: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Deep Salla integration diagnostic for a single tenant.

    Returns:
      • ``selected``    — the row Nahla currently treats as canonical
                          (highest id with api_key + enabled + not needs_reauth,
                          falling back to highest-id row).
      • ``all``         — every Salla integration row for this tenant.
      • ``store_groups``— rows grouped by ``store_id`` so duplicate / shadow
                          records are obvious at a glance.
      • per-row fields: token_source, app_type, created_at, updated_at,
                        token_expires_at, last_successful_refresh,
                        last_failed_refresh, first_failure_at,
                        refresh_attempts, last_error, needs_reauth,
                        superseded, superseded_by, alert_suppressed.

    Use this when an alert email shows a confusing refresh_attempts/last_error
    combination — it surfaces whether the alert points at an orphan record
    that has been superseded by a newer reinstall.
    """
    _require_enabled(secret)

    from models import Integration  # noqa: PLC0415

    integrations = (
        db.query(Integration)
        .filter(Integration.provider == "salla", Integration.tenant_id == tenant_id)
        .order_by(Integration.id.asc())
        .all()
    )
    rows = [_build_token_row(i) for i in integrations]

    # Pick canonical "selected" row using the same heuristic the registry uses:
    # newest enabled non-needs_reauth row with an api_key wins.
    def _is_healthy(r: dict) -> bool:
        return bool(r["enabled"] and r["has_access_token"] and not r["needs_reauth"])

    healthy = [r for r in rows if _is_healthy(r)]
    if healthy:
        selected = max(healthy, key=lambda r: r["integration_id"])
    elif rows:
        selected = max(rows, key=lambda r: r["integration_id"])
    else:
        selected = None

    # Group by store_id to expose orphan / shadow records.
    store_groups: Dict[str, List[dict]] = {}
    for r in rows:
        sid = str(r.get("store_id") or "unknown")
        store_groups.setdefault(sid, []).append(r)

    # Annotate each row with a "shadow" flag: this row is for a store that
    # also has a newer healthy sibling.
    for sid, members in store_groups.items():
        healthy_ids = [m["integration_id"] for m in members if _is_healthy(m)]
        newest_healthy = max(healthy_ids) if healthy_ids else None
        for m in members:
            m["shadow"] = bool(newest_healthy and m["integration_id"] < newest_healthy)
            m["newest_healthy_sibling_id"] = newest_healthy

    logger.info(
        "[admin] salla diagnose tenant=%s rows=%d selected_id=%s",
        tenant_id, len(rows), (selected or {}).get("integration_id"),
    )

    return {
        "ok":             True,
        "tenant_id":      tenant_id,
        "selected":       selected,
        "all":            rows,
        "store_groups":   store_groups,
        "summary": {
            "total":               len(rows),
            "stores":              len(store_groups),
            "duplicate_stores":    sum(1 for v in store_groups.values() if len(v) > 1),
            "needs_reauth":        sum(1 for r in rows if r["needs_reauth"]),
            "superseded":          sum(1 for r in rows if r.get("superseded")),
            "alert_suppressed":    sum(1 for r in rows if r.get("alert_suppressed")),
        },
    }


# ── Manual force-refresh endpoint ─────────────────────────────────────────────

@router.post("/integrations/{integration_id}/refresh")
async def salla_force_refresh_integration(
    integration_id: int = Path(..., ge=1),
    secret: Optional[str] = Query(None),
    dry_run: bool = Query(False, description="If true, return Salla response WITHOUT mutating DB"),
    db: Session = Depends(get_db),
):
    """Manually trigger a Salla token refresh for one integration.

    Returns ``{ before, salla_response, after }`` so ops can verify that
    ``token_refresh_attempts``, ``token_refresh_first_failed_at``,
    ``token_refresh_error`` and ``needs_reauth`` are updated correctly
    without waiting for the daily scheduler.

    Behaviour exactly mirrors the scheduler's per-integration block, including
    the superseded-integration check and ``stamp_refresh_failure`` invariants.
    """
    _require_enabled(secret)

    from models import Integration  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    intg = db.query(Integration).filter(Integration.id == integration_id).first()
    if not intg or intg.provider != "salla":
        raise HTTPException(status_code=404, detail="salla integration not found")

    cfg_before    = dict(intg.config or {})
    refresh_token = cfg_before.get("refresh_token") or ""
    store_id      = cfg_before.get("store_id") or intg.external_store_id or "?"
    client_id     = os.environ.get("SALLA_CLIENT_ID", "")
    client_secret = os.environ.get("SALLA_CLIENT_SECRET", "")

    if not refresh_token:
        return {
            "ok":     False,
            "reason": "no_refresh_token",
            "before": _build_token_row(intg),
        }
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="SALLA_CLIENT_ID/SECRET not configured")

    now = datetime.now(timezone.utc)
    salla_status: Optional[int] = None
    salla_text: str = ""
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                },
                headers={
                    "Accept":       "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            salla_status = resp.status_code
            salla_text   = resp.text[:600]
    except Exception as exc:
        return {
            "ok":     False,
            "reason": "salla_unreachable",
            "error":  str(exc)[:300],
            "before": _build_token_row(intg),
        }

    salla_payload: Any = salla_text
    try:
        salla_payload = json.loads(salla_text) if salla_text else None
    except Exception:
        pass

    response_summary: Dict[str, Any] = {
        "status": salla_status,
        "body":   salla_payload,
    }

    if dry_run:
        return {
            "ok":              True,
            "dry_run":         True,
            "salla_response":  response_summary,
            "before":          _build_token_row(intg),
            "note":            "dry_run=true — DB was not modified",
        }

    from core.salla_token_alerts import (  # noqa: PLC0415
        stamp_refresh_failure,
        find_superseding_integration,
        mark_superseded,
        log_metric_success,
        log_metric_failed,
        log_metric_needs_reauth,
        maybe_send_reauth_alert,
        should_escalate_to_needs_reauth,
    )

    cfg = dict(intg.config or {})

    if salla_status == 200 and isinstance(salla_payload, dict):
        new_access  = salla_payload.get("access_token") or ""
        new_refresh = salla_payload.get("refresh_token") or refresh_token
        exp_in      = salla_payload.get("expires_in")
        if new_access:
            cfg["api_key"]                 = new_access
            cfg["refresh_token"]           = new_refresh
            cfg["last_token_refresh"]      = now.isoformat()
            cfg["last_token_refresh_at"]   = now.isoformat()
            cfg["token_refresh_status"]    = "success"
            cfg["token_refresh_attempts"]  = 0
            for k in (
                "token_refresh_error", "token_refresh_failed_at",
                "token_refresh_first_failed_at",
                "needs_reauth", "needs_reauth_at", "needs_reauth_reason",
                "token_reauth_alert_sent_at",
                "no_auto_refresh", "no_auto_refresh_reason", "no_auto_refresh_at",
                "alert_suppressed", "alert_suppressed_reason",
                "alert_suppressed_by_integration_id", "alert_suppressed_at",
            ):
                cfg.pop(k, None)
            if exp_in:
                try:
                    from datetime import timedelta  # noqa: PLC0415
                    new_exp = (now + timedelta(seconds=int(exp_in))).isoformat()
                    cfg["expires_at"]       = new_exp
                    cfg["token_expires_at"] = new_exp
                except Exception:
                    pass
            intg.config = cfg
            db.commit()
            log_metric_success(intg.tenant_id, str(store_id))
            db.refresh(intg)
            return {
                "ok":             True,
                "outcome":        "refreshed",
                "salla_response": response_summary,
                "before":         _build_token_row_from_cfg(intg, cfg_before),
                "after":          _build_token_row(intg),
            }

    # Failure path — stamp counters consistently.
    if salla_status == 400 and "invalid_grant" in (salla_text or ""):
        stamp_refresh_failure(cfg, error="invalid_grant", now=now)
        cfg.pop("refresh_token", None)
        cfg["no_auto_refresh"]        = True
        cfg["no_auto_refresh_reason"] = "invalid_grant"
        cfg["no_auto_refresh_at"]     = now.isoformat()

        superseder = find_superseding_integration(db, intg)
        if superseder is not None:
            mark_superseded(cfg, by_integration_id=superseder.id, now=now)
            cfg.pop("needs_reauth",        None)
            cfg.pop("needs_reauth_reason", None)
            cfg.pop("needs_reauth_at",     None)
            intg.config  = cfg
            intg.enabled = False
            db.commit()
            log_metric_failed(intg.tenant_id, str(store_id), int(cfg["token_refresh_attempts"]))
            db.refresh(intg)
            return {
                "ok":             True,
                "outcome":        "superseded_invalid_grant",
                "superseded_by":  superseder.id,
                "salla_response": response_summary,
                "before":         _build_token_row_from_cfg(intg, cfg_before),
                "after":          _build_token_row(intg),
            }

        cfg["needs_reauth"]        = True
        cfg["needs_reauth_reason"] = "invalid_grant"
        cfg["needs_reauth_at"]     = now.isoformat()
        intg.config = cfg
        db.commit()
        log_metric_failed(intg.tenant_id, str(store_id), int(cfg["token_refresh_attempts"]))
        log_metric_needs_reauth(intg.tenant_id, str(store_id), "invalid_grant")
        await maybe_send_reauth_alert(
            tenant_id=intg.tenant_id,
            integration_id=intg.id,
            cfg=cfg,
            now=now,
        )
        intg.config = cfg
        db.commit()
        db.refresh(intg)
        return {
            "ok":             True,
            "outcome":        "invalid_grant_needs_reauth",
            "salla_response": response_summary,
            "before":         _build_token_row_from_cfg(intg, cfg_before),
            "after":          _build_token_row(intg),
        }

    # Transient failure
    stamp_refresh_failure(cfg, error=f"HTTP {salla_status}: {salla_text[:200]}", now=now)
    intg.config = cfg
    db.commit()
    log_metric_failed(intg.tenant_id, str(store_id), int(cfg["token_refresh_attempts"]))

    escalate, reason = should_escalate_to_needs_reauth(cfg, now)
    if escalate and not cfg.get("needs_reauth"):
        superseder = find_superseding_integration(db, intg)
        cfg["needs_reauth"]        = True
        cfg["needs_reauth_reason"] = reason
        cfg["needs_reauth_at"]     = now.isoformat()
        intg.config = cfg
        db.commit()
        await maybe_send_reauth_alert(
            tenant_id=intg.tenant_id,
            integration_id=intg.id,
            cfg=cfg,
            now=now,
            superseded_by=superseder.id if superseder else None,
        )
        intg.config = cfg
        db.commit()
    db.refresh(intg)
    return {
        "ok":             True,
        "outcome":        "transient_failure",
        "salla_response": response_summary,
        "before":         _build_token_row_from_cfg(intg, cfg_before),
        "after":          _build_token_row(intg),
    }


def _build_token_row_from_cfg(intg, cfg: dict) -> dict:
    """Build a token-status row using a *snapshot* cfg (e.g. pre-refresh state).

    We can't mutate ``intg.config`` to the snapshot, so this helper threads the
    snapshot through ``_build_token_row`` by temporarily attaching it.
    """
    real_cfg = intg.config
    try:
        intg.config = cfg
        return _build_token_row(intg)
    finally:
        intg.config = real_cfg
