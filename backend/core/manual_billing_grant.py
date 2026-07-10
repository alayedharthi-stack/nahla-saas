"""
core/manual_billing_grant.py
────────────────────────────
Tenant-scoped manual gift billing grants (metadata-only, no migration).

Stored in ``TenantSettings.extra_metadata`` (DB column ``metadata``):

    metadata.billing.manual_gift_grant = {
        "enabled": true,
        "grant_type": "gift",
        "plan_slug": "starter",
        "starts_at": "...",
        "ends_at": "...",
        "reason": "...",
        "granted_by": "...",
        "granted_at": "...",
        "revoked_at": null,
        "revoked_by": null,
    }

Audit history: ``metadata.billing.manual_gift_grant_history`` (last 20 entries).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.billing")

GIFT_METADATA_KEY = "manual_gift_grant"
GIFT_HISTORY_KEY = "manual_gift_grant_history"
GIFT_GRANT_TYPE = "gift"
DEFAULT_GIFT_PLAN_SLUG = "starter"
ALLOWED_GIFT_PLAN_SLUGS = frozenset({"starter"})
HISTORY_MAX_ENTRIES = 20


class ManualGiftGrantError(Exception):
    def __init__(self, message: str, *, code: str = "grant_error") -> None:
        super().__init__(message)
        self.code = code


def _coerce_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return _coerce_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _read_grant_blob(db: Session, tenant_id: int) -> Optional[Dict[str, Any]]:
    from core.tenant import get_or_create_settings  # noqa: PLC0415

    settings = get_or_create_settings(db, tenant_id)
    meta = settings.extra_metadata or {}
    billing = meta.get("billing") or {}
    blob = billing.get(GIFT_METADATA_KEY)
    return blob if isinstance(blob, dict) else None


def get_manual_gift_grant(db: Session, tenant_id: int) -> Optional[Dict[str, Any]]:
    """Return the raw gift grant config dict, or None when absent."""
    return _read_grant_blob(db, tenant_id)


def has_active_paid_subscription(db: Session, tenant_id: int) -> bool:
    """True when tenant has an active paid Nahla or Salla subscription."""
    from core.billing import get_tenant_subscription  # noqa: PLC0415

    if get_tenant_subscription(db, tenant_id):
        return True

    from models import Integration  # noqa: PLC0415

    integration = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .first()
    )
    if not integration:
        return False

    status = (integration.config or {}).get("billing_status", "none")
    return status == "active"


def is_manual_gift_grant_active(db: Session, tenant_id: int) -> bool:
    """True when tenant has an enabled, unexpired, unrevoked manual gift grant."""
    blob = _read_grant_blob(db, tenant_id)
    if not blob or not blob.get("enabled"):
        return False

    if blob.get("revoked_at"):
        return False

    now = datetime.now(timezone.utc)

    starts_at = _parse_dt(blob.get("starts_at"))
    if starts_at and starts_at > now:
        return False

    ends_at = _parse_dt(blob.get("ends_at"))
    if not ends_at or ends_at <= now:
        return False

    plan_slug = str(blob.get("plan_slug") or DEFAULT_GIFT_PLAN_SLUG).strip().lower()
    return plan_slug in ALLOWED_GIFT_PLAN_SLUGS


def get_manual_gift_grant_plan_slug(db: Session, tenant_id: int) -> str:
    blob = _read_grant_blob(db, tenant_id) or {}
    slug = str(blob.get("plan_slug") or DEFAULT_GIFT_PLAN_SLUG).strip().lower()
    return slug if slug in ALLOWED_GIFT_PLAN_SLUGS else DEFAULT_GIFT_PLAN_SLUG


def manual_gift_grant_status(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Dashboard / lifecycle payload fragment for manual gift grants."""
    blob = _read_grant_blob(db, tenant_id)
    active = is_manual_gift_grant_active(db, tenant_id)
    return {
        "manual_gift_grant_active": active,
        "manual_gift_grant_headline_ar": (
            "باقة هدية مفعلة لهذا المتجر" if active else None
        ),
        "manual_gift_grant_reason": (
            str(blob.get("reason") or "") if active and blob else None
        ),
        "manual_gift_grant_plan_slug": (
            get_manual_gift_grant_plan_slug(db, tenant_id) if active else None
        ),
        "manual_gift_grant_ends_at": (
            blob.get("ends_at") if active and blob else None
        ),
        "manual_gift_grant_billing_status": "gift" if active else None,
    }


