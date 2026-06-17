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

  get_current_period_usage(db, tenant_id)
      → dict  (SSOT — period usage + today count + lifetime)

  get_usage_this_month(db, tenant_id)
      → dict  (alias for get_current_period_usage — backwards compatible)

  get_daily_breakdown(db, tenant_id, year, month)
      → list[dict]  (for the detail page chart)

  reset_all_monthly_usage(db)
      → int  (rows reset)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-backend")

# ── Constants ─────────────────────────────────────────────────────────────────
TRIAL_LIMIT               = 100
WINDOW_HOURS              = 24      # Meta billing window
ALERT_PCT_LOW             = 70      # first warning threshold
ALERT_PCT_HIGH            = 90      # urgent red alert threshold

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


# Sentinel returned for unlimited plans (Scale). We use a finite integer
# rather than ``math.inf`` so it can be stored in the ``conversations_limit``
# integer column without overflow and so existing ``limit > 0`` and
# ``total / limit`` math stays valid. ``get_usage_this_month`` translates
# this back to ``unlimited=True`` for the API surface.
UNLIMITED_LIMIT_SENTINEL = 999_999_999


def _get_plan_limit(db: Session, tenant_id: int) -> int:
    """Return the merchant's monthly conversation cap.

    Reads from the *current* active subscription each call — this is the
    function that ``_get_or_create_usage`` re-syncs against, so a paid
    upgrade takes effect on the very next inbound message.

    Resolution:
      * No active sub → ``TRIAL_LIMIT`` (100).
      * Plan ``conversations_per_month == -1`` (Scale)
        → ``UNLIMITED_LIMIT_SENTINEL`` so percent-used calculations
        always fall to ~0% and enforcement gates never trip.
      * Otherwise → the integer from ``BillingPlan.limits``.
    """
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

    val = (plan.limits or {}).get("conversations_per_month")
    if val is None:
        return TRIAL_LIMIT
    if int(val) == -1:
        # Unlimited — Scale plan or admin-granted bypass.
        return UNLIMITED_LIMIT_SENTINEL
    return int(val)


def _get_active_subscription(db: Session, tenant_id: int):
    """Return the tenant's active, non-expired paid subscription (if any)."""
    from core.billing import get_tenant_subscription  # noqa: PLC0415

    return get_tenant_subscription(db, tenant_id)


def _usage_period_context(db: Session, tenant_id: int) -> dict:
    """
    Resolve whether usage is scoped to the active billing period or the
    calendar month (trial / no paid sub).
    """
    sub = _get_active_subscription(db, tenant_id)
    if sub is not None:
        return {
            "mode":              "subscription",
            "subscription_id":   int(sub.id),
            "period_started_at": sub.started_at,
            "period_ends_at":    sub.ends_at,
        }
    now = _utcnow()
    return {
        "mode":  "calendar",
        "year":  now.year,
        "month": now.month,
    }


