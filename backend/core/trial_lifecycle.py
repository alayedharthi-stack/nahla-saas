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


def _iso(dt: Optional[datetime]) -> Optional[str]:
    coerced = _coerce_utc(dt)
    return coerced.isoformat() if coerced else None


def _days_until(end: Optional[datetime]) -> int:
    coerced = _coerce_utc(end)
    if not coerced:
        return 0
    now = datetime.now(timezone.utc)
    remaining = (coerced - now).total_seconds()
    if remaining <= 0:
        return 0
    return max(0, int(remaining / 86400) + 1)


def _days_since(past: Optional[datetime]) -> int:
    coerced = _coerce_utc(past)
    if not coerced:
        return 0
    now = datetime.now(timezone.utc)
    elapsed = (now - coerced).total_seconds()
    if elapsed <= 0:
        return 0
    return max(0, int(elapsed / 86400))


def _effective_sub_ends_at(sub) -> Optional[datetime]:
    if sub is None:
        return None
    ends = _coerce_utc(sub.ends_at)
    if ends:
        return ends
    meta = sub.extra_metadata or {}
    raw_paid = meta.get("paid_at")
    anchor = None
    if raw_paid:
        try:
            anchor = _coerce_utc(datetime.fromisoformat(str(raw_paid)))
        except (TypeError, ValueError):
            anchor = None
    anchor = anchor or _coerce_utc(sub.started_at) or datetime.now(timezone.utc)
    return subscription_period_end(anchor)


def _sub_was_paid(sub) -> bool:
    if sub is None:
        return False
    meta = sub.extra_metadata or {}
    if meta.get("paid_at") or meta.get("moyasar_payment_id"):
        return True
    activated_by = meta.get("activated_by") or meta.get("activation_source") or ""
    return activated_by in ("dashboard", "demo_checkout", "webhook_invoice", "reconcile", "result_page_poll")


def get_latest_paid_subscription(db: Session, tenant_id: int):
    from models import BillingPayment, BillingSubscription  # noqa: PLC0415

    subs = (
        db.query(BillingSubscription)
        .filter(BillingSubscription.tenant_id == tenant_id)
        .order_by(BillingSubscription.id.desc())
        .all()
    )
    for sub in subs:
        if _sub_was_paid(sub):
            return sub

    payment = (
        db.query(BillingPayment)
        .filter(
            BillingPayment.tenant_id == tenant_id,
            BillingPayment.status == "paid",
        )
        .order_by(BillingPayment.paid_at.desc(), BillingPayment.id.desc())
        .first()
    )
    if payment and payment.subscription_id:
        return (
            db.query(BillingSubscription)
            .filter(BillingSubscription.id == payment.subscription_id)
            .first()
        )
    return None


def get_payment_history(db: Session, tenant_id: int, *, limit: int = 10) -> List[Dict[str, Any]]:
    from models import BillingPayment, BillingPlan, BillingSubscription  # noqa: PLC0415

    rows = (
        db.query(BillingPayment)
        .filter(BillingPayment.tenant_id == tenant_id)
        .order_by(BillingPayment.paid_at.desc(), BillingPayment.id.desc())
        .limit(limit)
        .all()
    )
    history: List[Dict[str, Any]] = []
    for row in rows:
        plan_name = "—"
        if row.subscription_id:
            sub = (
                db.query(BillingSubscription)
                .filter(BillingSubscription.id == row.subscription_id)
                .first()
            )
            if sub and sub.plan_id:
                plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
                if plan:
                    plan_name = (plan.extra_metadata or {}).get("name_ar") or plan.name
        history.append({
            "paid_at":    _iso(row.paid_at),
            "plan_name":  plan_name,
            "amount_sar": row.amount_sar,
            "status":     row.status,
            "gateway":    row.gateway or "unknown",
        })
    return history


def _load_plan_row(db: Session, sub) -> tuple:
    from models import BillingPlan  # noqa: PLC0415

    if not sub or not sub.plan_id:
        return None, "", ""
    plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    if not plan:
        return None, "", ""
    meta = plan.extra_metadata or {}
    return plan, meta.get("name_ar") or plan.name, plan.slug


