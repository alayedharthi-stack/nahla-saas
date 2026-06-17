"""
core/trial_lifecycle.py
───────────────────────
Free-trial lifecycle: trial starts only after WhatsApp connects.

Duration sources (platform-wide constants — not per-plan):
  • Free trial window: ``core.billing.FREE_TRIAL_DAYS`` (14 days today)
  • Paid subscription period: ``SUBSCRIPTION_PERIOD_DAYS`` below (30 days monthly)
  • Salla App Store trials use a separate path in ``salla_subscription.py``

Registration / app open  → trial_pending_whatsapp (no countdown)
WhatsApp connected       → trial_active (FREE_TRIAL_DAYS window starts once)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.billing import FREE_TRIAL_DAYS, _coerce_utc, get_tenant_subscription

logger = logging.getLogger("nahla.trial_lifecycle")

TRIAL_STATUS_PENDING_WHATSAPP = "trial_pending_whatsapp"
TRIAL_STATUS_ACTIVE = "trial_active"
TRIAL_STATUS_EXPIRED = "trial_expired"

SUBSCRIPTION_PERIOD_DAYS = 30


def init_new_tenant_trial_state(tenant) -> None:
    """Apply to every newly created tenant — trial must not start yet."""
    tenant.subscription_status = TRIAL_STATUS_PENDING_WHATSAPP
    tenant.trial_started_at = None
    tenant.trial_ends_at = None
    tenant.first_whatsapp_connected_at = None


def _tenant_has_connected_whatsapp(db: Session, tenant_id: int) -> bool:
    from models import WhatsAppConnection  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if not conn:
        return False
    return conn.status == "connected" and bool(conn.phone_number_id)


def _first_whatsapp_connection_at(db: Session, tenant_id: int) -> Optional[datetime]:
    from models import WhatsAppConnection  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if not conn or conn.status != "connected":
        return None
    for candidate in (
        getattr(conn, "whatsapp_ai_live_since", None),
        conn.connected_at,
    ):
        coerced = _coerce_utc(candidate)
        if coerced:
            return coerced
    return None


def start_trial_on_whatsapp_connect(
    db: Session,
    tenant_id: int,
    *,
    connected_at: Optional[datetime] = None,
) -> bool:
    """
    Start the free trial once, on first successful WhatsApp connection.

    Returns True when trial was started by this call, False when idempotent skip.
    """
    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return False

    now = _coerce_utc(connected_at) or datetime.now(timezone.utc)

    if not tenant.first_whatsapp_connected_at:
        tenant.first_whatsapp_connected_at = now.replace(tzinfo=None)

    if tenant.trial_started_at is not None:
        logger.info(
            "[TrialLifecycle] skip start — trial already started tenant=%s started_at=%s",
            tenant_id,
            tenant.trial_started_at,
        )
        return False

    if get_tenant_subscription(db, tenant_id):
        logger.info(
            "[TrialLifecycle] skip start — active paid subscription tenant=%s",
            tenant_id,
        )
        return False

    tenant.trial_started_at = now.replace(tzinfo=None)
    tenant.trial_ends_at = (now + timedelta(days=FREE_TRIAL_DAYS)).replace(tzinfo=None)
    tenant.subscription_status = TRIAL_STATUS_ACTIVE
    db.commit()

    logger.info(
        "[TrialLifecycle] trial started tenant=%s ends_at=%s",
        tenant_id,
        tenant.trial_ends_at,
    )
    return True


def _warning_level(days_remaining: int, *, expired: bool) -> str:
    if expired:
        return "expired"
    if days_remaining <= 1:
        return "1d"
    if days_remaining <= 3:
        return "3d"
    if days_remaining <= 7:
        return "7d"
    return "none"


def _status_reason_ar(
    *,
    pending_whatsapp: bool,
    is_trial: bool,
    trial_expired: bool,
    has_subscription: bool,
    subscription_expired: bool,
) -> str:
    if has_subscription and not subscription_expired:
        return "اشتراك مدفوع نشط"
    if subscription_expired:
        return "انتهى الاشتراك المدفوع — يرجى التجديد"
    if pending_whatsapp:
        return "التجربة المجانية لم تبدأ بعد — اربط واتساب لبدء التجربة"
    if is_trial:
        return "تجربة مجانية نشطة"
    if trial_expired:
        return "انتهت التجربة المجانية — يرجى الاشتراك"
    return "لا يوجد اشتراك نشط"


def compute_trial_info(tenant) -> dict:
    """
    Unified trial computation.

    Trial never falls back to tenant.created_at. Without trial_started_at the
    merchant is in trial_pending_whatsapp (or expired if they had a trial that ended).
    """
    now = datetime.now(timezone.utc)
    status = getattr(tenant, "subscription_status", None) or ""

    trial_started = _coerce_utc(getattr(tenant, "trial_started_at", None))
    trial_end = _coerce_utc(getattr(tenant, "trial_ends_at", None))

    if trial_started and trial_end is None:
        trial_end = trial_started + timedelta(days=FREE_TRIAL_DAYS)

    pending_whatsapp = trial_started is None and status in (
        TRIAL_STATUS_PENDING_WHATSAPP,
        None,
        "",
    )

    if trial_started is None and status not in (TRIAL_STATUS_EXPIRED,):
        return {
            "is_trial": False,
            "trial_pending_whatsapp": True,
            "trial_days_remaining": 0,
            "trial_expired": False,
            "trial_end": None,
            "trial_started_at": None,
            "status": TRIAL_STATUS_PENDING_WHATSAPP,
            "status_reason_ar": _status_reason_ar(
                pending_whatsapp=True,
                is_trial=False,
                trial_expired=False,
                has_subscription=False,
                subscription_expired=False,
            ),
            "warning_level": "none",
        }

    if trial_end is None:
        return {
            "is_trial": False,
            "trial_pending_whatsapp": False,
            "trial_days_remaining": 0,
            "trial_expired": True,
            "trial_end": None,
            "trial_started_at": trial_started.isoformat() if trial_started else None,
            "status": TRIAL_STATUS_EXPIRED,
            "status_reason_ar": _status_reason_ar(
                pending_whatsapp=False,
                is_trial=False,
                trial_expired=True,
                has_subscription=False,
                subscription_expired=False,
            ),
            "warning_level": "expired",
        }

    remaining = (trial_end - now).total_seconds()
    days_left = max(0, int(remaining / 86400) + (1 if remaining > 0 else 0))
    is_active = remaining > 0
    expired = remaining <= 0

    effective_status = TRIAL_STATUS_ACTIVE if is_active else TRIAL_STATUS_EXPIRED

    return {
        "is_trial": is_active,
        "trial_pending_whatsapp": False,
        "trial_days_remaining": days_left,
        "trial_expired": expired,
        "trial_end": trial_end.isoformat(),
        "trial_started_at": trial_started.isoformat() if trial_started else None,
        "status": effective_status,
        "status_reason_ar": _status_reason_ar(
            pending_whatsapp=False,
            is_trial=is_active,
            trial_expired=expired,
            has_subscription=False,
            subscription_expired=False,
        ),
        "warning_level": _warning_level(days_left, expired=expired),
    }


def subscription_period_end(started_at: datetime) -> datetime:
    start = _coerce_utc(started_at) or datetime.now(timezone.utc)
    return start + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)


def ensure_subscription_ends_at(sub) -> None:
    """Backfill ends_at on active subs that were activated without an expiry."""
    if sub is None or sub.status != "active":
        return
    if sub.ends_at:
        return
    paid_at = None
    meta = sub.extra_metadata or {}
    raw_paid = meta.get("paid_at")
    if raw_paid:
        try:
            paid_at = datetime.fromisoformat(str(raw_paid))
        except (TypeError, ValueError):
            paid_at = None
    anchor = _coerce_utc(paid_at) or _coerce_utc(sub.started_at) or datetime.now(timezone.utc)
    sub.ends_at = subscription_period_end(anchor).replace(tzinfo=None)


def audit_tenant_subscription(db: Session, tenant_id: int) -> Dict[str, Any]:
    """
    Read-only audit snapshot for a tenant's billing / trial state.
    Used for operator review (e.g. tenant 33) and regression tests.
    """
    from models import Tenant, WhatsAppConnection, BillingSubscription  # noqa: PLC0415
    from core.billing import has_billing_access  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return {"tenant_id": tenant_id, "found": False}

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    sub = (
        db.query(BillingSubscription)
        .filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status == "active",
        )
        .order_by(BillingSubscription.started_at.desc())
        .first()
    )
    effective_sub = get_tenant_subscription(db, tenant_id)
    trial = compute_trial_info(tenant)

    wa_connected = bool(conn and conn.status == "connected" and conn.phone_number_id)
    first_wa = _coerce_utc(getattr(tenant, "first_whatsapp_connected_at", None))
    if not first_wa and conn:
        first_wa = _coerce_utc(getattr(conn, "whatsapp_ai_live_since", None)) or _coerce_utc(
            conn.connected_at
        )

    sub_started = _coerce_utc(sub.started_at) if sub and sub.started_at else None
    sub_ends = _coerce_utc(sub.ends_at) if sub and sub.ends_at else None
    now = datetime.now(timezone.utc)
    sub_expired = bool(sub_ends and sub_ends <= now)

    return {
        "tenant_id": tenant_id,
        "found": True,
        "store_name": tenant.name,
        "tenant_created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "whatsapp_connected": wa_connected,
        "whatsapp_status": conn.status if conn else "not_connected",
        "first_whatsapp_connected_at": first_wa.isoformat() if first_wa else None,
        "trial_started_at": tenant.trial_started_at.isoformat() if tenant.trial_started_at else None,
        "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
        "subscription_started_at": sub_started.isoformat() if sub_started else None,
        "subscription_ends_at": sub_ends.isoformat() if sub_ends else None,
        "subscription_status": tenant.subscription_status,
        "billing_subscription_status": sub.status if sub else None,
        "trial_info": trial,
        "subscription_expired": sub_expired,
        "ai_auto_replies_allowed": has_billing_access(db, tenant_id),
        "paid_subscription_effective": effective_sub is not None,
    }


def migrate_existing_tenant_trials(db: Session) -> List[Dict[str, Any]]:
    """
    Correct trial state for existing tenants.

    Rules:
      • Never connected WhatsApp → trial_pending_whatsapp, clear trial dates
      • Has active paid sub     → leave trial dates alone
      • Connected WhatsApp      → anchor trial to first connection if missing/wrong
    """
    from models import Tenant  # noqa: PLC0415

    changes: List[Dict[str, Any]] = []
    tenants = db.query(Tenant).filter(Tenant.is_platform_tenant == False).all()  # noqa: E712

    for tenant in tenants:
        before = {
            "subscription_status": tenant.subscription_status,
            "trial_started_at": tenant.trial_started_at,
            "trial_ends_at": tenant.trial_ends_at,
            "first_whatsapp_connected_at": getattr(tenant, "first_whatsapp_connected_at", None),
        }

        if get_tenant_subscription(db, tenant.id):
            first_wa = _first_whatsapp_connection_at(db, tenant.id)
            if first_wa and not tenant.first_whatsapp_connected_at:
                tenant.first_whatsapp_connected_at = first_wa.replace(tzinfo=None)
            if before != {
                "subscription_status": tenant.subscription_status,
                "trial_started_at": tenant.trial_started_at,
                "trial_ends_at": tenant.trial_ends_at,
                "first_whatsapp_connected_at": tenant.first_whatsapp_connected_at,
            }:
                changes.append(_change_row(tenant.id, before, tenant, "paid_sub_preserve"))
            continue

        if not _tenant_has_connected_whatsapp(db, tenant.id):
            tenant.subscription_status = TRIAL_STATUS_PENDING_WHATSAPP
            tenant.trial_started_at = None
            tenant.trial_ends_at = None
            tenant.first_whatsapp_connected_at = None
        else:
            first_wa = _first_whatsapp_connection_at(db, tenant.id)
            if not first_wa:
                continue

            if not tenant.first_whatsapp_connected_at:
                tenant.first_whatsapp_connected_at = first_wa.replace(tzinfo=None)

            trial_started = _coerce_utc(tenant.trial_started_at)
            needs_reanchor = (
                trial_started is None
                or trial_started < first_wa - timedelta(minutes=5)
            )

            if needs_reanchor:
                tenant.trial_started_at = first_wa.replace(tzinfo=None)
                tenant.trial_ends_at = (
                    first_wa + timedelta(days=FREE_TRIAL_DAYS)
                ).replace(tzinfo=None)

            now = datetime.now(timezone.utc)
            trial_end = _coerce_utc(tenant.trial_ends_at)
            if trial_end and trial_end > now:
                tenant.subscription_status = TRIAL_STATUS_ACTIVE
            elif trial_end:
                tenant.subscription_status = TRIAL_STATUS_EXPIRED

        after = {
            "subscription_status": tenant.subscription_status,
            "trial_started_at": tenant.trial_started_at,
            "trial_ends_at": tenant.trial_ends_at,
            "first_whatsapp_connected_at": tenant.first_whatsapp_connected_at,
        }
        if before != after:
            changes.append(_change_row(tenant.id, before, tenant, "migrated"))

    if changes:
        db.commit()
        for row in changes:
            logger.info("[TrialLifecycle] migrated tenant=%s action=%s", row["tenant_id"], row["action"])

    return changes


def _change_row(tenant_id: int, before: dict, tenant, action: str) -> Dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "action": action,
        "before": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in before.items()},
        "after": {
            "subscription_status": tenant.subscription_status,
            "trial_started_at": tenant.trial_started_at.isoformat() if tenant.trial_started_at else None,
            "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
            "first_whatsapp_connected_at": (
                tenant.first_whatsapp_connected_at.isoformat()
                if getattr(tenant, "first_whatsapp_connected_at", None)
                else None
            ),
        },
    }
