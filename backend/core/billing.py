"""
core/billing.py
───────────────
Billing plan seed data and helper functions shared by billing routers.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import BillingPlan, BillingSubscription, Tenant  # noqa: E402

logger = logging.getLogger("nahla.billing")

# ── Billing constants ──────────────────────────────────────────────────────────
INTEGRATION_FEE_SAR = 59
LAUNCH_PROMO_MONTHS = 2
LAUNCH_PROMO_UNTIL  = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
FREE_TRIAL_DAYS     = 14

BILLING_PLANS_SEED: List[Dict[str, Any]] = [
    {
        "slug": "starter",
        "name": "Starter",
        "name_ar": "الأساسية",
        "description": "ابدأ مع الذكاء الاصطناعي والحملات على واتساب الأعمال الخاص بك",
        "price_sar": 899,
        "launch_price_sar": 449,
        "billing_cycle": "monthly",
        # Feature copy is the *single source of truth* — DO NOT duplicate
        # in the frontend. Frontend reads via GET /billing/plans.
        # Killer feature MUST stay first; UI gives index 0 a special pill.
        "features": [
            "📱 واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا",
            "✈️ الطيار الآلي للمبيعات والردود الذكية",
            "📣 حملات واتساب غير محدودة",
            "🛒 استرجاع السلات المتروكة (3 مراحل) + كوبونات تلقائية",
            "📦 إشعارات الطلبات التلقائية (تأكيد - شحن - تسليم)",
            "🔄 مزامنة منتجات سلة تلقائيًا",
            "🏷️ إنشاء كوبونات خصم تلقائية",
            "💬 حتى 5,000 محادثة شهريًا",
        ],
        "limits": {
            "conversations_per_month": 5000,
            "automations": -1,
            "campaigns_per_month": -1,
        },
    },
    {
        "slug": "growth",
        "name": "Growth",
        "name_ar": "النمو",
        "description": "للمتاجر المتنامية التي تريد تحقيق أقصى مبيعات",
        "price_sar": 1699,
        "launch_price_sar": 849,
        "billing_cycle": "monthly",
        "features": [
            "📱 واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا",
            "✈️ الطيار الآلي للمبيعات والردود الذكية",
            "📣 حملات واتساب تسويقية متقدمة",
            "🛒 استرجاع السلات المتروكة (3 مراحل) + كوبونات ذكية تلقائية",
            "💳 تأكيد طلبات الدفع عند الاستلام (COD) تلقائيًا",
            "🏷️ كوبونات خصم تلقائية بأربعة مستويات",
            "🔗 إرسال روابط الدفع المباشرة للعملاء",
            "📦 إشعارات الطلبات التلقائية (تأكيد - شحن - تسليم)",
            "🧠 لوحة تحليلات ومبيعات بالذكاء الاصطناعي",
            "🔄 مزامنة منتجات سلة تلقائيًا",
            "🛍️ مزامنة المنتجات مع كتالوج Meta (قريبًا)",
            "🔍 مزامنة المنتجات مع Google Merchant (قريبًا)",
            "📺 مزامنة المنتجات مع قناة YouTube (قريبًا)",
            "🎵 مزامنة المنتجات مع TikTok Shop (قريبًا)",
            "📊 لوحة تحكم متقدمة للإحصائيات والتحليلات",
            "💬 حتى 15,000 محادثة شهريًا",
        ],
        "limits": {
            "conversations_per_month": 15000,
            "automations": -1,
            "campaigns_per_month": -1,
        },
    },
    {
        "slug": "scale",
        "name": "Scale",
        "name_ar": "التوسع",
        "description": "للمتاجر الكبيرة والعلامات التجارية المتسارعة",
        "price_sar": 2999,
        "launch_price_sar": 1499,
        "billing_cycle": "monthly",
        "features": [
            "📱 واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا",
            "✈️ الطيار الآلي الكامل للمبيعات والردود وخدمة العملاء",
            "♾️ محادثات غير محدودة",
            "📣 حملات واتساب غير محدودة",
            "🛒 استرجاع السلات المتروكة (3 مراحل) + كوبونات ذكية تلقائية",
            "🏷️ كوبونات خصم تلقائية بأربعة مستويات",
            "💳 تأكيد طلبات الدفع عند الاستلام (COD) تلقائيًا",
            "🔗 إرسال روابط الدفع المباشرة للعملاء",
            "📦 إشعارات الطلبات التلقائية (تأكيد - شحن - تسليم)",
            "🧠 لوحة تحليلات ومبيعات متقدمة بالذكاء الاصطناعي",
            "🔄 مزامنة المنتجات تلقائيًا مع سلة",
            "🛍️ مزامنة المنتجات مع كتالوج Meta (قريبًا)",
            "🔍 مزامنة المنتجات مع Google Merchant (قريبًا)",
            "📺 مزامنة المنتجات مع قناة YouTube (قريبًا)",
            "🎵 مزامنة المنتجات مع TikTok Shop (قريبًا)",
            "👥 فرق عمل متعددة وصلاحيات متقدمة",
            "🔌 API كامل وربط مخصص",
            "📈 تقارير وتحليلات مخصصة للأعمال الكبيرة",
            "⚡ أولوية قصوى في سرعة الذكاء والمعالجة",
            "🛡️ مدير نجاح مخصص ودعم VIP على مدار الساعة",
        ],
        "limits": {
            "conversations_per_month": -1,
            "automations": -1,
            "campaigns_per_month": -1,
        },
    },
]


# ── Helper functions ───────────────────────────────────────────────────────────

def ensure_billing_plans(db: Session) -> None:
    """Seed system billing plans and keep PRODUCT-CONFIG fields canonical.

    Behaviour split (deliberate — see commit history for context):

    1.  **Inserted on first boot** — full row from BILLING_PLANS_SEED.

    2.  **Re-synced on every call** — the *product description* of a plan
        (``features`` list, ``limits`` map, ``description`` text, English
        ``name``, and ``extra_metadata.name_ar``).  These fields describe
        what the plan IS, so they must always reflect the latest seed.
        Previously this function refused to update them, which meant any
        edit to BILLING_PLANS_SEED was effectively dead code on existing
        deployments — the DB kept serving the original first-boot snapshot
        forever.  That's why the v1 pricing-update commit (34310c7d)
        appeared to do nothing in the UI.

    3.  **Preserved untouched** — pricing fields (``price_sar``,
        ``extra_metadata.launch_price_sar``).  Some merchants may have
        custom pricing applied, and we never want a deploy to silently
        change the price they see on the next page-load.  Pricing must
        change through an explicit migration / admin action, not a code
        push.
    """
    # Order matters — keep this list aligned with the actual JSONB shape
    # so the equality check below catches stale rows.
    PRODUCT_FIELDS_TO_SYNC = ("features", "limits", "description", "name")

    changed = False
    for seed in BILLING_PLANS_SEED:
        existing = db.query(BillingPlan).filter(BillingPlan.slug == seed["slug"]).first()
        if not existing:
            plan = BillingPlan(
                tenant_id=None,
                slug=seed["slug"],
                name=seed["name"],
                description=seed["description"],
                currency="SAR",
                price_sar=seed["price_sar"],
                billing_cycle=seed["billing_cycle"],
                features=seed["features"],
                limits=seed["limits"],
                extra_metadata={
                    "name_ar": seed["name_ar"],
                    "launch_price_sar": seed["launch_price_sar"],
                },
            )
            db.add(plan)
            changed = True
            continue

        # ── Re-sync product-config fields ──────────────────────────────
        for field in PRODUCT_FIELDS_TO_SYNC:
            if getattr(existing, field) != seed[field]:
                setattr(existing, field, seed[field])
                changed = True

        # ── Re-sync name_ar; preserve launch_price_sar only when missing
        meta = dict(existing.extra_metadata or {})
        if meta.get("name_ar") != seed["name_ar"]:
            meta["name_ar"] = seed["name_ar"]
            changed = True
        if "launch_price_sar" not in meta:
            # Only fill in when missing — never overwrite a price merchants
            # may have seen on a previous billing page-load.
            meta["launch_price_sar"] = seed["launch_price_sar"]
            changed = True
        if meta != (existing.extra_metadata or {}):
            existing.extra_metadata = meta

    if changed:
        db.commit()
        logger.info(
            "[billing] ensure_billing_plans: re-synced %d plan(s) from seed",
            len(BILLING_PLANS_SEED),
        )


def get_tenant_subscription(db: Session, tenant_id: int) -> Optional[BillingSubscription]:
    """Return the active, non-expired subscription for a tenant, or None."""
    sub = (
        db.query(BillingSubscription)
        .filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status == "active",
        )
        .order_by(BillingSubscription.started_at.desc())
        .first()
    )
    if not sub:
        return None

    from core.trial_lifecycle import subscription_period_end  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    ends = _coerce_utc(sub.ends_at)
    if not ends:
        meta = sub.extra_metadata or {}
        raw_paid = meta.get("paid_at")
        anchor = None
        if raw_paid:
            try:
                anchor = _coerce_utc(datetime.fromisoformat(str(raw_paid)))
            except (TypeError, ValueError):
                anchor = None
        anchor = anchor or _coerce_utc(sub.started_at) or now
        ends = subscription_period_end(anchor)

    if ends and ends <= now:
        return None
    return sub


def _coerce_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def compute_trial_info(tenant: "Tenant") -> dict:
    """
    Unified trial computation used by both the billing status API
    and the enforcement guard.

    Trial starts only after WhatsApp connects — see core.trial_lifecycle.
    """
    from core.trial_lifecycle import compute_trial_info as _compute  # noqa: PLC0415

    return _compute(tenant)


def has_active_trial(db: Session, tenant_id: int) -> bool:
    """True when the tenant is still within Nahla's free-trial window."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return False
    return compute_trial_info(tenant)["is_trial"]