def _lifecycle_headline_ar(
    lifecycle_status: str,
    *,
    plan_name: str,
    trial_end: Optional[str],
    subscription_end: Optional[str],
) -> str:
    trial_date = (trial_end or "")[:10] or "—"
    sub_date = (subscription_end or "")[:10] or "—"
    plan = plan_name or "الباقة"

    if lifecycle_status == "trial_pending_whatsapp":
        return "تجربتك المجانية لم تبدأ بعد — اربط واتساب لبدء التجربة المجانية"
    if lifecycle_status == "trial_active":
        return f"أنت الآن في التجربة المجانية — تنتهي بتاريخ: {trial_date}"
    if lifecycle_status == "trial_expired":
        return f"انتهت تجربتك المجانية بتاريخ: {trial_date} — اختر خطة للاشتراك ومتابعة تشغيل موظف المبيعات الذكي"
    if lifecycle_status == "paid_active":
        return f"اشتراكك في باقة {plan} نشط — ينتهي بتاريخ: {sub_date}"
    if lifecycle_status == "paid_expired":
        return f"انتهى اشتراكك في باقة {plan} بتاريخ: {sub_date} — يرجى التجديد لاستمرار الردود الذكية وموظف المبيعات الذكي"
    return ""


def _lifecycle_status_label_ar(lifecycle_status: str) -> str:
    return {
        "trial_pending_whatsapp": "بانتظار ربط واتساب",
        "trial_active":           "تجربة مجانية",
        "trial_expired":          "انتهت التجربة المجانية",
        "paid_active":            "نشط",
        "paid_expired":           "منتهي",
    }.get(lifecycle_status, "—")


def resolve_billing_lifecycle(
    db: Session,
    tenant_id: int,
    tenant,
    *,
    active_sub=None,
) -> Dict[str, Any]:
    """Decide the merchant-facing lifecycle state for any tenant (trial vs paid, active vs expired)."""
    from core.billing import has_billing_access  # noqa: PLC0415

    trial_info = compute_trial_info(tenant)
    latest_paid_sub = get_latest_paid_subscription(db, tenant_id)
    payments = get_payment_history(db, tenant_id, limit=1)
    has_paid_history = latest_paid_sub is not None or bool(payments)

    record_sub = active_sub or latest_paid_sub
    _, plan_name_ar, plan_slug = _load_plan_row(db, record_sub)

    now = datetime.now(timezone.utc)
    sub_started = _coerce_utc(record_sub.started_at) if record_sub else None
    sub_ends = _effective_sub_ends_at(record_sub) if record_sub else None
    sub_expired = bool(sub_ends and sub_ends <= now) if record_sub else False

    if active_sub:
        lifecycle_status = "paid_active"
        days_remaining = _days_until(sub_ends)
        warning_level = _warning_level(days_remaining, expired=False)
        is_trial = False
        trial_expired = False
        subscription_expired = False
        has_subscription = True
    elif has_paid_history and record_sub:
        lifecycle_status = "paid_expired"
        days_remaining = 0
        warning_level = "expired"
        is_trial = False
        trial_expired = False
        subscription_expired = True
        has_subscription = False
    elif trial_info.get("trial_pending_whatsapp"):
        lifecycle_status = "trial_pending_whatsapp"
        days_remaining = 0
        warning_level = "none"
        is_trial = False
        trial_expired = False
        subscription_expired = False
        has_subscription = False
    elif trial_info.get("is_trial"):
        lifecycle_status = "trial_active"
        days_remaining = trial_info["trial_days_remaining"]
        warning_level = trial_info.get("warning_level", "none")
        is_trial = True
        trial_expired = False
        subscription_expired = False
        has_subscription = False
    elif trial_info.get("trial_expired"):
        lifecycle_status = "trial_expired"
        days_remaining = 0
        warning_level = "expired"
        is_trial = False
        trial_expired = True
        subscription_expired = False
        has_subscription = False
    else:
        lifecycle_status = "trial_expired"
        days_remaining = 0
        warning_level = "expired"
        is_trial = False
        trial_expired = True
        subscription_expired = False
        has_subscription = False

    last_payment = payments[0] if payments else None
    payment_provider = "unknown"
    if last_payment:
        payment_provider = last_payment.get("gateway") or "unknown"
    elif record_sub:
        payment_provider = (record_sub.extra_metadata or {}).get("gateway") or "moyasar"

    headline = _lifecycle_headline_ar(
        lifecycle_status,
        plan_name=plan_name_ar,
        trial_end=trial_info.get("trial_end"),
        subscription_end=_iso(sub_ends),
    )

    expired_since_days = 0
    if lifecycle_status == "paid_expired" and sub_ends:
        expired_since_days = _days_since(sub_ends)
    elif lifecycle_status == "trial_expired":
        trial_end_dt = _coerce_utc(getattr(tenant, "trial_ends_at", None))
        if trial_end_dt:
            expired_since_days = _days_since(trial_end_dt)

    return {
        "lifecycle_status":            lifecycle_status,
        "lifecycle_status_label_ar":   _lifecycle_status_label_ar(lifecycle_status),
        "headline_ar":                 headline,
        "plan_name":                   plan_name_ar or None,
        "plan_slug":                   plan_slug or None,
        "has_subscription":            has_subscription,
        "is_trial":                    is_trial,
        "trial_pending_whatsapp":      lifecycle_status == "trial_pending_whatsapp",
        "trial_expired":               trial_expired,
        "trial_days_remaining":        trial_info["trial_days_remaining"] if is_trial else 0,
        "trial_started_at":            trial_info.get("trial_started_at"),
        "trial_ends_at":               trial_info.get("trial_end"),
        "subscription_started_at":     _iso(sub_started),
        "subscription_ends_at":        _iso(sub_ends),
        "subscription_expired":        subscription_expired,
        "days_remaining":              days_remaining,
        "expired_since_days":          expired_since_days,
        "warning_level":               warning_level,
        "status_reason_ar":            headline,
        "has_paid_subscription_history": has_paid_history,
        "last_payment_at":             last_payment.get("paid_at") if last_payment else None,
        "last_payment_amount":         last_payment.get("amount_sar") if last_payment else 0,
        "payment_provider":            payment_provider,
        "payment_history":             get_payment_history(db, tenant_id),
        "ai_auto_replies_allowed":     has_billing_access(db, tenant_id),
        "manual_replies_allowed":      True,
        "whatsapp_connected":          _tenant_has_connected_whatsapp(db, tenant_id),
        "record_sub":                  record_sub,
        "active_sub":                  active_sub,
    }


