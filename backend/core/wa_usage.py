"""
core/wa_usage.py
────────────────
WhatsApp Conversation Usage Tracking & Limit Enforcement.

Context
-------
Meta charges per "conversation" (a 24-hour window), NOT per message.
A new conversation opens when:
  • A customer sends the first message in >24 h
  • OR we send a template message outside a 24-h window

Since Nahla pays Meta on behalf of merchants during the early SaaS phase,
we must enforce per-plan monthly conversation limits.

Public API
----------
  track_conversation(db, tenant_id, customer_phone)
      → (counted: bool, used: int, limit: int)
      Call this on EVERY incoming/outgoing message.
      Returns (True, …) only the first time a new 24-h window opens.

  check_limit(db, tenant_id)
      → UsageSummary  (used, limit, pct, exceeded, near_limit)
      Call this BEFORE sending any message to block when over limit.

  get_usage_this_month(db, tenant_id)
      → dict  (safe for frontend / API)

  reset_all_monthly_usage(db)
      → int  (number of rows reset)
      Called by scheduler on the 1st of each month.

Plan limits (conversations_per_month)
--------------------------------------
  trial         →   100
  starter       → 1 000
  growth        → 5 000
  scale         →    -1  (unlimited)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-backend")

# ── Sentinel values ───────────────────────────────────────────────────────────
TRIAL_LIMIT     = 100
UNLIMITED       = -1
WINDOW_HOURS    = 24          # Meta conversation window
ALERT_PCT_LOW   = 80          # warn at 80 %
ALERT_PCT_HIGH  = 100         # block at 100 %


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class UsageSummary:
    used:        int
    limit:       int           # -1 = unlimited
    pct:         float         # 0-100; 0 when unlimited
    exceeded:    bool
    near_limit:  bool          # True when pct >= 80 and not yet exceeded


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _get_plan_limit(db: Session, tenant_id: int) -> int:
    """
    Return conversations_per_month for this tenant's active plan.
    Falls back to TRIAL_LIMIT if no subscription exists.
    """
    # Lazy import to avoid circular dependencies
    from models import BillingSubscription, BillingPlan  # noqa: PLC0415

    sub = (
        db.query(BillingSubscription)
        .filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status    == "active",
        )
        .order_by(BillingSubscription.started_at.desc())
        .first()
    )
    if sub is None:
        return TRIAL_LIMIT

    plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    if plan is None:
        return TRIAL_LIMIT

    limits = plan.limits or {}
    val = limits.get("conversations_per_month", TRIAL_LIMIT)
    return int(val) if val is not None else TRIAL_LIMIT


def _get_or_create_usage(
    db: Session,
    tenant_id: int,
    year: int,
    month: int,
) -> "WhatsAppUsage":  # noqa: F821
    """Return (or create) the monthly usage row for this tenant."""
    from models import WhatsAppUsage  # noqa: PLC0415

    row = (
        db.query(WhatsAppUsage)
        .filter(
            WhatsAppUsage.tenant_id == tenant_id,
            WhatsAppUsage.year      == year,
            WhatsAppUsage.month     == month,
        )
        .first()
    )
    if row is None:
        limit = _get_plan_limit(db, tenant_id)
        row   = WhatsAppUsage(
            tenant_id           = tenant_id,
            year                = year,
            month               = month,
            conversations_used  = 0,
            conversations_limit = limit,
            alert_80_sent       = False,
            alert_100_sent      = False,
        )
        db.add(row)
        db.flush()
        logger.info(
            "[WaUsage] Created usage row | tenant=%s %04d-%02d limit=%s",
            tenant_id, year, month, limit,
        )
    return row


def _is_new_meta_conversation(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> bool:
    """
    Return True if this is the START of a new 24-h Meta conversation window.
    A window is new when no message has been exchanged with this customer
    in the last WINDOW_HOURS hours.

    We check ConversationTrace because it's keyed on (tenant_id, customer_phone)
    and written for every AI-handled message turn.
    """
    from models import ConversationTrace  # noqa: PLC0415

    cutoff = _now_utc() - timedelta(hours=WINDOW_HOURS)
    # ConversationTrace.created_at uses datetime.utcnow (naive)
    cutoff_naive = cutoff.replace(tzinfo=None)

    recent = (
        db.query(ConversationTrace.id)
        .filter(
            ConversationTrace.tenant_id      == tenant_id,
            ConversationTrace.customer_phone == customer_phone,
            ConversationTrace.created_at     >= cutoff_naive,
        )
        .first()
    )
    return recent is None   # True → no recent trace → new conversation


# ── Public API ────────────────────────────────────────────────────────────────

def track_conversation(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> Tuple[bool, int, int]:
    """
    Check whether this message opens a new Meta conversation window.
    If so, increment conversations_used for this month.

    Returns
    -------
    (counted, used, limit)
      counted : True if this was counted as a new conversation
      used    : total conversations used this month (after incrementing)
      limit   : plan limit for this month (-1 = unlimited)
    """
    now   = _now_utc()
    year  = now.year
    month = now.month

    usage = _get_or_create_usage(db, tenant_id, year, month)
    limit = usage.conversations_limit

    is_new = _is_new_meta_conversation(db, tenant_id, customer_phone)
    if not is_new:
        # Still inside the 24-h window — no new conversation charged
        return False, usage.conversations_used, limit

    # ── New conversation window ────────────────────────────────────────────────
    usage.conversations_used += 1
    usage.updated_at          = now.replace(tzinfo=None)   # DB stores naive UTC
    used = usage.conversations_used

    logger.info(
        "[WaUsage] New conversation counted | tenant=%s phone=***%s used=%d limit=%s",
        tenant_id, customer_phone[-4:], used, limit,
    )

    # ── Alert thresholds ─────────────────────────────────────────────────────
    if limit > 0:
        pct = (used / limit) * 100
        if pct >= ALERT_PCT_LOW and not usage.alert_80_sent:
            usage.alert_80_sent = True
            _fire_usage_alert(db, tenant_id, used, limit, pct, "80%")
        if pct >= ALERT_PCT_HIGH and not usage.alert_100_sent:
            usage.alert_100_sent = True
            _fire_usage_alert(db, tenant_id, used, limit, pct, "100%")

    db.commit()
    return True, used, limit


def check_limit(db: Session, tenant_id: int) -> UsageSummary:
    """
    Return the current usage status for this tenant this month.
    Call this BEFORE sending any WhatsApp message to enforce limits.
    """
    now   = _now_utc()
    usage = _get_or_create_usage(db, tenant_id, now.year, now.month)

    used  = usage.conversations_used
    limit = usage.conversations_limit

    if limit == UNLIMITED or limit <= 0:
        return UsageSummary(used=used, limit=UNLIMITED, pct=0.0, exceeded=False, near_limit=False)

    pct       = (used / limit) * 100
    exceeded  = used >= limit
    near      = pct >= ALERT_PCT_LOW and not exceeded

    return UsageSummary(used=used, limit=limit, pct=round(pct, 1), exceeded=exceeded, near_limit=near)


def get_usage_this_month(db: Session, tenant_id: int) -> dict:
    """Return a dict safe to serialize as API response."""
    now   = _now_utc()
    usage = _get_or_create_usage(db, tenant_id, now.year, now.month)

    used  = usage.conversations_used
    limit = usage.conversations_limit
    pct   = round((used / limit) * 100, 1) if limit > 0 else 0.0

    return {
        "conversations_used":    used,
        "conversations_limit":   limit,
        "usage_pct":             pct,
        "exceeded":              (limit > 0 and used >= limit),
        "near_limit":            (limit > 0 and pct >= ALERT_PCT_LOW and used < limit),
        "unlimited":             (limit == UNLIMITED),
        "month":                 now.month,
        "year":                  now.year,
        "alert_80_sent":         usage.alert_80_sent,
        "alert_100_sent":        usage.alert_100_sent,
    }


def reset_all_monthly_usage(db: Session) -> int:
    """
    Reset all tenants' conversations_used to 0 for the new month.
    Also refreshes conversations_limit from the current plan.
    Called by the scheduler on the 1st of each month.

    Returns the number of rows updated.
    """
    from models import WhatsAppUsage  # noqa: PLC0415

    now   = _now_utc()
    year  = now.year
    month = now.month

    # Find tenants that already have a row (prior months)
    prior = (
        db.query(WhatsAppUsage)
        .filter(
            (WhatsAppUsage.year  != year) |
            (WhatsAppUsage.month != month),
        )
        .all()
    )

    count = 0
    for row in prior:
        new_limit = _get_plan_limit(db, row.tenant_id)
        # Create fresh row for this month if it doesn't exist
        _get_or_create_usage(db, row.tenant_id, year, month)
        count += 1

    db.commit()
    logger.info("[WaUsage] Monthly reset complete | tenants_refreshed=%d %04d-%02d", count, year, month)
    return count


# ── Alert helper (fire-and-forget) ────────────────────────────────────────────

def _fire_usage_alert(
    db: Session,
    tenant_id: int,
    used: int,
    limit: int,
    pct: float,
    threshold: str,
) -> None:
    """Send WhatsApp + email alert to the merchant when they hit a usage threshold."""
    try:
        import asyncio as _asyncio  # noqa: PLC0415
        from core.wa_notify import _send  # noqa: PLC0415
        from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_WHATSAPP, DEFAULT_STORE  # noqa: PLC0415

        settings    = get_or_create_settings(db, tenant_id)
        wa_settings = merge_defaults(settings.whatsapp_settings or {}, DEFAULT_WHATSAPP)
        st_settings = merge_defaults(settings.store_settings    or {}, DEFAULT_STORE)
        owner_phone = wa_settings.get("owner_whatsapp_number", "")
        store_name  = st_settings.get("store_name", f"متجر #{tenant_id}")

        if not owner_phone:
            return

        if threshold == "100%":
            msg = (
                f"🚨 *{store_name}* — وصلت إلى حد محادثات واتساب لهذا الشهر\n\n"
                f"استخدمت *{used:,}* من *{limit:,}* محادثة.\n\n"
                "⛔ تم إيقاف الردود التلقائية تلقائياً.\n"
                "⬆️ ارقِّ باقتك لاستئناف الخدمة 👇\n"
                "https://app.nahlah.ai/billing"
            )
        else:
            remaining = limit - used
            msg = (
                f"⚠️ *{store_name}* — استخدمت {threshold} من محادثات واتساب\n\n"
                f"الاستخدام: *{used:,}* / *{limit:,}* محادثة\n"
                f"المتبقي: *{remaining:,}* محادثة\n\n"
                "💡 فكر في ترقية باقتك قبل نهاية الشهر 👇\n"
                "https://app.nahlah.ai/billing"
            )

        _asyncio.ensure_future(_send(owner_phone, msg))
        logger.info(
            "[WaUsage] Alert sent | tenant=%s threshold=%s used=%d/%d",
            tenant_id, threshold, used, limit,
        )
    except Exception as exc:
        logger.warning("[WaUsage] Alert send failed: %s", exc)
