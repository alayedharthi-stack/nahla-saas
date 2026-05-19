"""
routers/admin_webhook_security.py
─────────────────────────────────
Phase 1B operator surface for webhook signature verification telemetry +
per-tenant enforcement flips.

Routes
──────
* GET  ``/admin/webhooks/audit-summary``          — daily counts per
                                                    (provider, tenant, status)
* GET  ``/admin/webhooks/audit-summary/failures`` — recent invalid /
                                                    missing samples for human review
* POST ``/admin/webhooks/enforcement``            — flip a per-tenant
                                                    enforcement flag

All routes require ``role=admin`` via the existing ``require_admin``
dependency. None of these routes accept untrusted external traffic — the
read endpoints aggregate Redis counters; the write endpoint mutates
``TenantSettings.extra_metadata`` only after admin authentication.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import require_admin
from core.config import (
    META_WEBHOOK_ALLOW_MISSING_SIGNATURE,
    META_WEBHOOK_ENFORCE_SIGNATURE,
    SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE,
    SALLA_WEBHOOK_ENFORCE_SIGNATURE,
    WEBHOOK_REPLAY_PROTECTION_ENABLED,
    ZID_WEBHOOK_ENFORCE_SIGNATURE,
    ZID_WEBHOOK_REQUIRED_AT_BOOT,
)
from core.database import get_db
from core.webhook_audit import get_recent_failures, get_summary
from core.webhook_enforcement import set_tenant_enforcement

logger = logging.getLogger("nahla.admin.webhook_security")

router = APIRouter(tags=["Admin · Webhook Security"])

_ALLOWED_PROVIDERS = frozenset({"salla", "salla_oauth", "meta", "zid"})


@router.get("/admin/webhooks/audit-summary")
def webhook_audit_summary(
    provider: Optional[str] = Query(default=None,
                                    description="Filter by provider (salla, salla_oauth, meta, zid, ...)."),
    days: int = Query(default=7, ge=1, le=30,
                      description="Number of recent days to aggregate."),
    _admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Daily / per-tenant counts of webhook signature outcomes.

    Used by the Phase 1B operator dashboard to confirm a clean
    ``valid >= 99%`` audit window per merchant before flipping
    enforcement. Driven by ``core.webhook_audit`` Redis counters with
    in-process fallback when Redis is unavailable.
    """
    summary = get_summary(provider=provider, days=days)
    summary["env_flags"] = {
        "META_WEBHOOK_ENFORCE_SIGNATURE":       META_WEBHOOK_ENFORCE_SIGNATURE,
        "META_WEBHOOK_ALLOW_MISSING_SIGNATURE": META_WEBHOOK_ALLOW_MISSING_SIGNATURE,
        "SALLA_WEBHOOK_ENFORCE_SIGNATURE":      SALLA_WEBHOOK_ENFORCE_SIGNATURE,
        "SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE": SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE,
        "ZID_WEBHOOK_ENFORCE_SIGNATURE":        ZID_WEBHOOK_ENFORCE_SIGNATURE,
        "ZID_WEBHOOK_REQUIRED_AT_BOOT":         ZID_WEBHOOK_REQUIRED_AT_BOOT,
        "WEBHOOK_REPLAY_PROTECTION_ENABLED":    WEBHOOK_REPLAY_PROTECTION_ENABLED,
    }
    return summary


@router.get("/admin/webhooks/audit-summary/failures")
def webhook_audit_failures(
    provider: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Bounded ring of recent invalid / missing samples for triage.

    Each entry includes a 16-char prefix of the signature header so the
    operator can spot a typo / wrong rotation without ever seeing the
    full forgeable secret. Stores no PII beyond the IP / user-agent
    that the provider's edge already publishes.
    """
    rows = get_recent_failures(provider=provider, limit=limit)
    return {"items": rows, "count": len(rows)}


class _EnforcementUpdate(BaseModel):
    tenant_id: int = Field(..., gt=0)
    provider: str = Field(..., description="One of: salla, salla_oauth, meta, zid")
    enforce: bool


@router.post("/admin/webhooks/enforcement")
def update_webhook_enforcement(
    body: _EnforcementUpdate,
    _admin: Dict[str, Any] = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Flip a per-tenant webhook enforcement flag.

    Used during the Phase 1B per-merchant rollout: operator confirms the
    merchant's Partner Portal config + a clean audit window, then sets
    ``enforce=true`` for that tenant. Idempotent — flipping twice is a
    no-op semantically (only ``updated_at`` advances).
    """
    if body.provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {body.provider}")

    actor = (_admin.get("email") or _admin.get("sub") or _admin.get("user") or "admin")
    ok = set_tenant_enforcement(
        db,
        tenant_id=body.tenant_id,
        provider=body.provider,
        enable=body.enforce,
        actor=str(actor),
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to persist enforcement flip")

    return {
        "status": "ok",
        "tenant_id": body.tenant_id,
        "provider": body.provider,
        "enforce": body.enforce,
    }
