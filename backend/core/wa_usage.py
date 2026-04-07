"""
core/wa_usage.py
────────────────
WhatsApp Conversation Usage Tracking — Production-grade SaaS implementation.

Architecture
------------
Meta charges per "conversation" (a 24-hour rolling window per customer),
NOT per message.  Nahla pays this cost on behalf of merchants, so we enforce
per-plan monthly limits.

Three tables work together:

  wa_conversation_windows   — One row per (tenant, customer_phone).
                              Tracks the start time of the CURRENT open window.
                              SELECT FOR UPDATE on this row serialises concurrent
                              webhook calls, eliminating race conditions.

  conversation_logs         — Immutable audit record written each time a new
                              billable window opens.  Used for the usage details
                              page and merchant support queries.

  whatsapp_usage            — Monthly counter per tenant, split by category.
                              Drives the dashboard widget and limit checks.

Conversation categories (Meta terminology)
------------------------------------------
  service    — Customer-initiated reply within the 24-h window.
               Cheaper, always allowed even when approaching the limit.
  marketing  — Merchant-initiated template message outside 24-h window.
               More expensive; blocked first when tenant is over limit.

Smart blocking policy
---------------------
The core rule: inbound customer replies (service conversations) must NEVER
be blocked — stopping them harms the merchant's customers and degrades their
experience.  Only merchant-initiated marketing traffic is throttled.

  used < limit                    → allow ALL messages
  used >= limit                   → block MARKETING only; allow SERVICE
  used >= limit × SERVICE_EMERGENCY_STOP (3 ×)
                                  → emergency hard-stop ALL (extreme abuse /
                                    runaway automation protection only)

Why no "soft" hard-stop for service?
  Service conversations are inbound-triggered (customer sent a message first).
  Blocking these would violate Meta's policy and ruin the merchant's customer
  experience.  We allow them freely and only bill the overage to the merchant
  at end-of-month if the plan limit is exceeded.

Public API
----------
  track_conversation(db, tenant_id, customer_phone, source, category)
      → TrackResult

  check_limit(db, tenant_id, category)
      → AllowResult   (allowed: bool, reason: str)

  get_usage_this_month(db, tenant_id)
      → dict  (safe to return as API response)

  get_daily_breakdown(db, tenant_id, year, month)
      → list[dict]  (for the detail page chart)

  reset_all_monthly_usage(db)
      → int  (rows reset)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-backend")

# ── Constants ─────────────────────────────────────────────────────────────────
TRIAL_LIMIT               = 100
WINDOW_HOURS              = 24      # Meta billing window
ALERT_PCT_LOW             = 80      # first alert threshold

# SERVICE conversations (customer-initiated) are never blocked at 100%.
# Only a true runaway-automation emergency triggers this hard stop.
# At 3× the plan limit we assume a bug or serious misuse — stop everything.
SERVICE_EMERGENCY_STOP    = 3.0     # 300 % → emergency block all

ConvCategory = Literal["service", "marketing"]
ConvSource   = Literal["inbound", "campaign", "template", "api"]


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class TrackResult:
    counted:     bool           # True if a new billable window was opened
    category:    str
    used_service:     int
    used_marketing:   int
    used_total:       int
    limit:       int


@dataclass
class AllowResult:
    allowed:     bool
    # "ok"                → message allowed
    # "marketing_blocked" → limit reached; marketing is blocked
    # "emergency_stop"    → 300 %+ overage; ALL messages stopped
    reason:      str
    used_total:  int
    limit:       int
    pct:         float


# ── Internal helpers ──────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    """Strip timezone info — DB stores naive UTC datetimes."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _get_plan_limit(db: Session, tenant_id: int) -> int:
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

    val = (plan.limits or {}).get("conversations_per_month", TRIAL_LIMIT)
    return int(val) if val and val != -1 else TRIAL_LIMIT


def _get_or_create_usage(
    db: Session,
    tenant_id: int,
    year: int,
    month: int,
) -> "WhatsAppUsage":  # noqa: F821
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
            tenant_id                    = tenant_id,
            year                         = year,
            month                        = month,
            service_conversations_used   = 0,
            marketing_conversations_used = 0,
            conversations_limit          = limit,
            alert_80_sent                = False,
            alert_100_sent               = False,
        )
        db.add(row)
        try:
            db.flush()
            logger.info(
                "[WaUsage] Created usage row | tenant=%s %04d-%02d limit=%s",
                tenant_id, year, month, limit,
            )
        except Exception as exc:
            db.rollback()
            logger.warning("[WaUsage] flush failed (table may be missing columns): %s", exc)
            raise
    return row


