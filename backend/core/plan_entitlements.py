"""
core/plan_entitlements.py
──────────────────────────
Single source of truth for Nahla plan entitlements.

Feature Map (authoritative — do NOT guess, do NOT add features not listed here):

  Starter  →  basic autopilot + 2-stage cart recovery + abandoned_cart_basic_coupon
              + templates + basic campaigns (monthly cap) + Salla/WA integration
              + meta_catalog_sync
  Growth   →  Starter + full autopilot + 3-stage recovery + advanced coupons
              + growth engine + offers + smart_discount_popup
              + AI analytics dashboard
  Scale    →  Growth + store_brain_advanced + advanced AI + Zid + team handoff
              + full AI customization + advanced discount rules + future integrations

Usage — backend enforcement:
    from core.plan_entitlements import get_entitlements, require_feature, require_limit

    ent = get_entitlements(db, tenant_id)
    require_feature(ent, "cart_recovery_stage_3")
    require_limit(ent, "campaigns_per_month", current=n)

Usage — frontend (GET /billing/entitlements):
    Returns PlanEntitlements.to_dict() as JSON.

Billing status flow:
  none      → plan="none"   → no features
  trial     → full plan features (14-day trial)
  active    → full plan features
  failed    → plan="failed" → read-only
  cancelled → plan="none"   → read-only
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.entitlements")

_UNLIMITED = 999_999_999


# ═══════════════════════════════════════════════════════════════════════════════
# Feature & Limit definitions
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlanLimits:
    monthly_conversations:  int
    campaigns_per_month:    int


@dataclass
class PlanFeatures:

    # ── Templates ─────────────────────────────────────────────────────────────
    nahla_template_library:        bool   # Starter+: مكتبة قوالب نحلة
    meta_template_sync:            bool   # Starter+: مزامنة قوالب Meta

    # ── Autopilot — basic (Starter) ───────────────────────────────────────────
    autopilot_order_confirmation:  bool   # Starter+: تأكيد الطلب
    autopilot_order_notifications: bool   # Starter+: إشعارات الطلب
    autopilot_shipping_tracking:   bool   # Starter+: الشحن والتتبع

    # ── Autopilot — full (Growth) ─────────────────────────────────────────────
    autopilot_full:                bool   # Growth+: الطيار الآلي الكامل
    autopilot_customer_recovery:   bool   # Growth+: استرجاع العملاء
    autopilot_cod_confirmation:    bool   # Growth+: تأكيد COD

    # ── Cart Recovery ─────────────────────────────────────────────────────────
    cart_recovery_stage_2:         bool   # Starter+: المرحلة الثانية للسلة المتروكة
    cart_recovery_stage_3:         bool   # Growth+: المرحلة الثالثة
    cart_recovery_advanced_coupon: bool   # Growth+: كوبون متقدم في السلة المتروكة

    # ── Coupons ───────────────────────────────────────────────────────────────
    abandoned_cart_basic_coupon:   bool   # Starter+: كوبون السلة المتروكة الأساسي
    advanced_coupon_types:         bool   # Growth+: VIP + inactive recovery + coupon levels

    # ── Campaigns ─────────────────────────────────────────────────────────────
    campaign_customer_segments:    bool   # Starter+: تصنيفات العملاء (الكل)
    campaign_ai_optimization:      bool   # Growth+: تحسين الحملات بالذكاء الاصطناعي

    # ── Growth engine ─────────────────────────────────────────────────────────
    predictive_reorder:            bool   # Growth+: إعادة الطلب التنبؤية
    vip_rewards:                   bool   # Growth+: مكافآت العملاء VIP
    back_in_stock_alerts:          bool   # Growth+: تنبيهات العودة للمخزن
    new_products_alerts:           bool   # Growth+: تنبيهات المنتجات الجديدة

    # ── Offers ────────────────────────────────────────────────────────────────
    seasonal_smart_offers:         bool   # Growth+: العروض الموسمية الذكية
    salary_offers:                 bool   # Growth+: عروض الراتب
    seasonal_calendar:             bool   # Growth+: تقويم المناسبات الموسمية

    # ── Conversion tools ──────────────────────────────────────────────────────
    smart_discount_popup:          bool   # Growth+: نافذة الخصم الذكية

    # ── Integrations ──────────────────────────────────────────────────────────
    meta_catalog_sync:             bool   # Starter+: مزامنة كاتالوج ميتا / واتساب
    zid_integration:               bool   # Scale+: تكامل Zid
    future_integrations:           bool   # Scale+: تكاملات مستقبلية

    # ── Analytics ─────────────────────────────────────────────────────────────
    ai_performance_dashboard:      bool   # Growth+: لوحة أداء الذكاء الاصطناعي
    conversion_funnel:             bool   # Growth+: مسار التحويل

    advanced_ai_analytics:         bool   # Scale+: تحليلات الذكاء المتقدمة
    revenue_breakdown:             bool   # Scale+: تفصيل الإيرادات
    top_products_analytics:        bool   # Scale+: تحليل أفضل المنتجات
    order_sources_analytics:       bool   # Scale+: مصادر الطلبات

    # ── AI advanced (Scale) ───────────────────────────────────────────────────
    store_brain_advanced:          bool   # Scale+: ذكاء المتجر المتقدم
    full_ai_customization:         bool   # Scale+: تخصيص الذكاء الكامل
    advanced_discount_rules:       bool   # Scale+: قواعد الخصم المتقدمة
    escalation_rules:              bool   # Scale+: قواعد التصعيد

    # ── Team ──────────────────────────────────────────────────────────────────
    team_handoff_queue:            bool   # Scale+: طابور تحويل للفريق


@dataclass
class PlanDefinition:
    slug:      str
    name_ar:   str
    price_sar: int
    limits:    PlanLimits
    features:  PlanFeatures


# ── Helper: all False ─────────────────────────────────────────────────────────
def _all_false() -> PlanFeatures:
    return PlanFeatures(
        **{k: False for k in PlanFeatures.__dataclass_fields__}
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Plan Definitions — authoritative, matches user's Feature Mapping exactly
# ═══════════════════════════════════════════════════════════════════════════════

PLAN_DEFINITIONS: Dict[str, PlanDefinition] = {

    # ── Starter ───────────────────────────────────────────────────────────────
    "starter": PlanDefinition(
        slug="starter",
        name_ar="الأساسية",
        price_sar=979,
        limits=PlanLimits(
            monthly_conversations=5_000,
            campaigns_per_month=_UNLIMITED,
        ),
        features=PlanFeatures(
            # Templates
            nahla_template_library=True,
            meta_template_sync=True,

            # Autopilot basic
            autopilot_order_confirmation=True,
            autopilot_order_notifications=True,
            autopilot_shipping_tracking=True,

            # Autopilot full — NOT in Starter
            autopilot_full=False,
            autopilot_customer_recovery=False,
            autopilot_cod_confirmation=False,

            # Cart recovery — 2 stages only
            cart_recovery_stage_2=True,
            cart_recovery_stage_3=False,
            cart_recovery_advanced_coupon=False,

            # Coupons — basic only
            abandoned_cart_basic_coupon=True,
            advanced_coupon_types=False,

            # Campaigns
            campaign_customer_segments=True,
            campaign_ai_optimization=False,

            # Growth engine — NOT in Starter
            predictive_reorder=False,
            vip_rewards=False,
            back_in_stock_alerts=False,
            new_products_alerts=False,

            # Offers — NOT in Starter
            seasonal_smart_offers=False,
            salary_offers=False,
            seasonal_calendar=False,

            # Conversion tools — basic only
            smart_discount_popup=False,

            # Integrations
            meta_catalog_sync=True,
            zid_integration=False,
            future_integrations=False,

            # Analytics — basic
            ai_performance_dashboard=False,
            conversion_funnel=False,
            advanced_ai_analytics=False,
            revenue_breakdown=False,
            top_products_analytics=False,
            order_sources_analytics=False,

            # AI advanced — NOT in Starter
            store_brain_advanced=False,
            full_ai_customization=False,
            advanced_discount_rules=False,
            escalation_rules=False,

            # Team
            team_handoff_queue=False,
        ),
    ),

    # ── Growth ────────────────────────────────────────────────────────────────
    "growth": PlanDefinition(
        slug="growth",
        name_ar="النمو",
        price_sar=1899,
        limits=PlanLimits(
            monthly_conversations=15_000,
            campaigns_per_month=_UNLIMITED,
        ),
        features=PlanFeatures(
            # Templates
            nahla_template_library=True,
            meta_template_sync=True,

            # Autopilot — all
            autopilot_order_confirmation=True,
            autopilot_order_notifications=True,
            autopilot_shipping_tracking=True,
            autopilot_full=True,
            autopilot_customer_recovery=True,
            autopilot_cod_confirmation=True,

            # Cart recovery — 3 stages
            cart_recovery_stage_2=True,
            cart_recovery_stage_3=True,
            cart_recovery_advanced_coupon=True,

            # Coupons — advanced
            abandoned_cart_basic_coupon=True,
            advanced_coupon_types=True,

            # Campaigns
            campaign_customer_segments=True,
            campaign_ai_optimization=True,

            # Growth engine
            predictive_reorder=True,
            vip_rewards=True,
            back_in_stock_alerts=True,
            new_products_alerts=True,

            # Offers
            seasonal_smart_offers=True,
            salary_offers=True,
            seasonal_calendar=True,

            # Conversion tools
            smart_discount_popup=True,

            # Integrations
            meta_catalog_sync=True,
            zid_integration=False,
            future_integrations=False,

            # Analytics (Growth dashboard)
            ai_performance_dashboard=True,
            conversion_funnel=True,
            advanced_ai_analytics=False,
            revenue_breakdown=False,
            top_products_analytics=False,
            order_sources_analytics=False,

            # AI advanced — NOT in Growth
            store_brain_advanced=False,
            full_ai_customization=False,
            advanced_discount_rules=False,
            escalation_rules=False,

            # Team
            team_handoff_queue=False,
        ),
    ),

    # ── Scale ─────────────────────────────────────────────────────────────────
    "scale": PlanDefinition(
        slug="scale",
        name_ar="التوسع",
        price_sar=3199,
        limits=PlanLimits(
            monthly_conversations=_UNLIMITED,
            campaigns_per_month=_UNLIMITED,
        ),
        features=PlanFeatures(
            # Templates
            nahla_template_library=True,
            meta_template_sync=True,

            # Autopilot — all
            autopilot_order_confirmation=True,
            autopilot_order_notifications=True,
            autopilot_shipping_tracking=True,
            autopilot_full=True,
            autopilot_customer_recovery=True,
            autopilot_cod_confirmation=True,

            # Cart recovery — all
            cart_recovery_stage_2=True,
            cart_recovery_stage_3=True,
            cart_recovery_advanced_coupon=True,

            # Coupons — all
            abandoned_cart_basic_coupon=True,
            advanced_coupon_types=True,

            # Campaigns — all
            campaign_customer_segments=True,
            campaign_ai_optimization=True,

            # Growth engine — all
            predictive_reorder=True,
            vip_rewards=True,
            back_in_stock_alerts=True,
            new_products_alerts=True,

            # Offers — all
            seasonal_smart_offers=True,
            salary_offers=True,
            seasonal_calendar=True,

            # Conversion tools — all
            smart_discount_popup=True,

            # Integrations — all
            meta_catalog_sync=True,
            zid_integration=True,
            future_integrations=True,

            # Analytics — all
            ai_performance_dashboard=True,
            conversion_funnel=True,
            advanced_ai_analytics=True,
            revenue_breakdown=True,
            top_products_analytics=True,
            order_sources_analytics=True,

            # AI advanced — all
            store_brain_advanced=True,
            full_ai_customization=True,
            advanced_discount_rules=True,
            escalation_rules=True,

            # Team — all
            team_handoff_queue=True,
        ),
    ),

    # ── Pseudo-plans for blocked billing states ───────────────────────────────
    "none": PlanDefinition(
        slug="none",
        name_ar="بدون اشتراك",
        price_sar=0,
        limits=PlanLimits(monthly_conversations=0, campaigns_per_month=0),
        features=_all_false(),
    ),

    "failed": PlanDefinition(
        slug="failed",
        name_ar="فشل الدفع",
        price_sar=0,
        limits=PlanLimits(monthly_conversations=0, campaigns_per_month=0),
        features=_all_false(),
    ),
}


# ── Plan name → slug normaliser ───────────────────────────────────────────────

_SLUG_MAP: Dict[str, str] = {
    "starter":  "starter",
    "المبتدئ":  "starter",
    "growth":   "growth",
    "النمو":    "growth",
    "scale":    "scale",
    "التوسع":   "scale",
}

_ACTIVE_STATUSES = frozenset({"active", "trial"})


# ═══════════════════════════════════════════════════════════════════════════════
# Live entitlements object
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlanEntitlements:
    plan_slug:      str
    plan_name_ar:   str
    billing_status: str
    is_active:      bool
    is_blocked:     bool
    features:       PlanFeatures
    limits:         PlanLimits
    raw_plan:       PlanDefinition = field(repr=False)

    def has_feature(self, key: str) -> bool:
        return bool(getattr(self.features, key, False))

    def get_limit(self, key: str) -> int:
        return getattr(self.limits, key, 0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan":           self.plan_slug,
            "plan_name_ar":   self.plan_name_ar,
            "billing_status": self.billing_status,
            "is_active":      self.is_active,
            "is_blocked":     self.is_blocked,
            "features": {
                k: getattr(self.features, k)
                for k in PlanFeatures.__dataclass_fields__
            },
            "limits": {
                k: (
                    None
                    if getattr(self.limits, k, 0) >= _UNLIMITED
                    else getattr(self.limits, k, 0)
                )
                for k in PlanLimits.__dataclass_fields__
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Resolve tenant entitlements
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_plan_slug(slug_or_name: str) -> str:
    key = (slug_or_name or "").strip().lower()
    return _SLUG_MAP.get(key, key)


def _call_entitlement_lookup(strict_lookup: bool, source: str, fn, *args, **kwargs):
    """Re-raise lookup failures as EntitlementLookupUnavailable only in strict mode."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if strict_lookup:
            raise EntitlementLookupUnavailable(source) from exc
        raise


