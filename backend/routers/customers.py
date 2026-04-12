"""
routers/customers.py
─────────────────────
Tenant-scoped customer CRUD with phone deduplication.

Routes:
  GET    /customers              — list customers with profiles, search, pagination
  GET    /customers/{id}         — single customer detail
  POST   /customers              — add customer manually (phone must be unique)
  PATCH  /customers/{id}         — update customer
  DELETE /customers/{id}         — delete customer
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import Customer, CustomerProfile

router = APIRouter(prefix="/customers", tags=["Customers"])

_PHONE_RE = re.compile(r"[^\d+]")


def _normalize_phone(raw: str) -> str:
    """Normalize phone: keep leading +, strip spaces/dashes, ensure + prefix for intl codes."""
    cleaned = _PHONE_RE.sub("", raw).strip()
    if not cleaned:
        return ""
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    elif cleaned.startswith("05") and len(cleaned) == 10:
        cleaned = "+966" + cleaned[1:]
    elif cleaned.startswith("5") and len(cleaned) == 9:
        cleaned = "+966" + cleaned
    elif not cleaned.startswith("+") and len(cleaned) >= 10:
        cleaned = "+" + cleaned
    return cleaned


class CustomerCreateIn(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None

    @validator("phone")
    def validate_phone(cls, v: str) -> str:
        normalized = _normalize_phone(v)
        if not normalized or len(normalized) < 8:
            raise ValueError("رقم الهاتف غير صالح")
        return normalized


class CustomerPatchIn(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

    @validator("phone", pre=True)
    def validate_phone(cls, v):
        if v is None:
            return v
        normalized = _normalize_phone(v)
        if not normalized or len(normalized) < 8:
            raise ValueError("رقم الهاتف غير صالح")
        return normalized


SEGMENT_LABELS = {
    "new": "عميل جديد",
    "active": "عميل نشط",
    "vip": "عميل VIP",
    "at_risk": "في خطر المغادرة",
    "churned": "خامل",
}


def _serialize_customer(cust: Customer, profile: Optional[CustomerProfile]) -> Dict[str, Any]:
    meta = cust.extra_metadata or {}
    source = meta.get("source", "salla")

    result: Dict[str, Any] = {
        "id": cust.id,
        "name": cust.name or "",
        "phone": cust.phone or "",
        "email": cust.email or "",
        "source": source,
        "source_label": "مضاف يدوياً" if source == "manual" else "سلة",
    }
    if profile:
        segment = profile.segment or "new"
        result.update({
            "segment": segment,
            "segment_label": SEGMENT_LABELS.get(segment, segment),
            "total_orders": profile.total_orders or 0,
            "total_spend": round(float(profile.total_spend_sar or 0), 2),
            "average_order_value": round(float(profile.average_order_value_sar or 0), 2),
            "last_order_at": profile.last_order_at.isoformat() if profile.last_order_at else None,
            "first_seen_at": profile.first_seen_at.isoformat() if profile.first_seen_at else None,
            "churn_risk_score": round(float(profile.churn_risk_score or 0), 3),
            "is_returning": profile.is_returning or False,
        })
    else:
        result.update({
            "segment": "new",
            "segment_label": SEGMENT_LABELS["new"],
            "total_orders": 0,
            "total_spend": 0,
            "average_order_value": 0,
            "last_order_at": None,
            "first_seen_at": None,
            "churn_risk_score": 0,
            "is_returning": False,
        })
    return result


@router.get("")
async def list_customers(
    request: Request,
    search: str = Query("", description="بحث بالاسم أو الهاتف"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    q = db.query(Customer).filter(Customer.tenant_id == tenant_id)

    if search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            (Customer.name.ilike(term)) | (Customer.phone.ilike(term))
        )

    total = q.count()
    rows = (
        q.order_by(Customer.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    customer_ids = [c.id for c in rows]
    profiles = {}
    if customer_ids:
        prof_rows = (
            db.query(CustomerProfile)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.customer_id.in_(customer_ids),
            )
            .all()
        )
        profiles = {p.customer_id: p for p in prof_rows}

    return {
        "customers": [_serialize_customer(c, profiles.get(c.id)) for c in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


@router.get("/{customer_id}")
async def get_customer(customer_id: int, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    profile = db.query(CustomerProfile).filter_by(
        customer_id=cust.id, tenant_id=tenant_id,
    ).first()

    return _serialize_customer(cust, profile)


@router.post("")
async def create_customer(body: CustomerCreateIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    existing = db.query(Customer).filter(
        Customer.tenant_id == tenant_id,
        Customer.phone == body.phone,
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"يوجد عميل بنفس رقم الواتساب: {existing.name or existing.phone}",
        )

    cust = Customer(
        tenant_id=tenant_id,
        name=body.name,
        phone=body.phone,
        email=body.email,
        extra_metadata={"source": "manual"},
    )
    db.add(cust)
    db.flush()

    profile = CustomerProfile(
        customer_id=cust.id,
        tenant_id=tenant_id,
        segment="new",
        first_seen_at=datetime.now(timezone.utc),
    )
    db.add(profile)
    db.commit()
    db.refresh(cust)

    return {"id": cust.id, "message": "تم إضافة العميل بنجاح"}


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: int, body: CustomerPatchIn, request: Request, db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    if body.phone is not None and body.phone != cust.phone:
        dup = db.query(Customer).filter(
            Customer.tenant_id == tenant_id,
            Customer.phone == body.phone,
            Customer.id != customer_id,
        ).first()
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"يوجد عميل آخر بنفس الرقم: {dup.name or dup.phone}",
            )
        cust.phone = body.phone

    if body.name is not None:
        cust.name = body.name
    if body.email is not None:
        cust.email = body.email

    db.commit()
    return {"updated": True}


@router.delete("/{customer_id}")
async def delete_customer(customer_id: int, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    db.query(CustomerProfile).filter_by(customer_id=cust.id, tenant_id=tenant_id).delete()
    db.delete(cust)
    db.commit()
    return {"deleted": True}
