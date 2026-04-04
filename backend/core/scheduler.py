"""
core/scheduler.py
──────────────────
Background scheduler for periodic Nahla platform tasks:
  • Subscription expiry warnings (7 days + 3 days before)
  • Expired subscription notifications
  • Trial ending warnings

Runs as an asyncio background task started from main.py lifespan.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-scheduler")

_CHECK_INTERVAL_HOURS = 12   # run every 12 hours


async def run_scheduler() -> None:
    """Main scheduler loop — runs forever in background."""
    logger.info("[Scheduler] Started — checking every %sh", _CHECK_INTERVAL_HOURS)
    while True:
        try:
            await _run_checks()
        except Exception as exc:
            logger.error("[Scheduler] Error in check cycle: %s", exc, exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_HOURS * 3600)


async def _run_checks() -> None:
    """Run all periodic checks."""
    logger.info("[Scheduler] Running periodic checks...")
    await _check_subscription_expiry()
    await _check_trial_expiry()
    logger.info("[Scheduler] Checks complete.")


async def _check_subscription_expiry() -> None:
    """Send WhatsApp warnings for expiring/expired subscriptions."""
    import sys, os  # noqa: PLC0415
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))

    from core.database import SessionLocal  # noqa: PLC0415
    from core.wa_notify import (  # noqa: PLC0415
        notify_subscription_expired,
        notify_subscription_expiring,
    )

    try:
        db: Session = SessionLocal()
    except Exception as exc:
        logger.error("[Scheduler] Cannot open DB session: %s", exc)
        return

    try:
        from models import BillingSubscription, Tenant  # noqa: PLC0415
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE, DEFAULT_WHATSAPP,
            get_or_create_settings, merge_defaults,
        )

        now = datetime.utcnow()

        active_subs = (
            db.query(BillingSubscription)
            .filter(BillingSubscription.status == "active")
            .all()
        )

        for sub in active_subs:
            if not sub.ends_at:
                continue

            _s         = get_or_create_settings(db, sub.tenant_id)
            _wa        = merge_defaults(_s.whatsapp_settings, DEFAULT_WHATSAPP)
            _st        = merge_defaults(_s.store_settings,    DEFAULT_STORE)
            phone      = _wa.get("owner_whatsapp_number", "")
            store_name = _st.get("store_name") or f"متجر #{sub.tenant_id}"
            plan_name  = sub.plan.name if sub.plan else "الباقة الحالية"

            if not phone:
                continue

            days_left = (sub.ends_at - now).days

            if days_left < 0:
                # Already expired — notify and mark cancelled
                logger.info("[Scheduler] Sub %s expired for tenant %s", sub.id, sub.tenant_id)
                sub.status = "expired"
                db.commit()
                await notify_subscription_expired(phone, store_name)

            elif days_left <= 3 and not _already_notified(sub, "warn_3"):
                await notify_subscription_expiring(phone, store_name, plan_name, days_left)
                _mark_notified(db, sub, "warn_3")

            elif days_left <= 7 and not _already_notified(sub, "warn_7"):
                await notify_subscription_expiring(phone, store_name, plan_name, days_left)
                _mark_notified(db, sub, "warn_7")

    finally:
        db.close()


async def _check_trial_expiry() -> None:
    """Send WhatsApp warnings for expiring trials."""
    import sys, os  # noqa: PLC0415
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))

    from core.database import SessionLocal  # noqa: PLC0415
    from core.wa_notify import notify_trial_ending  # noqa: PLC0415
    from core.billing import FREE_TRIAL_DAYS  # noqa: PLC0415

    try:
        db: Session = SessionLocal()
    except Exception as exc:
        logger.error("[Scheduler] Cannot open DB session: %s", exc)
        return

    try:
        from models import BillingSubscription, Tenant  # noqa: PLC0415
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE, DEFAULT_WHATSAPP,
            get_or_create_settings, merge_defaults,
        )

        now = datetime.utcnow()

        # Tenants with no active subscription = in trial
        subbed_tenants = {
            s.tenant_id for s in db.query(BillingSubscription)
            .filter(BillingSubscription.status == "active").all()
        }

        tenants = db.query(Tenant).filter(Tenant.is_active == True).all()  # noqa: E712

        for tenant in tenants:
            if tenant.id in subbed_tenants:
                continue  # already subscribed, skip

            trial_start    = tenant.created_at or now
            trial_elapsed  = (now - trial_start).days
            days_remaining = FREE_TRIAL_DAYS - trial_elapsed

            _s         = get_or_create_settings(db, tenant.id)
            _wa        = merge_defaults(_s.whatsapp_settings, DEFAULT_WHATSAPP)
            _st        = merge_defaults(_s.store_settings,    DEFAULT_STORE)
            phone      = _wa.get("owner_whatsapp_number", "")
            store_name = _st.get("store_name") or f"متجر #{tenant.id}"

            if not phone:
                continue

            meta = (_s.extra_metadata or {}).get("_scheduler_flags", {})
            if days_remaining == 7 and not meta.get("trial_warn_7"):
                await notify_trial_ending(phone, store_name, 7)
                _update_tenant_flag(db, tenant.id, _s, "trial_warn_7")
            elif days_remaining == 3 and not meta.get("trial_warn_3"):
                await notify_trial_ending(phone, store_name, 3)
                _update_tenant_flag(db, tenant.id, _s, "trial_warn_3")
            elif days_remaining == 1 and not meta.get("trial_warn_1"):
                await notify_trial_ending(phone, store_name, 1)
                _update_tenant_flag(db, tenant.id, _s, "trial_warn_1")

    finally:
        db.close()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _already_notified(sub: object, flag: str) -> bool:
    meta = getattr(sub, "extra_metadata", None) or {}
    return bool(meta.get(f"notified_{flag}"))


def _mark_notified(db: Session, sub: object, flag: str) -> None:
    meta = dict(getattr(sub, "extra_metadata", None) or {})
    meta[f"notified_{flag}"] = True
    sub.extra_metadata = meta  # type: ignore[attr-defined]
    db.commit()


def _update_tenant_flag(db: Session, tenant_id: int, settings_obj: object, flag: str) -> None:
    from core.tenant import get_or_create_settings  # noqa: PLC0415
    _s    = get_or_create_settings(db, tenant_id)
    meta  = dict(_s.extra_metadata or {})
    flags = meta.get("_scheduler_flags", {})
    flags[flag] = True
    meta["_scheduler_flags"] = flags
    _s.extra_metadata = meta
    try:
        db.commit()
    except Exception:
        db.rollback()
