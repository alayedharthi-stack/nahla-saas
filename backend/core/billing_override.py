"""
core/billing_override.py
────────────────────────
Temporary, tenant-scoped full-feature access for Salla partner testing.

Stored in ``TenantSettings.extra_metadata`` (DB column ``metadata``) — no migration:

    metadata.billing.partner_testing_override = {
        "enabled": true,
        "reason": "salla_partner_testing",
        "plan_slug": "scale",
        "expires_at": "2026-08-01T23:59:59+00:00",
        "granted_at": "...",
        "granted_by": "ops"
    }

Only ``PARTNER_TESTING_TENANT_ID`` (1) is honoured — prevents accidental
activation on other merchants even if metadata is mis-set.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.billing")

PARTNER_TESTING_TENANT_ID = 1
PARTNER_TESTING_REASON = "salla_partner_testing"
DEFAULT_OVERRIDE_PLAN_SLUG = "scale"
OVERRIDE_METADATA_KEY = "partner_testing_override"


def _coerce_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_expires_at(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return _coerce_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _read_override_blob(db: Session, tenant_id: int) -> Optional[Dict[str, Any]]:
    if tenant_id != PARTNER_TESTING_TENANT_ID:
        return None

    from core.tenant import get_or_create_settings  # noqa: PLC0415

    settings = get_or_create_settings(db, tenant_id)
    meta = settings.extra_metadata or {}
    billing = meta.get("billing") or {}
    blob = billing.get(OVERRIDE_METADATA_KEY)
    return blob if isinstance(blob, dict) else None


def get_partner_testing_override(db: Session, tenant_id: int) -> Optional[Dict[str, Any]]:
    """Return the raw override config dict, or None when absent / wrong tenant."""
    return _read_override_blob(db, tenant_id)


def is_partner_testing_override_active(db: Session, tenant_id: int) -> bool:
    """True when tenant 1 has an enabled, unexpired partner-testing override."""
    blob = _read_override_blob(db, tenant_id)
    if not blob or not blob.get("enabled"):
        return False

    expires_at = _parse_expires_at(blob.get("expires_at"))
    if expires_at and expires_at <= datetime.now(timezone.utc):
        return False

    return True


def get_partner_testing_override_plan_slug(db: Session, tenant_id: int) -> str:
    blob = _read_override_blob(db, tenant_id) or {}
    slug = str(blob.get("plan_slug") or DEFAULT_OVERRIDE_PLAN_SLUG).strip().lower()
    return slug or DEFAULT_OVERRIDE_PLAN_SLUG


def partner_testing_override_status(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Dashboard / lifecycle payload fragment for partner testing override."""
    blob = _read_override_blob(db, tenant_id)
    active = is_partner_testing_override_active(db, tenant_id)
    return {
        "partner_testing_override_active": active,
        "partner_testing_override_headline_ar": (
            "وضع اختبار سلة مفعل لهذا المتجر" if active else None
        ),
        "partner_testing_override_reason": (
            str(blob.get("reason") or PARTNER_TESTING_REASON) if active else None
        ),
        "partner_testing_override_expires_at": (
            blob.get("expires_at") if active and blob else None
        ),
        "partner_testing_override_plan_slug": (
            get_partner_testing_override_plan_slug(db, tenant_id) if active else None
        ),
    }


def log_billing_override_grant(tenant_id: int, *, reason: str = PARTNER_TESTING_REASON) -> None:
    logger.info(
        "[BillingOverride] tenant_id=%s reason=%s",
        tenant_id,
        reason,
    )