def get_entitlements(db: Session, tenant_id: int, *, strict_lookup: bool = False) -> PlanEntitlements:
    """
    Resolve the current entitlements for a tenant.

    Priority:
    0. Partner-testing override (tenant 1, metadata-driven, temporary)
    1. Salla Integration.config (Salla-billed tenants)
    2. Nahla BillingSubscription / BillingPlan (Moyasar/Stripe tenants)
    3. Manual gift grant (metadata-driven; paid/trial always wins)
    4. Fallback: plan="none"

    Safety rule: unknown/missing plan slug always resolves to "none"
    — never accidentally grant Growth or Scale features.
    """
    from core.billing_override import (  # noqa: PLC0415
        DEFAULT_OVERRIDE_PLAN_SLUG,
        get_partner_testing_override_plan_slug,
        is_partner_testing_override_active,
        log_billing_override_grant,
        PARTNER_TESTING_REASON,
    )

    try:
        override_active = is_partner_testing_override_active(db, tenant_id)
    except Exception as exc:
        if strict_lookup:
            raise EntitlementLookupUnavailable("partner_override") from exc
        raise

    if override_active:
        log_billing_override_grant(tenant_id, reason=PARTNER_TESTING_REASON)
        override_slug = _call_entitlement_lookup(
            strict_lookup,
            "partner_override_slug",
            get_partner_testing_override_plan_slug,
            db,
            tenant_id,
        )
        effective_slug = (
            override_slug if override_slug in PLAN_DEFINITIONS else DEFAULT_OVERRIDE_PLAN_SLUG
        )
        plan_def = PLAN_DEFINITIONS[effective_slug]
        return PlanEntitlements(
            plan_slug=effective_slug,
            plan_name_ar=plan_def.name_ar,
            billing_status="active",
            is_active=True,
            is_blocked=False,
            features=plan_def.features,
            limits=plan_def.limits,
            raw_plan=plan_def,
        )

    plan_slug      = "none"
    billing_status = "none"

    # ── 1. Salla subscription ─────────────────────────────────────────────────
    try:
        from models import Integration  # noqa: PLC0415
        salla_int = (
            db.query(Integration)
            .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
            .first()
        )
        if salla_int and salla_int.config:
            cfg        = salla_int.config
            raw_status = cfg.get("billing_status", "")
            raw_slug   = cfg.get("salla_plan_slug", "")
            if raw_status and raw_slug:
                plan_slug      = _resolve_plan_slug(raw_slug)
                billing_status = raw_status
    except Exception as exc:
        if strict_lookup:
            raise EntitlementLookupUnavailable("salla_lookup") from exc
        logger.debug("[Entitlements] Salla lookup error tenant=%s: %s", tenant_id, exc)

    # ── 2. Nahla BillingSubscription ──────────────────────────────────────────
    if plan_slug == "none":
        try:
            from core.billing import get_tenant_subscription  # noqa: PLC0415
            from models import BillingPlan  # noqa: PLC0415
            sub = get_tenant_subscription(db, tenant_id)
            if sub and sub.status in ("active", "trialing", "trial"):
                # ``sub.plan_id`` is the FK integer to billing_plans.id, NOT a
                # slug. The previous version passed the integer straight into
                # ``_resolve_plan_slug`` which returned the integer back, then
                # the integer fell through ``slug not in PLAN_DEFINITIONS`` and
                # collapsed to "none" — so every paid Moyasar subscription
                # was rendered as "no plan / trial" on the merchant
                # dashboard, even though admin tenants table (which JOINs
                # via plan_id) showed Growth + active correctly. The two
                # views were reading the same row but parsing different
                # columns. Fix: actually load the BillingPlan row and read
                # its ``slug`` column.
                plan_row = (
                    db.query(BillingPlan)
                    .filter(BillingPlan.id == sub.plan_id)
                    .first()
                    if sub.plan_id else None
                )
                raw_slug       = (plan_row.slug if plan_row else "") or ""
                plan_slug      = _resolve_plan_slug(raw_slug) or "starter"
                billing_status = "trial" if sub.status in ("trialing", "trial") else "active"
            elif sub and sub.status in ("past_due", "failed", "unpaid", "payment_failed"):
                plan_slug      = "failed"
                billing_status = "failed"
            elif sub and sub.status in ("canceled", "cancelled"):
                plan_slug      = "none"
                billing_status = "cancelled"
        except Exception as exc:
            if strict_lookup:
                raise EntitlementLookupUnavailable("billing_lookup") from exc
            logger.debug("[Entitlements] BillingSubscription lookup error tenant=%s: %s", tenant_id, exc)

    # ── 3. Determine effective plan ───────────────────────────────────────────
    # Unknown slug → "none". Never grant features by accident.
    effective_slug = plan_slug if plan_slug in PLAN_DEFINITIONS else "none"

    if billing_status == "cancelled":
        effective_slug = "none"
    elif billing_status == "failed":
        effective_slug = "failed"

    plan_def   = PLAN_DEFINITIONS[effective_slug]
    is_active  = billing_status in _ACTIVE_STATUSES
    is_blocked = not is_active and billing_status not in ("none",)

    # Paid / trial subscription always wins over manual gift grants.
    if is_active and effective_slug not in ("none", "failed"):
        return PlanEntitlements(
            plan_slug      = effective_slug,
            plan_name_ar   = plan_def.name_ar,
            billing_status = billing_status,
            is_active      = is_active,
            is_blocked     = is_blocked,
            features       = plan_def.features,
            limits         = plan_def.limits,
            raw_plan       = plan_def,
        )

    from core.manual_billing_grant import (  # noqa: PLC0415
        DEFAULT_GIFT_PLAN_SLUG,
        get_manual_gift_grant_plan_slug,
        is_manual_gift_grant_active,
        log_manual_gift_grant,
    )

    try:
        gift_active = is_manual_gift_grant_active(db, tenant_id)
    except Exception as exc:
        if strict_lookup:
            raise EntitlementLookupUnavailable("gift_lookup") from exc
        raise

    if gift_active:
        gift_slug = _call_entitlement_lookup(
            strict_lookup,
            "gift_slug",
            get_manual_gift_grant_plan_slug,
            db,
            tenant_id,
        )
        gift_slug = gift_slug if gift_slug in PLAN_DEFINITIONS else DEFAULT_GIFT_PLAN_SLUG
        log_manual_gift_grant(tenant_id)
        gift_def = PLAN_DEFINITIONS[gift_slug]
        return PlanEntitlements(
            plan_slug      = gift_slug,
            plan_name_ar   = gift_def.name_ar,
            billing_status = "gift",
            is_active      = True,
            is_blocked     = False,
            features       = gift_def.features,
            limits         = gift_def.limits,
            raw_plan       = gift_def,
        )

    return PlanEntitlements(
        plan_slug      = effective_slug,
        plan_name_ar   = plan_def.name_ar,
        billing_status = billing_status,
        is_active      = is_active,
        is_blocked     = is_blocked,
        features       = plan_def.features,
        limits         = plan_def.limits,
        raw_plan       = plan_def,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Enforcement helpers
# ═══════════════════════════════════════════════════════════════════════════════

class EntitlementLookupUnavailable(RuntimeError):
    """Transient failure reading plan entitlements; not a confirmed denial."""

    def __init__(self, source: str):
        self.source = source
        super().__init__(f"entitlement lookup unavailable ({source})")


class EntitlementError(Exception):
    def __init__(
        self,
        error_code:    str,
        feature_key:   str,
        required_plan: str,
        message_ar:    str,
    ) -> None:
        super().__init__(message_ar)
        self.error_code    = error_code
        self.feature_key   = feature_key
        self.required_plan = required_plan
        self.message_ar    = message_ar

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error":         self.error_code,
            "feature":       self.feature_key,
            "required_plan": self.required_plan,
            "message":       self.message_ar,
        }