GRANT_BLOCKED_MESSAGES_AR: Dict[str, str] = {
    "active_paid_subscription": (
        "لا يمكن منح هدية لأن التاجر لديه اشتراك مدفوع نشط."
    ),
    "active_gift_exists": "يوجد هدية نشطة بالفعل؛ ألغِها أولاً.",
}


def build_admin_manual_gift_context(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Read-only billing + gift snapshot for the owner admin dashboard."""
    from models import BillingPlan, Integration, Tenant, User  # noqa: PLC0415

    from core.billing import compute_trial_info, get_tenant_subscription  # noqa: PLC0415
    from core.plan_entitlements import get_entitlements  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise ManualGiftGrantError(f"Tenant {tenant_id} not found", code="tenant_not_found")

    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.role == "merchant")
        .order_by(User.created_at.asc(), User.id.asc())
        .first()
    )

    salla_cfg: Dict[str, Any] = {}
    salla_int = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .order_by(Integration.id.desc())
        .first()
    )
    if salla_int and salla_int.config:
        salla_cfg = salla_int.config

    nahla_sub = get_tenant_subscription(db, tenant_id)
    nahla_plan_slug: Optional[str] = None
    if nahla_sub and nahla_sub.plan_id:
        plan_row = db.query(BillingPlan).filter(BillingPlan.id == nahla_sub.plan_id).first()
        nahla_plan_slug = plan_row.slug if plan_row else None

    entitlements = get_entitlements(db, tenant_id)
    trial_info = compute_trial_info(tenant)
    gift_blob = get_manual_gift_grant(db, tenant_id)
    gift_active = is_manual_gift_grant_active(db, tenant_id)
    gift_status = manual_gift_grant_status(db, tenant_id)

    grant_blocked_reason: Optional[str] = None
    can_grant = True
    if has_active_paid_subscription(db, tenant_id):
        can_grant = False
        grant_blocked_reason = "active_paid_subscription"
    elif gift_active:
        can_grant = False
        grant_blocked_reason = "active_gift_exists"

    return {
        "tenant_id": tenant_id,
        "store_name": tenant.name,
        "domain": tenant.domain,
        "owner_email": owner.email if owner else None,
        "owner_phone": getattr(owner, "phone", None) if owner else None,
        "entitlements": {
            "plan_slug": entitlements.plan_slug,
            "billing_status": entitlements.billing_status,
            "is_active": entitlements.is_active,
        },
        "trial": {
            "is_trial": trial_info.get("is_trial", False),
            "trial_expired": trial_info.get("trial_expired", False),
            "trial_ends_at": trial_info.get("trial_end"),
            "trial_days_remaining": trial_info.get("trial_days_remaining", 0),
        },
        "salla": {
            "billing_status": salla_cfg.get("billing_status"),
            "plan_slug": salla_cfg.get("salla_plan_slug"),
        },
        "nahla_subscription": {
            "active": nahla_sub is not None,
            "status": nahla_sub.status if nahla_sub else None,
            "plan_slug": nahla_plan_slug,
            "ends_at": nahla_sub.ends_at.isoformat() if nahla_sub and nahla_sub.ends_at else None,
        },
        "gift": {
            "active": gift_active,
            "blob": gift_blob,
            **gift_status,
        },
        "can_grant": can_grant,
        "grant_blocked_reason": grant_blocked_reason,
        "grant_blocked_message_ar": (
            GRANT_BLOCKED_MESSAGES_AR.get(grant_blocked_reason or "", "")
            if grant_blocked_reason
            else None
        ),
    }


def log_manual_gift_grant(tenant_id: int, *, reason: str = "") -> None:
    logger.info(
        "[ManualGiftGrant] tenant_id=%s reason=%s",
        tenant_id,
        reason or "gift",
    )


def _validate_plan_slug(plan_slug: str) -> str:
    slug = (plan_slug or DEFAULT_GIFT_PLAN_SLUG).strip().lower()
    if slug not in ALLOWED_GIFT_PLAN_SLUGS:
        allowed = ", ".join(sorted(ALLOWED_GIFT_PLAN_SLUGS))
        raise ManualGiftGrantError(
            f"Unknown or unsupported plan slug {slug!r}. v1 allows: {allowed}",
            code="invalid_plan_slug",
        )
    return slug


def _append_history(
    billing: Dict[str, Any],
    entry: Dict[str, Any],
) -> None:
    history: List[Dict[str, Any]] = list(billing.get(GIFT_HISTORY_KEY) or [])
    history.append(entry)
    billing[GIFT_HISTORY_KEY] = history[-HISTORY_MAX_ENTRIES:]


def apply_manual_gift_grant(
    db: Session,
    tenant_id: int,
    *,
    days: int,
    plan_slug: str = DEFAULT_GIFT_PLAN_SLUG,
    reason: str,
    granted_by: str,
    force: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Create or replace a manual gift grant. Never touches subscription/payment rows."""
    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise ManualGiftGrantError(f"Tenant {tenant_id} not found", code="tenant_not_found")

    if days < 1 or days > 365:
        raise ManualGiftGrantError("days must be between 1 and 365", code="invalid_days")

    slug = _validate_plan_slug(plan_slug)
    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ManualGiftGrantError("reason is required", code="reason_required")

    granted_by_clean = (granted_by or "").strip()
    if not granted_by_clean:
        raise ManualGiftGrantError("granted_by is required", code="granted_by_required")

    if has_active_paid_subscription(db, tenant_id):
        raise ManualGiftGrantError(
            "Tenant has an active paid subscription; gift grant rejected",
            code="active_paid_subscription",
        )

    if is_manual_gift_grant_active(db, tenant_id) and not force:
        raise ManualGiftGrantError(
            "Active gift grant already exists; revoke first or pass --force",
            code="active_gift_exists",
        )

    now = datetime.now(timezone.utc)
    ends_at = (now + timedelta(days=days)).replace(microsecond=0)
    blob = {
        "enabled": True,
        "grant_type": GIFT_GRANT_TYPE,
        "plan_slug": slug,
        "starts_at": now.replace(microsecond=0).isoformat(),
        "ends_at": ends_at.isoformat(),
        "reason": reason_clean,
        "granted_by": granted_by_clean,
        "granted_at": now.replace(microsecond=0).isoformat(),
        "revoked_at": None,
        "revoked_by": None,
    }

    result = {
        "tenant_id": tenant_id,
        "action": "grant",
        "dry_run": dry_run,
        "plan_slug": slug,
        "starts_at": blob["starts_at"],
        "ends_at": blob["ends_at"],
        "reason": reason_clean,
        "granted_by": granted_by_clean,
    }

    if dry_run:
        return result

    from core.tenant import get_or_create_settings  # noqa: PLC0415

    settings = get_or_create_settings(db, tenant_id)
    meta = dict(settings.extra_metadata or {})
    billing = dict(meta.get("billing") or {})
    previous = billing.get(GIFT_METADATA_KEY)
    billing[GIFT_METADATA_KEY] = blob
    _append_history(
        billing,
        {
            "action": "grant",
            "at": now.isoformat(),
            "plan_slug": slug,
            "starts_at": blob["starts_at"],
            "ends_at": blob["ends_at"],
            "reason": reason_clean,
            "granted_by": granted_by_clean,
            "force": force,
            "previous": previous,
        },
    )
    meta["billing"] = billing
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()

    log_manual_gift_grant(tenant_id, reason=reason_clean)
    return result


def revoke_manual_gift_grant(
    db: Session,
    tenant_id: int,
    *,
    granted_by: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Revoke the current manual gift grant metadata blob."""
    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise ManualGiftGrantError(f"Tenant {tenant_id} not found", code="tenant_not_found")

    granted_by_clean = (granted_by or "").strip()
    if not granted_by_clean:
        raise ManualGiftGrantError("granted_by is required", code="granted_by_required")

    blob = _read_grant_blob(db, tenant_id)
    if not blob or not blob.get("enabled"):
        raise ManualGiftGrantError(
            "No active gift grant metadata to revoke",
            code="no_grant_to_revoke",
        )

    now = datetime.now(timezone.utc)
    result = {
        "tenant_id": tenant_id,
        "action": "revoke",
        "dry_run": dry_run,
        "revoked_by": granted_by_clean,
        "revoked_at": now.replace(microsecond=0).isoformat(),
    }

    if dry_run:
        return result

    from core.tenant import get_or_create_settings  # noqa: PLC0415

    settings = get_or_create_settings(db, tenant_id)
    meta = dict(settings.extra_metadata or {})
    billing = dict(meta.get("billing") or {})
    current = dict(billing.get(GIFT_METADATA_KEY) or {})
    current["enabled"] = False
    current["revoked_at"] = now.replace(microsecond=0).isoformat()
    current["revoked_by"] = granted_by_clean
    billing[GIFT_METADATA_KEY] = current
    _append_history(
        billing,
        {
            "action": "revoke",
            "at": now.isoformat(),
            "revoked_by": granted_by_clean,
            "grant_snapshot": dict(blob),
        },
    )
    meta["billing"] = billing
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()

    logger.info(
        "[ManualGiftGrant] revoked tenant_id=%s revoked_by=%s",
        tenant_id,
        granted_by_clean,
    )
    return result
