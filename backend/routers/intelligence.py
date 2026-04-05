"""
routers/intelligence.py
────────────────────────
Customer intelligence dashboard — segments, churn risk, reorder predictions.

Routes:
  GET  /intelligence/dashboard              — full intelligence summary
  GET  /intelligence/reorder-predictions    — predictive reorder list
  POST /intelligence/analyze-customers      — re-compute segments for all profiles
  GET  /intelligence/segments/live          — real-time segment counts
  GET  /intelligence/customer-profile/{id}  — full profile for one customer
  POST /intelligence/reorder-estimate       — create a predictive reorder estimate
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from models import (  # noqa: E402
    Customer,
    CustomerProfile,
    PredictiveReorderEstimate,
    Product,
    SmartAutomation,
)

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from core.automations_seed import seed_automations_if_empty as _seed_automations_if_empty

router = APIRouter()


# ── Demo / mock data ───────────────────────────────────────────────────────────

DEMO_CUSTOMERS_DATA: List[Dict[str, Any]] = [
    {"name": "أحمد الراشد",   "phone": "+966501234567", "email": "ahmed@example.com",
     "total_orders": 18, "total_spend": 4820.0, "days_since_last_order": 7},
    {"name": "نورا المطيري",  "phone": "+966526543210", "email": "nora@example.com",
     "total_orders": 6,  "total_spend": 1240.0, "days_since_last_order": 18},
    {"name": "خالد إبراهيم",  "phone": "+966578887766", "email": "khalid@example.com",
     "total_orders": 12, "total_spend": 3150.0, "days_since_last_order": 5},
    {"name": "ليلى السعود",   "phone": "+966545512200", "email": "leila@example.com",
     "total_orders": 3,  "total_spend": 540.0,  "days_since_last_order": 30},
    {"name": "عمر الغامدي",   "phone": "+966563219900", "email": "omar@example.com",
     "total_orders": 9,  "total_spend": 2340.0, "days_since_last_order": 22},
    {"name": "ريم الحربي",    "phone": "+966554100033", "email": "reem@example.com",
     "total_orders": 4,  "total_spend": 820.0,  "days_since_last_order": 110},
    {"name": "يوسف الشهري",   "phone": "+966507755522", "email": "yousef@example.com",
     "total_orders": 5,  "total_spend": 1100.0, "days_since_last_order": 71},
    {"name": "سارة القحطاني", "phone": "+966532218800", "email": "sara@example.com",
     "total_orders": 3,  "total_spend": 650.0,  "days_since_last_order": 87},
    {"name": "محمد العتيبي",  "phone": "+966561234567", "email": "mohammed@example.com",
     "total_orders": 1,  "total_spend": 150.0,  "days_since_last_order": 3},
    {"name": "فاطمة الدوسري", "phone": "+966547896543", "email": "fatima@example.com",
     "total_orders": 2,  "total_spend": 380.0,  "days_since_last_order": 14},
    {"name": "عبدالله الزهراني", "phone": "+966509876543", "email": "abdullah@example.com",
     "total_orders": 7,  "total_spend": 1650.0, "days_since_last_order": 45},
    {"name": "منى الحارثي",   "phone": "+966551234000", "email": "mona@example.com",
     "total_orders": 1,  "total_spend": 250.0,  "days_since_last_order": 2},
]

DEMO_REORDER_PRODUCTS: List[Dict[str, Any]] = [
    {"product_name": "عسل السدر 500g",    "consumption_days": 30},
    {"product_name": "عسل الطلح 1kg",     "consumption_days": 60},
    {"product_name": "عسل الأكاسيا 250g", "consumption_days": 20},
    {"product_name": "عسل السمر 1kg",     "consumption_days": 60},
]

_MOCK_REORDER_CUSTOMERS = [
    {"customer_name": "Ahmed Al-Rashid", "phone": "+966 50 123 4567", "product_name": "عسل السدر 500g", "predicted_date": "2026-04-05", "confidence": 87},
    {"customer_name": "Nora Al-Mutairi",  "phone": "+966 52 654 3210", "product_name": "عسل الطلح 1kg",  "predicted_date": "2026-04-08", "confidence": 74},
    {"customer_name": "Khalid Ibrahim",   "phone": "+966 57 888 7766", "product_name": "عسل السدر 500g", "predicted_date": "2026-04-10", "confidence": 91},
    {"customer_name": "Lina Al-Saud",     "phone": "+966 54 551 2200", "product_name": "عسل الأكاسيا 250g", "predicted_date": "2026-04-12", "confidence": 68},
    {"customer_name": "Omar Al-Ghamdi",   "phone": "+966 56 321 9900", "product_name": "عسل السمر 1kg",  "predicted_date": "2026-04-14", "confidence": 82},
]

_MOCK_CHURN_RISK = [
    {"customer_name": "Reem Al-Harbi",    "phone": "+966 55 410 0033", "last_purchase": "2025-12-10", "days_inactive": 110, "risk_score": 0.82},
    {"customer_name": "Yousef Al-Shehri", "phone": "+966 50 775 5522", "last_purchase": "2026-01-18", "days_inactive": 71,  "risk_score": 0.65},
    {"customer_name": "Sara Al-Qahtani",  "phone": "+966 53 221 8800", "last_purchase": "2026-01-02", "days_inactive": 87,  "risk_score": 0.71},
]

_MOCK_VIP_CUSTOMERS = [
    {"customer_name": "Ahmed Al-Rashid",  "total_spent": 4820, "orders": 18, "segment": "VIP"},
    {"customer_name": "Khalid Ibrahim",   "total_spent": 3150, "orders": 12, "segment": "VIP"},
    {"customer_name": "Omar Al-Ghamdi",   "total_spent": 2340, "orders": 9,  "segment": "VIP"},
]

_MOCK_SUGGESTIONS = [
    {
        "id": "s1",
        "type": "reorder",
        "priority": "high",
        "title": "أطلق حملة إعادة طلب لعملاء عسل السدر",
        "desc": "5 عملاء يُتوقع احتياجهم لإعادة الطلب خلال 2 أسبوع.",
        "action": "launch_campaign",
        "automation_type": "predictive_reorder",
    },
    {
        "id": "s2",
        "type": "winback",
        "priority": "medium",
        "title": "3 عملاء في خطر المغادرة",
        "desc": "لم يتسوقوا منذ أكثر من 60 يوماً — أرسل عرضاً لاستعادتهم.",
        "action": "launch_campaign",
        "automation_type": "customer_winback",
    },
    {
        "id": "s3",
        "type": "vip",
        "priority": "low",
        "title": "فعّل التشغيل التلقائي لـ VIP",
        "desc": "3 عملاء أنفقوا أكثر من 2000 ر.س ولم يتلقوا عرض VIP بعد.",
        "action": "enable_automation",
        "automation_type": "vip_upgrade",
    },
]


# ── Helper functions ───────────────────────────────────────────────────────────

def _compute_customer_segment(total_orders: int, total_spend: float, days_inactive: int) -> tuple:
    """Classify a customer into one of 5 segments and compute a churn risk score."""
    from math import exp

    if days_inactive <= 14:
        churn_risk = max(0.02, days_inactive * 0.005)
    elif days_inactive <= 30:
        churn_risk = 0.10 + (days_inactive - 14) * 0.008
    elif days_inactive <= 60:
        churn_risk = 0.23 + (days_inactive - 30) * 0.01
    elif days_inactive <= 90:
        churn_risk = 0.53 + (days_inactive - 60) * 0.008
    else:
        churn_risk = 0.77 + min((days_inactive - 90) * 0.002, 0.23)

    churn_risk = round(min(churn_risk, 1.0), 3)

    if days_inactive > 90:
        segment = "churned"
    elif days_inactive > 60:
        segment = "at_risk"
    elif total_orders <= 1:
        segment = "new"
    elif total_spend >= 2000 and total_orders >= 5:
        segment = "vip"
    else:
        segment = "active"

    return segment, churn_risk


def _seed_demo_customers(db: Session, tenant_id: int) -> None:
    """Seed demo customers with CustomerProfile records if the tenant has none."""
    count = db.query(Customer).filter(Customer.tenant_id == tenant_id).count()
    if count > 0:
        return

    from datetime import timedelta, timezone

    for demo in DEMO_CUSTOMERS_DATA:
        customer = Customer(
            name=demo["name"],
            phone=demo["phone"],
            email=demo["email"],
            tenant_id=tenant_id,
        )
        db.add(customer)
        db.flush()

        days = demo["days_since_last_order"]
        last_order_at = datetime.now(timezone.utc) - timedelta(days=days)
        first_seen_at = last_order_at - timedelta(days=demo["total_orders"] * 14)

        segment, churn_risk = _compute_customer_segment(
            demo["total_orders"],
            demo["total_spend"],
            days,
        )

        profile = CustomerProfile(
            customer_id=customer.id,
            tenant_id=tenant_id,
            total_orders=demo["total_orders"],
            total_spend_sar=demo["total_spend"],
            average_order_value_sar=round(demo["total_spend"] / max(demo["total_orders"], 1), 2),
            max_single_order_sar=round(demo["total_spend"] / max(demo["total_orders"], 1) * 1.4, 2),
            segment=segment,
            churn_risk_score=churn_risk,
            is_returning=demo["total_orders"] > 1,
            first_seen_at=first_seen_at,
            last_seen_at=datetime.now(timezone.utc) - timedelta(days=max(1, days - 2)),
            last_order_at=last_order_at,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(profile)

    db.flush()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/intelligence/dashboard")
async def intelligence_dashboard(request: Request, db: Session = Depends(get_db)):
    """Return intelligence summary for the current tenant."""
    import logging as _log
    _logger = _log.getLogger("nahla-backend")
    tenant_id = resolve_tenant_id(request)
    _logger.info("intelligence/dashboard called for tenant_id=%s", tenant_id)
    try:
        get_or_create_tenant(db, tenant_id)
        _seed_automations_if_empty(db, tenant_id)
        _seed_demo_customers(db, tenant_id)
        db.commit()
    except Exception as exc:
        _logger.error("intelligence seed failed: %s", exc, exc_info=True)
        db.rollback()

    autos = db.query(SmartAutomation).filter(SmartAutomation.tenant_id == tenant_id).all()
    active_automations = sum(1 for a in autos if a.enabled)

    profiles_exist = (
        db.query(CustomerProfile).filter(CustomerProfile.tenant_id == tenant_id).count() > 0
    )

    if profiles_exist:
        now = datetime.now(timezone.utc)

        from sqlalchemy import func as sqlfunc
        seg_rows = (
            db.query(CustomerProfile.segment, sqlfunc.count(CustomerProfile.id))
            .filter(CustomerProfile.tenant_id == tenant_id)
            .group_by(CustomerProfile.segment)
            .all()
        )
        seg_map = {row[0]: row[1] for row in seg_rows}

        vip_rows = (
            db.query(CustomerProfile, Customer)
            .join(Customer, CustomerProfile.customer_id == Customer.id)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.segment == "vip",
            )
            .order_by(CustomerProfile.total_spend_sar.desc())
            .limit(10)
            .all()
        )
        vip_customers = [
            {
                "customer_name": c.name or "—",
                "total_spent": round(float(p.total_spend_sar or 0), 2),
                "orders": p.total_orders or 0,
                "segment": "VIP",
            }
            for p, c in vip_rows
        ]

        churn_rows = (
            db.query(CustomerProfile, Customer)
            .join(Customer, CustomerProfile.customer_id == Customer.id)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.segment.in_(["at_risk", "churned"]),
            )
            .order_by(CustomerProfile.churn_risk_score.desc())
            .limit(10)
            .all()
        )
        churn_risk = [
            {
                "customer_name": c.name or "—",
                "phone": c.phone or "",
                "last_purchase": (p.last_order_at or now).isoformat(),
                "days_inactive": max(0, (now - (p.last_order_at or now)).days),
                "risk_score": round((p.churn_risk_score or 0) * 100),
            }
            for p, c in churn_rows
        ]

        suggestions: List[Dict[str, Any]] = []
        reorder_count = len(_MOCK_REORDER_CUSTOMERS)
        if reorder_count > 0:
            suggestions.append({
                "id": "s1", "type": "reorder", "priority": "high",
                "title": "أطلق حملة إعادة طلب لعملاء عسل السدر",
                "desc": f"{reorder_count} عملاء يُتوقع احتياجهم لإعادة الطلب خلال أسبوعين.",
                "action": "launch_campaign",
                "automation_type": "predictive_reorder",
            })
        if churn_risk:
            suggestions.append({
                "id": "s2", "type": "winback", "priority": "medium",
                "title": f"{len(churn_risk)} عملاء في خطر المغادرة",
                "desc": "لم يتسوقوا منذ أكثر من 60 يوماً — أرسل عرضاً لاستعادتهم.",
                "action": "launch_campaign",
                "automation_type": "customer_winback",
            })
        vip_auto_on = any(a.automation_type == "vip_upgrade" and a.enabled for a in autos)
        if vip_customers and not vip_auto_on:
            suggestions.append({
                "id": "s3", "type": "vip", "priority": "low",
                "title": "فعّل التشغيل التلقائي لـ VIP",
                "desc": f"{len(vip_customers)} عملاء أنفقوا أكثر من 2000 ر.س ولم يتلقوا عرض VIP بعد.",
                "action": "enable_automation",
                "automation_type": "vip_upgrade",
            })

        return {
            "summary": {
                "reorder_soon_count": reorder_count,
                "churn_risk_count": len(churn_risk),
                "vip_count": len(vip_customers),
                "active_automations": active_automations,
            },
            "reorder_predictions": _MOCK_REORDER_CUSTOMERS,
            "churn_risk": churn_risk,
            "vip_customers": vip_customers,
            "suggestions": suggestions,
            "segments": [
                {"key": "new",     "label": "عملاء جدد",      "count": seg_map.get("new", 0),     "color": "blue"},
                {"key": "active",  "label": "عملاء نشطون",     "count": seg_map.get("active", 0),  "color": "green"},
                {"key": "vip",     "label": "VIP",              "count": seg_map.get("vip", 0),     "color": "amber"},
                {"key": "at_risk", "label": "خطر المغادرة",    "count": seg_map.get("at_risk", 0), "color": "red"},
                {"key": "churned", "label": "خاملون",           "count": seg_map.get("churned", 0), "color": "slate"},
            ],
        }

    return {
        "summary": {
            "reorder_soon_count": len(_MOCK_REORDER_CUSTOMERS),
            "churn_risk_count": len(_MOCK_CHURN_RISK),
            "vip_count": len(_MOCK_VIP_CUSTOMERS),
            "active_automations": active_automations,
        },
        "reorder_predictions": _MOCK_REORDER_CUSTOMERS,
        "churn_risk": _MOCK_CHURN_RISK,
        "vip_customers": _MOCK_VIP_CUSTOMERS,
        "suggestions": _MOCK_SUGGESTIONS,
        "segments": [
            {"key": "new",      "label": "عملاء جدد",    "count": 240, "color": "blue"},
            {"key": "active",   "label": "عملاء نشطون",   "count": 890, "color": "green"},
            {"key": "vip",      "label": "VIP",            "count": 83,  "color": "amber"},
            {"key": "churned",  "label": "خاملون",         "count": 420, "color": "slate"},
            {"key": "at_risk",  "label": "خطر المغادرة",  "count": 127, "color": "red"},
        ],
    }


@router.get("/intelligence/reorder-predictions")
async def reorder_predictions(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()
    return {"predictions": _MOCK_REORDER_CUSTOMERS}


@router.post("/intelligence/analyze-customers")
async def analyze_customers(request: Request, db: Session = Depends(get_db)):
    """Re-compute segment + churn_risk_score for every CustomerProfile in this tenant."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _seed_demo_customers(db, tenant_id)
    db.commit()

    profiles = db.query(CustomerProfile).filter(CustomerProfile.tenant_id == tenant_id).all()
    updated = 0

    for profile in profiles:
        days_inactive = (
            (datetime.now(timezone.utc) - profile.last_order_at).days
            if profile.last_order_at
            else 999
        )
        segment, churn_risk = _compute_customer_segment(
            profile.total_orders or 0,
            float(profile.total_spend_sar or 0),
            days_inactive,
        )
        profile.segment = segment
        profile.churn_risk_score = churn_risk
        profile.updated_at = datetime.now(timezone.utc)
        updated += 1

    db.commit()
    return {"analyzed": updated, "message": f"تم تحليل {updated} عميل وتحديث شرائحهم"}