def _has_salla_active_subscription(db: Session, tenant_id: int) -> bool:
    """
    True when the tenant has an active or in-trial Salla App Store subscription.

    Salla billing_status values that grant access:
      - "active"  → paid subscription
      - "trial"   → first-time free trial (still within trial window)

    Values that do NOT grant access:
      - "trial_blocked" → trial already used on a previous install
      - "failed"        → payment failed
      - "cancelled"     → subscription cancelled
      - "none" / absent → no subscription yet
    """
    from models import Integration  # noqa: PLC0415

    integration = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
        )
        .first()
    )
    if not integration:
        return False

    status = (integration.config or {}).get("billing_status", "none")
    return status in ("active", "trial")


def has_billing_access(db: Session, tenant_id: int) -> bool:
    """
    True when the tenant can use paid AI features (outbound sends, AI replies, campaigns).

    Checks in priority order:
      1. Nahla-native active subscription (Stripe / HyperPay)
      2. Salla App Store active or trial subscription
      3. Nahla internal free-trial window

    Inbound ingestion, store sync, analytics, and webhook processing
    are ALWAYS allowed regardless of this flag — see automation_engine._execute_action.
    """
    return bool(
        get_tenant_subscription(db, tenant_id)
        or _has_salla_active_subscription(db, tenant_id)
        or has_active_trial(db, tenant_id)
    )


