"""
routers/intelligence.py
────────────────────────
Customer intelligence dashboard — unified deterministic status + RFM insights.

Routes:
  GET  /intelligence/dashboard              — full intelligence summary
  GET  /intelligence/reorder-predictions    — predictive reorder list
  POST /intelligence/analyze-customers      — re-compute segments for all profiles
  GET  /intelligence/segments/live          — real-time segment counts
  GET  /intelligence/customer-profile/{id}  — full profile for one customer
  POST /intelligence/reorder-estimate       — create a predictive reorder estimate
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func as sqlfunc
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
from services.customer_intelligence import (
    CUSTOMER_STATUS_LABELS,
    RFM_SEGMENT_LABELS,
    CustomerIntelligenceService,
)


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Make a datetime timezone-aware (UTC). DB stores naive UTC datetimes."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

router = APIRouter()


def _cleanup_demo_customers(db: Session, tenant_id: int) -> None:
    """Remove any demo customers (seeded with @example.com emails) from the DB."""
    import logging as _log
    _logger = _log.getLogger("nahla-backend")
    demo_customers = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.email.like("%@example.com"))
        .all()
    )
    if not demo_customers:
        return
    for c in demo_customers:
        db.query(CustomerProfile).filter(CustomerProfile.customer_id == c.id).delete()
        db.delete(c)
    db.flush()
    _logger.info("Cleaned up %d demo customers for tenant_id=%s", len(demo_customers), tenant_id)


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
        _cleanup_demo_customers(db, tenant_id)
        db.commit()
    except Exception as exc:
        _logger.error("intelligence cleanup failed: %s", exc, exc_info=True)
        db.rollback()

    autos = db.query(SmartAutomation).filter(SmartAutomation.tenant_id == tenant_id).all()
    active_automations = sum(1 for a in autos if a.enabled)
    now = datetime.now(timezone.utc)
    intelligence = CustomerIntelligenceService(db, tenant_id)
    metrics = intelligence.customers_metrics_summary()
    seg_map = metrics["status_counts"]

    vip_rows = (
        db.query(CustomerProfile, Customer)
        .join(Customer, CustomerProfile.customer_id == Customer.id)
        .filter(
            CustomerProfile.tenant_id == tenant_id,
            CustomerProfile.customer_status == "vip",
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
            CustomerProfile.customer_status.in_(["at_risk", "inactive"]),
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
            "days_inactive": max(0, (now - (_utc(p.last_order_at) or now)).days),
            "risk_score": round((p.churn_risk_score or 0) * 100),
        }
        for p, c in churn_rows
    ]

    reorder_predictions = (
        db.query(PredictiveReorderEstimate, CustomerProfile, Customer, Product)
        .join(CustomerProfile, PredictiveReorderEstimate.customer_id == CustomerProfile.customer_id)
        .join(Customer, Customer.id == PredictiveReorderEstimate.customer_id)
        .join(Product, Product.id == PredictiveReorderEstimate.product_id)
        .filter(PredictiveReorderEstimate.tenant_id == tenant_id)
        .order_by(PredictiveReorderEstimate.predicted_reorder_date.asc())
        .limit(10)
        .all()
    )
    reorder_list = [
        {
            "customer_name": c.name or "—",
            "phone": c.phone or "",
            "product_name": p.title if p else "—",
            "predicted_date": r.predicted_reorder_date.isoformat() if r.predicted_reorder_date else "",
            "confidence": 75,
        }
        for r, _cp, c, p in reorder_predictions
    ]

    suggestions: List[Dict[str, Any]] = []
    if reorder_list:
        suggestions.append({
            "id": "s1", "type": "reorder", "priority": "high",
            "title": f"أطلق حملة إعادة طلب ({len(reorder_list)} عملاء)",
            "desc": f"{len(reorder_list)} عملاء يُتوقع احتياجهم لإعادة الطلب قريباً.",
            "action": "launch_campaign",
            "automation_type": "predictive_reorder",
        })
    if churn_risk:
        suggestions.append({
            "id": "s2", "type": "winback", "priority": "medium",
            "title": f"{len(churn_risk)} عملاء في خطر المغادرة",
            "desc": "العملاء غير النشطين أو المعرّضين للمغادرة يحتاجون حملة استعادة.",
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
            "reorder_soon_count": len(reorder_list),
            "churn_risk_count": len(churn_risk),
            "vip_count": len(vip_customers),
            "active_automations": active_automations,
            "leads_count": metrics["leads"],
            "inactive_count": metrics["inactive_customers"],
        },
        "reorder_predictions": reorder_list,
        "churn_risk": churn_risk,
        "vip_customers": vip_customers,
        "suggestions": suggestions,
        "segments": [
            {"key": "lead",     "label": CUSTOMER_STATUS_LABELS["lead"],     "count": seg_map.get("lead", 0),     "color": "blue"},
            {"key": "new",      "label": CUSTOMER_STATUS_LABELS["new"],      "count": seg_map.get("new", 0),      "color": "blue"},
            {"key": "active",   "label": CUSTOMER_STATUS_LABELS["active"],   "count": seg_map.get("active", 0),   "color": "green"},
            {"key": "vip",      "label": "VIP",                              "count": seg_map.get("vip", 0),      "color": "amber"},
            {"key": "at_risk",  "label": CUSTOMER_STATUS_LABELS["at_risk"],  "count": seg_map.get("at_risk", 0),  "color": "red"},
            {"key": "inactive", "label": CUSTOMER_STATUS_LABELS["inactive"], "count": seg_map.get("inactive", 0), "color": "slate"},
        ],
        "rfm_segments": [
            {"key": key, "label": RFM_SEGMENT_LABELS.get(key, key), "count": count}
            for key, count in metrics["rfm_segment_counts"].items()
            if count > 0
        ],
    }


@router.get("/intelligence/reorder-predictions")
async def reorder_predictions(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()
    rows = (
        db.query(PredictiveReorderEstimate, Customer, Product)
        .join(Customer, Customer.id == PredictiveReorderEstimate.customer_id)
        .join(Product, Product.id == PredictiveReorderEstimate.product_id)
        .filter(PredictiveReorderEstimate.tenant_id == tenant_id)
        .order_by(PredictiveReorderEstimate.predicted_reorder_date.asc())
        .limit(20)
        .all()
    )
    predictions = [
        {
            "customer_name": c.name or "—",
            "phone": c.phone or "",
            "product_name": p.title if p else "—",
            "predicted_date": r.predicted_reorder_date.isoformat() if r.predicted_reorder_date else "",
            "confidence": 75,
        }
        for r, c, p in rows
    ]
    return {"predictions": predictions}


@router.post("/intelligence/analyze-customers")
async def analyze_customers(request: Request, db: Session = Depends(get_db)):
    """Rebuild all profiles using the unified deterministic intelligence service."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _cleanup_demo_customers(db, tenant_id)
    db.commit()

    try:
        rebuilt = CustomerIntelligenceService(db, tenant_id).rebuild_profiles_for_tenant(
            reason="manual_analyze_customers",
            commit=True,
            emit_event=True,
        )
    except Exception as exc:
        # Fail loudly — this endpoint used to silently catch-and-return 0, which
        # hid classification bugs. Surface the real error so ops can diagnose.
        import logging as _logging  # noqa: PLC0415
        _logging.getLogger("nahla-backend").exception(
            "[Intelligence] rebuild_profiles_for_tenant failed tenant=%s: %s",
            tenant_id, exc,
        )
        try:
            from core.obs import EVENTS, log_event  # noqa: PLC0415
            log_event(
                EVENTS.CUSTOMER_CLASSIFICATION_ERROR,
                tenant_id=tenant_id,
                reason="manual_analyze_customers",
                err=exc,
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"Customer analysis failed: {type(exc).__name__}: {exc}",
        )
    return {
        "analyzed": rebuilt,
        "profiles_rebuilt": rebuilt,
        "message": f"تم تحليل {rebuilt} عميل وتحديث حالتهم ودرجات RFM",
    }


