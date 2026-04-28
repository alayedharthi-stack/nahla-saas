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
    ensure_order_notifications_automation as _ensure_order_notifications_automation,
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


def _sync_automation_catalog_for_tenant(db: Session, tenant_id: int) -> None:
    """Seed catalogue additions + guarantee ``order_notifications`` + repair ``engine``."""
    _seed_automations_if_empty(db, tenant_id)
    _ensure_order_notifications_automation(db, tenant_id)
    _ensure_engine_for_tenant(db, tenant_id)


# ── Feature flags (process-level, runtime-readable) ───────────────────────────
def _manual_retry_enabled() -> bool:
    """
    Switch for the abandoned-cart manual retry button.

    Default: **on**. Set ``AUTOPILOT_ENABLE_MANUAL_RETRY=false`` to hide.
    """
    val = str(os.getenv("AUTOPILOT_ENABLE_MANUAL_RETRY", "true")).strip().lower()
    return val not in {"0", "false", "no", "off"}


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
        # Three-stage recovery workflow (managed end-to-end by the
        # `abandoned_cart` SmartAutomation row + the cart_followups
        # sweeper). The merchant sees one toggle per stage in the
        # dashboard, but execution is unified in the engine.
        #   • reminder_30min  — stage 1, friendly nudge, no discount.
        #   • reminder_6h     — stage 2, "need help?", no discount.
        #   • coupon_24h      — stage 3, optional last-chance coupon.
        # The legacy `coupon_48h` field is RETIRED and intentionally
        # absent here; merchants who saved it under the old shape get
        # it migrated to `coupon_24h` on next read (see
        # `_migrate_legacy_abandoned_cart_settings`).
        "enabled": True,
        "reminder_30min": True,
        "reminder_6h":    True,
        "coupon_24h":     False,
        "coupon_code":    "",
        "template_name":  "abandoned_cart_recovery_ar",
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
    reminder_6h: Optional[bool] = None
    coupon_24h: Optional[bool] = None
    # Legacy shape — accepted for backward compat with merchants who
    # saved settings before the 3-stage rollout. Migrated to
    # `reminder_6h` / `coupon_24h` by `_migrate_legacy_abandoned_cart_settings`.
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


