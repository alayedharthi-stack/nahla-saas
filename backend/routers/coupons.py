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
from models import Coupon

router = APIRouter(prefix="/coupons", tags=["Coupons"])

DEFAULT_COUPON_RULES: List[Dict[str, Any]] = [
    {
        "id":               "abandoned_cart",
        "label":            "كوبون استرجاع السلة المتروكة",
        "description":      "يُولِّد الطيار الآلي كوداً للعميل الذي ترك السلة لأكثر من 30 دقيقة",
        "enabled":          True,
        "discount_type":    "percentage",
        "discount_value":   10,
        "validity_days":    1,
        "min_order_amount": 0,
        "max_uses":         1,
    },
    {
        "id":               "vip_customers",
        "label":            "مكافأة العملاء VIP",
        "description":      "كود حصري للعملاء الذين أنفقوا فوق حد VIP أو أكملوا 5 طلبات",
        "enabled":          True,
        "discount_type":    "percentage",
        "discount_value":   20,
        "validity_days":    7,
        "min_order_amount": 0,
        "max_uses":         1,
    },
    {
        "id":               "customer_winback",
        "label":            "استرجاع العملاء الخاملين",
        "description":      "يُرسل عرضاً للعملاء الذين لم يشتروا منذ 60 يوماً أو أكثر",
        "enabled":          True,
        "discount_type":    "percentage",
        "discount_value":   25,
        "validity_days":    3,
        "min_order_amount": 0,
        "max_uses":         1,
    },
    {
        "id":               "birthday",
        "label":            "هدية يوم الميلاد",
        "description":      "كود تلقائي يوم ميلاد العميل (إن توفّر تاريخ الميلاد)",
        "enabled":          False,
        "discount_type":    "percentage",
        "discount_value":   10,
        "validity_days":    7,
        "min_order_amount": 0,
        "max_uses":         1,
    },
    {
        "id":               "repeat_purchase",
        "label":            "تحفيز الشراء المتكرر",
        "description":      "كود يُرسل بعد أول طلب لتشجيع الطلب الثاني خلال أيام قليلة",
        "enabled":          True,
        "discount_type":    "percentage",
        "discount_value":   10,
        "validity_days":    5,
        "min_order_amount": 0,
        "max_uses":         1,
    },
    {
        "id":               "first_purchase",
        "label":            "خصم أول شراء",
        "description":      "ترحيب بالعملاء الجدد بكود لأول طلب",
        "enabled":          False,
        "discount_type":    "percentage",
        "discount_value":   15,
        "validity_days":    1,
        "min_order_amount": 0,
        "max_uses":         1,
    },
]

# Legacy rule ids (`r1`..`r5`) → semantic ids. When we read settings stored
# by older builds we silently rewrite them so the new editable form binds
# to the right defaults.
_LEGACY_RULE_ID_MAP = {
    "r1": "abandoned_cart",
    "r2": "vip_customers",
    "r3": "birthday",
    "r4": "repeat_purchase",
    "r5": "first_purchase",
}

# Slug → rule id used by the automation engine when picking which rule's
# discount/validity overrides apply to a given automation. Public so the
# automation engine can import it without duplicating the map.
AUTOMATION_TO_RULE_ID: Dict[str, str] = {
    "abandoned_cart":         "abandoned_cart",
    "customer_winback":       "customer_winback",
    "vip_upgrade":            "vip_customers",
    "predictive_reorder":     "repeat_purchase",
    "back_in_stock":          "first_purchase",
}


DEFAULT_VIP_TIERS = [
    {"tier": "فضي", "threshold": "+3 طلبات", "discount": "10%"},
    {"tier": "ذهبي", "threshold": "+7 طلبات", "discount": "20%"},
    {"tier": "بلاتيني", "threshold": "+15 طلب", "discount": "30%"},
]


class CouponRuleIn(BaseModel):
    id: str
    label: str
    enabled: bool
    description: Optional[str] = None
    # Rich parameters — nullable so older payloads (only id/label/enabled)
    # still validate. Validation is enforced in `_normalise_rule` below.
    discount_type:    Optional[str]   = None
    discount_value:   Optional[float] = None
    validity_days:    Optional[int]   = None
    min_order_amount: Optional[float] = None
    max_uses:         Optional[int]   = None


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


_ALLOWED_DISCOUNT_TYPES = {"percentage", "fixed"}


