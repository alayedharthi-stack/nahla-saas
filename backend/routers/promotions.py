"""
routers/promotions.py
─────────────────────
Promotions CRUD + summary KPIs.

The merchant manages *Promotions* — automatic discount rules — from the
"العروض" page in the dashboard. A Promotion stores the *terms* of a
discount (type, value, conditions, validity window). When an automation
fires, `services.promotion_engine.materialise_for_customer` issues a
personal `Coupon` row carrying those terms, so the same flow works
across Salla / Zid / Shopify without depending on each platform's
promotional API surface.

Endpoints
─────────
  GET    /promotions                  list (with optional ?status= filter)
  POST   /promotions                  create
  GET    /promotions/{id}             single + materialisation stats
  PUT    /promotions/{id}             update (partial)
  DELETE /promotions/{id}             delete
  POST   /promotions/{id}/activate    set status='active'
  POST   /promotions/{id}/pause       set status='paused'
  GET    /promotions/summary          headline KPIs for the page header
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import Coupon, Promotion, SmartAutomation
from services.promotion_engine import (
    ACTIVE_STATUS,
    DRAFT_STATUS,
    EXPIRED_STATUS,
    PAUSED_STATUS,
    PROMOTION_STATUSES,
    PROMOTION_TYPES,
    SCHEDULED_STATUS,
    compute_effective_status,
    sweep_expired,
)


router = APIRouter(prefix="/promotions", tags=["Promotions"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class PromotionConditionsIn(BaseModel):
    min_order_amount: Optional[float] = None
    customer_segments: Optional[List[str]] = None
    applicable_products: Optional[List[str]] = None
    applicable_categories: Optional[List[int]] = None
    x_quantity: Optional[int] = None
    y_quantity: Optional[int] = None
    x_product_ids: Optional[List[str]] = None
    y_product_ids: Optional[List[str]] = None


class PromotionCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    promotion_type: str
    discount_value: Optional[float] = None
    conditions: Optional[PromotionConditionsIn] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    status: Optional[str] = DRAFT_STATUS
    usage_limit: Optional[int] = None
    extra_metadata: Optional[Dict[str, Any]] = None


class PromotionPatchIn(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    promotion_type: Optional[str] = None
    discount_value: Optional[float] = None
    conditions: Optional[PromotionConditionsIn] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    status: Optional[str] = None
    usage_limit: Optional[int] = None
    extra_metadata: Optional[Dict[str, Any]] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_type(promotion_type: Optional[str]) -> None:
    if promotion_type is None:
        return
    if promotion_type not in PROMOTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported promotion_type. Must be one of: {sorted(PROMOTION_TYPES)}",
        )


def _validate_status(status: Optional[str]) -> None:
    if status is None:
        return
    if status not in PROMOTION_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported status. Must be one of: {sorted(PROMOTION_STATUSES)}",
        )


def _coerce_decimal(value: Optional[float]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="Invalid discount_value")


def _to_dict(promo: Promotion) -> Dict[str, Any]:
    """Serialise a Promotion row to the dashboard payload shape."""
    effective = compute_effective_status(promo)
    return {
        "id": promo.id,
        "name": promo.name,
        "description": promo.description,
        "promotion_type": promo.promotion_type,
        "discount_value": (
            float(promo.discount_value) if promo.discount_value is not None else None
        ),
        "conditions": dict(promo.conditions or {}),
        "starts_at": promo.starts_at.isoformat() if promo.starts_at else None,
        "ends_at": promo.ends_at.isoformat() if promo.ends_at else None,
        "status": promo.status,
        "effective_status": effective,
        "is_live": effective == ACTIVE_STATUS,
        "usage_count": int(promo.usage_count or 0),
        "usage_limit": promo.usage_limit,
        "extra_metadata": dict(promo.extra_metadata or {}),
        "created_at": promo.created_at.isoformat() if promo.created_at else None,
        "updated_at": promo.updated_at.isoformat() if promo.updated_at else None,
    }


def _strip_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Postgres + SQLite both store naive in our schema → always strip tz."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_promotions(
    request: Request,
    status: Optional[str] = Query(None),
    promotion_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    # Best-effort sweep so the merchant sees correct badges.
    sweep_expired(db, tenant_id)

    q = db.query(Promotion).filter(Promotion.tenant_id == tenant_id)
    if status:
        _validate_status(status)
        q = q.filter(Promotion.status == status)
    if promotion_type:
        _validate_type(promotion_type)
        q = q.filter(Promotion.promotion_type == promotion_type)

    rows = q.order_by(Promotion.id.desc()).limit(500).all()
    return {"promotions": [_to_dict(r) for r in rows]}


@router.get("/seasonal-calendar")
async def seasonal_calendar(
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Structured view of the public seasonal calendar — one entry per
    occasion (Founding Day, National Day, Ramadan, Eid Fitr, Eid Adha,
    White Friday, Salary Payday).

    Each entry binds the merchant-editable Promotion row that drives the
    occasion to the linked SmartAutomation surface plus an AI summary
    explaining what the autopilot does once the offer is activated. The
    Promotions page renders this as the "Seasonal Calendar" panel above
    the freeform promotions grid so the merchant has a first-class
    configuration surface for every public occasion the engine knows
    about — without having to invent the offer from scratch.

    Idempotently calls `ensure_default_promotions_for_tenant` so a fresh
    tenant gets the default seasonal Promotion rows on first visit. The
    response always includes the canonical occasion list even if a row
    is missing — the card renders with a "configure" CTA in that case.
    """
    from core.automations_seed import (  # noqa: PLC0415
        SEASONAL_OCCASIONS,
        ensure_default_promotions_for_tenant,
    )
    from core.calendar_events import event_for_slug, next_occurrence_for  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    try:
        ensure_default_promotions_for_tenant(db, tenant_id)
        db.commit()
    except Exception:  # pragma: no cover — never block a read
        db.rollback()

    sweep_expired(db, tenant_id)

    promos = (
        db.query(Promotion)
        .filter(Promotion.tenant_id == tenant_id)
        .all()
    )
    by_promo_slug: Dict[str, Promotion] = {}
    by_occasion: Dict[str, Promotion] = {}
    for promo in promos:
        meta = promo.extra_metadata or {}
        if meta.get("slug"):
            by_promo_slug[str(meta["slug"])] = promo
        if meta.get("occasion_slug"):
            by_occasion.setdefault(str(meta["occasion_slug"]), promo)

    automations_by_type: Dict[str, SmartAutomation] = {}
    for auto in (
        db.query(SmartAutomation)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .all()
    ):
        automations_by_type.setdefault(auto.automation_type, auto)

    occasions: List[Dict[str, Any]] = []
    for spec in SEASONAL_OCCASIONS:
        occasion_slug = spec["occasion_slug"]
        promo = by_occasion.get(occasion_slug) or by_promo_slug.get(spec["promotion_slug"])

        ev = event_for_slug(occasion_slug)
        if ev is not None:
            display_name = ev.name_ar
            category = ev.category
            next_date = next_occurrence_for(occasion_slug)
        elif occasion_slug == "salary_payday":
            display_name = "يوم الراتب"
            category = "salary"
            next_date = None
        else:
            display_name = occasion_slug
            category = "other"
            next_date = None

        auto = automations_by_type.get(spec["automation_type"])
        occasions.append({
            "occasion_slug":   occasion_slug,
            "name":            display_name,
            "category":        category,
            "next_date":       next_date.isoformat() if next_date else None,
            "ai_summary":      spec["ai_summary"],
            "automation_type": spec["automation_type"],
            "automation_id":   getattr(auto, "id", None),
            "automation_enabled": bool(getattr(auto, "enabled", False)),
            "promotion":       _to_dict(promo) if promo is not None else None,
            "promotion_slug":  spec["promotion_slug"],
        })

    return {"occasions": occasions}


