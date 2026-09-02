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
from services.coupon_sync_visibility import (
    compute_source_type_counts,
    derive_coupon_sync_visibility,
    normalize_coupon_usage_display,
    resolve_coupon_source_type,
)
from services.coupon_salla_push import (
    FULL_API_INCOMPLETE_MSG_AR,
    NO_SALLA_ADAPTER_MSG_AR,
    apply_not_pushed_metadata,
    evaluate_salla_coupon_sync_readiness,
    is_pushable_manual_coupon,
    push_coupon_to_salla,
)
from services.native_ai_coupon_eligibility import (
    NATIVE_AI_CHANNELS,
    NATIVE_AI_LEVELS,
    explicit_ai_allocatable,
    validate_native_ai_opt_in,
)

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
#
# `r3` historically pointed at a per-customer "birthday gift" rule that we
# never wired into any automation. The concept was retired in favour of
# *seasonal* promotions (Founding Day, National Day, Ramadan, …) which live
# in the Promotions surface — see `core/automations_seed.SEASONAL_OCCASIONS`
# and the `seasonal_offer` automation. Old `r3` payloads now collapse onto
# the most-similar surviving rule (`repeat_purchase`) so the merchant's
# previous on/off intent isn't lost; truly deprecated rule ids are then
# filtered out by `_normalise_rules` so they never re-appear in the UI.
_LEGACY_RULE_ID_MAP = {
    "r1": "abandoned_cart",
    "r2": "vip_customers",
    "r3": "repeat_purchase",
    "r4": "repeat_purchase",
    "r5": "first_purchase",
}

# Rule ids that previously shipped in `DEFAULT_COUPON_RULES` but were
# retired. `_normalise_rules` drops any persisted entry matching one of
# these so the dashboard never re-surfaces them — including the old
# `birthday` rule, which is now modelled as a seasonal Promotion (see
# `core/automations_seed.SEASONAL_OCCASIONS`).
_DEPRECATED_RULE_IDS = frozenset({"birthday"})

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


# ── Coupon levels (replaces the legacy 3-tier VIP grid) ───────────────────
# Four-tier ladder: bronze → silver → gold → vip. Each level acts as an
# override layer over `global_defaults`; an empty/None field means "use
# global". Discount min/max bound the AI / pool generator so a "bronze"
# coupon can never accidentally hand out 30%.
DEFAULT_COUPON_LEVELS: List[Dict[str, Any]] = [
    {
        "id":                "bronze",
        "label":             "برونزي",
        "threshold":         "+1 طلب",
        "discount_default":  5,
        "discount_min":      3,
        "discount_max":      5,
        "validity_hours":    24,
        "max_uses":          1,
        "per_customer_usage": 1,
        "allowed_channels":  ["ai", "campaign", "autopilot"],
        "enabled":           True,
        "min_orders":        1,
    },
    {
        "id":                "silver",
        "label":             "فضي",
        "threshold":         "+3 طلبات",
        "discount_default":  10,
        "discount_min":      8,
        "discount_max":      12,
        "validity_hours":    48,
        "max_uses":          1,
        "per_customer_usage": 1,
        "allowed_channels":  ["ai", "campaign", "autopilot"],
        "enabled":           True,
        "min_orders":        3,
    },
    {
        "id":                "gold",
        "label":             "ذهبي",
        "threshold":         "+7 طلبات",
        "discount_default":  20,
        "discount_min":      15,
        "discount_max":      25,
        "validity_hours":    72,
        "max_uses":          2,
        "per_customer_usage": 1,
        "allowed_channels":  ["campaign", "autopilot"],
        "enabled":           True,
        "min_orders":        7,
    },
    {
        "id":                "vip",
        "label":             "استثنائي",
        "threshold":         "+15 طلب",
        "discount_default":  30,
        "discount_min":      25,
        "discount_max":      40,
        "validity_hours":    72,
        "max_uses":          3,
        "per_customer_usage": 1,
        "allowed_channels":  ["campaign", "autopilot"],
        "enabled":           True,
        "min_orders":        15,
    },
]