# ── Core: race-safe 24-h window check ────────────────────────────────────────

def _open_new_window(
    db: Session,
    tenant_id: int,
    customer_phone: str,
    category: ConvCategory,
    source: ConvSource,
    now_naive: datetime,
) -> bool:
    """
    Atomically check and update the conversation window for this customer.
    Uses SELECT FOR UPDATE to serialise concurrent calls for the same customer.

    Returns True  → a NEW billable window was opened (counter must be incremented)
    Returns False → still inside an existing window (no charge)
    """
    from models import WaConversationWindow, ConversationLog  # noqa: PLC0415

    cutoff = now_naive - timedelta(hours=WINDOW_HOURS)

    # Lock the row for this tenant+customer — prevents race conditions
    window = (
        db.query(WaConversationWindow)
        .filter(
            WaConversationWindow.tenant_id      == tenant_id,
            WaConversationWindow.customer_phone == customer_phone,
        )
        .with_for_update()
        .first()
    )

    if window is not None and window.window_start >= cutoff:
        # Still inside the 24-h window — no new conversation
        return False

    # ── New window starts now ────────────────────────────────────────────────
    if window is None:
        window = WaConversationWindow(
            tenant_id      = tenant_id,
            customer_phone = customer_phone,
            window_start   = now_naive,
            category       = category,
        )
        db.add(window)
    else:
        window.window_start = now_naive
        window.category     = category
        window.updated_at   = now_naive

    # Write audit log
    log = ConversationLog(
        tenant_id               = tenant_id,
        customer_phone          = customer_phone,
        conversation_started_at = now_naive,
        source                  = source,
        category                = category,
    )
    db.add(log)

    return True


# ── Public API ────────────────────────────────────────────────────────────────

def track_conversation(
    db: Session,
    tenant_id: int,
    customer_phone: str,
    source: ConvSource = "inbound",
    category: ConvCategory = "service",
) -> TrackResult:
    """
    Check whether this message opens a new Meta conversation window.
    If so, increment the relevant monthly counter.

    Thread/process safety
    ---------------------
    _open_new_window() uses SELECT FOR UPDATE, so concurrent webhook calls
    for the same tenant+customer are serialised at the DB level.

    Parameters
    ----------
    source   : "inbound" for customer messages, "campaign"/"template" for
               merchant-initiated bulk or one-off messages
    category : "service" (customer-initiated) | "marketing" (merchant-initiated)
    """
    now        = _utcnow()
    now_naive  = _naive(now)
    year, month = now.year, now.month

    usage = _get_or_create_usage(db, tenant_id, year, month)

    is_new = _open_new_window(db, tenant_id, customer_phone, category, source, now_naive)

    if not is_new:
        total = usage.service_conversations_used + usage.marketing_conversations_used
        return TrackResult(
            counted=False,
            category=category,
            used_service=usage.service_conversations_used,
            used_marketing=usage.marketing_conversations_used,
            used_total=total,
            limit=usage.conversations_limit,
        )

    # ── Increment the right counter ──────────────────────────────────────────
    if category == "marketing":
        usage.marketing_conversations_used += 1
    else:
        usage.service_conversations_used   += 1

    usage.updated_at = now_naive
    total = usage.service_conversations_used + usage.marketing_conversations_used

    logger.info(
        "[WaUsage] New %s window | tenant=%s phone=***%s total=%d/%d",
        category, tenant_id, customer_phone[-4:], total, usage.conversations_limit,
    )

    # ── Check alert thresholds ───────────────────────────────────────────────
    limit = usage.conversations_limit
    if limit > 0:
        pct = (total / limit) * 100
        if pct >= 80 and not usage.alert_80_sent:
            usage.alert_80_sent = True
            _fire_alert(db, tenant_id, total, limit, "80%")
        if pct >= 100 and not usage.alert_100_sent:
            usage.alert_100_sent = True
            _fire_alert(db, tenant_id, total, limit, "100%")

    db.commit()
    return TrackResult(
        counted=True,
        category=category,
        used_service=usage.service_conversations_used,
        used_marketing=usage.marketing_conversations_used,
        used_total=total,
        limit=limit,
    )