@router.get("/summary")
async def promotions_summary(
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    sweep_expired(db, tenant_id)

    rows = db.query(Promotion).filter(Promotion.tenant_id == tenant_id).all()

    by_status: Dict[str, int] = {s: 0 for s in PROMOTION_STATUSES}
    by_type: Dict[str, int] = {t: 0 for t in PROMOTION_TYPES}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_type[r.promotion_type] = by_type.get(r.promotion_type, 0) + 1

    # Materialised coupons that came from a promotion → real usage signal.
    materialised_total = (
        db.query(func.count(Coupon.id))
        .filter(
            Coupon.tenant_id == tenant_id,
            Coupon.extra_metadata["source"].astext == "promotion",
        )
        .scalar()
        or 0
    )

    return {
        "total":             len(rows),
        "active":            by_status.get(ACTIVE_STATUS, 0),
        "scheduled":         by_status.get(SCHEDULED_STATUS, 0),
        "paused":            by_status.get(PAUSED_STATUS, 0),
        "draft":             by_status.get(DRAFT_STATUS, 0),
        "expired":           by_status.get(EXPIRED_STATUS, 0),
        "by_type":           by_type,
        "codes_materialised": int(materialised_total),
    }


@router.post("", status_code=201)
async def create_promotion(
    body: PromotionCreateIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    _validate_type(body.promotion_type)
    _validate_status(body.status)

    promo = Promotion(
        tenant_id=tenant_id,
        name=body.name.strip(),
        description=(body.description or "").strip() or None,
        promotion_type=body.promotion_type,
        discount_value=_coerce_decimal(body.discount_value),
        conditions=(body.conditions.dict(exclude_none=True) if body.conditions else {}),
        starts_at=_strip_naive(body.starts_at),
        ends_at=_strip_naive(body.ends_at),
        status=body.status or DRAFT_STATUS,
        usage_limit=body.usage_limit,
        extra_metadata=dict(body.extra_metadata or {}),
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return _to_dict(promo)


@router.get("/{promotion_id}")
async def get_promotion(
    promotion_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    promo = (
        db.query(Promotion)
        .filter(Promotion.id == promotion_id, Promotion.tenant_id == tenant_id)
        .first()
    )
    if promo is None:
        raise HTTPException(status_code=404, detail="Promotion not found")
    return _to_dict(promo)


@router.put("/{promotion_id}")
async def update_promotion(
    promotion_id: int,
    body: PromotionPatchIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    promo = (
        db.query(Promotion)
        .filter(Promotion.id == promotion_id, Promotion.tenant_id == tenant_id)
        .first()
    )
    if promo is None:
        raise HTTPException(status_code=404, detail="Promotion not found")

    if body.promotion_type is not None:
        _validate_type(body.promotion_type)
        promo.promotion_type = body.promotion_type
    if body.status is not None:
        _validate_status(body.status)
        promo.status = body.status
    if body.name is not None:
        promo.name = body.name.strip()
    if body.description is not None:
        promo.description = body.description.strip() or None
    if body.discount_value is not None:
        promo.discount_value = _coerce_decimal(body.discount_value)
    if body.conditions is not None:
        promo.conditions = body.conditions.dict(exclude_none=True)
        flag_modified(promo, "conditions")
    if body.starts_at is not None:
        promo.starts_at = _strip_naive(body.starts_at)
    if body.ends_at is not None:
        promo.ends_at = _strip_naive(body.ends_at)
    if body.usage_limit is not None:
        promo.usage_limit = body.usage_limit
    if body.extra_metadata is not None:
        promo.extra_metadata = dict(body.extra_metadata)
        flag_modified(promo, "extra_metadata")

    promo.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(promo)
    return _to_dict(promo)


@router.delete("/{promotion_id}", status_code=204)
async def delete_promotion(
    promotion_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    promo = (
        db.query(Promotion)
        .filter(Promotion.id == promotion_id, Promotion.tenant_id == tenant_id)
        .first()
    )
    if promo is None:
        raise HTTPException(status_code=404, detail="Promotion not found")
    db.delete(promo)
    db.commit()
    return None


@router.post("/{promotion_id}/activate")
async def activate_promotion(
    promotion_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    promo = (
        db.query(Promotion)
        .filter(Promotion.id == promotion_id, Promotion.tenant_id == tenant_id)
        .first()
    )
    if promo is None:
        raise HTTPException(status_code=404, detail="Promotion not found")

    effective = compute_effective_status(promo)
    if effective == EXPIRED_STATUS:
        raise HTTPException(
            status_code=409,
            detail="Cannot activate an expired promotion. Update ends_at first.",
        )

    promo.status = ACTIVE_STATUS
    promo.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(promo)
    return _to_dict(promo)


@router.post("/{promotion_id}/pause")
async def pause_promotion(
    promotion_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    promo = (
        db.query(Promotion)
        .filter(Promotion.id == promotion_id, Promotion.tenant_id == tenant_id)
        .first()
    )
    if promo is None:
        raise HTTPException(status_code=404, detail="Promotion not found")

    promo.status = PAUSED_STATUS
    promo.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(promo)
    return _to_dict(promo)