def _tenant_salla_integration(db: Session, tenant_id: int):
    from models import Integration  # noqa: PLC0415

    return (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .first()
    )


def resolve_billing_renewal_info(
    db: Session,
    tenant_id: int,
    lifecycle: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Decide how a merchant should renew — Salla App Store vs Nahla direct checkout.

    Platform-wide: do not assume every merchant is Salla-managed.
    """
    salla_integ = _tenant_salla_integration(db, tenant_id)
    cfg = (salla_integ.config or {}) if salla_integ else {}
    billing_status = str(cfg.get("billing_status") or "none").strip().lower()
    salla_sub_id = cfg.get("salla_subscription_id")

    is_salla_managed = bool(
        salla_integ
        and (
            salla_sub_id
            or billing_status in ("active", "trial", "trial_blocked", "cancelled", "failed")
        )
    )

    payment_provider = str(lifecycle.get("payment_provider") or "unknown").lower()
    has_paid_history = bool(lifecycle.get("has_paid_subscription_history"))

    if is_salla_managed:
        billing_channel = "salla"
        renewal_method = "salla_app"
        can_renew_directly = False
        renewal_url = None
    elif payment_provider == "moyasar":
        billing_channel = "moyasar"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"
    elif payment_provider == "manual":
        billing_channel = "manual"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"
    elif has_paid_history:
        billing_channel = "direct"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"
    else:
        billing_channel = "unknown"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"

    return {
        "billing_channel":      billing_channel,
        "renewal_method":       renewal_method,
        "can_renew_directly":   can_renew_directly,
        "renewal_url":          renewal_url,
        "is_salla_managed":     is_salla_managed,
        "campaigns_automations_allowed": lifecycle.get("ai_auto_replies_allowed", False),
    }


def build_billing_status_payload(
    db: Session,
    tenant_id: int,
    tenant,
    *,
    active_sub,
    conversations_used: int,
    usage_data: Dict[str, Any],
    integration_fee_sar: int,
) -> Dict[str, Any]:
    """Unified billing/status response for every merchant on the dashboard."""
    from core.billing import is_launch_discount_active  # noqa: PLC0415

    lifecycle = resolve_billing_lifecycle(db, tenant_id, tenant, active_sub=active_sub)
    record_sub = lifecycle.pop("record_sub")
    active_sub = lifecycle.pop("active_sub")

    payload: Dict[str, Any] = {
        "conversations_used":      conversations_used,
        "conversations_limit":     usage_data["conversations_limit"],
        "usage_pct":               usage_data["usage_pct"],
        "conversations_exceeded":  usage_data["exceeded"],
        "integration_fee_sar":     integration_fee_sar,
        "launch_discount_active":  False,
        "current_price_sar":       0,
        "plan":                    None,
        "status":                  lifecycle["lifecycle_status"],
        "started_at":              lifecycle.get("subscription_started_at"),
        **lifecycle,
    }

    sub_for_plan = active_sub or record_sub
    if sub_for_plan:
        plan, plan_name_ar, _slug = _load_plan_row(db, sub_for_plan)
        if plan:
            meta = plan.extra_metadata or {}
            launch = is_launch_discount_active(sub_for_plan) if active_sub else False
            price = meta.get("launch_price_sar", plan.price_sar) if launch else plan.price_sar
            payload["plan"] = {
                "id":               plan.id,
                "slug":             plan.slug,
                "name":             plan.name,
                "name_ar":          plan_name_ar,
                "price_sar":        plan.price_sar,
                "launch_price_sar": meta.get("launch_price_sar", plan.price_sar),
                "features":         plan.features or [],
                "limits":           plan.limits or {},
            }
            payload["launch_discount_active"] = launch
            payload["current_price_sar"] = int(price)

    payload.update(resolve_billing_renewal_info(db, tenant_id, lifecycle))

    return payload


def audit_tenant_subscription(db: Session, tenant_id: int) -> Dict[str, Any]:
    """
    Read-only audit snapshot for any merchant's billing / trial state.

    Used for operator review after deploy and regression tests. Individual
    tenant IDs in tests (e.g. production regression examples) are fixtures
    only — the resolver applies platform-wide to every merchant.
    """
    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return {"tenant_id": tenant_id, "found": False}

    active_sub = get_tenant_subscription(db, tenant_id)
    latest_paid = get_latest_paid_subscription(db, tenant_id)
    lifecycle = resolve_billing_lifecycle(db, tenant_id, tenant, active_sub=active_sub)
    renewal = resolve_billing_renewal_info(db, tenant_id, lifecycle)
    trial = compute_trial_info(tenant)

    first_wa = _coerce_utc(getattr(tenant, "first_whatsapp_connected_at", None))
    if not first_wa:
        first_wa = _first_whatsapp_connection_at(db, tenant_id)

    raw_sub = active_sub or latest_paid

    try:
        from core.wa_usage import get_usage_this_month  # noqa: PLC0415
        usage_snapshot = get_usage_this_month(db, tenant_id)
    except Exception:
        usage_snapshot = {}

    return {
        "tenant_id": tenant_id,
        "found": True,
        "store_name": tenant.name,
        "tenant_created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "billing_channel": renewal.get("billing_channel"),
        "whatsapp_connected": lifecycle["whatsapp_connected"],
        "whatsapp_status": "connected" if lifecycle["whatsapp_connected"] else "not_connected",
        "first_whatsapp_connected_at": _iso(first_wa),
        "trial_started_at": tenant.trial_started_at.isoformat() if tenant.trial_started_at else None,
        "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
        "subscription_started_at": lifecycle.get("subscription_started_at"),
        "subscription_ends_at": lifecycle.get("subscription_ends_at"),
        "days_remaining": lifecycle.get("days_remaining"),
        "expired_since_days": lifecycle.get("expired_since_days"),
        "subscription_status": tenant.subscription_status,
        "billing_subscription_status": raw_sub.status if raw_sub else None,
        "lifecycle_status": lifecycle["lifecycle_status"],
        "plan_name": lifecycle.get("plan_name"),
        "has_paid_subscription_history": lifecycle.get("has_paid_subscription_history"),
        "last_payment_at": lifecycle.get("last_payment_at"),
        "latest_payment_date": lifecycle.get("last_payment_at"),
        "last_payment_amount": lifecycle.get("last_payment_amount"),
        "latest_payment_amount": lifecycle.get("last_payment_amount"),
        "payment_provider": lifecycle.get("payment_provider"),
        "payment_history": lifecycle.get("payment_history"),
        "trial_info": trial,
        "subscription_expired": lifecycle["subscription_expired"],
        "trial_expired": lifecycle["trial_expired"],
        "ai_auto_replies_allowed": lifecycle["ai_auto_replies_allowed"],
        "campaigns_automations_allowed": renewal.get("campaigns_automations_allowed"),
        "paid_subscription_effective": lifecycle["has_subscription"],
        "manual_replies_allowed": lifecycle["manual_replies_allowed"],
        "dashboard_access_allowed": True,
        "is_salla_managed": renewal.get("is_salla_managed"),
        "renewal_method": renewal.get("renewal_method"),
        "usage_period_mode": usage_snapshot.get("period_mode"),
        "period_started_at": usage_snapshot.get("period_started_at"),
        "period_ends_at": usage_snapshot.get("period_ends_at"),
        "conversations_used_this_period": usage_snapshot.get("conversations_used"),
        "conversations_limit_this_period": usage_snapshot.get("conversations_limit"),
        "lifetime_conversations_used": usage_snapshot.get("lifetime_conversations_used"),
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
