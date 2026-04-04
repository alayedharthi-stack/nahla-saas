"""
core/billing.py
───────────────
Billing plan seed data and helper functions shared by billing routers.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
from models import BillingPlan, BillingSubscription  # noqa: E402

# ── Billing constants ──────────────────────────────────────────────────────────
INTEGRATION_FEE_SAR = 59
LAUNCH_PROMO_MONTHS = 2
LAUNCH_PROMO_UNTIL  = datetime(2026, 6, 30, 23, 59, 59)
FREE_TRIAL_DAYS     = 14

BILLING_PLANS_SEED: List[Dict[str, Any]] = [
    {
        "slug": "starter",
        "name": "Starter",
        "name_ar": "المبتدئ",
        "description": "للمتاجر الصغيرة التي تبدأ رحلة الأتمتة",
        "price_sar": 899,
        "launch_price_sar": 449,
        "billing_cycle": "monthly",
        "features": [
            "ردود ذكاء اصطناعي تلقائية",
            "حتى 1,000 محادثة/شهر",
            "3 أتمتات فعّالة",
            "حملتان/شهر",
            "تحليلات أساسية",
        ],
        "limits": {
            "conversations_per_month": 1000,
            "automations": 3,
            "campaigns_per_month": 2,
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
            "ردود ذكاء اصطناعي تلقائية",
            "حتى 5,000 محادثة/شهر",
            "أتمتات غير محدودة",
            "10 حملات/شهر",
            "تحليلات متقدمة",
            "أولوية الدعم",
        ],
        "limits": {
            "conversations_per_month": 5000,
            "automations": -1,
            "campaigns_per_month": 10,
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
            "ردود ذكاء اصطناعي تلقائية",
            "محادثات غير محدودة",
            "أتمتات غير محدودة",
            "حملات غير محدودة",
            "تحليلات متقدمة + تقارير مخصصة",
            "دعم مخصص 24/7",
            "وصول API كامل",
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
    """Seed system billing plans on first use (idempotent)."""
    for seed in BILLING_PLANS_SEED:
        if not db.query(BillingPlan).filter(BillingPlan.slug == seed["slug"]).first():
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
    db.commit()


def get_tenant_subscription(db: Session, tenant_id: int) -> Optional[BillingSubscription]:
    """Return the active subscription for a tenant, or None."""
    return (
        db.query(BillingSubscription)
        .filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status == "active",
        )
        .order_by(BillingSubscription.started_at.desc())
        .first()
    )


def is_launch_discount_active(sub: BillingSubscription) -> bool:
    """True if the subscription is still within the launch promo window."""
    if not sub.started_at:
        return False
    now = datetime.utcnow()
    months_active = (
        (now.year - sub.started_at.year) * 12
        + (now.month - sub.started_at.month)
    )
    return months_active < LAUNCH_PROMO_MONTHS and sub.started_at <= LAUNCH_PROMO_UNTIL


def require_subscription(db: Session, tenant_id: int) -> None:
    """Raise HTTP 402 if the tenant has no active Nahla subscription."""
    if not get_tenant_subscription(db, tenant_id):
        raise HTTPException(
            status_code=402,
            detail="الرجاء اختيار خطة نحلة لتفعيل الطيار الآلي للمبيعات.",
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
