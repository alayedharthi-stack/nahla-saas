"""
routers/coupons.py
──────────────────
Tenant-scoped coupon listing and lightweight coupon dashboard endpoints.

Backed by the real `Coupon` table and tenant AI settings where available.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.database import get_db
from core.tenant import DEFAULT_AI, get_or_create_settings, get_or_create_tenant, merge_defaults, resolve_tenant_id
from models import Coupon, CustomerProfile

router = APIRouter(prefix="/coupons", tags=["Coupons"])

DEFAULT_COUPON_RULES = [
    {"id": "r1", "label": "إرسال كوبون تلقائي بعد ترك العربة (أكثر من 30 دقيقة)", "enabled": True},
    {"id": "r2", "label": "كوبون VIP للعملاء الذين لديهم أكثر من 5 طلبات", "enabled": True},
    {"id": "r3", "label": "خصم عيد الميلاد (10% في يوم ميلاد العميل)", "enabled": False},
    {"id": "r4", "label": "خصم التجميع — اشتر 3 واحصل على خصم 10%", "enabled": True},
    {"id": "r5", "label": "خصم أول شراء — 15% على أول طلب", "enabled": False},
]

DEFAULT_VIP_TIERS = [
    {"tier": "فضي", "threshold": "+3 طلبات", "discount": "10%"},
    {"tier": "ذهبي", "threshold": "+7 طلبات", "discount": "20%"},
    {"tier": "بلاتيني", "threshold": "+15 طلب", "discount": "30%"},
]


class CouponRuleIn(BaseModel):
    id: str
    label: str
    enabled: bool


class VipTierIn(BaseModel):
    tier: str
    threshold: str
    discount: str


class CouponCreateIn(BaseModel):
    code: str
    type: str = "percentage"
    value: str
    description: str = ""
    limit: int = 0
    expires: Optional[str] = None
    category: str = "standard"
    active: bool = True


class CouponPatchIn(BaseModel):
    code: Optional[str] = None
    type: Optional[str] = None
    value: Optional[str] = None
    description: Optional[str] = None
    limit: Optional[int] = None
    expires: Optional[str] = None
    category: Optional[str] = None
    active: Optional[bool] = None


class CouponDashboardSettingsIn(BaseModel):
    rules: List[CouponRuleIn]
    vip_tiers: List[VipTierIn]


def _ensure_coupon_dashboard_config(settings) -> Dict[str, Any]:
    meta = dict(settings.extra_metadata or {})
    coupon_dash = dict(meta.get("coupons_dashboard") or {})
    changed = False
    if "rules" not in coupon_dash:
        coupon_dash["rules"] = DEFAULT_COUPON_RULES
        changed = True
    if "vip_tiers" not in coupon_dash:
        coupon_dash["vip_tiers"] = DEFAULT_VIP_TIERS
        changed = True
    if changed:
        meta["coupons_dashboard"] = coupon_dash
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")
    return coupon_dash


@router.get("")
async def list_coupons(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    settings = get_or_create_settings(db, tenant_id)
    coupon_dash = _ensure_coupon_dashboard_config(settings)
    db.add(settings)
    db.commit()
    ai_settings = merge_defaults(settings.ai_settings or {}, DEFAULT_AI)

    rows = (
        db.query(Coupon)
        .filter(Coupon.tenant_id == tenant_id)
        .order_by(Coupon.id.desc())
        .limit(200)
        .all()
    )

    now = datetime.now(timezone.utc)
    coupons: List[Dict[str, Any]] = []
    for coupon in rows:
        meta = coupon.extra_metadata or {}
        expires = coupon.expires_at
        if expires and getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        active = expires is None or expires > now
        category = str(meta.get("category") or ("vip" if meta.get("vip") else ("auto" if meta.get("source") in {"automation", "widget"} else "standard")))
        active_override = meta.get("active")
        if isinstance(active_override, bool):
            active = active_override

        coupons.append({
            "id": str(coupon.id),
            "code": coupon.code,
            "type": coupon.discount_type or "percentage",
            "value": float(str(coupon.discount_value or "0").replace(",", ".")) if str(coupon.discount_value or "").replace(",", ".").replace(".", "", 1).isdigit() else coupon.discount_value,
            "usages": int(meta.get("usage_count", 0)),
            "limit": int(meta.get("usage_limit", 0) or 0),
            "expires": expires.isoformat() if expires else "",
            "category": category,
            "active": active,
        })

    vip_count = db.query(CustomerProfile).filter(
        CustomerProfile.tenant_id == tenant_id,
        CustomerProfile.customer_status == "vip",
    ).count()

    rules = list(coupon_dash.get("rules") or DEFAULT_COUPON_RULES)
    if rules:
        for rule in rules:
            if rule["id"] == "vip_customers":
                rule["enabled"] = vip_count > 0
            elif rule["id"] == "active_coupons":
                rule["enabled"] = any(c["active"] for c in coupons)
            elif rule["id"] == "coupon_rules":
                rule["enabled"] = bool((ai_settings.get("coupon_rules") or "").strip()) or bool(rule.get("enabled"))

    return {
        "rules": rules,
        "vip_tiers": list(coupon_dash.get("vip_tiers") or DEFAULT_VIP_TIERS),
        "coupons": coupons,
    }


@router.put("/settings")
async def save_coupon_dashboard_settings(
    body: CouponDashboardSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    settings = get_or_create_settings(db, tenant_id)
    meta = dict(settings.extra_metadata or {})
    meta["coupons_dashboard"] = {
        "rules": [r.dict() for r in body.rules],
        "vip_tiers": [t.dict() for t in body.vip_tiers],
    }
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.add(settings)
    db.commit()
    return {"rules": meta["coupons_dashboard"]["rules"], "vip_tiers": meta["coupons_dashboard"]["vip_tiers"]}


@router.post("")
async def create_coupon(body: CouponCreateIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    existing = db.query(Coupon).filter(
        Coupon.tenant_id == tenant_id,
        Coupon.code == body.code,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Coupon code already exists")

    expires_at = None
    if body.expires:
        expires_at = datetime.fromisoformat(body.expires.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

    coupon = Coupon(
        tenant_id=tenant_id,
        code=body.code,
        description=body.description,
        discount_type=body.type,
        discount_value=str(body.value),
        expires_at=expires_at,
        extra_metadata={
            "usage_count": 0,
            "usage_limit": body.limit,
            "category": body.category,
            "active": body.active,
            "source": "dashboard",
        },
    )
    db.add(coupon)
    db.commit()
    db.refresh(coupon)
    return {"id": coupon.id}


@router.patch("/{coupon_id}")
async def patch_coupon(coupon_id: int, body: CouponPatchIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.tenant_id == tenant_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")

    if body.code is not None:
        duplicate = db.query(Coupon).filter(
            Coupon.tenant_id == tenant_id,
            Coupon.code == body.code,
            Coupon.id != coupon_id,
        ).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="Coupon code already exists")
        coupon.code = body.code
    if body.description is not None:
        coupon.description = body.description
    if body.type is not None:
        coupon.discount_type = body.type
    if body.value is not None:
        coupon.discount_value = str(body.value)
    if body.expires is not None:
        coupon.expires_at = datetime.fromisoformat(body.expires.replace("Z", "+00:00")) if body.expires else None

    meta = dict(coupon.extra_metadata or {})
    if body.limit is not None:
        meta["usage_limit"] = body.limit
    if body.category is not None:
        meta["category"] = body.category
    if body.active is not None:
        meta["active"] = body.active
    coupon.extra_metadata = meta
    flag_modified(coupon, "extra_metadata")

    db.add(coupon)
    db.commit()
    return {"updated": True}


@router.delete("/{coupon_id}")
async def delete_coupon(coupon_id: int, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.tenant_id == tenant_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    db.delete(coupon)
    db.commit()
    return {"deleted": True}