# Feature → minimum plan that enables it
_FEATURE_MIN_PLAN: Dict[str, str] = {}

def _build_feature_min_plan() -> None:
    """Pre-compute which plan is the minimum for each feature."""
    for slug in ("starter", "growth", "scale"):
        plan = PLAN_DEFINITIONS[slug]
        for key in PlanFeatures.__dataclass_fields__:
            if key not in _FEATURE_MIN_PLAN and getattr(plan.features, key, False):
                _FEATURE_MIN_PLAN[key] = slug

_build_feature_min_plan()

_PLAN_LABELS = {"starter": "المبتدئ", "growth": "النمو", "scale": "التوسع"}


def require_feature(ent: PlanEntitlements, feature_key: str) -> None:
    """Raise EntitlementError if feature is not available in the tenant's plan."""
    if ent.is_blocked:
        raise EntitlementError(
            error_code    = "billing_blocked",
            feature_key   = feature_key,
            required_plan = ent.plan_slug,
            message_ar    = (
                "تم تعليق الاشتراك. "
                "يرجى تحديث بيانات الدفع لاستئناف الخدمة."
            ),
        )
    if not ent.is_active:
        raise EntitlementError(
            error_code    = "no_active_subscription",
            feature_key   = feature_key,
            required_plan = "starter",
            message_ar    = "لا يوجد اشتراك نشط. اشترك في إحدى باقات نحلة للمتابعة.",
        )
    if not ent.has_feature(feature_key):
        required = _FEATURE_MIN_PLAN.get(feature_key, "scale")
        raise EntitlementError(
            error_code    = "upgrade_required",
            feature_key   = feature_key,
            required_plan = required,
            message_ar    = (
                f"هذه الميزة متاحة في باقة {_PLAN_LABELS.get(required, required)}. "
                "رقِّ باقتك للاستمرار."
            ),
        )


