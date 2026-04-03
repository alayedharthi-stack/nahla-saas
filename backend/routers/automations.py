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
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
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

from core.billing import require_subscription
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
    "cod_confirmation": {
        "enabled": True,
        "reminder_hours": 2,
        "auto_cancel_hours": 24,
        "template_name": "cod_order_confirmation_ar",
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
    "cod_confirmation":   "autopilot_cod_sent",
    "predictive_reorder": "autopilot_reorder_sent",
    "abandoned_cart":     "autopilot_cart_sent",
    "inactive_recovery":  "autopilot_inactive_sent",
}

AUTOPILOT_SUMMARY_LABELS: Dict[str, str] = {
    "autopilot_cod_sent":      "تأكيدات طلبات COD أُرسلت",
    "autopilot_reorder_sent":  "تذكيرات إعادة طلب أُرسلت",
    "autopilot_cart_sent":     "سلات متروكة تم التواصل بشأنها",
    "autopilot_inactive_sent": "عملاء غير نشطين تم استرجاعهم",
}

AUTOPILOT_SUMMARY_ICONS: Dict[str, str] = {
    "autopilot_cod_sent":      "🍯",
    "autopilot_reorder_sent":  "🔄",
    "autopilot_cart_sent":     "🛒",
    "autopilot_inactive_sent": "💙",
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
    cod_confirmation: Optional[AutopilotSubIn] = None
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
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
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
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    if not settings:
        return False
    ai = merge_defaults(settings.ai_settings, DEFAULT_AI)
    return bool(ai.get("autopilot_enabled", False))


def _get_autopilot_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Read autopilot config from TenantSettings.extra_metadata."""
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    stored: Dict[str, Any] = {}
    if settings and settings.extra_metadata:
        stored = settings.extra_metadata.get("autopilot", {})
    merged = dict(DEFAULT_AUTOPILOT)
    if stored:
        merged.update({k: v for k, v in stored.items() if k in DEFAULT_AUTOPILOT})
        for sub in ("cod_confirmation", "predictive_reorder", "abandoned_cart", "inactive_recovery"):
            if sub in stored and isinstance(stored[sub], dict):
                base = dict(DEFAULT_AUTOPILOT[sub])
                base.update(stored[sub])
                merged[sub] = base
    return merged


def _save_autopilot_settings(db: Session, tenant_id: int, autopilot: Dict[str, Any]) -> None:
    """Persist autopilot config to TenantSettings.extra_metadata."""
    settings = get_or_create_settings(db, tenant_id)
    extra: Dict[str, Any] = dict(settings.extra_metadata or {})
    extra["autopilot"] = autopilot
    settings.extra_metadata = extra
    settings.updated_at = datetime.utcnow()


def _get_daily_summary(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Count today's autopilot actions from AutomationEvent."""
    from datetime import date
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
        created_at=datetime.utcnow(),
    )
    db.add(event)


def _job_cod_confirmation(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    from datetime import timedelta

    sent = 0
    cod_orders = db.query(Order).filter(
        Order.tenant_id == tenant_id,
        Order.status == "pending",
    ).all()

    for order in cod_orders:
        meta = order.extra_metadata or {}
        payment_method = meta.get("payment_method", "")
        if payment_method not in ("cod", "cash_on_delivery", ""):
            continue

        customer_info = order.customer_info or {}
        customer_name = customer_info.get("name", "العميل")

        already_sent = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == AUTOPILOT_EVENT_TYPES["cod_confirmation"],
            AutomationEvent.payload.op("->")("order_id").astext == str(order.id),
        ).count()

        if already_sent > 0:
            continue

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["cod_confirmation"],
            None,
            {
                "order_id": order.id,
                "customer_name": customer_name,
                "template": config.get("template_name", "cod_order_confirmation_ar"),
                "action": "confirmation_sent",
            },
        )
        sent += 1

    return sent


def _job_predictive_reorder(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    from datetime import timedelta

    days_before = int(config.get("days_before", 3))
    window_end = datetime.utcnow() + timedelta(days=days_before)
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
    from datetime import timedelta

    inactive_days = int(config.get("inactive_days", 60))
    discount_pct = int(config.get("discount_pct", 15))
    sent = 0

    threshold = datetime.utcnow() - timedelta(days=inactive_days)
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
        cutoff = datetime.utcnow() - timedelta(days=inactive_days // 2)
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
                "days_inactive": (datetime.utcnow() - profile.last_order_at).days if profile.last_order_at else inactive_days,
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
    auto.updated_at = datetime.utcnow()
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
    auto.updated_at = datetime.utcnow()
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
    settings = get_or_create_settings(db, tenant_id)
    current = merge_defaults(settings.ai_settings, DEFAULT_AI)
    current["autopilot_enabled"] = body.enabled
    settings.ai_settings = current
    settings.updated_at = datetime.utcnow()
    db.commit()
    return {"autopilot_enabled": body.enabled}


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
        created_at=datetime.utcnow(),
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
        require_subscription(db, int(tenant_id))

    current = _get_autopilot_settings(db, tenant_id)

    if body.enabled is not None:
        current["enabled"] = body.enabled

    for sub_key, sub_in in [
        ("cod_confirmation",   body.cod_confirmation),
        ("predictive_reorder", body.predictive_reorder),
        ("abandoned_cart",     body.abandoned_cart),
        ("inactive_recovery",  body.inactive_recovery),
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

    if ap["cod_confirmation"].get("enabled", True):
        results["cod_confirmation"] = _job_cod_confirmation(db, tenant_id, ap["cod_confirmation"])

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
        "ran_at": datetime.utcnow().isoformat(),
        "message": f"الطيار التلقائي أرسل {total} رسالة في هذه الجلسة",
    }
