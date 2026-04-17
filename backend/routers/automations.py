"""
routers/automations.py
───────────────────────
Smart automation management + Sales Autopilot endpoints.

Routes:
  GET  /automations                        — list all automations
  PUT  /automations/{id}/toggle            — enable / disable an automation
  PUT  /automations/{id}/config            — update automation config
  POST /automations/autopilot              — master autopilot switch
  POST /automations/events                 — emit an automation event

  GET  /autopilot/status                   — current autopilot state + daily summary
  PUT  /autopilot/settings                 — save autopilot settings
  POST /autopilot/run                      — manually trigger all enabled autopilot jobs
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from models import (  # noqa: E402
    AutomationEvent,
    AutomationExecution,
    Customer,
    Order,
    PredictiveReorderEstimate,
    Product,
    SmartAutomation,
    TenantSettings,
)

# ── Order status label map (Salla + common) ───────────────────────────────────
ORDER_STATUS_LABELS: Dict[str, str] = {
    "pending":           "قيد الانتظار",
    "under_review":      "قيد المراجعة",
    "in_progress":       "قيد المعالجة",
    "processing":        "قيد المعالجة",
    "shipped":           "تم الشحن",
    "out_for_delivery":  "خرج للتوصيل",
    "delivered":         "تم التوصيل",
    "completed":         "مكتمل",
    "cancelled":         "ملغي",
    "refunded":          "مسترجع",
    "payment_pending":   "في انتظار الدفع",
    "ready_for_pickup":  "جاهز للاستلام",
    "on_hold":           "في الانتظار",
    "failed":            "فشل",
    "draft":             "مسودة",
    "cod":               "الدفع عند الاستلام",
}

from core.automations_seed import (
    ENGINE_BY_TYPE as _ENGINE_BY_TYPE,
    ensure_engine_for_tenant as _ensure_engine_for_tenant,
    seed_automations_if_empty as _seed_automations_if_empty,
)
from core.billing import require_billing_access
from core.database import get_db
from core.tenant import (
    DEFAULT_AI,
    DEFAULT_STORE,
    get_or_create_settings,
    get_or_create_tenant,
    merge_defaults,
    resolve_tenant_id,
)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────────
#
# The canonical seed list now lives in core/automations_seed.py. Both this
# router and routers/intelligence.py import from there, guaranteeing that
# every tenant gets the same automations with `trigger_event` pre-populated.

DEFAULT_AUTOPILOT: Dict[str, Any] = {
    "enabled": False,
    "order_status_update": {
        "enabled": True,
        "notify_statuses": ["pending", "shipped", "out_for_delivery", "delivered", "cancelled", "refunded"],
        "template_name": "order_status_update_ar",
    },
    "predictive_reorder": {
        "enabled": True,
        "days_before": 3,
        "consumption_days_default": 45,
        "template_name": "predictive_reorder_reminder_ar",
    },
    "abandoned_cart": {
        "enabled": True,
        "reminder_30min": True,
        "reminder_24h": True,
        "coupon_48h": False,
        "coupon_code": "",
        "template_name": "abandoned_cart_reminder",
    },
    "inactive_recovery": {
        "enabled": True,
        "inactive_days": 60,
        "discount_pct": 15,
        "template_name": "win_back",
    },
}

# ── Summary label map (keyed by AutomationTrigger value) ─────────────────────
#
# Used by `_get_daily_summary` to turn real AutomationExecution rows into the
# `{key, label, count, icon}` items the dashboard expects. Keys MUST match the
# AutomationTrigger enum values — no fake `autopilot_*_sent` strings anymore.

from core.automation_triggers import AutomationTrigger  # noqa: E402

AUTOPILOT_SUMMARY_LABELS: Dict[str, str] = {
    AutomationTrigger.CART_ABANDONED.value:         "سلات متروكة تم التواصل بشأنها",
    AutomationTrigger.CUSTOMER_INACTIVE.value:      "عملاء غير نشطين تم استرجاعهم",
    AutomationTrigger.PREDICTIVE_REORDER_DUE.value: "تذكيرات إعادة طلب أُرسلت",
    AutomationTrigger.VIP_CUSTOMER_UPGRADE.value:   "عملاء VIP كوفئوا",
    AutomationTrigger.PRODUCT_CREATED.value:        "تنبيهات منتجات جديدة أُرسلت",
    AutomationTrigger.PRODUCT_BACK_IN_STOCK.value:  "تنبيهات عودة المنتج للمخزون",
}

AUTOPILOT_SUMMARY_ICONS: Dict[str, str] = {
    AutomationTrigger.CART_ABANDONED.value:         "🛒",
    AutomationTrigger.CUSTOMER_INACTIVE.value:      "💙",
    AutomationTrigger.PREDICTIVE_REORDER_DUE.value: "🔄",
    AutomationTrigger.VIP_CUSTOMER_UPGRADE.value:   "👑",
    AutomationTrigger.PRODUCT_CREATED.value:        "✨",
    AutomationTrigger.PRODUCT_BACK_IN_STOCK.value:  "📦",
}


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class ToggleAutomationIn(BaseModel):
    enabled: bool


class UpdateAutomationConfigIn(BaseModel):
    config: Dict[str, Any]
    template_id: Optional[int] = None


class EmitEventIn(BaseModel):
    event_type: str
    customer_id: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None


class AutopilotSubIn(BaseModel):
    enabled: Optional[bool] = None
    reminder_hours: Optional[int] = None
    auto_cancel_hours: Optional[int] = None
    days_before: Optional[int] = None
    consumption_days_default: Optional[int] = None
    reminder_30min: Optional[bool] = None
    reminder_24h: Optional[bool] = None
    coupon_48h: Optional[bool] = None
    coupon_code: Optional[str] = None
    inactive_days: Optional[int] = None
    discount_pct: Optional[int] = None


class AutopilotSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    order_status_update: Optional[AutopilotSubIn] = None
    cod_confirmation: Optional[AutopilotSubIn] = None   # backward-compat alias
    predictive_reorder: Optional[AutopilotSubIn] = None
    abandoned_cart: Optional[AutopilotSubIn] = None
    inactive_recovery: Optional[AutopilotSubIn] = None


# ── Helper functions ───────────────────────────────────────────────────────────


def _auto_to_dict(a: SmartAutomation) -> Dict[str, Any]:
    return {
        "id": a.id,
        "automation_type": a.automation_type,
        "name": a.name,
        "enabled": a.enabled,
        # 4-engine grouping for the SmartAutomations dashboard. Falls back
        # to the canonical map for legacy rows whose `engine` column was
        # never backfilled (defensive — ensure_engine_for_tenant should
        # already have repaired this on the previous engine cycle).
        "engine": a.engine or _ENGINE_BY_TYPE.get(a.automation_type, "recovery"),
        "config": a.config or {},
        "template_id": a.template_id,
        "template_name": a.template.name if a.template else None,
        "stats_triggered": a.stats_triggered,
        "stats_sent": a.stats_sent,
        "stats_converted": a.stats_converted,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _get_autopilot_enabled(db: Session, tenant_id: int) -> bool:
    return bool(_get_autopilot_settings(db, tenant_id).get("enabled", False))


def _get_autopilot_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Read autopilot config from TenantSettings.extra_metadata with backward compat."""
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    stored: Dict[str, Any] = {}
    legacy_enabled: Optional[bool] = None
    if settings and settings.ai_settings:
        legacy_ai = merge_defaults(settings.ai_settings, DEFAULT_AI)
        if "autopilot_enabled" in legacy_ai:
            legacy_enabled = bool(legacy_ai.get("autopilot_enabled"))
    if settings and settings.extra_metadata:
        stored = settings.extra_metadata.get("autopilot", {})

    merged = dict(DEFAULT_AUTOPILOT)
    if stored:
        merged.update({k: v for k, v in stored.items() if k in DEFAULT_AUTOPILOT})
        for sub in ("order_status_update", "predictive_reorder", "abandoned_cart", "inactive_recovery"):
            if sub in stored and isinstance(stored[sub], dict):
                base = dict(DEFAULT_AUTOPILOT[sub])
                base.update(stored[sub])
                merged[sub] = base
        # Migrate legacy cod_confirmation → order_status_update if present
        if "cod_confirmation" in stored and "order_status_update" not in stored:
            base = dict(DEFAULT_AUTOPILOT["order_status_update"])
            base.update(stored["cod_confirmation"])
            merged["order_status_update"] = base
    elif legacy_enabled is not None:
        merged["enabled"] = legacy_enabled
    if legacy_enabled is not None and "enabled" not in stored:
        merged["enabled"] = legacy_enabled
    return merged