def check_limit(
    db: Session,
    tenant_id: int,
    category: ConvCategory = "service",
) -> AllowResult:
    """
    Decide whether a message of this category is allowed to be sent.

    Blocking policy
    ───────────────
    SERVICE conversations (customer-initiated inbound replies):
      ✅  Always allowed — until the emergency stop threshold (3 × plan limit).
      Reason: blocking service replies damages merchant–customer relationships
      and violates Meta's messaging guidelines.

    MARKETING messages (campaigns, abandoned cart, broadcast templates):
      ✅  Allowed while usage < plan limit.
      ❌  Blocked once usage >= plan limit.

    Emergency stop (all categories):
      ❌  Triggered only at ≥ 300 % of the plan limit.
      Purpose: protect the platform from runaway automations or API abuse.

    Parameters
    ----------
    category : "service" | "marketing"
        Pass "marketing" for any merchant-initiated broadcast, campaign,
        abandoned-cart, or template message.
        Pass "service" for replies to inbound customer messages.
    """
    now   = _utcnow()
    usage = _get_or_create_usage(db, tenant_id, now.year, now.month)

    used  = usage.service_conversations_used + usage.marketing_conversations_used
    limit = usage.conversations_limit
    pct   = round((used / limit) * 100, 1) if limit > 0 else 0.0

    # No limit configured (should not happen, but guard anyway)
    if limit <= 0:
        return AllowResult(allowed=True, reason="ok", used_total=used, limit=limit, pct=0.0)

    # ── Emergency stop — runaway automation / abuse (300 % threshold) ─────────
    # Only ever triggered by a serious bug or intentional abuse; normal SaaS
    # merchants will never approach this.
    if used >= int(limit * SERVICE_EMERGENCY_STOP):
        logger.warning(
            "[WaUsage] EMERGENCY STOP | tenant=%s used=%d limit=%d (%.0f%%)",
            tenant_id, used, limit, pct,
        )
        return AllowResult(
            allowed    = False,
            reason     = "emergency_stop",
            used_total = used,
            limit      = limit,
            pct        = pct,
        )

    # ── Marketing blocked at 100 % ────────────────────────────────────────────
    if used >= limit and category == "marketing":
        return AllowResult(
            allowed    = False,
            reason     = "marketing_blocked",
            used_total = used,
            limit      = limit,
            pct        = pct,
        )

    # ── All other cases — allow ───────────────────────────────────────────────
    # Includes:
    #   • service conversations at any usage level below emergency stop
    #   • all messages while usage < plan limit
    return AllowResult(allowed=True, reason="ok", used_total=used, limit=limit, pct=pct)


def get_usage_this_month(db: Session, tenant_id: int) -> dict:
    """Return a dict safe to serialise as an API response."""
    now   = _utcnow()
    usage = _get_or_create_usage(db, tenant_id, now.year, now.month)

    svc   = usage.service_conversations_used
    mkt   = usage.marketing_conversations_used
    total = svc + mkt
    limit = usage.conversations_limit
    pct   = round((total / limit) * 100, 1) if limit > 0 else 0.0

    return {
        "service_conversations_used":   svc,
        "marketing_conversations_used": mkt,
        "conversations_used":           total,          # kept for backward compat
        "conversations_limit":          limit,
        "usage_pct":                    pct,
        "exceeded":                     (limit > 0 and total >= limit),
        "near_limit":                   (limit > 0 and pct >= 80 and total < limit),
        # hard_stop: only marketing is blocked when exceeded=True.
        # emergency_stop (300%+) would block everything — shown separately.
        "marketing_blocked":            (limit > 0 and total >= limit),
        "emergency_stop":               (limit > 0 and total >= int(limit * SERVICE_EMERGENCY_STOP)),
        "unlimited":                    False,           # no plan is truly unlimited
        "month":                        now.month,
        "year":                         now.year,
        "reset_date":                   f"01/{now.month + 1 if now.month < 12 else 1}/{now.year if now.month < 12 else now.year + 1}",
        "alert_80_sent":                usage.alert_80_sent,
        "alert_100_sent":               usage.alert_100_sent,
    }


