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
from sqlalchemy.orm import Session

from models import (  # noqa: E402
    AutomationEvent,
    Customer,
    CustomerProfile,
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

SEED_AUTOMATIONS: List[Dict[str, Any]] = [
    {
        "automation_type": "abandoned_cart",
        "name": "استرداد العربة المتروكة",
        "enabled": False,
        "config": {
            "steps": [
                {"delay_minutes": 30,   "message_type": "reminder"},
                {"delay_minutes": 180,  "message_type": "reminder"},
                {"delay_minutes": 1440, "message_type": "coupon", "coupon_code": "CART10AUTO"},
            ],
            "template_name": "abandoned_cart_reminder",
        },
    },
    {
        "automation_type": "predictive_reorder",
        "name": "تذكير إعادة الطلب التنبؤي",
        "enabled": False,
        "config": {
            "template_name": "predictive_reorder_reminder_ar",
            "var_map": {"{{1}}": "customer_name", "{{2}}": "product_name", "{{3}}": "reorder_url"},
            "days_before": 3,
        },
    },
    {
        "automation_type": "customer_winback",
        "name": "استرجاع العملاء غير النشطين",
        "enabled": False,
        "config": {
            "inactive_days_first": 60,
            "inactive_days_second": 90,
            "discount_pct": 15,
            "template_name": "win_back",
        },
    },
    {
        "automation_type": "vip_upgrade",
        "name": "مكافأة عملاء VIP",
        "enabled": False,
        "config": {
            "min_spent_sar": 2000,
            "discount_pct": 20,
            "template_name": "vip_exclusive",
        },
    },
    {
        "automation_type": "new_product_alert",
        "name": "تنبيه المنتجات الجديدة",
        "enabled": False,
        "config": {
            "target_interested_only": True,
            "template_name": "new_arrivals",
        },
    },
    {
        "automation_type": "back_in_stock",
        "name": "تنبيه عودة المنتج للمخزون",
        "enabled": False,
        "config": {
            "notify_previous_buyers": True,
            "notify_previous_viewers": True,
            "template_name": "new_arrivals",
        },
    },
]

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

AUTOPILOT_EVENT_TYPES = {
    "order_status_update": "autopilot_order_update_sent",
    "predictive_reorder":  "autopilot_reorder_sent",
    "abandoned_cart":      "autopilot_cart_sent",
    "inactive_recovery":   "autopilot_inactive_sent",
    # backward compat alias kept for existing DB rows
    "cod_confirmation":    "autopilot_cod_sent",
}

AUTOPILOT_SUMMARY_LABELS: Dict[str, str] = {
    "autopilot_order_update_sent": "إشعارات تحديثات الطلبات أُرسلت",
    "autopilot_cod_sent":          "تأكيدات طلبات COD أُرسلت",
    "autopilot_reorder_sent":      "تذكيرات إعادة طلب أُرسلت",
    "autopilot_cart_sent":         "سلات متروكة تم التواصل بشأنها",
    "autopilot_inactive_sent":     "عملاء غير نشطين تم استرجاعهم",
}

AUTOPILOT_SUMMARY_ICONS: Dict[str, str] = {
    "autopilot_order_update_sent": "📦",
    "autopilot_cod_sent":          "🍯",
    "autopilot_reorder_sent":      "🔄",
    "autopilot_cart_sent":         "🛒",
    "autopilot_inactive_sent":     "💙",
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

def _seed_automations_if_empty(db: Session, tenant_id: int) -> None:
    count = db.query(SmartAutomation).filter(SmartAutomation.tenant_id == tenant_id).count()
    if count == 0:
        for seed in SEED_AUTOMATIONS:
            auto = SmartAutomation(
                tenant_id=tenant_id,
                automation_type=seed["automation_type"],
                name=seed["name"],
                enabled=seed["enabled"],
                config=seed["config"],
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(auto)
        db.flush()


def _auto_to_dict(a: SmartAutomation) -> Dict[str, Any]:
    return {
        "id": a.id,
        "automation_type": a.automation_type,
        "name": a.name,
        "enabled": a.enabled,
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
    """Count today's autopilot actions from AutomationEvent."""
    from datetime import date, timezone
    today_start = datetime.combine(date.today(), datetime.min.time())
    summary = []
    for evt_type, label in AUTOPILOT_SUMMARY_LABELS.items():
        count = (
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.tenant_id == tenant_id,
                AutomationEvent.event_type == evt_type,
                AutomationEvent.created_at >= today_start,
            )
            .count()
        )
        summary.append({
            "key": evt_type,
            "label": label,
            "count": count,
            "icon": AUTOPILOT_SUMMARY_ICONS.get(evt_type, "📨"),
        })
    return summary


def _log_autopilot_event(
    db: Session,
    tenant_id: int,
    event_type: str,
    customer_id: Optional[int],
    payload: Dict[str, Any],
) -> None:
    """Write an AutomationEvent row for an autopilot action."""
    event = AutomationEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        customer_id=customer_id,
        payload=payload,
        processed=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)


def _job_order_status_update(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    """
    Send WhatsApp notifications for ALL order status changes.
    Tracks `last_notified_status` per order in extra_metadata to avoid duplicates.
    """
    notify_statuses: List[str] = config.get(
        "notify_statuses",
        ["pending", "shipped", "out_for_delivery", "delivered", "cancelled", "refunded"],
    )
    sent = 0

    orders = db.query(Order).filter(Order.tenant_id == tenant_id).all()
    for order in orders:
        meta = order.extra_metadata or {}
        current_status = order.status or "pending"
        last_notified = meta.get("last_notified_status")

        if current_status == last_notified:
            continue

        new_meta = {**meta, "last_notified_status": current_status}

        if current_status in notify_statuses:
            customer_info = order.customer_info or {}
            customer_name = customer_info.get("name", "العميل")
            status_label = ORDER_STATUS_LABELS.get(current_status, current_status)

            _log_autopilot_event(
                db, tenant_id,
                AUTOPILOT_EVENT_TYPES["order_status_update"],
                None,
                {
                    "order_id": order.id,
                    "external_id": order.external_id,
                    "customer_name": customer_name,
                    "status": current_status,
                    "status_label": status_label,
                    "previous_status": last_notified,
                    "template": config.get("template_name", "order_status_update_ar"),
                    "vars": {
                        "{{1}}": customer_name,
                        "{{2}}": order.external_id or str(order.id),
                        "{{3}}": status_label,
                    },
                },
            )
            sent += 1

        order.extra_metadata = new_meta

    return sent


def _job_predictive_reorder(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    from datetime import timedelta, timezone

    days_before = int(config.get("days_before", 3))
    window_end = datetime.now(timezone.utc) + timedelta(days=days_before)
    sent = 0

    estimates = db.query(PredictiveReorderEstimate).filter(
        PredictiveReorderEstimate.tenant_id == tenant_id,
        PredictiveReorderEstimate.notified == False,
        PredictiveReorderEstimate.predicted_reorder_date <= window_end,
    ).all()

    for est in estimates:
        customer = db.query(Customer).filter(
            Customer.id == est.customer_id, Customer.tenant_id == tenant_id
        ).first()
        product = db.query(Product).filter(
            Product.id == est.product_id, Product.tenant_id == tenant_id
        ).first()

        customer_name = customer.name if customer else "العميل"
        product_name = product.title if product else f"المنتج #{est.product_id}"
        store_url = ""
        settings_row = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
        if settings_row:
            store = merge_defaults(settings_row.store_settings, DEFAULT_STORE)
            store_url = store.get("store_url", "")

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["predictive_reorder"],
            est.customer_id,
            {
                "estimate_id": est.id,
                "customer_name": customer_name,
                "product_name": product_name,
                "template": config.get("template_name", "predictive_reorder_reminder_ar"),
                "vars": {
                    "{{1}}": customer_name,
                    "{{2}}": product_name,
                    "{{3}}": store_url or "https://store.example.com",
                },
            },
        )
        est.notified = True
        sent += 1

    return sent


def _job_abandoned_cart(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    sent = 0

    abandoned = db.query(Order).filter(
        Order.tenant_id == tenant_id,
        Order.is_abandoned == True,
    ).all()

    for order in abandoned:
        already = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == AUTOPILOT_EVENT_TYPES["abandoned_cart"],
            AutomationEvent.payload.op("->")("order_id").astext == str(order.id),
        ).count()

        if already > 0:
            continue

        customer_info = order.customer_info or {}
        customer_name = customer_info.get("name", "العميل")
        checkout_url = order.checkout_url or ""

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["abandoned_cart"],
            None,
            {
                "order_id": order.id,
                "customer_name": customer_name,
                "checkout_url": checkout_url,
                "template": config.get("template_name", "abandoned_cart_reminder"),
                "vars": {
                    "{{1}}": customer_name,
                    "{{2}}": checkout_url or "https://store.example.com/cart",
                },
                "steps": [
                    {"delay": "30m", "sent": True},
                    {"delay": "24h", "scheduled": True},
                    {"delay": "48h", "coupon": config.get("coupon_code", ""), "scheduled": bool(config.get("coupon_48h"))},
                ],
            },
        )
        sent += 1

    return sent


def _job_inactive_customers(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    from datetime import timedelta, timezone

    inactive_days = int(config.get("inactive_days", 60))
    discount_pct = int(config.get("discount_pct", 15))
    sent = 0

    threshold = datetime.now(timezone.utc) - timedelta(days=inactive_days)
    at_risk = (
        db.query(CustomerProfile, Customer)
        .join(Customer, CustomerProfile.customer_id == Customer.id)
        .filter(
            CustomerProfile.tenant_id == tenant_id,
            CustomerProfile.segment.in_(["at_risk", "churned"]),
            CustomerProfile.last_order_at <= threshold,
        )
        .all()
    )

    for profile, customer in at_risk:
        cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_days // 2)
        already = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == AUTOPILOT_EVENT_TYPES["inactive_recovery"],
            AutomationEvent.customer_id == customer.id,
            AutomationEvent.created_at >= cutoff,
        ).count()

        if already > 0:
            continue

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["inactive_recovery"],
            customer.id,
            {
                "customer_name": customer.name,
                "days_inactive": (datetime.now(timezone.utc) - profile.last_order_at).days if profile.last_order_at else inactive_days,
                "template": config.get("template_name", "win_back"),
                "discount_pct": discount_pct,
                "vars": {
                    "{{1}}": customer.name or "العميل",
                    "{{2}}": f"{discount_pct}%",
                    "{{3}}": f"WINBACK{discount_pct}",
                },
            },
        )
        sent += 1

    return sent


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/automations")
async def list_automations(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _seed_automations_if_empty(db, tenant_id)
    db.commit()
    autos = (
        db.query(SmartAutomation)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .order_by(SmartAutomation.id)
        .all()
    )
    autopilot = _get_autopilot_enabled(db, tenant_id)
    return {"automations": [_auto_to_dict(a) for a in autos], "autopilot_enabled": autopilot}


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

    last_event = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type.in_(list(AUTOPILOT_EVENT_TYPES.values())),
        )
        .order_by(AutomationEvent.created_at.desc())
        .first()
    )
    last_run_at = last_event.created_at.isoformat() if last_event else None

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
    """Manually trigger all enabled autopilot jobs for this tenant."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    ap = _get_autopilot_settings(db, tenant_id)

    if not ap.get("enabled", False):
        return {"ran": False, "message": "الطيار التلقائي معطّل — فعّله أولاً من الإعدادات"}

    results: Dict[str, int] = {}

    if ap["order_status_update"].get("enabled", True):
        results["order_status_update"] = _job_order_status_update(db, tenant_id, ap["order_status_update"])

    if ap["predictive_reorder"].get("enabled", True):
        results["predictive_reorder"] = _job_predictive_reorder(db, tenant_id, ap["predictive_reorder"])

    if ap["abandoned_cart"].get("enabled", True):
        results["abandoned_cart"] = _job_abandoned_cart(db, tenant_id, ap["abandoned_cart"])

    if ap["inactive_recovery"].get("enabled", True):
        results["inactive_recovery"] = _job_inactive_customers(db, tenant_id, ap["inactive_recovery"])

    db.commit()

    total = sum(results.values())
    return {
        "ran": True,
        "total_actions": total,
        "breakdown": results,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "message": f"الطيار التلقائي أرسل {total} رسالة في هذه الجلسة",
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