def require_outbound_access(db: Session, tenant_id: int) -> None:
    """
    Raise HTTP 402 before any outbound action:
      - AI / manual WhatsApp reply
      - Campaign send
      - Template send
      - Automation execution
      - Coupon auto-send

    Does NOT apply to: webhook ingestion, store sync, analytics reads.
    """
    if not has_billing_access(db, tenant_id):
        raise HTTPException(
            status_code=402,
            detail={
                "code":    "billing_access_denied",
                "message": "الاشتراك منتهٍ أو التجربة المجانية مستخدمة. يرجى الاشتراك للاستمرار في الإرسال.",
            },
        )


def is_launch_discount_active(sub: BillingSubscription) -> bool:
    """True if the subscription is still within the launch promo window."""
    if not sub.started_at:
        return False
    now = datetime.now(timezone.utc)
    started_at = _coerce_utc(sub.started_at)
    months_active = (
        (now.year - started_at.year) * 12
        + (now.month - started_at.month)
    )
    return months_active < LAUNCH_PROMO_MONTHS and started_at <= LAUNCH_PROMO_UNTIL


def require_subscription(db: Session, tenant_id: int) -> None:
    """Raise HTTP 402 if the tenant has no active Nahla subscription."""
    if not get_tenant_subscription(db, tenant_id):
        raise HTTPException(
            status_code=402,
            detail="الرجاء اختيار خطة نحلة لتفعيل الطيار الآلي للمبيعات.",
        )


def require_billing_access(db: Session, tenant_id: int) -> None:
    """Raise HTTP 402 if the tenant has neither an active subscription nor trial access."""
    if not has_billing_access(db, tenant_id):
        raise HTTPException(
            status_code=402,
            detail="انتهت التجربة المجانية. الرجاء اختيار خطة نحلة لمواصلة تفعيل الطيار الآلي.",
        )


# ── Moyasar gateway helpers ───────────────────────────────────────────────────

DEFAULT_MOYASAR: Dict[str, Any] = {
    "enabled": False,
    "secret_key": "",
    "publishable_key": "",
    "webhook_secret": "",
    "callback_url": "",
    "success_url": "",
    "error_url": "",
}


def get_moyasar_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Return Moyasar gateway config for a tenant, merged with defaults."""
    from core.tenant import get_or_create_settings, merge_defaults
    s = get_or_create_settings(db, tenant_id)
    meta = s.extra_metadata or {}
    return merge_defaults(meta.get("moyasar", {}), DEFAULT_MOYASAR)


def get_billing_gateway(db: Session, tenant_id: int):
    """
    Return (gateway_client, gateway_name, gateway_cfg) for billing checkout.
    Priority: Moyasar (tenant config) → Moyasar (env vars) → demo.
    Returns (None, 'demo', {}) when no gateway is configured.
    """
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

    # 1. Tenant-specific Moyasar config from DB
    cfg = get_moyasar_settings(db, tenant_id)
    if cfg.get("enabled") and cfg.get("secret_key"):
        from payment_gateways.moyasar import MoyasarClient  # noqa: PLC0415
        return (
            MoyasarClient(
                secret_key=cfg["secret_key"],
                publishable_key=cfg.get("publishable_key", ""),
            ),
            "moyasar",
            cfg,
        )

    # 2. Platform-level Moyasar env vars fallback
    from core.config import MOYASAR_SECRET_KEY, MOYASAR_PUBLISHABLE_KEY  # noqa: PLC0415
    if MOYASAR_SECRET_KEY:
        from payment_gateways.moyasar import MoyasarClient  # noqa: PLC0415
        env_cfg = {
            "enabled": True,
            "secret_key": MOYASAR_SECRET_KEY,
            "publishable_key": MOYASAR_PUBLISHABLE_KEY,
            "callback_url": "",
            "success_url": "",
            "error_url": "",
        }
        return (
            MoyasarClient(
                secret_key=MOYASAR_SECRET_KEY,
                publishable_key=MOYASAR_PUBLISHABLE_KEY,
            ),
            "moyasar",
            env_cfg,
        )

    return None, "demo", {}