DEFAULT_GLOBAL_DEFAULTS: Dict[str, Any] = {
    "discount_type":          "percentage",
    "default_discount_value": 10,
    "total_usage_limit":      None,      # null = unlimited
    "customer_limit":         None,      # null = no per-coupon customer cap
    "per_customer_usage":     1,
    "min_order_amount":       0,
    "default_validity":       "24h",     # 3h | 6h | 24h | custom
    "custom_validity_hours":  None,
    "allowed_channels":       ["ai", "campaign", "autopilot"],
    "combinable_with_offers": False,
}


DEFAULT_AI_POLICY: Dict[str, Any] = {
    "enabled":             True,
    "allowed_levels":      ["bronze", "silver"],
    "min_remaining_hours": 3,
    "pool_mode":           "pool_first",  # pool_first | pool_only | on_demand_only
}


_COUPON_LEVEL_IDS = frozenset({"bronze", "silver", "gold", "vip"})
_ALLOWED_CHANNEL_IDS = frozenset({"ai", "campaign", "autopilot", "shared"})
_VALIDITY_PRESETS = frozenset({"3h", "6h", "24h", "custom"})
_POOL_MODES = frozenset({"pool_first", "pool_only", "on_demand_only"})

DEFAULT_WARM_POOL: Dict[str, Any] = {
    "target_per_level": 3,
    "refill_threshold": 1,
}


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
    ai_allocatable: bool = False
    coupon_level: Optional[str] = None
    allocation_channel: Optional[str] = None


class CouponPatchIn(BaseModel):
    code: Optional[str] = None
    type: Optional[str] = None
    value: Optional[str] = None
    description: Optional[str] = None
    limit: Optional[int] = None
    expires: Optional[str] = None
    category: Optional[str] = None
    active: Optional[bool] = None
    ai_allocatable: Optional[bool] = None
    coupon_level: Optional[str] = None
    allocation_channel: Optional[str] = None


class CouponLevelIn(BaseModel):
    id: str
    label: Optional[str] = None
    threshold: Optional[str] = None
    min_orders:          Optional[int]   = None
    discount_default:    Optional[float] = None
    discount_min:        Optional[float] = None
    discount_max:        Optional[float] = None
    validity_hours:      Optional[int]   = None
    max_uses:            Optional[int]   = None
    per_customer_usage:  Optional[int]   = None
    allowed_channels:    Optional[List[str]] = None
    enabled:             Optional[bool]  = None


class GlobalDefaultsIn(BaseModel):
    discount_type:          Optional[str]   = None
    default_discount_value: Optional[float] = None
    total_usage_limit:      Optional[int]   = None
    customer_limit:         Optional[int]   = None
    per_customer_usage:     Optional[int]   = None
    min_order_amount:       Optional[float] = None
    default_validity:       Optional[str]   = None
    custom_validity_hours:  Optional[int]   = None
    allowed_channels:       Optional[List[str]] = None
    combinable_with_offers: Optional[bool]  = None


class AiPolicyIn(BaseModel):
    enabled:             Optional[bool] = None
    allowed_levels:      Optional[List[str]] = None
    min_remaining_hours: Optional[int]  = None
    pool_mode:           Optional[str]  = None


class WarmPoolIn(BaseModel):
    target_per_level: Optional[int] = None
    refill_threshold: Optional[int] = None


class CouponDashboardSettingsIn(BaseModel):
    rules: List[CouponRuleIn]
    vip_tiers: Optional[List[VipTierIn]] = None
    levels: Optional[List[CouponLevelIn]] = None
    global_defaults: Optional[GlobalDefaultsIn] = None
    ai_policy: Optional[AiPolicyIn] = None
    warm_pool: Optional[WarmPoolIn] = None


_ALLOWED_DISCOUNT_TYPES = {"percentage", "fixed"}