def _auto_to_dict(a: SmartAutomation, db: Optional[Session] = None, tenant_id: Optional[int] = None) -> Dict[str, Any]:
    enabled = bool(a.enabled)
    # Order notifications toggle is mirrored in TenantSettings autopilot.order_status_update
    # so merchants see one consistent switch with legacy autopilot payloads.
    if (
        getattr(a, "automation_type", None) == "order_notifications"
        and db is not None
        and tenant_id is not None
    ):
        ap_osu = _get_autopilot_settings(db, int(tenant_id)).get("order_status_update") or {}
        enabled = bool(ap_osu.get("enabled", enabled))
    return {
        "id": a.id,
        "automation_type": a.automation_type,
        "name": a.name,
        "enabled": enabled,
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
    merged["abandoned_cart"] = _migrate_legacy_abandoned_cart_settings(
        merged.get("abandoned_cart") or {}
    )
    return merged


def _migrate_legacy_abandoned_cart_settings(sub: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translate the pre-3-stage abandoned-cart shape into the new shape.

    The dashboard used to expose `reminder_24h` and `coupon_48h` because
    the old engine only wired stage 1 → no follow-up + an optional 48h
    coupon. The new 3-stage workflow uses `reminder_6h` (stage 2) and
    `coupon_24h` (stage 3) instead. Merchants who saved the legacy
    shape get an in-memory rewrite on every read so:

      • their previous "send a coupon" intent is preserved
        (legacy `coupon_48h=True` → new `coupon_24h=True`);
      • the absence of `reminder_6h` defaults to ON (the workflow's
        own default) so an upgrade never silently disables a stage;
      • the legacy keys are stripped from the response so the dashboard
        never renders the retired toggles.

    Pure function — does NOT write back to the DB. The next save from
    the merchant's UI will persist the new shape.
    """
    if not isinstance(sub, dict):
        return dict(DEFAULT_AUTOPILOT["abandoned_cart"])

    out = dict(sub)
    # `reminder_24h` was the legacy stage-2 toggle (24h reminder, no
    # coupon). The new workflow places the second nudge at 6h. We map
    # the merchant's intent ("yes, do send a second reminder") onto
    # the new field; merchants who turned the old toggle off keep
    # second-stage nudges off too.
    if "reminder_24h" in out and "reminder_6h" not in out:
        out["reminder_6h"] = bool(out.get("reminder_24h"))
    out.pop("reminder_24h", None)

    # Same for the old coupon toggle.
    if "coupon_48h" in out and "coupon_24h" not in out:
        out["coupon_24h"] = bool(out.get("coupon_48h"))
    out.pop("coupon_48h", None)

    # Backfill defaults for any keys the merchant never had.
    base = dict(DEFAULT_AUTOPILOT["abandoned_cart"])
    base.update(out)
    return base


def _save_autopilot_settings(db: Session, tenant_id: int, autopilot: Dict[str, Any]) -> None:
    """Persist autopilot config and keep legacy ai_settings in sync."""
    from sqlalchemy.orm.attributes import flag_modified
    settings = get_or_create_settings(db, tenant_id)
    extra: Dict[str, Any] = dict(settings.extra_metadata or {})
    extra["autopilot"] = autopilot
    settings.extra_metadata = extra
    flag_modified(settings, "extra_metadata")
    ai = merge_defaults(settings.ai_settings, DEFAULT_AI)
    ai["autopilot_enabled"] = bool(autopilot.get("enabled", False))
    settings.ai_settings = ai
    flag_modified(settings, "ai_settings")
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
    _sync_automation_catalog_for_tenant(db, tenant_id)
    db.commit()
    autos = (
        db.query(SmartAutomation)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .order_by(SmartAutomation.id)
        .all()
    )
    autopilot = _get_autopilot_enabled(db, tenant_id)
    return {"automations": [_auto_to_dict(a, db, tenant_id) for a in autos], "autopilot_enabled": autopilot}


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
    "order_notifications": 7,
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
    _sync_automation_catalog_for_tenant(db, tenant_id)
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
    if auto.automation_type == "order_notifications":
        cur_ap = _get_autopilot_settings(db, tenant_id)
        osu = dict(cur_ap.get("order_status_update") or DEFAULT_AUTOPILOT["order_status_update"])
        osu["enabled"] = bool(body.enabled)
        cur_ap["order_status_update"] = osu
        _save_autopilot_settings(db, tenant_id, cur_ap)
    db.commit()
    db.refresh(auto)
    return _auto_to_dict(auto, db, tenant_id)


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
    return _auto_to_dict(auto, db, tenant_id)


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
    _sync_automation_catalog_for_tenant(db, tenant_id)
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
        # Surface the manual-retry feature flag so the dashboard can
        # hide the temporary "إعادة الإرسال" button cleanly when it's
        # turned off, instead of relying on an env var leaked into the
        # frontend bundle (which would require a redeploy to flip).
        "manual_retry_enabled": _manual_retry_enabled(),
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
    _sync_automation_catalog_for_tenant(db, tenant_id)

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

    # Keep SmartAutomation order_notifications.enabled aligned with autopilot.order_status_update.enabled.
    osu_en = (current.get("order_status_update") or {}).get("enabled")
    if osu_en is not None:
        sync_auto = (
            db.query(SmartAutomation)
            .filter(
                SmartAutomation.tenant_id == tenant_id,
                SmartAutomation.automation_type == "order_notifications",
            )
            .first()
        )
        if sync_auto is not None and sync_auto.enabled != bool(osu_en):
            sync_auto.enabled = bool(osu_en)
            sync_auto.updated_at = datetime.now(timezone.utc)
            db.commit()

    return {"settings": current}


@router.get("/autopilot/cart-recovery/readiness")
async def cart_recovery_readiness(request: Request, db: Session = Depends(get_db)):
    """Check whether all 3 cart_recovery templates are APPROVED.

    Uses the full resolver chain (layers a-d) so templates that exist
    but aren't explicitly bound to a step yet get auto-bound on the
    fly. This matches exactly what the engine does at send time.
    """
    tenant_id = resolve_tenant_id(request)

    from core.service_template_resolver import resolve_template_for_send  # noqa: PLC0415
    from models import WhatsAppTemplate  # noqa: PLC0415

    steps_status = []
    all_ready = True

    step_labels = {
        1: "التذكير الأول",
        2: "المتابعة",
        3: "التذكير الأخير مع كوبون",
    }

    for step_num in (1, 2, 3):
        # resolve_template_for_send walks layers a-d: strict binding,
        # inactive match, nahla_source_key, config template_name.
        # For cart_recovery it skips cross-step fallback (layers e-f),
        # so each step must have its own template.
        tpl = resolve_template_for_send(
            db, tenant_id, "cart_recovery", step_num,
        )

        if tpl and tpl.status == "APPROVED":
            steps_status.append({
                "step": step_num,
                "label": step_labels[step_num],
                "ready": True,
                "template_id": tpl.id,
                "template_name": tpl.name,
                "status": "APPROVED",
            })
        else:
            all_ready = False
            # Look for ANY template bound to this step (even non-APPROVED)
            # so we can tell the merchant what state it's in.
            any_tpl = (
                db.query(WhatsAppTemplate)
                .filter(
                    WhatsAppTemplate.tenant_id == tenant_id,
                    WhatsAppTemplate.service_key == "cart_recovery",
                    WhatsAppTemplate.step_number == step_num,
                )
                .order_by(WhatsAppTemplate.updated_at.desc())
                .first()
            )
            if not any_tpl:
                # Also check by name pattern / nahla_source_key without
                # requiring the binding, so we catch templates that exist
                # but haven't been auto-bound yet.
                from services.whatsapp_templates.nahla_templates import NAHLA_TEMPLATES  # noqa: PLC0415
                lib_keys = [
                    t["key"] for t in NAHLA_TEMPLATES
                    if t.get("service_key") == "cart_recovery"
                    and t.get("step_number") == step_num
                ]
                if lib_keys:
                    any_tpl = (
                        db.query(WhatsAppTemplate)
                        .filter(
                            WhatsAppTemplate.tenant_id == tenant_id,
                            WhatsAppTemplate.nahla_source_key.in_(lib_keys),
                        )
                        .order_by(WhatsAppTemplate.updated_at.desc())
                        .first()
                    )

            if any_tpl:
                steps_status.append({
                    "step": step_num,
                    "label": step_labels[step_num],
                    "ready": False,
                    "template_id": any_tpl.id,
                    "template_name": any_tpl.name,
                    "status": any_tpl.status or "UNKNOWN",
                    "reason": "not_approved",
                })
            else:
                steps_status.append({
                    "step": step_num,
                    "label": step_labels[step_num],
                    "ready": False,
                    "template_id": None,
                    "template_name": None,
                    "status": "MISSING",
                    "reason": "no_template",
                })

    try:
        db.commit()
    except Exception:
        db.rollback()

    return {
        "all_ready": all_ready,
        "steps": steps_status,
    }


@router.get("/autopilot/readiness")
async def all_automations_readiness(request: Request, db: Session = Depends(get_db)):
    """Return template readiness for every automation type in a single call.

    Each entry in the result map has:
      all_ready: bool   — true only when every required template is APPROVED
      steps: list       — one entry per template with status details
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _sync_automation_catalog_for_tenant(db, tenant_id)
    db.commit()

    from core.service_template_resolver import resolve_template_for_send  # noqa: PLC0415
    from models import WhatsAppTemplate  # noqa: PLC0415

    # ── Template requirements per automation type ──────────────────────────
    # Maps automation_type → list of "slot" descriptors. Each slot can be
    # satisfied by ANY of the following (first APPROVED match wins):
    #   1. legacy_names: exact `WhatsAppTemplate.name` matches (seed names)
    #   2. library_keys: matches via `nahla_source_key` (Nahla library import)
    #   3. service_key:  any APPROVED template bound to that service_key
    #
    # The legacy seed used hard-coded names like "win_back_ar" — but the
    # actual library import names are hashed (e.g. `nahla_comeback_discount_c7d3`).
    # That is why this endpoint used to report "غير موجود" for templates
    # that were demonstrably APPROVED in the merchant's account.
    # "abandoned_cart" is handled separately above with full step resolution.
    TEMPLATE_REQUIREMENTS: dict = {
        "predictive_reorder": [
            {
                "label": "قالب تذكير إعادة الطلب",
                "legacy_names": ["predictive_reorder_reminder_ar"],
                "library_keys": ["predictive_reorder_reminder", "reorder_quick_link"],
                "service_key":  "predictive_reorder",
            },
        ],
        "customer_winback": [
            {
                "label": "قالب استرجاع العميل",
                "legacy_names": ["win_back_ar", "win_back_en"],
                "library_keys": ["comeback_discount"],
                "service_key":  "customer_retention",
            },
        ],
        "vip_upgrade": [
            {
                "label": "قالب مكافأة VIP",
                "legacy_names": ["vip_reward_ar", "vip_reward_en"],
                "library_keys": ["vip_exclusive"],
                "service_key":  "vip_rewards",
            },
        ],
        "new_product_alert": [
            {
                "label": "قالب المنتجات الجديدة",
                "legacy_names": ["new_arrivals"],
                "library_keys": ["new_arrivals"],
            },
        ],
        "back_in_stock": [
            {
                "label": "قالب عودة المخزون",
                "legacy_names": ["back_in_stock_ar", "back_in_stock_en"],
                "library_keys": ["back_in_stock_alert"],
                "service_key":  "back_in_stock",
            },
        ],
        "unpaid_order_reminder": [
            {
                "label": "قالب الطلب غير المدفوع",
                "legacy_names": ["unpaid_order_reminder_ar", "unpaid_order_reminder_en"],
                "library_keys": ["payment_reminder"],
                "service_key":  "payment_reminder",
            },
        ],
        "cod_confirmation": [
            {
                "label": "قالب تأكيد الدفع عند الاستلام",
                "legacy_names": ["cod_confirmation_reminder_ar", "cod_confirmation_reminder_en"],
                "library_keys": ["cod_confirmation", "cod_reminder_before_shipping"],
                "service_key":  "cod_confirmation",
            },
        ],
        "seasonal_offer": [
            {
                "label": "قالب العروض الموسمية",
                "legacy_names": ["seasonal_offer_ar", "seasonal_offer_en"],
                "library_keys": ["seasonal_offer_template"],
                "service_key":  "seasonal_offers",
            },
        ],
        "salary_payday_offer": [
            {
                "label": "قالب عرض الراتب",
                "legacy_names": ["salary_payday_offer_ar", "salary_payday_offer_en"],
                "library_keys": ["salary_payday_offer_template"],
                "service_key":  "salary_payday_offers",
            },
        ],
        # Nahla library service families linked to store order lifecycle notices.
        "order_notifications": [
            {
                "label": "المرحلة 1 — تأكيد الطلب والملخص",
                "legacy_names": ["order_status_update_ar"],
                "library_keys": ["post_purchase_thanks", "order_summary", "order_confirmed"],
                "service_key":  "order_confirmation",
            },
            {
                "label": "المرحلة 2 — الشحن والتتبع",
                "legacy_names": [],
                "library_keys": ["shipping_update", "order_out_for_delivery"],
                "service_key":  "shipping_tracking",
            },
            {
                "label": "المرحلة 3 — التسليم وتجربة ما بعد الشراء",
                "legacy_names": [],
                "library_keys": ["order_delivered", "review_request"],
                "service_key":  "post_delivery",
            },
            {
                "label": "المرحلة 4 — تأكيد COD",
                "legacy_names": [],
                "library_keys": ["cod_confirmation", "cod_reminder_before_shipping"],
                "service_key":  "cod_confirmation",
            },
        ],
    }

    STEP_LABELS_AR: dict = {
        "predictive_reorder_reminder_ar": "قالب تذكير إعادة الطلب (AR)",
        "win_back_ar":                    "قالب استرجاع العميل (AR)",
        "win_back_en":                    "قالب استرجاع العميل (EN)",
        "vip_reward_ar":                  "قالب مكافأة VIP (AR)",
        "vip_reward_en":                  "قالب مكافأة VIP (EN)",
        "new_arrivals":                   "قالب المنتجات الجديدة",
        "back_in_stock_ar":               "قالب عودة المخزون (AR)",
        "back_in_stock_en":               "قالب عودة المخزون (EN)",
        "unpaid_order_reminder_ar":       "قالب الطلب غير المدفوع (AR)",
        "unpaid_order_reminder_en":       "قالب الطلب غير المدفوع (EN)",
        "cod_confirmation_reminder_ar":   "قالب تأكيد الدفع عند الاستلام (AR)",
        "cod_confirmation_reminder_en":   "قالب تأكيد الدفع عند الاستلام (EN)",
        "seasonal_offer_ar":              "قالب العروض الموسمية (AR)",
        "seasonal_offer_en":              "قالب العروض الموسمية (EN)",
        "salary_payday_offer_ar":         "قالب عرض الراتب (AR)",
        "salary_payday_offer_en":         "قالب عرض الراتب (EN)",
    }

    result: dict = {}

    # ── Handle abandoned_cart (service_key resolver) ───────────────────────
    cart_steps_status = []
    cart_all_ready = True
    cart_step_labels = {
        1: "التذكير الأول",
        2: "المتابعة",
        3: "التذكير الأخير مع كوبون",
    }
    for step_num in (1, 2, 3):
        tpl = resolve_template_for_send(db, tenant_id, "cart_recovery", step_num)
        if tpl and tpl.status == "APPROVED":
            cart_steps_status.append({
                "step": step_num,
                "label": cart_step_labels[step_num],
                "ready": True,
                "template_name": tpl.name,
                "status": "APPROVED",
            })
        else:
            cart_all_ready = False
            any_tpl = (
                db.query(WhatsAppTemplate)
                .filter(
                    WhatsAppTemplate.tenant_id == tenant_id,
                    WhatsAppTemplate.service_key == "cart_recovery",
                    WhatsAppTemplate.step_number == step_num,
                )
                .order_by(WhatsAppTemplate.updated_at.desc())
                .first()
            )
            cart_steps_status.append({
                "step": step_num,
                "label": cart_step_labels[step_num],
                "ready": False,
                "template_name": any_tpl.name if any_tpl else None,
                "status": (any_tpl.status or "UNKNOWN") if any_tpl else "MISSING",
                "reason": "not_approved" if any_tpl else "no_template",
            })

    result["abandoned_cart"] = {
        "all_ready": cart_all_ready,
        "steps": cart_steps_status,
    }

    # ── Handle all template_name-based automations ──────────────────────────
    def _find_approved_for_slot(slot: dict) -> Optional["WhatsAppTemplate"]:  # noqa: F821
        """Try every match strategy for a slot and return the first APPROVED
        template (or any matching template if none is APPROVED yet, so we
        can surface its real status to the merchant).

        Strategies tried in order, all scoped to APPROVED first:
          1. exact `name` match against legacy_names
          2. `nahla_source_key` match against library_keys
          3. `service_key` match (any APPROVED template under that service)
        Falls back to the same lookups without the APPROVED filter so the
        merchant sees PENDING / REJECTED templates instead of "MISSING".
        """
        legacy_names = slot.get("legacy_names") or []
        library_keys = slot.get("library_keys") or []
        service_key  = slot.get("service_key")

        def _q():
            return db.query(WhatsAppTemplate).filter(
                WhatsAppTemplate.tenant_id == tenant_id,
            )

        approved_filter = WhatsAppTemplate.status == "APPROVED"

        for status_filter in (approved_filter, None):
            if legacy_names:
                q = _q().filter(WhatsAppTemplate.name.in_(legacy_names))
                if status_filter is not None:
                    q = q.filter(status_filter)
                tpl = q.order_by(WhatsAppTemplate.updated_at.desc()).first()
                if tpl:
                    return tpl

            if library_keys:
                q = _q().filter(WhatsAppTemplate.nahla_source_key.in_(library_keys))
                if status_filter is not None:
                    q = q.filter(status_filter)
                tpl = q.order_by(WhatsAppTemplate.updated_at.desc()).first()
                if tpl:
                    return tpl

            if service_key:
                q = _q().filter(WhatsAppTemplate.service_key == service_key)
                if status_filter is not None:
                    q = q.filter(status_filter)
                tpl = (
                    q.order_by(
                        WhatsAppTemplate.is_active.desc(),
                        WhatsAppTemplate.updated_at.desc(),
                    )
                    .first()
                )
                if tpl:
                    return tpl

        return None

    for auto_type, slots in TEMPLATE_REQUIREMENTS.items():
        steps: List[dict] = []
        all_ready = True
        for slot in slots:
            tpl = _find_approved_for_slot(slot)
            label = slot.get("label") or (slot.get("legacy_names") or ["—"])[0]
            if tpl and tpl.status == "APPROVED":
                steps.append({
                    "label": label,
                    "template_name": tpl.name,
                    "ready": True,
                    "status": "APPROVED",
                })
            else:
                all_ready = False
                steps.append({
                    "label": label,
                    "template_name": tpl.name if tpl else (slot.get("legacy_names") or [None])[0],
                    "ready": False,
                    "status": (tpl.status or "UNKNOWN") if tpl else "MISSING",
                    "reason": "not_approved" if tpl else "no_template",
                })
        result[auto_type] = {"all_ready": all_ready, "steps": steps}

    try:
        db.commit()
    except Exception:
        db.rollback()

    return result


@router.get("/automations/governor/log")
async def get_governor_log(
    request: Request,
    db: Session = Depends(get_db),
    customer_id: Optional[int] = None,
    limit: int = 50,
):
    """
    سجل Global Send Governor — يُظهر للتاجر كل حالة منع أو تأجيل
    مع السبب بالعربي ومقترح الحل.

    الفلترة الاختيارية: customer_id لعرض سجل عميل بعينه.
    """
    tenant_id = resolve_tenant_id(request)
    from core.send_governor import get_governor_log as _gov_log  # noqa: PLC0415
    rows = _gov_log(db, tenant_id, customer_id=customer_id, limit=min(limit, 200))
    return {"items": rows, "count": len(rows)}


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
    # Source of truth: ``Order.is_abandoned`` rows for this tenant. These
    # come from two paths:
    #   1) ``StoreSyncService.sync_abandoned_carts`` polling Salla's
    #      /admin/v2/carts endpoint on every full_sync.
    #   2) Real-time ``abandoned.cart`` Salla webhooks routed through
    #      ``StoreSyncService.handle_abandoned_cart_webhook``.
    # If both paths are dark we surface that as a clear empty state — and
    # the /admin/debug/abandoned-carts-sync endpoint can be used to confirm
    # which stage of the pipeline broke.
    abandoned = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned == True)
        .order_by(Order.id.desc())
        .limit(50)
        .all()
    )

    # Pull recovery progress for the whole batch in one helper call so the
    # queue page can answer "who got reminder #1/#2/#3?", "whose reminder
    # is pending right now?", "whose reminder failed and why?", "who
    # converted?" without N+1 round-trips. The helper joins
    # AutomationEvent + AutomationExecution + the cancel-on-purchase
    # markers stored on event payloads — see services/cart_recovery_status.
    from services.cart_recovery_status import summarise_for_orders  # noqa: PLC0415

    recovery_summaries = summarise_for_orders(db, tenant_id, abandoned)

    cart_items = []
    for o in abandoned:
        ci = o.customer_info or {}
        meta = o.extra_metadata or {}
        try:
            total_val = float(o.total or 0)
        except (TypeError, ValueError):
            total_val = 0.0
        cart_items.append({
            "order_id":       o.id,
            "external_id":    o.external_id,
            "customer_name":  ci.get("name") or o.customer_name or "—",
            "customer_phone": ci.get("phone") or ci.get("mobile") or "",
            "checkout_url":   o.checkout_url or "",
            "total":          total_val,
            "status":         o.status or "abandoned",
            "created_at":     meta.get("created_at", "") or meta.get("abandoned_at", ""),
            "abandoned_at":   meta.get("abandoned_at") or meta.get("created_at", ""),
            # Recovery progress — derived per-cart, never null. See
            # services/cart_recovery_status.RECOVERY_STATUS_* for the
            # taxonomy used by the dashboard badge.
            "recovery":       recovery_summaries.get(o.id) or {
                "status":              "no_recovery",
                "steps_sent":          0,
                "steps_failed":        0,
                "last_sent_at":        None,
                "last_status":         None,
                "last_error":          None,
                "last_failure_code":   None,
                "last_failure_label":  None,
                "next_pending_at":     None,
                "converted_at":        None,
                "cancel_reason":       None,
                "recovery_event_id":   None,
            },
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

    # ── Pending payment orders ───────────────────────────────────────────────
    # Real orders (not abandoned carts) whose payment has not been completed.
    # Mirrors the status set used by the `unpaid_order_reminder` sweeper in
    # automation_emitters so the queue and the automation act on the same rows.
    # A 15-minute grace period keeps freshly created orders off the list.
    _PENDING_PAY_STATUSES = frozenset({
        "pending", "pending_payment", "payment_pending",
        "awaiting_payment", "draft", "new",
    })
    grace = timedelta(minutes=15)

    def _resolve_name(ci: dict, fallback: Optional[str]) -> str:
        """Resolve customer display name from customer_info JSON.

        Salla stores first_name / last_name without a composite 'name' key in
        older synced rows.  We build it on the fly so the dashboard is never
        blank for these orders.
        """
        name = ci.get("name") or ""
        if not name:
            first = (ci.get("first_name") or "").strip()
            last  = (ci.get("last_name")  or "").strip()
            name  = (first + " " + last).strip()
        return name or (fallback or "").strip() or "—"

    pending_payment_items = []
    for o in (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned.is_(False))
        .order_by(Order.id.desc())
        .limit(100)
        .all()
    ):
        raw = (o.status or "").strip().lower()
        if raw not in _PENDING_PAY_STATUSES:
            continue
        meta = o.extra_metadata or {}
        cstr = meta.get("created_at")
        if cstr:
            try:
                cdt = datetime.fromisoformat(str(cstr).replace("Z", "+00:00"))
                if not cdt.tzinfo:
                    cdt = cdt.replace(tzinfo=timezone.utc)
                if (now - cdt) < grace:
                    continue
            except Exception:
                pass
        ci = o.customer_info or {}
        # Automated reminders are tracked under `unpaid_reminders` by the
        # emitter; manual reminders (sent from the orders dashboard) land
        # under `payment_reminders`. We surface the combined count and derive
        # `current_stage` from the automated tracker so the merchant sees
        # which escalation step has been reached.
        unpaid_reminders: list = list(meta.get("unpaid_reminders") or [])
        manual_reminders: list = list(meta.get("payment_reminders") or [])
        reminders_sent = len(unpaid_reminders) + len(manual_reminders)
        # The last emitted stage index (0-based); -1 means not yet emitted.
        last_step_idx: int = max(
            (int(r.get("step_idx", -1)) for r in unpaid_reminders),
            default=-1,
        )
        last_emitted_at: Optional[str] = None
        for r in reversed(unpaid_reminders):
            if r.get("emitted_at"):
                last_emitted_at = r["emitted_at"]
                break
        if not last_emitted_at and manual_reminders:
            last_emitted_at = meta.get("last_reminder_at")
        pending_payment_items.append({
            "order_id":         o.id,
            "external_id":      o.external_id,
            "order_number":     o.external_order_number or o.external_id or f"#{o.id}",
            "customer_name":    _resolve_name(ci, o.customer_name),
            "customer_phone":   ci.get("phone") or ci.get("mobile") or "",
            "checkout_url":     o.checkout_url or "",
            "total":            float(o.total or 0),
            "status":           raw,
            "created_at":       meta.get("created_at", ""),
            "reminders_sent":   reminders_sent,
            "last_reminder_at": last_emitted_at,
            "current_stage":    last_step_idx + 1,  # 0 = no reminder yet, 1-3 = stage reached
        })

    # ── COD pending confirmation orders ──────────────────────────────────────
    # Orders waiting for the customer to confirm a Cash-on-Delivery purchase.
    # Mirrors the status set used by `automation_emitters.scan_cod_confirmations`
    # and is disjoint from `_PENDING_PAY_STATUSES` by design — the same order
    # never appears in both lists.
    _COD_PENDING_STATUSES = frozenset({
        "pending_confirmation", "awaiting_confirmation",
        "under_review", "in_review",
    })
    cod_pending_items = []
    for o in (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned.is_(False))
        .order_by(Order.id.desc())
        .limit(100)
        .all()
    ):
        raw = (o.status or "").strip().lower()
        if raw not in _COD_PENDING_STATUSES:
            continue
        meta = o.extra_metadata or {}
        ci = o.customer_info or {}
        # COD reminders are tracked in `cod_reminders` by
        # automation_emitters.scan_cod_confirmations.
        cod_reminders: list = list(meta.get("cod_reminders") or [])
        cod_last_reminder_at: Optional[str] = None
        for r in reversed(cod_reminders):
            if r.get("emitted_at"):
                cod_last_reminder_at = r["emitted_at"]
                break
        cod_pending_items.append({
            "order_id":           o.id,
            "external_id":        o.external_id,
            "order_number":       o.external_order_number or o.external_id or f"#{o.id}",
            "customer_name":      _resolve_name(ci, o.customer_name),
            "customer_phone":     ci.get("phone") or ci.get("mobile") or "",
            "total":              float(o.total or 0),
            "status":             raw,
            "created_at":         meta.get("created_at", ""),
            "reminders_sent":     len(cod_reminders),
            "last_reminder_at":   cod_last_reminder_at,
            # True if the auto-cancel sweep has already scheduled a cancel.
            "auto_cancel_at":     meta.get("cod_auto_cancelled_at"),
        })

    return {
        "abandoned_carts":        cart_items,
        "predictive_reorder":     reorder_items,
        "order_status_updates":   order_updates,
        "pending_payment_orders": pending_payment_items,
        "cod_pending_orders":     cod_pending_items,
    }