def _save_autopilot_settings(db: Session, tenant_id: int, autopilot: Dict[str, Any]) -> None:
    """Persist autopilot config and keep legacy ai_settings in sync."""
    settings = get_or_create_settings(db, tenant_id)
    extra: Dict[str, Any] = dict(settings.extra_metadata or {})
    extra["autopilot"] = autopilot
    settings.extra_metadata = extra
    ai = merge_defaults(settings.ai_settings, DEFAULT_AI)
    ai["autopilot_enabled"] = bool(autopilot.get("enabled", False))
    settings.ai_settings = ai
    settings.updated_at = datetime.now(timezone.utc)


def _get_daily_summary(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """
    Today's autopilot summary — counted from real, delivered executions.

    Previously this counted fake `AutomationEvent(event_type='autopilot_*_sent',
    processed=True)` rows that `_log_autopilot_event` wrote *without* actually
    sending anything — so the dashboard showed confident success counts for
    messages that never reached a customer. That entire code path is gone.

    The source of truth is now `AutomationExecution.status='sent'`, which is
    only written by `automation_engine._try_execute` after a successful
    `provider_send_message(...)` response.
    """
    from datetime import date  # noqa: PLC0415

    today_start = datetime.combine(date.today(), datetime.min.time())
    rows = (
        db.query(SmartAutomation.trigger_event, sa_func.count(AutomationExecution.id))
        .join(AutomationExecution, AutomationExecution.automation_id == SmartAutomation.id)
        .filter(
            AutomationExecution.tenant_id == tenant_id,
            AutomationExecution.status == "sent",
            AutomationExecution.executed_at >= today_start,
        )
        .group_by(SmartAutomation.trigger_event)
        .all()
    )
    counts_by_trigger = {trigger: int(n) for trigger, n in rows if trigger}

    summary: List[Dict[str, Any]] = []
    for trigger, label in AUTOPILOT_SUMMARY_LABELS.items():
        summary.append({
            "key":   trigger,
            "label": label,
            "count": counts_by_trigger.get(trigger, 0),
            "icon":  AUTOPILOT_SUMMARY_ICONS.get(trigger, "📨"),
        })
    return summary


# NOTE ON THE REMOVED LEGACY EXECUTION PATH
# ─────────────────────────────────────────
# The following four functions used to live here:
#
#   _log_autopilot_event      — wrote an AutomationEvent row with processed=True
#   _job_order_status_update  — looped Orders and called _log_autopilot_event
#   _job_predictive_reorder   — looped PredictiveReorderEstimate and called it
#   _job_abandoned_cart       — looped is_abandoned orders and called it
#   _job_inactive_customers   — looped inactive CustomerProfile and called it
#
# None of them invoked `provider_send_message`. They only simulated sending
# by writing log-style AutomationEvent rows that the daily-summary then
# counted. This gave the dashboard a confident "we sent N messages" number
# for messages that never left our servers.
#
# They are DELETED. Audit trail: git blame this comment for migration date.
# The equivalent real path is:
#   emit_automation_event(tenant_id, AutomationTrigger.<X>.value, customer_id, payload)
# which the engine picks up within ≤60 s and actually sends via WhatsApp.
#
# POST /autopilot/run is retained for dashboard compatibility but is now a
# no-op that explains the change — see run_autopilot() below.


def _placeholder_removed_job(name: str) -> None:  # pragma: no cover - sentinel
    """Kept only so accidental imports of the old names fail loudly."""
    raise RuntimeError(
        f"{name} was deleted in the legacy-autopilot purge. "
        "Use emit_automation_event(tenant_id, AutomationTrigger.<X>.value, ...) instead."
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/automations")
async def list_automations(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _seed_automations_if_empty(db, tenant_id)
    # Repair the 4-engine bucket for any pre-0027 rows (no-op once migrated).
    _ensure_engine_for_tenant(db, tenant_id)
    db.commit()
    autos = (
        db.query(SmartAutomation)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .order_by(SmartAutomation.id)
        .all()
    )
    autopilot = _get_autopilot_enabled(db, tenant_id)
    return {"automations": [_auto_to_dict(a) for a in autos], "autopilot_enabled": autopilot}


# ── Attribution windows (days) per automation type ───────────────────────────
# How long after a message send do we still credit a subsequent order to the
# automation that sent it. Tuned to be conservative — short enough that a
# coincidental purchase doesn't get attributed, long enough that legitimate
# delayed conversions still count.
_ATTRIBUTION_WINDOW_DAYS: Dict[str, int] = {
    "abandoned_cart":     7,    # cart recovery has the strongest signal
    "customer_winback":  14,    # winback is slower-burn
    "vip_upgrade":       30,    # VIP coupons travel further
    "predictive_reorder": 7,
    "new_product_alert":  7,
    "back_in_stock":      7,
}


def _parse_order_total(raw: Optional[str]) -> float:
    """Best-effort parse of the freeform `Order.total` string into SAR."""
    if raw is None:
        return 0.0
    try:
        # Strip currency symbols / Arabic digits / commas before parsing.
        cleaned = "".join(ch for ch in str(raw) if (ch.isdigit() or ch == "."))
        return float(cleaned) if cleaned else 0.0
    except Exception:
        return 0.0


@router.get("/automations/{automation_id}/metrics")
async def get_automation_metrics(
    automation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    days: int = 30,
):
    """
    Per-automation conversion metrics.

    Returns the canonical "Sent / Recovered / Revenue" trio the merchant
    dashboard shows for each automation card. All numbers are derived from
    real `AutomationExecution` rows joined to `Order` — no fake counters.

    Definitions
    ───────────
      sent         : count of AutomationExecution rows with status='sent'
                     in the rolling `days` window. This is the only count
                     that ever increments — `_log_autopilot_event` is gone.

      recovered    : distinct customers from those sent rows who placed at
                     least one order within `_ATTRIBUTION_WINDOW_DAYS` of
                     the send. Joined via customer.phone == orders.customer_info->>phone.

      revenue_sar  : SUM of `orders.total` for all orders that count as
                     `recovered`. Best-effort parse of the freeform
                     `Order.total` string; coupon discounts, refunds, and
                     cancellations are not subtracted here (the dashboard
                     can layer that later from order status).

    The `days` query parameter controls only the *send* window — the
    attribution window per send is fixed by the automation type.
    """
    from datetime import timedelta  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    auto = db.query(SmartAutomation).filter(
        SmartAutomation.id == automation_id,
        SmartAutomation.tenant_id == tenant_id,
    ).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found")

    days = max(1, min(int(days or 30), 365))
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    # ── Sent count (real, from executions only) ──────────────────────────
    sent_executions = (
        db.query(AutomationExecution)
        .filter(
            AutomationExecution.tenant_id   == tenant_id,
            AutomationExecution.automation_id == auto.id,
            AutomationExecution.status      == "sent",
            AutomationExecution.executed_at >= window_start,
        )
        .all()
    )
    sent_count = len(sent_executions)

    # ── Recovered: distinct customers who ordered within attribution window
    # Build {customer_id: earliest_send_time} so we attribute the *first*
    # send per customer (avoids double-counting when multiple cart_abandoned
    # reminders fired for the same cart).
    earliest_send_by_customer: Dict[int, datetime] = {}
    for ex in sent_executions:
        cid = ex.customer_id
        if not cid:
            continue
        existing = earliest_send_by_customer.get(cid)
        if existing is None or ex.executed_at < existing:
            earliest_send_by_customer[cid] = ex.executed_at

    recovered_customer_ids: set[int] = set()
    revenue_sar = 0.0

    if earliest_send_by_customer:
        attribution_days = _ATTRIBUTION_WINDOW_DAYS.get(
            auto.automation_type or "", 7
        )

        # Pull customers + their phones in one go.
        customer_rows = (
            db.query(Customer.id, Customer.phone)
            .filter(
                Customer.tenant_id == tenant_id,
                Customer.id.in_(list(earliest_send_by_customer.keys())),
                Customer.phone.isnot(None),
            )
            .all()
        )
        phone_to_cid: Dict[str, int] = {
            str(phone).strip(): cid for cid, phone in customer_rows if phone
        }

        if phone_to_cid:
            # Pull every order whose customer_info.phone matches one of those
            # customers, then filter in Python by per-customer window. We
            # prefer this to a per-customer SQL loop because the customer
            # set is bounded by `sent_count`.
            phones = list(phone_to_cid.keys())
            orders = (
                db.query(Order)
                .filter(
                    Order.tenant_id == tenant_id,
                    Order.customer_info["phone"].astext.in_(phones),
                )
                .all()
            )
            for o in orders:
                phone = (o.customer_info or {}).get("phone")
                if not phone:
                    continue
                cid = phone_to_cid.get(str(phone).strip())
                if not cid or cid in recovered_customer_ids:
                    continue
                send_time = earliest_send_by_customer.get(cid)
                if send_time is None:
                    continue

                # Order must come AFTER the send and within the attribution
                # window. We don't have a reliable created_at on Order, so
                # use orders.id ordering as a tiebreaker by inspecting
                # extra_metadata.created_at when present, otherwise accept.
                created_raw = (o.extra_metadata or {}).get("created_at")
                order_time: Optional[datetime] = None
                if created_raw:
                    try:
                        order_time = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                    except Exception:
                        order_time = None

                if order_time is not None:
                    send_time_aware = send_time if send_time.tzinfo else send_time.replace(tzinfo=timezone.utc)
                    if order_time < send_time_aware:
                        continue
                    if (order_time - send_time_aware).days > attribution_days:
                        continue

                recovered_customer_ids.add(cid)
                revenue_sar += _parse_order_total(o.total)

    return {
        "automation_id":    auto.id,
        "automation_type":  auto.automation_type,
        "trigger_event":    auto.trigger_event,
        "window_days":      days,
        "attribution_days": _ATTRIBUTION_WINDOW_DAYS.get(auto.automation_type or "", 7),
        "sent":             sent_count,
        "recovered":        len(recovered_customer_ids),
        "revenue_sar":      round(revenue_sar, 2),
    }


# ── 4-engine grouping ────────────────────────────────────────────────────────

ENGINE_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "engine":      "recovery",
        "name":        "محرك استرجاع المبيعات",
        "description": "أتمتات تستعيد المبيعات التي كادت تضيع.",
        "available":   True,
    },
    {
        "engine":      "growth",
        "name":        "محرك نمو المبيعات",
        "description": "أتمتات تخلق مبيعات جديدة من قاعدة عملائك الحالية.",
        "available":   True,
    },
    {
        "engine":      "experience",
        "name":        "محرك تجربة العميل",
        "description": "أتمتات تحسّن تجربة العميل بعد الشراء (قريباً).",
        "available":   False,
    },
    {
        "engine":      "intelligence",
        "name":        "محرك الذكاء والتحليل",
        "description": "تحليل ذكي للعملاء واقتراحات حملات (قريباً).",
        "available":   False,
    },
]


def _aggregate_engine_kpis(
    db: Session,
    tenant_id: int,
    automations: List[SmartAutomation],
    *,
    days: int = 30,
) -> Dict[int, Dict[str, float]]:
    """
    Compute the per-automation `{messages_sent, orders_attributed, revenue_sar}`
    triple for every automation in `automations` over the past `days`. Mirrors
    `get_automation_metrics` exactly so the engines summary can sum the same
    numbers the per-automation cards display.

    Returns `{automation_id: {messages_sent, orders_attributed, revenue_sar}}`.
    """
    from datetime import timedelta  # noqa: PLC0415

    if not automations:
        return {}
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=max(1, min(int(days or 30), 365)))
    auto_ids = [a.id for a in automations]

    sent_rows = (
        db.query(AutomationExecution)
        .filter(
            AutomationExecution.tenant_id == tenant_id,
            AutomationExecution.automation_id.in_(auto_ids),
            AutomationExecution.status == "sent",
            AutomationExecution.executed_at >= window_start,
        )
        .all()
    )
    by_auto: Dict[int, Dict[str, Any]] = {
        a.id: {
            "messages_sent":      0,
            "orders_attributed":  0,
            "revenue_sar":        0.0,
            "_earliest_by_cust":  {},
            "_attribution_days":  _ATTRIBUTION_WINDOW_DAYS.get(a.automation_type or "", 7),
        } for a in automations
    }
    for ex in sent_rows:
        bucket = by_auto.get(ex.automation_id)
        if bucket is None:
            continue
        bucket["messages_sent"] += 1
        cid = ex.customer_id
        if cid:
            existing = bucket["_earliest_by_cust"].get(cid)
            if existing is None or ex.executed_at < existing:
                bucket["_earliest_by_cust"][cid] = ex.executed_at

    # Pull every customer in scope at once so per-automation attribution is one
    # extra query, not N. Same bounded-set assumption as get_automation_metrics.
    all_cids = {cid for b in by_auto.values() for cid in b["_earliest_by_cust"]}
    phone_to_cid: Dict[str, int] = {}
    if all_cids:
        cust_rows = (
            db.query(Customer.id, Customer.phone)
            .filter(
                Customer.tenant_id == tenant_id,
                Customer.id.in_(list(all_cids)),
                Customer.phone.isnot(None),
            )
            .all()
        )
        phone_to_cid = {str(p).strip(): cid for cid, p in cust_rows if p}

    orders_by_phone: Dict[str, List[Order]] = {}
    if phone_to_cid:
        orders = (
            db.query(Order)
            .filter(
                Order.tenant_id == tenant_id,
                Order.customer_info["phone"].astext.in_(list(phone_to_cid.keys())),
            )
            .all()
        )
        for o in orders:
            phone = (o.customer_info or {}).get("phone")
            if not phone:
                continue
            orders_by_phone.setdefault(str(phone).strip(), []).append(o)

    cid_to_phone: Dict[int, str] = {cid: phone for phone, cid in phone_to_cid.items()}

    for auto_id, bucket in by_auto.items():
        attribution_days = int(bucket.pop("_attribution_days"))
        earliest = bucket.pop("_earliest_by_cust")
        attributed: set[int] = set()
        for cid, send_time in earliest.items():
            phone = cid_to_phone.get(cid)
            if not phone:
                continue
            for o in orders_by_phone.get(phone, []):
                created_raw = (o.extra_metadata or {}).get("created_at")
                order_time: Optional[datetime] = None
                if created_raw:
                    try:
                        order_time = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                    except Exception:
                        order_time = None
                if order_time is not None:
                    send_time_aware = send_time if send_time.tzinfo else send_time.replace(tzinfo=timezone.utc)
                    if order_time < send_time_aware:
                        continue
                    if (order_time - send_time_aware).days > attribution_days:
                        continue
                if cid in attributed:
                    continue
                attributed.add(cid)
                bucket["revenue_sar"] += _parse_order_total(o.total)
        bucket["orders_attributed"] = len(attributed)
        bucket["revenue_sar"] = round(bucket["revenue_sar"], 2)

    return by_auto