def _normalise_level(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a single level dict, falling back to DEFAULT_COUPON_LEVELS
    so the merchant never loses fields by submitting a partial payload."""
    rid = str(raw.get("id") or "").strip().lower()
    if rid not in _COUPON_LEVEL_IDS:
        rid = "bronze"
    default = next((l for l in DEFAULT_COUPON_LEVELS if l["id"] == rid), DEFAULT_COUPON_LEVELS[0])
    base = dict(default)

    def _take(key: str, caster, *, allow_none: bool = False, default_val: Any = None):
        if key not in raw:
            return base.get(key, default_val)
        v = raw.get(key)
        if v is None and allow_none:
            return None
        if v is None:
            return base.get(key, default_val)
        try:
            return caster(v)
        except (ValueError, TypeError):
            return base.get(key, default_val)

    base["label"]              = str(raw.get("label") or base["label"])
    base["threshold"]          = str(raw.get("threshold") or base["threshold"])
    base["discount_default"]   = max(0, min(100, _take("discount_default", float, default_val=5)))
    base["discount_min"]       = max(0, min(100, _take("discount_min",     float, default_val=base["discount_default"])))
    base["discount_max"]       = max(base["discount_min"], min(100, _take("discount_max", float, default_val=base["discount_default"])))
    if base["discount_default"] < base["discount_min"]:
        base["discount_default"] = base["discount_min"]
    if base["discount_default"] > base["discount_max"]:
        base["discount_default"] = base["discount_max"]
    base["validity_hours"]     = max(1, _take("validity_hours",     int, default_val=24))
    base["max_uses"]           = max(1, _take("max_uses",           int, default_val=1))
    base["per_customer_usage"] = max(1, _take("per_customer_usage", int, default_val=1))
    from services.coupon_level_contract import CANONICAL_LEVEL_MIN_ORDERS  # noqa: PLC0415
    base["min_orders"]         = max(0, _take(
        "min_orders",
        int,
        default_val=int(CANONICAL_LEVEL_MIN_ORDERS.get(rid, 0)),
    ))

    chans = raw.get("allowed_channels")
    if isinstance(chans, list):
        cleaned = [c for c in (str(x).lower() for x in chans) if c in _ALLOWED_CHANNEL_IDS]
        base["allowed_channels"] = cleaned or list(default["allowed_channels"])
    base["enabled"] = bool(raw.get("enabled", base.get("enabled", True)))
    return base


def _normalise_levels(levels: Any) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    if isinstance(levels, list):
        for raw in levels:
            if not isinstance(raw, dict):
                continue
            n = _normalise_level(raw)
            seen[n["id"]] = n
    for default in DEFAULT_COUPON_LEVELS:
        if default["id"] not in seen:
            seen[default["id"]] = dict(default)
    return [seen[lid] for lid in ("bronze", "silver", "gold", "vip")]


def _normalise_global_defaults(raw: Any) -> Dict[str, Any]:
    base = dict(DEFAULT_GLOBAL_DEFAULTS)
    if not isinstance(raw, dict):
        return base

    dt = str(raw.get("discount_type") or base["discount_type"]).lower()
    base["discount_type"] = dt if dt in _ALLOWED_DISCOUNT_TYPES else "percentage"

    def _num(key: str, allow_none: bool = False) -> Any:
        if key not in raw:
            return base.get(key)
        v = raw.get(key)
        if v in (None, ""):
            return None if allow_none else base.get(key)
        try:
            return float(v) if isinstance(base.get(key), float) else int(v)
        except (ValueError, TypeError):
            return base.get(key)

    base["default_discount_value"] = max(0, _num("default_discount_value") or 0)
    base["total_usage_limit"]      = _num("total_usage_limit", allow_none=True)
    base["customer_limit"]         = _num("customer_limit",    allow_none=True)
    base["per_customer_usage"]     = max(1, _num("per_customer_usage") or 1)
    base["min_order_amount"]       = max(0, float(_num("min_order_amount") or 0))

    validity = str(raw.get("default_validity") or base["default_validity"]).lower()
    base["default_validity"] = validity if validity in _VALIDITY_PRESETS else "24h"
    cust_v = raw.get("custom_validity_hours")
    base["custom_validity_hours"] = (
        max(1, int(cust_v)) if cust_v not in (None, "") else None
    )

    chans = raw.get("allowed_channels")
    if isinstance(chans, list):
        cleaned = [c for c in (str(x).lower() for x in chans) if c in _ALLOWED_CHANNEL_IDS]
        base["allowed_channels"] = cleaned or list(DEFAULT_GLOBAL_DEFAULTS["allowed_channels"])
    base["combinable_with_offers"] = bool(raw.get("combinable_with_offers", base["combinable_with_offers"]))
    return base


def _normalise_ai_policy(raw: Any) -> Dict[str, Any]:
    base = dict(DEFAULT_AI_POLICY)
    if not isinstance(raw, dict):
        return base
    base["enabled"] = bool(raw.get("enabled", base["enabled"]))
    levels = raw.get("allowed_levels")
    if isinstance(levels, list):
        cleaned = [l for l in (str(x).lower() for x in levels) if l in _COUPON_LEVEL_IDS]
        base["allowed_levels"] = cleaned or list(base["allowed_levels"])
    try:
        base["min_remaining_hours"] = max(0, int(raw.get("min_remaining_hours", base["min_remaining_hours"])))
    except (ValueError, TypeError):
        pass
    mode = str(raw.get("pool_mode") or base["pool_mode"]).lower()
    base["pool_mode"] = mode if mode in _POOL_MODES else "pool_first"
    return base


def _normalise_warm_pool(raw: Any) -> Dict[str, Any]:
    base = dict(DEFAULT_WARM_POOL)
    if not isinstance(raw, dict):
        return base
    try:
        target = int(raw.get("target_per_level", base["target_per_level"]))
    except (ValueError, TypeError):
        target = base["target_per_level"]
    target = max(0, min(3, target))
    try:
        refill = int(raw.get("refill_threshold", base["refill_threshold"]))
    except (ValueError, TypeError):
        refill = base["refill_threshold"]
    refill = max(0, min(refill, target))
    base["target_per_level"] = target
    base["refill_threshold"] = refill
    return base


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
        if normalised["id"] in _DEPRECATED_RULE_IDS:
            # Persisted entry for a retired rule (e.g. `birthday`). Drop it
            # so the surface stays in sync with the new architecture
            # without forcing a one-shot data migration.
            continue
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
    """Hydrate the coupon dashboard config so every field exists before
    the GET endpoint serialises it. Run on every read so older tenants
    pick up new defaults (4-tier levels, AI policy, …) lazily without a
    one-shot data migration."""
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
    # New: 4-tier levels (bronze/silver/gold/vip) + global defaults + AI
    # policy. Always re-normalise so partial historical writes don't break
    # the merchant's view.
    levels_norm = _normalise_levels(coupon_dash.get("levels"))
    if levels_norm != coupon_dash.get("levels"):
        coupon_dash["levels"] = levels_norm
        changed = True
    gd_norm = _normalise_global_defaults(coupon_dash.get("global_defaults"))
    if gd_norm != coupon_dash.get("global_defaults"):
        coupon_dash["global_defaults"] = gd_norm
        changed = True
    ai_norm = _normalise_ai_policy(coupon_dash.get("ai_policy"))
    if ai_norm != coupon_dash.get("ai_policy"):
        coupon_dash["ai_policy"] = ai_norm
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
        elif meta_source in ("automation", "auto", "auto_generated", "system", "pool"):
            origin = "automation"
        elif meta_source == "widget":
            origin = "widget"
        elif meta.get("vip") or str(meta.get("category") or "").lower() == "vip":
            origin = "vip"
        elif meta.get("auto_generated") is True:
            origin = "automation"
        elif meta_source == "dashboard" or meta_source == "manual":
            origin = "manual"
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

        # Prefer taxonomy columns, then metadata evidence, then legacy origin.
        source_type = resolve_coupon_source_type(
            column_source_type=getattr(coupon, "source_type", None),
            meta=meta,
            origin=origin,
        )
        coupon_level = getattr(coupon, "coupon_level", None) or _infer_level_from_meta(meta)
        allocation_channel = (
            getattr(coupon, "allocation_channel", None)
            or _infer_channel_from_meta(meta, origin)
        )

        # remaining_seconds — handy for the merchant table; UI formats it.
        remaining_seconds: Optional[int] = None
        if expires is not None:
            delta = expires - now
            remaining_seconds = max(0, int(delta.total_seconds()))

        usages, limit = normalize_coupon_usage_display(meta)
        sync_fields = derive_coupon_sync_visibility(source_type=source_type, meta=meta)

        coupons.append({
            "id": str(coupon.id),
            "code": coupon.code,
            "type": coupon.discount_type or "percentage",
            "value": float(str(coupon.discount_value or "0").replace(",", ".")) if str(coupon.discount_value or "").replace(",", ".").replace(".", "", 1).isdigit() else coupon.discount_value,
            "usages": usages,
            "limit": limit,
            "expires": expires.isoformat() if expires else "",
            "remaining_seconds": remaining_seconds,
            "category": category,
            "origin": origin,
            "source_type": source_type,
            "coupon_level": coupon_level,
            "allocation_channel": allocation_channel,
            "ai_allocatable": explicit_ai_allocatable(meta),
            "automation_type": meta.get("automation_type") or None,
            "promotion_id":    meta.get("promotion_id") or None,
            "active": active,
            **sync_fields,
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
        "levels":          _normalise_levels(coupon_dash.get("levels")),
        "global_defaults": coupon_dash.get("global_defaults") or dict(DEFAULT_GLOBAL_DEFAULTS),
        "ai_policy":       coupon_dash.get("ai_policy") or dict(DEFAULT_AI_POLICY),
        "coupons": coupons,
        "source_counts": compute_source_type_counts(coupons),
    }


def _infer_level_from_meta(meta: Dict[str, Any]) -> Optional[str]:
    """Best-effort fallback for rows written before migration 0038."""
    explicit = str(meta.get("coupon_level") or "").lower()
    if explicit in _COUPON_LEVEL_IDS:
        return explicit
    seg = str(meta.get("target_segment") or "").lower()
    if seg in ("vip", "at_risk"):
        return "vip"
    if seg == "gold":
        return "gold"
    if seg in ("active", "silver"):
        return "silver"
    if seg in ("new", "bronze", "lead"):
        return "bronze"
    return None


def _infer_channel_from_meta(meta: Dict[str, Any], origin: str) -> Optional[str]:
    explicit = str(meta.get("allocation_channel") or "").lower()
    if explicit in _ALLOWED_CHANNEL_IDS:
        return explicit
    if meta.get("campaign_id"):
        return "campaign"
    if origin == "promotion":
        return "shared"
    if origin == "automation":
        return "autopilot"
    if str(meta.get("channel") or "").lower() in ("ai", "chat", "brain"):
        return "ai"
    return None


@router.put("/settings")
async def save_coupon_dashboard_settings(
    body: CouponDashboardSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    # ── Entitlement check: advanced coupon rules need advanced_coupon_types ──
    # Starter: only abandoned_cart rule allowed.
    # Growth+: VIP, inactive recovery, coupon levels (bronze/silver/gold/vip).
    from core.plan_entitlements import (  # noqa: PLC0415
        get_entitlements, require_feature, entitlement_http_error, EntitlementError,
    )
    ent = get_entitlements(db, tenant_id)
    # Rule IDs that require advanced_coupon_types (Growth+)
    _ADVANCED_RULE_IDS = frozenset({
        "vip_customers",
        "customer_winback",
        "repeat_purchase",
        "first_purchase",
    })
    if body.rules:
        for rule in body.rules:
            if rule.enabled and rule.id in _ADVANCED_RULE_IDS:
                try:
                    require_feature(ent, "advanced_coupon_types")
                except EntitlementError as exc:
                    entitlement_http_error(exc)
                break

    settings = get_or_create_settings(db, tenant_id)
    meta = dict(settings.extra_metadata or {})

    # Merge over any pre-existing block so partial PUTs (e.g. only AI
    # policy from a future modal) don't wipe other fields.
    existing = dict(meta.get("coupons_dashboard") or {})
    new_block: Dict[str, Any] = {
        "rules":     _normalise_rules([r.dict() for r in body.rules]),
        "vip_tiers": [t.dict() for t in body.vip_tiers] if body.vip_tiers else (existing.get("vip_tiers") or DEFAULT_VIP_TIERS),
        "levels":    _normalise_levels([l.dict() for l in body.levels]) if body.levels else _normalise_levels(existing.get("levels")),
        "global_defaults": _normalise_global_defaults(body.global_defaults.dict() if body.global_defaults else existing.get("global_defaults")),
        "ai_policy":       _normalise_ai_policy(body.ai_policy.dict() if body.ai_policy else existing.get("ai_policy")),
        "warm_pool": _normalise_warm_pool(body.warm_pool.dict()) if body.warm_pool is not None else (existing.get("warm_pool") or dict(DEFAULT_WARM_POOL)),
    }
    meta["coupons_dashboard"] = new_block
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.add(settings)
    db.commit()
    return new_block


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

    usage_limit = int(body.limit or 0)
    opt_in_error = validate_native_ai_opt_in(
        ai_allocatable=bool(body.ai_allocatable),
        coupon_level=body.coupon_level,
        allocation_channel=body.allocation_channel,
        usage_limit=usage_limit,
    )
    if opt_in_error:
        raise HTTPException(status_code=400, detail=opt_in_error)

    expires_at = None
    if body.expires:
        expires_at = datetime.fromisoformat(body.expires.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

    level = str(body.coupon_level or "").strip().lower() or None
    if level and level not in NATIVE_AI_LEVELS:
        raise HTTPException(status_code=400, detail="مستوى الكوبون غير صالح.")
    channel = str(body.allocation_channel or "").strip().lower() or None
    if channel and channel not in (NATIVE_AI_CHANNELS | {"campaign", "autopilot"}):
        raise HTTPException(status_code=400, detail="قناة الكوبون غير صالحة.")

    extra_metadata: Dict[str, Any] = {
        "usage_count": 0,
        "usage_limit": usage_limit,
        "category": body.category,
        "active": body.active,
        "source": "dashboard",
        "ai_allocatable": bool(body.ai_allocatable),
    }
    if level:
        extra_metadata["coupon_level"] = level
    if channel:
        extra_metadata["allocation_channel"] = channel

    coupon = Coupon(
        tenant_id=tenant_id,
        code=body.code,
        description=body.description,
        discount_type=body.type,
        discount_value=str(body.value),
        expires_at=expires_at,
        source_type="manual",
        coupon_level=level,
        allocation_channel=channel,
        extra_metadata=extra_metadata,
    )
    db.add(coupon)
    db.commit()
    db.refresh(coupon)

    readiness = evaluate_salla_coupon_sync_readiness(db, tenant_id)
    if readiness["full_api_ready"] and readiness["adapter_ready"]:
        await push_coupon_to_salla(db, tenant_id, coupon, adapter=readiness.get("adapter"))
        db.commit()
        db.refresh(coupon)
    elif not readiness["full_api_ready"]:
        apply_not_pushed_metadata(
            coupon,
            reason=readiness["reason"] or FULL_API_INCOMPLETE_MSG_AR,
        )
        db.add(coupon)
        db.commit()
        db.refresh(coupon)

    meta = coupon.extra_metadata or {}
    source_type = resolve_coupon_source_type(
        column_source_type=coupon.source_type,
        meta=meta,
        origin="manual",
    )
    sync_fields = derive_coupon_sync_visibility(source_type=source_type, meta=meta)
    return {
        "id": coupon.id,
        "code": coupon.code,
        "source_type": source_type,
        "ai_allocatable": explicit_ai_allocatable(meta),
        "coupon_level": coupon.coupon_level,
        "allocation_channel": coupon.allocation_channel,
        **sync_fields,
    }


@router.post("/{coupon_id}/push-salla")
async def push_coupon_salla(coupon_id: int, request: Request, db: Session = Depends(get_db)):
    """Push an existing local manual coupon to Salla."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.tenant_id == tenant_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")

    if not is_pushable_manual_coupon(coupon):
        raise HTTPException(status_code=400, detail="يمكن إرسال الكوبونات اليدوية فقط إلى سلة")

    readiness = evaluate_salla_coupon_sync_readiness(db, tenant_id)
    if not readiness["full_api_ready"]:
        raise HTTPException(
            status_code=400,
            detail=readiness["reason"] or FULL_API_INCOMPLETE_MSG_AR,
        )

    ok, result = await push_coupon_to_salla(db, tenant_id, coupon, adapter=readiness.get("adapter"))
    db.commit()
    db.refresh(coupon)

    meta = coupon.extra_metadata or {}
    source_type = resolve_coupon_source_type(
        column_source_type=coupon.source_type,
        meta=meta,
        origin="manual",
    )
    sync_fields = derive_coupon_sync_visibility(source_type=source_type, meta=meta)
    if not ok:
        raise HTTPException(
            status_code=502,
            detail=result.get("sync_error") or "فشل إرسال الكوبون إلى سلة",
        )

    return {
        "id": coupon.id,
        "code": coupon.code,
        "source_type": source_type,
        **sync_fields,
        **result,
    }