@router.get("/autopilot/abandoned-carts/debug-events")
async def debug_abandoned_cart_events(
    request: Request,
    db: Session = Depends(get_db),
):
    """Diagnostic: dump every cart-related AutomationEvent for this tenant."""
    tenant_id = resolve_tenant_id(request)

    from sqlalchemy import func as sa_func

    all_events = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type.in_(["cart_abandoned", "abandoned_cart"]),
        )
        .order_by(AutomationEvent.created_at.desc())
        .limit(50)
        .all()
    )

    from models import SmartAutomation
    automations = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.trigger_event.in_(["cart_abandoned", "abandoned_cart"]),
        )
        .all()
    )

    from core.automation_engine import _is_autopilot_enabled
    from core.billing import has_billing_access

    autopilot_on = _is_autopilot_enabled(db, tenant_id)
    billing_ok = has_billing_access(db, tenant_id)

    execs = (
        db.query(AutomationExecution)
        .filter(AutomationExecution.tenant_id == tenant_id)
        .order_by(AutomationExecution.executed_at.desc())
        .limit(20)
        .all()
    )

    return {
        "tenant_id": tenant_id,
        "autopilot_enabled": autopilot_on,
        "billing_access": billing_ok,
        "total_events": len(all_events),
        "recent_executions": [
            {
                "id": x.id,
                "event_id": x.event_id,
                "automation_id": x.automation_id,
                "status": x.status,
                "error_message": x.error_message,
                "executed_at": str(x.executed_at),
                "skip_reason": getattr(x, "skip_reason", None),
            }
            for x in execs
        ],
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "customer_id": e.customer_id,
                "processed": e.processed,
                "created_at": str(e.created_at),
                "automation_id": e.automation_id,
                "payload_keys": list((e.payload or {}).keys()),
                "manual_retry": (e.payload or {}).get("manual_retry"),
                "step_idx": (e.payload or {}).get("step_idx"),
                "cleaned_stale": (e.payload or {}).get("cleaned_stale"),
            }
            for e in all_events
        ],
        "matching_automations": [
            {
                "id": a.id,
                "name": a.name,
                "trigger_event": a.trigger_event,
                "automation_type": getattr(a, "automation_type", None),
                "enabled": a.enabled,
                "engine": getattr(a, "engine", None),
            }
            for a in automations
        ],
    }