@router.get("/automations/engines/summary")
async def get_engines_summary(
    request: Request,
    db: Session = Depends(get_db),
    days: int = 30,
):
    """
    Aggregated KPIs for the 4-engine SmartAutopilot dashboard.

    For each of the four engines (recovery, growth, experience, intelligence)
    returns:

      • automations_count / active_automations  — how many rows live in the
        engine and how many of those are toggled on right now
      • enabled                                 — true iff at least one row in
        the engine is enabled (drives the per-engine master switch UI)
      • kpis.messages_sent_30d                  — sum of `AutomationExecution`
        rows with status='sent' across the engine's automations
      • kpis.orders_attributed_30d              — distinct customers who placed
        an order within the per-automation attribution window after a send
      • kpis.revenue_sar_30d                    — SUM of attributed `Order.total`

    The two "coming soon" engines (experience, intelligence) return zero KPIs
    today because they have no automations seeded yet, but the structure is
    the same so the frontend can render them with a placeholder badge.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _seed_automations_if_empty(db, tenant_id)
    _ensure_engine_for_tenant(db, tenant_id)
    db.commit()

    autos: List[SmartAutomation] = (
        db.query(SmartAutomation)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .all()
    )

    by_engine: Dict[str, List[SmartAutomation]] = {}
    for a in autos:
        engine = a.engine or _ENGINE_BY_TYPE.get(a.automation_type, "recovery")
        by_engine.setdefault(engine, []).append(a)

    kpis = _aggregate_engine_kpis(db, tenant_id, autos, days=days)

    autopilot_enabled = _get_autopilot_enabled(db, tenant_id)
    engines_payload: List[Dict[str, Any]] = []
    for definition in ENGINE_DEFINITIONS:
        engine_key = definition["engine"]
        engine_autos = by_engine.get(engine_key, [])
        active_count = sum(1 for a in engine_autos if a.enabled)
        sent = sum(int(kpis.get(a.id, {}).get("messages_sent", 0)) for a in engine_autos)
        attributed = sum(int(kpis.get(a.id, {}).get("orders_attributed", 0)) for a in engine_autos)
        revenue = round(sum(float(kpis.get(a.id, {}).get("revenue_sar", 0.0)) for a in engine_autos), 2)
        engines_payload.append({
            **definition,
            "automations_count":   len(engine_autos),
            "active_automations":  active_count,
            "enabled":             active_count > 0,
            "automation_ids":      [a.id for a in engine_autos],
            "kpis": {
                "messages_sent_30d":      sent,
                "orders_attributed_30d":  attributed,
                "revenue_sar_30d":        revenue,
            },
        })

    return {
        "engines":           engines_payload,
        "autopilot_enabled": autopilot_enabled,
        "window_days":       days,
    }


class EngineToggleIn(BaseModel):
    enabled: bool


@router.put("/automations/engines/{engine}/toggle")
async def toggle_engine(
    engine: str,
    body: EngineToggleIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Enable or disable every automation that belongs to a given engine in one
    request. Returns the updated count + the new state. Refuses unknown
    engine slugs and the two "coming soon" engines (experience, intelligence)
    so the merchant can't accidentally toggle a section that has nothing in
    it.
    """
    tenant_id = resolve_tenant_id(request)
    engine = (engine or "").strip().lower()

    definition = next((d for d in ENGINE_DEFINITIONS if d["engine"] == engine), None)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    if not definition["available"]:
        raise HTTPException(
            status_code=409,
            detail=f"Engine '{engine}' is not available yet (coming soon).",
        )

    if body.enabled:
        require_billing_access(db, int(tenant_id))

    _ensure_engine_for_tenant(db, tenant_id)

    rows: List[SmartAutomation] = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.engine == engine,
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    changed = 0
    for r in rows:
        if r.enabled != body.enabled:
            r.enabled = body.enabled
            r.updated_at = now
            changed += 1
    db.commit()
    return {
        "engine":            engine,
        "enabled":           body.enabled,
        "automations_count": len(rows),
        "automations_changed": changed,
    }