def require_limit_not_exceeded(
    ent:       PlanEntitlements,
    limit_key: str,
    current:   int,
) -> None:
    """Raise EntitlementError if current usage >= plan limit."""
    limit = ent.get_limit(limit_key)
    if limit >= _UNLIMITED:
        return
    if not ent.is_active:
        raise EntitlementError(
            error_code    = "no_active_subscription",
            feature_key   = limit_key,
            required_plan = "starter",
            message_ar    = "لا يوجد اشتراك نشط.",
        )
    if current >= limit:
        required = "growth" if limit_key == "campaigns_per_month" else "scale"
        raise EntitlementError(
            error_code    = "limit_exceeded",
            feature_key   = limit_key,
            required_plan = required,
            message_ar    = (
                f"وصلت للحد الشهري ({limit:,}). "
                f"رقِّ باقتك إلى {_PLAN_LABELS.get(required, required)} للاستمرار."
            ),
        )


def entitlement_unavailable_http_error(exc: EntitlementLookupUnavailable) -> None:
    """Temporary entitlement-store outage: 503, not a plan-upgrade 403."""
    from fastapi import HTTPException  # noqa: PLC0415

    message = "تعذّر التحقق من أهلية المزامنة مؤقتًا، وسيعاد المحاولة."
    raise HTTPException(
        status_code=503,
        detail={
            "error": "entitlement_unavailable",
            "blocker_code": "entitlement_unavailable",
            "message": message,
            "message_ar": message,
        },
    ) from exc


def entitlement_http_error(exc: EntitlementError) -> None:
    """Convert EntitlementError to FastAPI HTTPException 403."""
    from fastapi import HTTPException  # noqa: PLC0415
    raise HTTPException(status_code=403, detail=exc.to_dict())