@router.post("/autopilot/abandoned-carts/retry-all-stale")
async def retry_all_stale_carts(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Order-based bulk retry: find every abandoned Order whose recovery
    hasn't completed yet, wipe old events/executions, and create a
    fresh Stage-1 event linked to the Order so the engine sends
    immediately.

    SAFETY: wrapped in a top-level try/except so it always returns JSON.
    """
    import logging as _logging
    _log = _logging.getLogger("nahla.retry_all_stale")

    try:
        return await _retry_all_stale_impl(request, db)
    except HTTPException:
        raise
    except Exception as exc:
        _log.error("retry_all_stale CRASHED: %s", exc, exc_info=True)
        return {"ok": False, "retried": 0, "engine_error": f"خطأ داخلي: {exc}", "errors": [], "message": f"حدث خطأ غير متوقع: {exc}"}


async def _retry_all_stale_impl(request: Request, db: Session):
    import logging as _logging
    from sqlalchemy.orm.attributes import flag_modified

    _log = _logging.getLogger("nahla.retry_all_stale")

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    abandoned_orders = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned == True)
        .all()
    )
    _log.warning(
        "tenant=%s abandoned orders found=%d", tenant_id, len(abandoned_orders),
    )

    if not abandoned_orders:
        return {"ok": True, "retried": 0, "message": "لا توجد سلات متروكة."}

    from services.cart_recovery_status import summarise_for_orders

    summaries = summarise_for_orders(db, tenant_id, abandoned_orders)

    retried = 0
    errors = []
    new_event_ids = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for order in abandoned_orders:
        summary = summaries.get(order.id, {})
        status = summary.get("status", "no_recovery")

        if status in ("completed", "converted", "in_progress"):
            continue

        meta = dict(order.extra_metadata or {})
        root_event_id = None
        try:
            raw_rid = meta.get("recovery_event_id")
            root_event_id = int(raw_rid) if raw_rid is not None else None
        except (TypeError, ValueError):
            pass

        root_event = None
        if root_event_id:
            root_event = (
                db.query(AutomationEvent)
                .filter(AutomationEvent.id == root_event_id,
                        AutomationEvent.tenant_id == tenant_id)
                .first()
            )

        customer_id = root_event.customer_id if root_event else None
        base_payload: dict = {}

        if root_event:
            base_payload = dict(root_event.payload or {})

            old_events = (
                db.query(AutomationEvent)
                .filter(
                    AutomationEvent.tenant_id == tenant_id,
                    AutomationEvent.event_type == "cart_abandoned",
                )
                .all()
            )
            ids_to_delete = []
            for ev in old_events:
                ep = ev.payload or {}
                if (
                    ev.id == root_event_id
                    or str(ep.get("parent_event_id", "")) == str(root_event_id)
                    or str(ep.get("order_id", "")) == str(order.id)
                    or (customer_id and ev.customer_id == customer_id)
                ):
                    ids_to_delete.append(ev.id)

            if ids_to_delete:
                db.query(AutomationExecution).filter(
                    AutomationExecution.tenant_id == tenant_id,
                    AutomationExecution.event_id.in_(ids_to_delete),
                ).delete(synchronize_session="fetch")
                db.query(AutomationEvent).filter(
                    AutomationEvent.id.in_(ids_to_delete),
                ).delete(synchronize_session="fetch")
                db.flush()

            _log.info(
                "tenant=%s order=%s deleted %d old events/execs",
                tenant_id, order.id, len(ids_to_delete),
            )
        else:
            ci = order.customer_info or {}
            phone = ci.get("phone") or ci.get("mobile") or ""
            base_payload = {
                "source": "bulk_retry",
                "checkout_url": order.checkout_url or "",
                "cart_total": float(order.total or 0),
                "phone": phone,
                "customer_name": ci.get("name") or order.customer_name or "",
            }
            if not customer_id and phone:
                try:
                    from services.customer_intelligence import (
                        CustomerIntelligenceService,
                        normalize_phone,
                    )
                    np = normalize_phone(phone) or phone
                    svc = CustomerIntelligenceService(db, tenant_id)
                    cust = svc.find_customer_by_phone(np)
                    if cust:
                        customer_id = cust.id
                    else:
                        lead = svc.upsert_lead_customer(
                            phone=np,
                            name=base_payload.get("customer_name", np),
                            source="bulk_retry",
                            commit=False,
                        )
                        customer_id = lead.id if lead else None
                except Exception as exc:
                    _log.warning("customer resolve failed order=%s: %s", order.id, exc)

        if not customer_id:
            errors.append(f"order {order.id}: no customer_id")
            continue

        new_payload = dict(base_payload)
        new_payload["step_idx"] = 0
        new_payload["order_id"] = order.id
        new_payload["manual_retry"] = True
        new_payload["restart_from_stage1"] = True
        new_payload["retry_reason"] = "bulk_stale_cleanup"
        new_payload["retry_requested_at"] = now.replace(tzinfo=timezone.utc).isoformat()
        for key in ("cleaned_stale", "cleaned_at", "recovery_followups",
                     "superseded_by_retry", "processed_at", "result",
                     "cancelled_by_retry"):
            new_payload.pop(key, None)

        fresh = AutomationEvent(
            tenant_id=tenant_id,
            event_type="cart_abandoned",
            customer_id=customer_id,
            payload=new_payload,
            processed=False,
            created_at=now,
        )
        db.add(fresh)
        db.flush()

        new_event_ids.append(fresh.id)
        meta["recovery_event_id"] = fresh.id
        order.extra_metadata = meta
        flag_modified(order, "extra_metadata")
        retried += 1
        _log.info(
            "tenant=%s order=%s new event=%s customer=%s",
            tenant_id, order.id, fresh.id, customer_id,
        )

    db.commit()

    # Pre-flight checks (fast, no network I/O)
    engine_error = None
    if new_event_ids:
        try:
            from core.automation_engine import _is_autopilot_enabled
            from core.billing import has_billing_access

            diag_billing = has_billing_access(db, tenant_id)
            diag_automations = (
                db.query(SmartAutomation)
                .filter(
                    SmartAutomation.tenant_id == tenant_id,
                    SmartAutomation.trigger_event == "cart_abandoned",
                )
                .all()
            )

            if not diag_billing:
                engine_error = "انتهت التجربة المجانية — يجب الاشتراك لإرسال التذكيرات"
            elif not diag_automations or not any(a.enabled for a in diag_automations):
                engine_error = "أتمتة استرداد العربة المتروكة غير مفعّلة — فعّلها من إعدادات الطيار الآلي"
            else:
                # Fire-and-forget: process events in the background so the
                # endpoint returns immediately instead of hanging for 2+ min
                # while WhatsApp API calls complete.
                import asyncio
                async def _bg_process():
                    from core.database import SessionLocal
                    bg_db = SessionLocal()
                    try:
                        from core.automation_engine import process_pending_events
                        sent = await process_pending_events(
                            bg_db, tenant_id,
                            skip_autopilot_check=True,
                            event_ids=new_event_ids,
                        )
                        _log.info("tenant=%s bg engine sent=%d", tenant_id, sent)
                    except Exception as exc:
                        _log.error("tenant=%s bg engine failed: %s", tenant_id, exc, exc_info=True)
                    finally:
                        bg_db.close()

                asyncio.create_task(_bg_process())
        except Exception as exc:
            engine_error = str(exc)
            _log.error("tenant=%s pre-flight failed: %s", tenant_id, exc, exc_info=True)

    msg = f"تم إعادة جدولة {retried} سلة من المرحلة الأولى."
    if engine_error:
        msg += f" ⚠ {engine_error}"
    elif new_event_ids:
        msg += " جارٍ الإرسال في الخلفية..."
    if errors:
        msg += f" ({len(errors)} سلة بدون عميل مرتبط)"
    return {
        "ok": True,
        "retried": retried,
        "engine_error": engine_error,
        "errors": errors,
        "message": msg,
    }


@router.get("/autopilot/abandoned-carts/{order_id}/recovery")
async def abandoned_cart_recovery_timeline(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Per-cart recovery timeline used by the queue drawer / detail view.

    Returns the same summary fields as the per-cart entry on
    ``/autopilot/queues`` (status, last_sent_at, next_pending_at,
    converted_at, …) plus a chronologically-ordered ``steps`` array —
    one entry per follow-up stage emitted, with delivery status, error
    message, channel and template id when available.

    Empty / missing recovery returns ``status="no_recovery"`` with an
    empty ``steps`` array — never an error — so the UI can render a
    clear "no reminders queued for this cart" empty state.
    """
    from services.cart_recovery_status import timeline_for_order  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.tenant_id == tenant_id)
        .first()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found for this tenant")

    return timeline_for_order(db, tenant_id, order)