@router.put("/automations/{automation_id}/toggle")
async def toggle_automation(
    automation_id: int,
    body: ToggleAutomationIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    auto = db.query(SmartAutomation).filter(
        SmartAutomation.id == automation_id,
        SmartAutomation.tenant_id == tenant_id,
    ).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found")
    auto.enabled = body.enabled
    auto.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(auto)
    return _auto_to_dict(auto)


@router.put("/automations/{automation_id}/config")
async def update_automation_config(
    automation_id: int,
    body: UpdateAutomationConfigIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    auto = db.query(SmartAutomation).filter(
        SmartAutomation.id == automation_id,
        SmartAutomation.tenant_id == tenant_id,
    ).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found")
    auto.config = body.config
    if body.template_id is not None:
        auto.template_id = body.template_id
    auto.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(auto)
    return _auto_to_dict(auto)


@router.post("/automations/autopilot")
async def set_autopilot(
    body: ToggleAutomationIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable/disable the Marketing Autopilot master switch."""
    tenant_id = resolve_tenant_id(request)
    if body.enabled:
        require_billing_access(db, int(tenant_id))
    current = _get_autopilot_settings(db, tenant_id)
    current["enabled"] = body.enabled
    _save_autopilot_settings(db, tenant_id, current)
    db.commit()
    return {"autopilot_enabled": bool(current["enabled"])}


@router.post("/automations/events")
async def emit_event(body: EmitEventIn, request: Request, db: Session = Depends(get_db)):
    """Emit a system event that automations can react to."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    event = AutomationEvent(
        tenant_id=tenant_id,
        event_type=body.event_type,
        customer_id=body.customer_id,
        payload=body.payload or {},
        processed=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.commit()
    return {"event_id": event.id, "event_type": event.event_type}


@router.get("/autopilot/status")
async def autopilot_status(request: Request, db: Session = Depends(get_db)):
    """Return autopilot settings, today's action summary, and next scheduled run time."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()

    ap = _get_autopilot_settings(db, tenant_id)
    summary = _get_daily_summary(db, tenant_id)

    # `last_run_at` = timestamp of the most recent real send (AutomationExecution
    # with status='sent'). Previously we read the most recent fake
    # `autopilot_*_sent` AutomationEvent row, which could be created even when
    # nothing was sent.
    last_exec = (
        db.query(AutomationExecution)
        .filter(
            AutomationExecution.tenant_id == tenant_id,
            AutomationExecution.status == "sent",
        )
        .order_by(AutomationExecution.executed_at.desc())
        .first()
    )
    last_run_at = last_exec.executed_at.isoformat() if last_exec else None

    return {
        "settings": ap,
        "daily_summary": summary,
        "last_run_at": last_run_at,
        "is_running": False,
    }


@router.put("/autopilot/settings")
async def update_autopilot_settings(
    body: AutopilotSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save autopilot master toggle and sub-automation settings."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    if body.enabled:
        require_billing_access(db, int(tenant_id))

    current = _get_autopilot_settings(db, tenant_id)

    if body.enabled is not None:
        current["enabled"] = body.enabled

    for sub_key, sub_in in [
        # order_status_update: accept either field; body.cod_confirmation is the backward-compat alias
        ("order_status_update", body.order_status_update or body.cod_confirmation),
        ("predictive_reorder",  body.predictive_reorder),
        ("abandoned_cart",      body.abandoned_cart),
        ("inactive_recovery",   body.inactive_recovery),
    ]:
        if sub_in is not None:
            patch = sub_in.model_dump(exclude_none=True)
            current[sub_key] = {**current[sub_key], **patch}

    _save_autopilot_settings(db, tenant_id, current)
    db.commit()
    return {"settings": current}


@router.post("/autopilot/run")
async def run_autopilot(request: Request, db: Session = Depends(get_db)):
    """
    DEPRECATED — the manual "Run Now" trigger is now a no-op.

    This endpoint used to call `_job_order_status_update`, `_job_predictive_reorder`,
    `_job_abandoned_cart`, and `_job_inactive_customers` — a parallel execution
    path that wrote fake `autopilot_*_sent` AutomationEvent rows *without* ever
    invoking `provider_send_message`. It inflated the dashboard's "sent today"
    counters for messages that never actually left our servers.

    The real execution path is now the only path:

        emit_automation_event(...) → AutomationEvent
                                   → automation_engine.process_pending_events()
                                   → AutomationExecution(status='sent')
                                   → provider_send_message(...)

    The engine's background loop runs every ~60 seconds, so nothing needs to
    be manually triggered. This route is kept only for dashboard API
    compatibility and to tell operators about the change.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    ap = _get_autopilot_settings(db, tenant_id)

    if not ap.get("enabled", False):
        return {
            "ran": False,
            "total_actions": 0,
            "breakdown": {},
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "message": "الطيار التلقائي معطّل — فعّله أولاً من الإعدادات",
        }

    total = 0
    results: Dict[str, int] = {}
    return {
        "ran": True,
        "total_actions": total,
        "breakdown": results,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "message": (
            "الطيار يعمل الآن تلقائيًا كل 60 ثانية عبر محرك الأتمتة الموحّد — "
            "لم يعد زر التشغيل اليدوي ضروريًا. الأحداث المتراكمة ستُعالَج في الجولة التالية."
        ),
    }


@router.get("/autopilot/queues")
async def autopilot_queues(request: Request, db: Session = Depends(get_db)):
    """
    Return operational queues for the autopilot dashboard:
    - abandoned_carts: orders flagged as abandoned
    - predictive_reorder: estimates due within the next 7 days, not yet notified
    - order_status_updates: orders whose status changed since the last notification
    """
    from datetime import timedelta

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    now = datetime.now(timezone.utc)

    # ── Abandoned carts ──────────────────────────────────────────────────────
    abandoned = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned == True)
        .order_by(Order.id.desc())
        .limit(50)
        .all()
    )
    cart_items = []
    for o in abandoned:
        ci = o.customer_info or {}
        meta = o.extra_metadata or {}
        cart_items.append({
            "order_id":       o.id,
            "external_id":    o.external_id,
            "customer_name":  ci.get("name", "—"),
            "customer_phone": ci.get("phone") or ci.get("mobile", ""),
            "checkout_url":   o.checkout_url or "",
            "total":          float(o.total or 0),
            "status":         o.status or "abandoned",
            "created_at":     meta.get("created_at", ""),
        })

    # ── Predictive reorder ───────────────────────────────────────────────────
    window_end = now + timedelta(days=7)
    try:
        reorder_rows = (
            db.query(PredictiveReorderEstimate)
            .filter(
                PredictiveReorderEstimate.tenant_id == tenant_id,
                PredictiveReorderEstimate.notified == False,
                PredictiveReorderEstimate.predicted_reorder_date <= window_end,
            )
            .order_by(PredictiveReorderEstimate.predicted_reorder_date.asc())
            .limit(50)
            .all()
        )
    except Exception:
        reorder_rows = []

    reorder_items = []
    for est in reorder_rows:
        customer = (
            db.query(Customer)
            .filter(Customer.id == est.customer_id, Customer.tenant_id == tenant_id)
            .first()
        )
        product = (
            db.query(Product)
            .filter(Product.id == est.product_id, Product.tenant_id == tenant_id)
            .first()
        )
        pred_date = est.predicted_reorder_date
        days_left = 0
        if pred_date:
            if pred_date.tzinfo is None:
                pred_date = pred_date.replace(tzinfo=timezone.utc)
            days_left = max(0, (pred_date - now).days)
        reorder_items.append({
            "estimate_id":    est.id,
            "customer_name":  customer.name if customer else "—",
            "customer_phone": customer.phone if customer else "",
            "product_name":   product.title if product else f"منتج #{est.product_id}",
            "predicted_date": pred_date.isoformat() if pred_date else None,
            "days_remaining": days_left,
            "notified":       est.notified,
        })

    # ── Order status updates ─────────────────────────────────────────────────
    # Show orders whose current status differs from the last notified status
    recent_orders = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id)
        .order_by(Order.id.desc())
        .limit(100)
        .all()
    )
    order_updates = []
    for o in recent_orders:
        meta = o.extra_metadata or {}
        current_status = o.status or "pending"
        last_notified = meta.get("last_notified_status")
        if current_status == last_notified:
            continue
        ci = o.customer_info or {}
        order_updates.append({
            "order_id":              o.id,
            "external_id":           o.external_id,
            "customer_name":         ci.get("name", "—"),
            "customer_phone":        ci.get("phone") or ci.get("mobile", ""),
            "status":                current_status,
            "status_label":          ORDER_STATUS_LABELS.get(current_status, current_status),
            "previous_status":       last_notified,
            "previous_status_label": ORDER_STATUS_LABELS.get(last_notified, last_notified) if last_notified else None,
            "created_at":            meta.get("created_at", ""),
        })

    return {
        "abandoned_carts":      cart_items,
        "predictive_reorder":   reorder_items,
        "order_status_updates": order_updates,
    }