@router.get("/intelligence/segments/live")
async def live_segments(request: Request, db: Session = Depends(get_db)):
    """Return real-time segment counts computed from CustomerProfile records."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _seed_demo_customers(db, tenant_id)
    db.commit()

    from sqlalchemy import func as sqlfunc
    rows = (
        db.query(CustomerProfile.segment, sqlfunc.count(CustomerProfile.id))
        .filter(CustomerProfile.tenant_id == tenant_id)
        .group_by(CustomerProfile.segment)
        .all()
    )
    seg_map = {r[0]: r[1] for r in rows}

    return {
        "segments": [
            {"key": "new",     "label": "عملاء جدد",      "count": seg_map.get("new", 0),     "color": "blue"},
            {"key": "active",  "label": "عملاء نشطون",     "count": seg_map.get("active", 0),  "color": "green"},
            {"key": "vip",     "label": "VIP",              "count": seg_map.get("vip", 0),     "color": "amber"},
            {"key": "at_risk", "label": "خطر المغادرة",    "count": seg_map.get("at_risk", 0), "color": "red"},
            {"key": "churned", "label": "خاملون",           "count": seg_map.get("churned", 0), "color": "slate"},
        ],
        "total": sum(seg_map.values()),
    }


@router.get("/intelligence/customer-profile/{customer_id}")
async def get_customer_profile(customer_id: int, request: Request, db: Session = Depends(get_db)):
    """Return the full behavior profile for a single customer."""
    tenant_id = resolve_tenant_id(request)
    customer = db.query(Customer).filter(
        Customer.id == customer_id,
        Customer.tenant_id == tenant_id,
    ).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    profile = db.query(CustomerProfile).filter(
        CustomerProfile.customer_id == customer_id,
        CustomerProfile.tenant_id == tenant_id,
    ).first()

    reorders = (
        db.query(PredictiveReorderEstimate)
        .filter(
            PredictiveReorderEstimate.customer_id == customer_id,
            PredictiveReorderEstimate.tenant_id == tenant_id,
        )
        .order_by(PredictiveReorderEstimate.predicted_reorder_date.asc())
        .all()
    )

    SEGMENT_LABELS = {
        "new": "عميل جديد",
        "active": "عميل نشط",
        "vip": "عميل VIP",
        "at_risk": "في خطر المغادرة",
        "churned": "خامل",
    }

    profile_data: Dict[str, Any] = {
        "customer_id": customer.id,
        "customer_name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
    }

    if profile:
        days_inactive = (
            (datetime.now(timezone.utc) - profile.last_order_at).days
            if profile.last_order_at
            else None
        )
        profile_data.update({
            "total_orders": profile.total_orders,
            "total_spent": profile.total_spend_sar,
            "average_order_value": profile.average_order_value_sar,
            "last_order_date": profile.last_order_at.isoformat() if profile.last_order_at else None,
            "first_seen_date": profile.first_seen_at.isoformat() if profile.first_seen_at else None,
            "days_inactive": days_inactive,
            "segment": profile.segment,
            "segment_label": SEGMENT_LABELS.get(profile.segment or "new", profile.segment),
            "churn_risk_score": profile.churn_risk_score,
            "lifetime_value_score": profile.lifetime_value_score,
            "is_returning": profile.is_returning,
        })
    else:
        profile_data.update({
            "total_orders": 0, "total_spent": 0, "average_order_value": 0,
            "last_order_date": None, "first_seen_date": None, "days_inactive": None,
            "segment": "new", "segment_label": "عميل جديد",
            "churn_risk_score": 0.0, "lifetime_value_score": 0.0, "is_returning": False,
        })

    reorder_data = []
    for r in reorders:
        product = db.query(Product).filter(
            Product.id == r.product_id, Product.tenant_id == tenant_id
        ).first()
        reorder_data.append({
            "product_id": r.product_id,
            "product_name": product.title if product else f"Product #{r.product_id}",
            "purchase_date": r.purchase_date.isoformat() if r.purchase_date else None,
            "predicted_reorder_date": r.predicted_reorder_date.isoformat() if r.predicted_reorder_date else None,
            "consumption_rate_days": r.consumption_rate_days,
            "notified": r.notified,
        })

    profile_data["reorder_estimates"] = reorder_data
    return profile_data


@router.post("/intelligence/reorder-estimate")
async def create_reorder_estimate(
    request: Request,
    db: Session = Depends(get_db),
):
    """Compute a predicted reorder date given product + purchase history."""
    body = await request.json()
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    from datetime import timedelta, timezone
    purchase_dt = datetime.now(timezone.utc)
    try:
        purchase_dt = datetime.fromisoformat(body.get("purchase_date", datetime.now(timezone.utc).isoformat()))
    except (ValueError, TypeError):
        pass

    consumption_days = int(body.get("consumption_rate_days", 30))
    predicted = purchase_dt + timedelta(days=consumption_days)

    estimate = PredictiveReorderEstimate(
        tenant_id=tenant_id,
        customer_id=int(body.get("customer_id", 0)),
        product_id=int(body.get("product_id", 0)),
        quantity_purchased=body.get("quantity_purchased"),
        purchase_date=purchase_dt,
        consumption_rate_days=consumption_days,
        predicted_reorder_date=predicted,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(estimate)
    db.commit()
    return {
        "predicted_reorder_date": predicted.isoformat(),
        "consumption_rate_days": consumption_days,
    }