# ── Pending-payment / COD reminder timeline ───────────────────────────────────

_PENDING_PAY_STATUSES_TL = frozenset({
    "pending", "pending_payment", "payment_pending",
    "awaiting_payment", "draft", "new",
})
_COD_PENDING_STATUSES_TL = frozenset({
    "pending_confirmation", "awaiting_confirmation",
    "under_review", "in_review",
})

_STEP_STATUS_LABELS = {
    "sent":    "أُرسلت",
    "skipped": "تم التخطّي",
    "failed":  "فشلت",
    "pending": "قيد الإرسال",
    "emitted": "جُدولت",
}


@router.get("/autopilot/orders/{order_id}/reminder-timeline")
async def order_reminder_timeline(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Timeline of emitted + executed reminder stages for a pending-payment
    or COD-pending order.

    Each step carries the actual delivery status from AutomationExecution
    (``sent`` | ``failed`` | ``skipped``) rather than just the emit timestamp,
    giving the merchant an honest view of what was delivered.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.tenant_id == tenant_id)
        .first()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found for this tenant")

    meta = order.extra_metadata or {}
    raw_status = (order.status or "").strip().lower()

    if raw_status in _PENDING_PAY_STATUSES_TL:
        reminder_type = "pending_payment"
        event_type    = "order_payment_pending"
        meta_key      = "unpaid_reminders"
    elif raw_status in _COD_PENDING_STATUSES_TL:
        reminder_type = "cod"
        event_type    = "order_cod_pending"
        meta_key      = "cod_reminders"
    else:
        # Order may have been paid/completed since. Pick whichever history exists.
        if meta.get("unpaid_reminders"):
            reminder_type = "pending_payment"
            event_type    = "order_payment_pending"
            meta_key      = "unpaid_reminders"
        elif meta.get("cod_reminders"):
            reminder_type = "cod"
            event_type    = "order_cod_pending"
            meta_key      = "cod_reminders"
        else:
            ci = order.customer_info or {}
            return {
                "order_id":      order_id,
                "order_number":  order.external_order_number or order.external_id or f"#{order_id}",
                "customer_name": ci.get("name") or order.customer_name or "—",
                "reminder_type": "unknown",
                "total_emitted": 0,
                "steps_sent":    0,
                "steps":         [],
                "order_status":  raw_status,
            }

    reminders: list = list(meta.get(meta_key) or [])

    # Fetch all automation events of the relevant type for this tenant,
    # then filter to this order in Python to avoid JSONB operator dependencies.
    all_events = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == event_type,
        )
        .order_by(AutomationEvent.created_at.asc())
        .all()
    )
    order_events = [
        e for e in all_events
        if str((e.payload or {}).get("order_internal_id", "")) == str(order.id)
    ]

    event_by_step: dict = {}
    for e in order_events:
        s = int((e.payload or {}).get("step_idx", -1))
        if s >= 0:
            event_by_step[s] = e

    emitted_by_step = {
        int(r.get("step_idx", -1)): r
        for r in reminders
        if r.get("step_idx") is not None
    }

    all_step_indices = sorted(
        set(list(emitted_by_step.keys()) + list(event_by_step.keys()))
    )

    steps = []
    for step_idx in all_step_indices:
        if step_idx < 0:
            continue
        emitted_record = emitted_by_step.get(step_idx)
        event          = event_by_step.get(step_idx)

        execution = None
        if event:
            execution = (
                db.query(AutomationExecution)
                .filter(AutomationExecution.event_id == event.id)
                .order_by(AutomationExecution.executed_at.desc())
                .first()
            )

        if execution:
            if execution.status == "sent":
                status = "sent"
            elif execution.status == "skipped":
                status = "skipped"
            else:
                status = "failed"
        elif event and not event.processed:
            status = "pending"
        elif emitted_record:
            status = "emitted"
        else:
            status = "pending"

        steps.append({
            "step_idx":      step_idx + 1,       # 1-based for display
            "emitted_at":    emitted_record.get("emitted_at") if emitted_record else None,
            "executed_at":   execution.executed_at.isoformat() if execution and execution.executed_at else None,
            "status":        status,
            "status_label":  _STEP_STATUS_LABELS.get(status, status),
            "skip_reason":   execution.skip_reason if execution else None,
            "error_message": execution.error_message if execution else None,
            "template_name": (execution.action_taken or {}).get("template_name") if execution else None,
        })

    ci = order.customer_info or {}
    sent_count = sum(1 for s in steps if s["status"] == "sent")
    return {
        "order_id":      order_id,
        "order_number":  order.external_order_number or order.external_id or f"#{order_id}",
        "customer_name": ci.get("name") or order.customer_name or "—",
        "reminder_type": reminder_type,
        "total_emitted": len(reminders),
        "steps_sent":    sent_count,
        "steps":         steps,
        "order_status":  raw_status,
    }