def _normalise_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bring a rule dict (from storage or from the wire) into the canonical
    rich shape so the dashboard can always assume every field is present.

    Handles three back-compat scenarios:
      • Legacy id (`r1`..`r5`)  → mapped to semantic id.
      • Legacy shape (only id/label/enabled) → defaults filled in from
        the matching DEFAULT_COUPON_RULES entry, otherwise from a safe
        baseline (10% / 1-day validity).
      • Stale fields (string discount_value, etc.) → coerced.
    """
    raw_id = str(rule.get("id") or "").strip()
    rid    = _LEGACY_RULE_ID_MAP.get(raw_id, raw_id) or "custom_rule"

    default = next((r for r in DEFAULT_COUPON_RULES if r["id"] == rid), None)
    base = dict(default) if default else {
        "id":               rid,
        "label":            rule.get("label") or rid,
        "description":      "",
        "enabled":          False,
        "discount_type":    "percentage",
        "discount_value":   10,
        "validity_days":    1,
        "min_order_amount": 0,
        "max_uses":         1,
    }
    base["id"]          = rid
    base["label"]       = str(rule.get("label") or base["label"])
    base["description"] = str(rule.get("description") or base.get("description") or "")
    base["enabled"]     = bool(rule.get("enabled", base["enabled"]))

    dt = str(rule.get("discount_type") or base["discount_type"]).lower()
    if dt not in _ALLOWED_DISCOUNT_TYPES:
        dt = "percentage"
    base["discount_type"] = dt

    def _num(key: str, default_val: Any) -> float:
        v = rule.get(key, base.get(key, default_val))
        if v is None:
            return float(default_val) if default_val is not None else 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return float(default_val) if default_val is not None else 0.0

    base["discount_value"]   = round(_num("discount_value", 10), 2)
    base["min_order_amount"] = round(_num("min_order_amount", 0), 2)
    base["validity_days"]    = max(1, int(_num("validity_days", 1)))
    max_uses_raw = rule.get("max_uses", base.get("max_uses"))
    if max_uses_raw in (None, "", 0):
        base["max_uses"] = None
    else:
        try:
            base["max_uses"] = max(1, int(max_uses_raw))
        except (ValueError, TypeError):
            base["max_uses"] = 1

    if dt == "percentage":
        base["discount_value"] = max(0, min(100, base["discount_value"]))
    return base


def _normalise_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rules or []:
        normalised = _normalise_rule(r)
        seen[normalised["id"]] = normalised
    # Always guarantee the catalogue of default rules exists so the merchant
    # never sees a half-empty list — they may have only configured one rule
    # historically, but the others should still be visible (disabled).
    for default in DEFAULT_COUPON_RULES:
        if default["id"] not in seen:
            seen[default["id"]] = dict(default)
    return list(seen.values())


def get_rule_for_automation(
    settings,
    automation_type: str,
) -> Optional[Dict[str, Any]]:
    """
    Public helper used by the automation engine to fetch the merchant-set
    overrides for a given automation. Returns None if the automation isn't
    mapped to a rule, the rule isn't found, or the rule is disabled.
    """
    rule_id = AUTOMATION_TO_RULE_ID.get(str(automation_type or ""))
    if not rule_id:
        return None
    meta = (settings.extra_metadata or {}) if settings is not None else {}
    rules = (meta.get("coupons_dashboard") or {}).get("rules") or []
    for r in rules:
        if str(r.get("id") or "") == rule_id and bool(r.get("enabled", True)):
            return _normalise_rule(r)
    return None


def _ensure_coupon_dashboard_config(settings) -> Dict[str, Any]:
    meta = dict(settings.extra_metadata or {})
    coupon_dash = dict(meta.get("coupons_dashboard") or {})
    changed = False
    if "rules" not in coupon_dash:
        coupon_dash["rules"] = [dict(r) for r in DEFAULT_COUPON_RULES]
        changed = True
    else:
        normalised = _normalise_rules(coupon_dash["rules"])
        if normalised != coupon_dash["rules"]:
            coupon_dash["rules"] = normalised
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

        # Origin classification — what *generated* this code? Used by the
        # dashboard to render the "🤖 Autopilot" vs "✋ Manual" badges so
        # the merchant immediately understands which incentives the AI is
        # running and which are one-off manual codes they entered.
        meta_source = str(meta.get("source") or "").lower()
        if meta_source == "promotion":
            origin = "promotion"
        elif meta_source == "automation":
            origin = "automation"
        elif meta_source == "widget":
            origin = "widget"
        elif meta.get("vip") or str(meta.get("category") or "").lower() == "vip":
            origin = "vip"
        elif meta.get("auto_generated") is True:
            origin = "automation"
        else:
            origin = "manual"

        # Legacy `category` field — kept for backward-compat with old
        # frontend builds. New UI prefers `origin`.
        category = str(meta.get("category") or (
            "vip" if origin == "vip"
            else ("auto" if origin in {"automation", "promotion", "widget"} else "standard")
        ))
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
            "origin": origin,
            "automation_type": meta.get("automation_type") or None,
            "promotion_id":    meta.get("promotion_id") or None,
            "active": active,
        })

    # The merchant's manual on/off is now the source of truth. The previous
    # behaviour silently flipped `enabled` based on system state (vip_count,
    # active coupons, …), which fought the merchant's edits — now that rules
    # are fully editable from the dashboard we respect the stored value as-is.
    rules = _normalise_rules(list(coupon_dash.get("rules") or DEFAULT_COUPON_RULES))
    _ = ai_settings  # kept for future merchant-instruction integration

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
        "rules": _normalise_rules([r.dict() for r in body.rules]),
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