def _get_or_create_usage(
    db: Session,
    tenant_id: int,
    ctx: Optional[dict] = None,
) -> "WhatsAppUsage":  # noqa: F821
    """Return the WhatsAppUsage row for the current billing/calendar period."""
    from models import WhatsAppUsage  # noqa: PLC0415

    if ctx is None:
        ctx = _usage_period_context(db, tenant_id)

    now = _utcnow()

    if ctx.get("mode") == "subscription":
        sub_id = int(ctx["subscription_id"])
        row = (
            db.query(WhatsAppUsage)
            .filter(
                WhatsAppUsage.tenant_id       == tenant_id,
                WhatsAppUsage.subscription_id == sub_id,
            )
            .first()
        )
        if row is None:
            limit = _get_plan_limit(db, tenant_id)
            row   = WhatsAppUsage(
                tenant_id                    = tenant_id,
                subscription_id              = sub_id,
                year                         = now.year,
                month                        = now.month,
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
                    "[WaUsage] Created subscription-period usage row | tenant=%s sub=%s limit=%s",
                    tenant_id, sub_id, limit,
                )
            except Exception as exc:
                db.rollback()
                logger.warning("[WaUsage] flush failed (table may be missing columns): %s", exc)
                raise
            _reconcile_usage_from_logs(db, tenant_id, row, ctx)
    else:
        year, month = int(ctx["year"]), int(ctx["month"])
        row = (
            db.query(WhatsAppUsage)
            .filter(
                WhatsAppUsage.tenant_id       == tenant_id,
                WhatsAppUsage.year            == year,
                WhatsAppUsage.month           == month,
                WhatsAppUsage.subscription_id.is_(None),
            )
            .first()
        )
        if row is None:
            limit = _get_plan_limit(db, tenant_id)
            row   = WhatsAppUsage(
                tenant_id                    = tenant_id,
                subscription_id              = None,
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
            _reconcile_usage_from_logs(db, tenant_id, row, ctx)

    current_limit = _get_plan_limit(db, tenant_id)
    if int(row.conversations_limit or 0) != int(current_limit):
        previous = int(row.conversations_limit or 0)
        row.conversations_limit = current_limit
        if current_limit > previous:
            row.alert_80_sent  = False
            row.alert_100_sent = False
        try:
            db.flush()
            logger.info(
                "[WaUsage] Re-synced limit | tenant=%s mode=%s old=%s new=%s",
                tenant_id, ctx.get("mode"), previous, current_limit,
            )
        except Exception as exc:
            db.rollback()
            logger.warning("[WaUsage] limit re-sync flush failed: %s", exc)
    return row


def _period_bounds_naive(ctx: dict, now: Optional[datetime] = None) -> tuple[datetime, Optional[datetime]]:
    """Return naive-UTC [start, end) bounds for the active usage period."""
    now = now or _utcnow()
    if ctx.get("mode") == "subscription":
        raw_start = ctx.get("period_started_at")
        raw_end   = ctx.get("period_ends_at")
        if raw_start is None:
            start = _naive(now)
        elif isinstance(raw_start, datetime):
            start = _naive(raw_start if raw_start.tzinfo else raw_start.replace(tzinfo=timezone.utc))
        else:
            start = _naive(now)
        end: Optional[datetime] = None
        if isinstance(raw_end, datetime):
            end = _naive(raw_end if raw_end.tzinfo else raw_end.replace(tzinfo=timezone.utc))
        return start, end

    year, month = int(ctx["year"]), int(ctx["month"])
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end


def _count_conversation_logs(
    db: Session,
    tenant_id: int,
    start_naive: datetime,
    end_naive: Optional[datetime] = None,
    *,
    category: Optional[str] = None,
) -> int:
    """Count billable conversation windows in ``ConversationLog`` (audit SSOT)."""
    from models import ConversationLog  # noqa: PLC0415
    from sqlalchemy import func  # noqa: PLC0415

    q = (
        db.query(func.count(ConversationLog.id))
        .filter(
            ConversationLog.tenant_id == tenant_id,
            ConversationLog.conversation_started_at >= start_naive,
        )
    )
    if end_naive is not None:
        q = q.filter(ConversationLog.conversation_started_at < end_naive)
    if category is not None:
        q = q.filter(ConversationLog.category == category)
    return int(q.scalar() or 0)


def count_conversations_in_window(
    db: Session,
    tenant_id: int,
    window_start: datetime,
    window_end: Optional[datetime] = None,
) -> int:
    """Public helper — count ConversationLog rows in a time window (aware or naive UTC)."""
    if window_start.tzinfo is not None:
        start_naive = _naive(window_start)
    else:
        start_naive = window_start
    end_naive: Optional[datetime] = None
    if window_end is not None:
        end_naive = _naive(window_end) if window_end.tzinfo else window_end
    return _count_conversation_logs(db, tenant_id, start_naive, end_naive)


def get_today_conversations_count(db: Session, tenant_id: int) -> int:
    """Billable conversation windows opened today (UTC day boundary)."""
    now   = _utcnow()
    start = datetime(now.year, now.month, now.day)
    return _count_conversation_logs(db, tenant_id, start)


def _reconcile_usage_from_logs(
    db: Session,
    tenant_id: int,
    usage: "WhatsAppUsage",  # noqa: F821
    ctx: dict,
) -> bool:
    """
    Heal under-counted ``WhatsAppUsage`` rows from ``ConversationLog``.

    Increment path and read path can drift when a new subscription-period
    row is created mid-day — logs exist but the fresh counter row reads 0.
    We only ever *raise* counters to match logs inside the active period;
    we never decrease (renewals get a new row instead).
    """
    start_naive, end_naive = _period_bounds_naive(ctx)
    log_svc = _count_conversation_logs(db, tenant_id, start_naive, end_naive, category="service")
    log_mkt = _count_conversation_logs(db, tenant_id, start_naive, end_naive, category="marketing")
    stored_svc = int(usage.service_conversations_used or 0)
    stored_mkt = int(usage.marketing_conversations_used or 0)
    changed = False
    if log_svc > stored_svc:
        usage.service_conversations_used = log_svc
        changed = True
    if log_mkt > stored_mkt:
        usage.marketing_conversations_used = log_mkt
        changed = True
    if changed:
        usage.updated_at = _naive(_utcnow())
        try:
            db.flush()
            logger.info(
                "[WaUsage] Reconciled from ConversationLog | tenant=%s mode=%s "
                "stored=%s+%s logs=%s+%s",
                tenant_id, ctx.get("mode"),
                stored_svc, stored_mkt, log_svc, log_mkt,
            )
        except Exception as exc:
            db.rollback()
            logger.warning("[WaUsage] reconcile flush failed: %s", exc)
            raise
    return changed


def get_lifetime_conversations(db: Session, tenant_id: int) -> int:
    """Total billable conversation windows opened since tenant onboarding."""
    from models import ConversationLog  # noqa: PLC0415
    from sqlalchemy import func  # noqa: PLC0415

    return int(
        db.query(func.count(ConversationLog.id))
        .filter(ConversationLog.tenant_id == tenant_id)
        .scalar()
        or 0
    )


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


def has_open_service_window(
    db: Session,
    tenant_id: int,
    customer_phone: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """
    Return True when the customer currently has an open *service* window.

    Free-form text replies are only allowed after a customer-initiated message.
    A marketing conversation opened by a template must not be treated as a
    customer-service window for manual text replies.
    """
    if not customer_phone:
        return False

    from models import WaConversationWindow  # noqa: PLC0415

    now_naive = _naive(now or _utcnow())
    cutoff = now_naive - timedelta(hours=WINDOW_HOURS)
    window = (
        db.query(WaConversationWindow)
        .filter(
            WaConversationWindow.tenant_id == tenant_id,
            WaConversationWindow.customer_phone == customer_phone,
        )
        .first()
    )
    return bool(
        window is not None
        and window.category == "service"
        and window.window_start >= cutoff
    )


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
    ctx        = _usage_period_context(db, tenant_id)

    usage = _get_or_create_usage(db, tenant_id, ctx)

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
        if pct >= ALERT_PCT_LOW and not usage.alert_80_sent:
            usage.alert_80_sent = True
            _fire_alert(db, tenant_id, total, limit, f"{ALERT_PCT_LOW}%")
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
    ctx   = _usage_period_context(db, tenant_id)
    usage = _get_or_create_usage(db, tenant_id, ctx)

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


# Meta phone-number `messaging_limit_tier` enum → numeric cap + Arabic-leaning
# label. Source of truth: Meta Graph API field ``messaging_limit_tier`` on
# /<phone_number_id>. We accept BOTH legacy ("UNLIMITED") and current
# ("TIER_UNLIMITED") spellings because Meta has shipped both over time and
# different WABAs can return either depending on when they were provisioned.
#
# Any tier value Meta returns that isn't in this map will be displayed verbatim
# in the UI (e.g. "TIER_FOO") instead of being hidden — that surfaces the
# mismatch instead of silently showing a wrong number.
META_TIER_MAP = {
    "TIER_50":          50,
    "TIER_250":        250,
    "TIER_1K":       1_000,
    "TIER_10K":     10_000,
    "TIER_100K":   100_000,
    "TIER_UNLIMITED":   -1,
    "UNLIMITED":        -1,   # legacy spelling, still seen on older WABAs
}

META_TIER_LABEL = {
    "TIER_50":         "Tier 0 — 50 محادثة / 24س",
    "TIER_250":        "Tier 0 — 250 محادثة / 24س",
    "TIER_1K":         "Tier 1 — 1,000",
    "TIER_10K":        "Tier 2 — 10,000",
    "TIER_100K":       "Tier 3 — 100,000",
    "TIER_UNLIMITED":  "Tier 4 — غير محدود",
    "UNLIMITED":       "Tier 4 — غير محدود",
}


def get_current_period_usage(db: Session, tenant_id: int) -> dict:
    """
    Single source of truth for subscription-limit usage + daily analytics.

    ``WhatsAppUsage`` drives enforcement; ``ConversationLog`` is the audit
    trail written on every billable window. We reconcile on read so the
    dashboard never shows 0 when logs exist inside the active period.
    """
    now   = _utcnow()
    ctx   = _usage_period_context(db, tenant_id)
    usage = _get_or_create_usage(db, tenant_id, ctx)
    _reconcile_usage_from_logs(db, tenant_id, usage, ctx)

    svc   = int(usage.service_conversations_used or 0)
    mkt   = int(usage.marketing_conversations_used or 0)
    total = svc + mkt
    limit = int(usage.conversations_limit or 0)
    is_unlimited = limit >= UNLIMITED_LIMIT_SENTINEL
    pct   = (
        0.0 if is_unlimited
        else (round((total / limit) * 100, 1) if limit > 0 else 0.0)
    )
    remaining = (-1 if is_unlimited else max(0, limit - total)) if limit > 0 else 0

    meta_tier = _get_meta_tier(db, tenant_id)
    lifetime  = get_lifetime_conversations(db, tenant_id)
    today_count = get_today_conversations_count(db, tenant_id)

    period_started_at = ctx.get("period_started_at")
    period_ends_at    = ctx.get("period_ends_at")
    if ctx.get("mode") == "calendar":
        period_started_at = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            period_ends_at = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            period_ends_at = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

    def _iso(dt: Optional[datetime]) -> Optional[str]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    reset_label = "01/2/2026"
    if period_ends_at is not None:
        end_naive = period_ends_at if period_ends_at.tzinfo else period_ends_at.replace(tzinfo=timezone.utc)
        reset_label = end_naive.strftime("%d/%m/%Y")

    return {
        "service_conversations_used":          svc,
        "marketing_conversations_used":        mkt,
        "conversations_used":                  total,
        "current_period_conversations_used": total,
        "conversations_limit":               (-1 if is_unlimited else limit),
        "current_period_conversations_limit": (-1 if is_unlimited else limit),
        "remaining_conversations":           remaining,
        "usage_pct":                         pct,
        "exceeded":                          (not is_unlimited and limit > 0 and total >= limit),
        "near_limit":                        (not is_unlimited and limit > 0 and pct >= 70 and total < limit),
        "warning_70":                        (not is_unlimited and limit > 0 and 70 <= pct < 90),
        "warning_90":                        (not is_unlimited and limit > 0 and pct >= 90 and total < limit),
        "marketing_blocked":                 (not is_unlimited and limit > 0 and total >= limit),
        "emergency_stop":                    (not is_unlimited and limit > 0 and total >= int(limit * SERVICE_EMERGENCY_STOP)),
        "unlimited":                         is_unlimited,
        "month":                             now.month,
        "year":                              now.year,
        "reset_date":                        reset_label,
        "period_mode":                       ctx.get("mode"),
        "period_started_at":                 _iso(period_started_at),
        "period_ends_at":                    _iso(period_ends_at),
        "current_period_started_at":         _iso(period_started_at),
        "current_period_ends_at":            _iso(period_ends_at),
        "subscription_id":                   ctx.get("subscription_id"),
        "lifetime_conversations_used":       lifetime,
        "today_conversations_count":         today_count,
        "alert_80_sent":                     usage.alert_80_sent,
        "alert_100_sent":                    usage.alert_100_sent,
        **meta_tier,
    }


def get_usage_this_month(db: Session, tenant_id: int) -> dict:
    """Backwards-compatible alias for ``get_current_period_usage``."""
    return get_current_period_usage(db, tenant_id)


# Tier "stale" horizon. After this many hours without a successful sync from
# the provider we surface ``meta_tier_is_stale=True`` so the UI can show a
# "تحديث الآن" button and a subdued tone instead of pretending the cached
# value is fresh. Default is INTENTIONALLY tight (6h) because tier changes
# happen unannounced from Meta's side and merchants must see the new ceiling
# before sending a campaign that could trip rate limits.
_META_TIER_STALE_HOURS = int(os.environ.get("NAHLA_META_TIER_STALE_HOURS", "6"))


def _meta_tier_source(conn: Any) -> str:
    """Best-effort label for which provider the cached tier came from.

    Reads ``WhatsAppConnection.provider`` (``'meta'`` or ``'dialog360'``).
    Does NOT promise the value is fresh — that's what ``last_synced_at`` and
    ``is_stale`` are for. The string is only displayed to the merchant for
    debuggability ("من أين هذا الرقم؟"), never used as a routing decision.
    """
    provider = (getattr(conn, "provider", None) or "meta").strip().lower()
    if provider in ("dialog360", "360dialog", "d360"):
        return "dialog360"
    return "meta_graph"


def _get_meta_tier(db: Session, tenant_id: int) -> dict:
    """Return Meta messaging tier info for the tenant's WhatsApp connection.

    Includes ``meta_tier_source`` / ``meta_tier_last_synced_at`` /
    ``meta_tier_is_stale`` so the dashboard can show "from where" and "how
    fresh", and trigger a force-refresh when needed without guessing.
    """
    try:
        from models import WhatsAppConnection  # noqa: PLC0415
        conn = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == tenant_id)
            .first()
        )
        if conn and conn.meta_messaging_limit:
            tier_key = conn.meta_messaging_limit
            last = getattr(conn, "meta_tier_updated_at", None)
            last_iso: str | None = None
            is_stale = True
            if last is not None:
                ts = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
                last_iso = ts.isoformat()
                is_stale = (datetime.now(timezone.utc) - ts).total_seconds() > _META_TIER_STALE_HOURS * 3600
            return {
                "meta_messaging_limit":     tier_key,
                "meta_messaging_limit_num": META_TIER_MAP.get(tier_key, 0),
                "meta_tier_label":          META_TIER_LABEL.get(tier_key, tier_key),
                "meta_tier_source":         _meta_tier_source(conn),
                "meta_tier_last_synced_at": last_iso,
                "meta_tier_is_stale":       is_stale,
                "meta_quality_rating":      conn.meta_quality_rating,
            }
    except Exception as exc:
        logger.warning("[WaUsage] _get_meta_tier failed: %s", exc)
    return {
        "meta_messaging_limit":     None,
        "meta_messaging_limit_num": None,
        "meta_tier_label":          None,
        "meta_tier_source":         None,
        "meta_tier_last_synced_at": None,
        "meta_tier_is_stale":       True,
        "meta_quality_rating":      None,
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
            _get_or_create_usage(db, row.tenant_id)
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
                f"⚠️ *استخدمت {threshold} من محادثات واتساب هذا الشهر*\n\n"
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