@router.get("/intelligence/segments/live")
async def live_segments(request: Request, db: Session = Depends(get_db)):
    """Return real-time segment counts computed from CustomerProfile records."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _cleanup_demo_customers(db, tenant_id)
    db.commit()
    seg_map = CustomerIntelligenceService(db, tenant_id).status_counts()

    return {
        "segments": [
            {"key": "lead",     "label": CUSTOMER_STATUS_LABELS["lead"],     "count": seg_map.get("lead", 0),     "color": "blue"},
            {"key": "new",      "label": CUSTOMER_STATUS_LABELS["new"],      "count": seg_map.get("new", 0),      "color": "blue"},
            {"key": "active",   "label": CUSTOMER_STATUS_LABELS["active"],   "count": seg_map.get("active", 0),   "color": "green"},
            {"key": "vip",      "label": "VIP",                              "count": seg_map.get("vip", 0),      "color": "amber"},
            {"key": "at_risk",  "label": CUSTOMER_STATUS_LABELS["at_risk"],  "count": seg_map.get("at_risk", 0),  "color": "red"},
            {"key": "inactive", "label": CUSTOMER_STATUS_LABELS["inactive"], "count": seg_map.get("inactive", 0), "color": "slate"},
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

    profile_data: Dict[str, Any] = {
        "customer_id": customer.id,
        "customer_name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
    }

    if profile:
        days_inactive = (
            (datetime.now(timezone.utc) - _utc(profile.last_order_at)).days
            if profile.last_order_at
            else None
        )
        profile_data.update({
            "total_orders": profile.total_orders,
            "total_spent": profile.total_spend_sar,
            "average_order_value": profile.average_order_value_sar,
            "first_order_date": profile.first_order_at.isoformat() if getattr(profile, "first_order_at", None) else None,
            "last_order_date": profile.last_order_at.isoformat() if profile.last_order_at else None,
            "first_seen_date": profile.first_seen_at.isoformat() if profile.first_seen_at else None,
            "days_inactive": days_inactive,
            "status": profile.customer_status or profile.segment or "lead",
            "status_label": CUSTOMER_STATUS_LABELS.get(profile.customer_status or profile.segment or "lead", profile.customer_status or profile.segment or "lead"),
            "segment": profile.customer_status or profile.segment or "lead",
            "segment_label": CUSTOMER_STATUS_LABELS.get(profile.customer_status or profile.segment or "lead", profile.customer_status or profile.segment or "lead"),
            "rfm_segment": getattr(profile, "rfm_segment", None) or "lead",
            "rfm_segment_label": RFM_SEGMENT_LABELS.get(getattr(profile, "rfm_segment", None) or "lead", getattr(profile, "rfm_segment", None) or "lead"),
            "rfm_scores": {
                "recency": int(getattr(profile, "rfm_recency_score", 0) or 0),
                "frequency": int(getattr(profile, "rfm_frequency_score", 0) or 0),
                "monetary": int(getattr(profile, "rfm_monetary_score", 0) or 0),
                "total": int(getattr(profile, "rfm_total_score", 0) or 0),
                "code": getattr(profile, "rfm_code", None),
            },
            "churn_risk_score": profile.churn_risk_score,
            "lifetime_value_score": profile.lifetime_value_score,
            "is_returning": profile.is_returning,
            "metrics_computed_at": profile.metrics_computed_at.isoformat() if getattr(profile, "metrics_computed_at", None) else None,
            "last_recomputed_reason": getattr(profile, "last_recomputed_reason", None),
        })
    else:
        profile_data.update({
            "total_orders": 0, "total_spent": 0, "average_order_value": 0,
            "first_order_date": None, "last_order_date": None, "first_seen_date": None, "days_inactive": None,
            "status": "lead", "status_label": CUSTOMER_STATUS_LABELS["lead"],
            "segment": "lead", "segment_label": CUSTOMER_STATUS_LABELS["lead"],
            "rfm_segment": "lead", "rfm_segment_label": RFM_SEGMENT_LABELS["lead"],
            "rfm_scores": {"recency": 0, "frequency": 0, "monetary": 0, "total": 0, "code": "000"},
            "churn_risk_score": 0.0, "lifetime_value_score": 0.0, "is_returning": False,
            "metrics_computed_at": None, "last_recomputed_reason": None,
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