def get_daily_breakdown(
    db: Session,
    tenant_id: int,
    year: int,
    month: int,
) -> list:
    """
    Return a day-by-day breakdown of new conversation windows for the given
    month, split by category.  Used by the usage detail page chart.
    """
    from models import ConversationLog  # noqa: PLC0415
    from sqlalchemy import func, extract  # noqa: PLC0415

    rows = (
        db.query(
            func.date(ConversationLog.conversation_started_at).label("day"),
            ConversationLog.category,
            func.count().label("count"),
        )
        .filter(
            ConversationLog.tenant_id == tenant_id,
            extract("year",  ConversationLog.conversation_started_at) == year,
            extract("month", ConversationLog.conversation_started_at) == month,
        )
        .group_by("day", ConversationLog.category)
        .order_by("day")
        .all()
    )

    # Aggregate into dict[day] → {service, marketing}
    days: dict = {}
    for row in rows:
        day_str = str(row.day)
        if day_str not in days:
            days[day_str] = {"day": day_str, "service": 0, "marketing": 0, "total": 0}
        days[day_str][row.category] = row.count
        days[day_str]["total"]     += row.count

    return list(days.values())


def reset_all_monthly_usage(db: Session) -> int:
    """
    Called by the scheduler on the 1st of each month.
    Creates fresh usage rows (with updated plan limits) for every tenant
    that had activity in the previous month.
    Returns the number of tenants processed.
    """
    from models import WhatsAppUsage  # noqa: PLC0415

    now   = _utcnow()
    year  = now.year
    month = now.month

    prior = (
        db.query(WhatsAppUsage)
        .filter(
            (WhatsAppUsage.year  != year) |
            (WhatsAppUsage.month != month),
        )
        .all()
    )

    count = 0
    seen  = set()
    for row in prior:
        if row.tenant_id not in seen:
            seen.add(row.tenant_id)
            _get_or_create_usage(db, row.tenant_id, year, month)
            count += 1

    db.commit()
    logger.info("[WaUsage] Monthly reset | tenants_refreshed=%d %04d-%02d", count, year, month)
    return count


# ── Alert notifications ───────────────────────────────────────────────────────

def _fire_alert(
    db: Session,
    tenant_id: int,
    used: int,
    limit: int,
    threshold: str,
) -> None:
    """Send a concise WhatsApp alert to the merchant."""
    try:
        import asyncio  # noqa: PLC0415
        from core.wa_notify import _send  # noqa: PLC0415
        from core.tenant import get_or_create_settings, merge_defaults  # noqa: PLC0415

        settings = get_or_create_settings(db, tenant_id)
        wa       = merge_defaults(settings.whatsapp_settings or {}, {})
        owner_phone = wa.get("owner_whatsapp_number", "")
        if not owner_phone:
            return

        if threshold == "100%":
            remaining = 0
            msg = (
                f"⛔ *تجاوزت حد محادثات واتساب لهذا الشهر*\n\n"
                f"الاستخدام: *{used:,} / {limit:,}* محادثة\n\n"
                "📌 الحملات التسويقية متوقفة مؤقتاً.\n"
                "الردود على العملاء لا تزال تعمل.\n\n"
                "⬆️ *ارقِّ باقتك لاستئناف الحملات:*\n"
                "https://app.nahlah.ai/billing"
            )
        else:
            remaining = limit - used
            msg = (
                f"⚠️ *استخدمت 80% من محادثات واتساب هذا الشهر*\n\n"
                f"الاستخدام: *{used:,} / {limit:,}* محادثة\n"
                f"المتبقي: *{remaining:,}* محادثة\n\n"
                "💡 ارقِّ باقتك الآن لتجنب توقف الحملات:\n"
                "https://app.nahlah.ai/billing"
            )

        asyncio.ensure_future(_send(owner_phone, msg))
        logger.info(
            "[WaUsage] Alert sent | tenant=%s threshold=%s used=%d/%d",
            tenant_id, threshold, used, limit,
        )
    except Exception as exc:
        logger.warning("[WaUsage] Alert failed: %s", exc)