# ── Reschedule failed/skipped reminder stages ─────────────────────────────────

# Skips that are permanent (unsubscribe) or user-action-required (template
# not approved) are flagged in the response so the UI can warn the merchant.
_PERMANENT_SKIP_REASONS = frozenset({
    "blocked_by_unsubscribe",
    "order_no_longer_pending",
})
_TEMPLATE_SKIP_REASONS = frozenset({
    "no_approved_template",
    "template_not_found",
    "template_not_approved",
})


@router.post("/autopilot/orders/{order_id}/reschedule-reminders")
async def reschedule_order_reminders(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Re-queue failed or temporarily-skipped reminder stages for a
    pending-payment or COD-pending order.

    Clears the failed/skipped step markers from ``extra_metadata`` so the
    next emitter sweep (≤ 60 s) picks them up and re-queues fresh events.

    Only failed / governor-skipped steps are cleared — steps that were
    successfully sent are always preserved.

    Returns::

        {
          "ok": true,
          "steps_cleared": <int>,
          "has_template_error": <bool>,  // true → merchant must approve templates first
          "has_permanent_block": <bool>, // true → unsubscribe or order closed
          "message": "<Arabic summary>",
        }
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.tenant_id == tenant_id)
        .first()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found for this tenant")

    meta = dict(order.extra_metadata or {})
    raw_status = (order.status or "").strip().lower()

    if raw_status in _PENDING_PAY_STATUSES_TL or meta.get("unpaid_reminders"):
        meta_key   = "unpaid_reminders"
        event_type = "order_payment_pending"
    elif raw_status in _COD_PENDING_STATUSES_TL or meta.get("cod_reminders"):
        meta_key   = "cod_reminders"
        event_type = "order_cod_pending"
    else:
        raise HTTPException(
            status_code=400,
            detail="Order is not in a reschedulable state (not pending-payment or COD-pending)",
        )

    reminders: list = list(meta.get(meta_key) or [])

    # Resolve automation events for this order (Python-level filter for DB compat)
    all_events = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == event_type,
        )
        .all()
    )
    order_events = [
        e for e in all_events
        if str((e.payload or {}).get("order_internal_id", "")) == str(order.id)
    ]
    event_by_step: dict = {}
    for e in order_events:
        s = int((e.payload or {}).get("step_idx", -1))
        if s >= 0:
            event_by_step[s] = e

    steps_to_clear:    set  = set()
    has_template_error: bool = False
    has_permanent_block: bool = False

    for reminder in reminders:
        step_idx = int(reminder.get("step_idx", -1))
        if step_idx < 0:
            continue
        event = event_by_step.get(step_idx)
        if event is None:
            continue
        execution = (
            db.query(AutomationExecution)
            .filter(AutomationExecution.event_id == event.id)
            .order_by(AutomationExecution.executed_at.desc())
            .first()
        )
        if execution is None:
            continue

        if execution.status == "sent":
            continue  # never clear successfully sent steps

        skip = execution.skip_reason or ""
        err  = execution.error_message or ""

        # Classify the failure type to surface warnings in the UI.
        if skip in _TEMPLATE_SKIP_REASONS or "no_approved_template" in err:
            has_template_error = True
        if skip in _PERMANENT_SKIP_REASONS:
            has_permanent_block = True
            continue  # permanent blocks: do NOT clear (re-queuing is pointless)

        if execution.status in ("failed", "skipped"):
            steps_to_clear.add(step_idx)

    if not steps_to_clear and not has_template_error:
        return {
            "ok":                  True,
            "steps_cleared":       0,
            "has_template_error":  False,
            "has_permanent_block": has_permanent_block,
            "message":             "لا توجد مراحل فاشلة قابلة لإعادة الجدولة",
        }

    if steps_to_clear:
        meta[meta_key] = [
            r for r in reminders
            if int(r.get("step_idx", -1)) not in steps_to_clear
        ]
        order.extra_metadata = meta
        db.commit()

    return {
        "ok":                  True,
        "steps_cleared":       len(steps_to_clear),
        "has_template_error":  has_template_error,
        "has_permanent_block": has_permanent_block,
        "message": (
            "لم يتم تغيير أي شيء — القالب يحتاج اعتماد Meta أولاً"
            if has_template_error and not steps_to_clear
            else f"تمت إعادة جدولة {len(steps_to_clear)} مرحلة — ستُرسَل في الدورة القادمة"
        ),
    }