@router.post("/sync-salla")
async def sync_salla_coupons(request: Request, db: Session = Depends(get_db)):
    """Import/refresh coupons from the connected Salla store only."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    readiness = evaluate_salla_coupon_sync_readiness(db, tenant_id)
    if not readiness["full_api_ready"]:
        raise HTTPException(
            status_code=400,
            detail=readiness["reason"] or FULL_API_INCOMPLETE_MSG_AR,
        )
    if not readiness["adapter_ready"]:
        raise HTTPException(
            status_code=400,
            detail=NO_SALLA_ADAPTER_MSG_AR,
        )

    from services.store_sync import StoreSyncService  # noqa: PLC0415

    svc = StoreSyncService(db, tenant_id)
    svc._adapter = readiness["adapter"]  # noqa: SLF001

    try:
        synced = await svc.sync_coupons()
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail=f"فشلت مزامنة كوبونات سلة: {exc}",
        ) from exc

    return {"status": "ok", "synced": synced}


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
    if body.coupon_level is not None:
        level = str(body.coupon_level or "").strip().lower()
        if level and level not in NATIVE_AI_LEVELS:
            raise HTTPException(status_code=400, detail="مستوى الكوبون غير صالح.")
        coupon.coupon_level = level or None
        if level:
            meta["coupon_level"] = level
        else:
            meta.pop("coupon_level", None)
    if body.allocation_channel is not None:
        channel = str(body.allocation_channel or "").strip().lower()
        allowed_channels = NATIVE_AI_CHANNELS | {"campaign", "autopilot"}
        if channel and channel not in allowed_channels:
            raise HTTPException(status_code=400, detail="قناة الكوبون غير صالحة.")
        coupon.allocation_channel = channel or None
        if channel:
            meta["allocation_channel"] = channel
        else:
            meta.pop("allocation_channel", None)
    if body.ai_allocatable is not None:
        meta["ai_allocatable"] = bool(body.ai_allocatable)

    next_allocatable = explicit_ai_allocatable(meta)
    next_limit = meta.get("usage_limit")
    try:
        next_limit_int = int(next_limit) if next_limit not in (None, "") else None
    except (TypeError, ValueError):
        next_limit_int = None
    opt_in_error = validate_native_ai_opt_in(
        ai_allocatable=next_allocatable,
        coupon_level=coupon.coupon_level,
        allocation_channel=coupon.allocation_channel,
        usage_limit=next_limit_int,
    )
    if opt_in_error:
        raise HTTPException(status_code=400, detail=opt_in_error)

    coupon.extra_metadata = meta
    flag_modified(coupon, "extra_metadata")

    db.add(coupon)
    db.commit()
    return {
        "updated": True,
        "ai_allocatable": explicit_ai_allocatable(meta),
        "coupon_level": coupon.coupon_level,
        "allocation_channel": coupon.allocation_channel,
    }


@router.delete("/{coupon_id}")
async def delete_coupon(coupon_id: int, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.tenant_id == tenant_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    db.delete(coupon)
    db.commit()
    return {"deleted": True}