# ── Manual retry (temporary, feature-flagged) ────────────────────────────────
@router.post("/autopilot/abandoned-carts/{order_id}/retry")
async def retry_abandoned_cart(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Restart the cart recovery sequence **from Stage 1**.

    Creates a fresh automation event starting from step 1, so the
    entire reminder chain runs again. Useful for testing and for
    merchants who want to re-trigger reminders after fixing template
    or configuration issues.

    The new event carries ``restart_from_stage1=True`` and
    ``manual_retry=True`` so the engine treats it as a normal pending
    event starting from the beginning.

    Idempotency: a retry within 60s of an unprocessed retry for the
    same cart short-circuits to prevent double-clicks.
    """
    if not _manual_retry_enabled():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "manual_retry_disabled",
                "message": (
                    "زر إعادة الإرسال اليدوي معطّل في هذه البيئة. "
                    "اضبط المتغير AUTOPILOT_ENABLE_MANUAL_RETRY=true لتفعيله."
                ),
            },
        )

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.tenant_id == tenant_id)
        .first()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found for this tenant")

    from services.cart_recovery_status import timeline_for_order  # noqa: PLC0415

    timeline = timeline_for_order(db, tenant_id, order)

    # Eligibility — every refusal returns a structured error so the UI
    # can render a precise message and the merchant knows why.
    if not timeline.get("recovery_event_id"):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_recovery_event",
                "message": "لا توجد حملة استعادة مرتبطة بهذه السلة بعد.",
            },
        )

    if timeline.get("status") == "converted":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_converted",
                "message": "تم الشراء بالفعل — لا حاجة لإعادة الإرسال.",
            },
        )

    root_event_id = int(timeline["recovery_event_id"])

    # ── Idempotency: short-circuit on a recent unprocessed retry ─────────
    from datetime import timedelta

    recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    existing_retry = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == "cart_abandoned",
            AutomationEvent.processed == False,
            AutomationEvent.created_at >= recent_cutoff,
        )
        .all()
    )
    for ev in existing_retry:
        payload = ev.payload or {}
        if (
            payload.get("manual_retry") is True
            and int(payload.get("parent_event_id") or 0) == root_event_id
        ):
            return {
                "ok":               True,
                "deduplicated":     True,
                "retry_event_id":   ev.id,
                "step_idx":         1,
                "queued_at":        ev.created_at.replace(tzinfo=timezone.utc).isoformat(),
                "message":          "هناك إعادة إرسال قيد التنفيذ بالفعل لهذه السلة.",
            }

    # ── Find the root event to copy customer + payload ──────────────────
    root_event = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.id == root_event_id,
        )
        .first()
    )
    if root_event is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error":   "root_event_missing",
                "message": "تعذّر العثور على حدث الاستعادة الأصلي.",
            },
        )

    # ── Delete ALL old events + executions for this cart ─────────────────
    target_order_id = str(order_id)
    root_customer_id = root_event.customer_id

    def _matches_cart(ev: AutomationEvent) -> bool:
        ep = ev.payload or {}
        return (
            ev.id == root_event_id
            or str(ep.get("order_id", "")) == target_order_id
            or str(ep.get("parent_event_id", "")) == str(root_event_id)
            or (root_customer_id and ev.customer_id == root_customer_id)
        )

    all_cart_events = (
        db.query(AutomationEvent)
        .filter(AutomationEvent.tenant_id == tenant_id)
        .all()
    )
    event_ids_to_delete = []
    for ev in all_cart_events:
        if ev.id != root_event_id and _matches_cart(ev):
            event_ids_to_delete.append(ev.id)

    deleted_count = 0
    if event_ids_to_delete:
        db.query(AutomationExecution).filter(
            AutomationExecution.tenant_id == tenant_id,
            AutomationExecution.event_id.in_(event_ids_to_delete),
        ).delete(synchronize_session="fetch")
        db.query(AutomationEvent).filter(
            AutomationEvent.id.in_(event_ids_to_delete),
        ).delete(synchronize_session="fetch")
        deleted_count = len(event_ids_to_delete)

    old_execs = db.query(AutomationExecution).filter(
        AutomationExecution.tenant_id == tenant_id,
        AutomationExecution.event_id == root_event_id,
    ).delete(synchronize_session="fetch")
    deleted_count += old_execs

    # Mark the root event so the sweeper ignores it for future follow-ups.
    # Without this, both the old root AND the new retry event would look
    # like Stage-1 parents, causing duplicate Stage 2-4 emissions.
    root_payload = dict(root_event.payload or {})
    root_payload["recovery_followups"] = []
    root_payload["superseded_by_retry"] = True
    root_event.payload = root_payload
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(root_event, "payload")

    db.flush()

    base_payload: Dict[str, Any] = dict(root_event.payload or {})
    base_payload["step_idx"]            = 0
    base_payload["order_id"]            = order_id
    base_payload["parent_event_id"]     = root_event_id
    base_payload["manual_retry"]        = True
    base_payload["restart_from_stage1"] = True
    base_payload["retry_requested_at"]  = datetime.now(timezone.utc).isoformat()
    base_payload["retry_reason"]        = "manual_restart_stage1"
    base_payload.pop("processed_at", None)
    base_payload.pop("result", None)
    base_payload.pop("cancelled_by_retry", None)
    base_payload.pop("superseded_by_retry", None)
    base_payload.pop("recovery_followups", None)

    new_event = AutomationEvent(
        tenant_id   = tenant_id,
        event_type  = root_event.event_type or "cart_abandoned",
        customer_id = root_event.customer_id,
        payload     = base_payload,
        processed   = False,
        created_at  = datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(new_event)
    db.commit()
    db.refresh(new_event)

    import asyncio
    _eid = new_event.id
    async def _bg_send():
        from core.database import SessionLocal
        bg_db = SessionLocal()
        try:
            from core.automation_engine import process_pending_events
            sent = await process_pending_events(
                bg_db, tenant_id,
                skip_autopilot_check=True,
                event_ids=[_eid],
            )
            logger.info("bg engine order=%s sent=%d", order_id, sent)
        except Exception as exc:
            logger.error("bg engine order=%s failed: %s", order_id, exc, exc_info=True)
        finally:
            bg_db.close()

    asyncio.create_task(_bg_send())

    return {
        "ok":             True,
        "deduplicated":   False,
        "retry_event_id": new_event.id,
        "step_idx":       0,
        "deleted_old":    deleted_count,
        "queued_at":      new_event.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "message":        f"تم حذف {deleted_count} سجل سابق وإعادة الجدولة. جارٍ الإرسال...",
    }


# ── Reschedule failed steps only (surgical, keeps sent stages intact) ────────
@router.post("/autopilot/abandoned-carts/{order_id}/reschedule-failed")
async def reschedule_failed_cart_steps(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Re-queue only the cart-recovery steps that failed — without touching
    stages that were already sent successfully.

    For each AutomationEvent whose execution ended with status='failed':
      1. Delete its AutomationExecution record(s).
      2. Reset AutomationEvent.processed = False so the engine picks it up again.

    The engine re-runs each reset event independently; sent stages are untouched.
    """
    if not _manual_retry_enabled():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "manual_retry_disabled",
                "message": (
                    "زر إعادة الإرسال اليدوي معطّل في هذه البيئة. "
                    "اضبط المتغير AUTOPILOT_ENABLE_MANUAL_RETRY=true لتفعيله."
                ),
            },
        )

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.tenant_id == tenant_id)
        .first()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found for this tenant")

    from services.cart_recovery_status import timeline_for_order  # noqa: PLC0415

    timeline = timeline_for_order(db, tenant_id, order)

    if not timeline.get("recovery_event_id"):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_recovery_event",
                "message": "لا توجد حملة استعادة مرتبطة بهذه السلة بعد.",
            },
        )

    if timeline.get("status") == "converted":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_converted",
                "message": "تم الشراء بالفعل — لا حاجة لإعادة الإرسال.",
            },
        )

    # ── Collect failed-step event IDs ────────────────────────────────────────
    failed_event_ids = [
        s["event_id"]
        for s in timeline.get("steps", [])
        if s.get("status") == "failed" and s.get("event_id")
    ]

    if not failed_event_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_failed_steps",
                "message": "لا توجد مراحل فاشلة لإعادة جدولتها.",
            },
        )

    # ── For each failed event: wipe its execution, reset processed=False ─────
    cleared = 0
    for ev_id in failed_event_ids:
        deleted = db.query(AutomationExecution).filter(
            AutomationExecution.tenant_id == tenant_id,
            AutomationExecution.event_id == ev_id,
        ).delete(synchronize_session="fetch")
        cleared += deleted

        db.query(AutomationEvent).filter(
            AutomationEvent.id == ev_id,
            AutomationEvent.tenant_id == tenant_id,
        ).update({"processed": False}, synchronize_session="fetch")

    db.commit()

    # ── Trigger engine for the reset events only ─────────────────────────────
    import asyncio

    _eids = list(failed_event_ids)

    async def _bg_retry():
        from core.database import SessionLocal          # noqa: PLC0415
        bg_db = SessionLocal()
        try:
            from core.automation_engine import process_pending_events  # noqa: PLC0415
            sent = await process_pending_events(
                bg_db, tenant_id,
                skip_autopilot_check=True,
                event_ids=_eids,
            )
            logger.info(
                "reschedule-failed order=%s events=%s sent=%d", order_id, _eids, sent
            )
        except Exception as exc:
            logger.error(
                "reschedule-failed bg order=%s failed: %s", order_id, exc, exc_info=True
            )
        finally:
            bg_db.close()

    asyncio.create_task(_bg_retry())

    return {
        "ok":             True,
        "steps_rescheduled": len(failed_event_ids),
        "executions_cleared": cleared,
        "message": (
            f"تمت إعادة جدولة {len(failed_event_ids)} مرحلة فاشلة. "
            "المراحل المُرسَلة بنجاح لم تُمَس."
        ),
    }


