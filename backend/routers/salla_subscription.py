"""
routers/salla_subscription.py
──────────────────────────────
Salla Official App Subscriptions — backend support.

Salla manages billing entirely. Nahla receives subscription lifecycle webhooks
and stores status in the tenant's Integration.config JSONB field.

Stored fields (Integration.config):
  salla_subscription_id    str   — Salla subscription ID
  salla_plan_slug          str   — plan identifier slug (starter / growth / scale)
  salla_plan_name          str   — human-readable plan name
  billing_status           str   — active | trial | failed | cancelled | none
  salla_valid_till         str   — ISO timestamp when subscription expires
  billing_updated_at       str   — ISO timestamp of last billing event

Webhook events handled (called from webhook_dispatcher):
  subscription.created          → store sub ID, activate billing
  subscription.charge.succeeded → renew valid_till, mark active
  subscription.charge.failed    → mark billing_status=failed
  subscription.cancelled        → mark billing_status=cancelled
  subscription.updated          → update plan info

Endpoints:
  GET /salla/subscription/status  — current subscription for authenticated tenant
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import resolve_tenant_id

logger = logging.getLogger("nahla.salla_subscription")
router = APIRouter(tags=["salla-subscription"])

# ── Salla App Store URL ────────────────────────────────────────────────────────
SALLA_APP_ID = os.getenv("SALLA_APP_ID", os.getenv("SALLA_CLIENT_ID", ""))
SALLA_APP_STORE_URL = os.getenv(
    "SALLA_APP_STORE_URL",
    f"https://s.salla.sa/apps/{SALLA_APP_ID}" if SALLA_APP_ID else "https://s.salla.sa/apps",
)

# ── Plan catalogue ─────────────────────────────────────────────────────────────

PLAN_CATALOGUE = [
    {
        "slug":       "starter",
        "name":       "Starter",
        "name_ar":    "الأساسية",
        "price_sar":  979,
        "trial_days": 14,
        "badge":      None,
        "conversations_limit": 2_000,
        "campaigns_limit":     5,
        "features": [
            "لوحة التحكم والمحادثات الكاملة",
            "إدارة الطلبات والعملاء",
            "مكتبة قوالب نحلة + مزامنة Meta",
            "تأكيد الطلب والشحن التلقائي",
            "السلة المتروكة — مرحلتين",
            "كوبون استرجاع السلة الأساسي",
            "حملات تسويقية (حتى 5 / شهر)",
            "شرائح العملاء في الحملات",
            "تكامل سلة + WhatsApp Business",
        ],
        "locked_features": [
            {"key": "cart_recovery_stage_3",    "label": "السلة المتروكة — المرحلة الثالثة"},
            {"key": "advanced_coupon_types",    "label": "كوبونات VIP والاسترجاع المتقدمة"},
            {"key": "predictive_reorder",       "label": "إعادة الطلب التنبؤية"},
            {"key": "vip_rewards",              "label": "مكافآت العملاء VIP"},
            {"key": "seasonal_smart_offers",    "label": "العروض الموسمية الذكية"},
            {"key": "smart_discount_popup",     "label": "نافذة الخصم الذكية"},
            {"key": "meta_catalog_sync",        "label": "مزامنة كاتالوج ميتا"},
            {"key": "ai_performance_dashboard", "label": "لوحة أداء الذكاء الاصطناعي"},
        ],
    },
    {
        "slug":       "growth",
        "name":       "Growth",
        "name_ar":    "النمو",
        "price_sar":  1899,
        "trial_days": 14,
        "badge":      "🔥 الأكثر استخدامًا",
        "conversations_limit": 10_000,
        "campaigns_limit":     None,
        "features": [
            "كل مميزات باقة الأساسية",
            "الطيار الآلي الكامل + استرجاع العملاء",
            "السلة المتروكة — 3 مراحل مع كوبون",
            "كوبونات VIP + Inactive Recovery + مستويات",
            "حملات غير محدودة + تحسين بالذكاء",
            "إعادة الطلب التنبؤية",
            "مكافآت العملاء VIP",
            "العروض الموسمية + عروض الراتب",
            "نافذة الخصم الذكية",
            "مزامنة كاتالوج ميتا (Facebook / Instagram)",
            "لوحة أداء AI + مسار التحويل",
        ],
        "locked_features": [
            {"key": "store_brain_advanced",       "label": "ذكاء المتجر المتقدم"},
            {"key": "advanced_ai_analytics",      "label": "تحليلات الذكاء المتقدمة"},
            {"key": "team_handoff_queue",          "label": "طابور تحويل للفريق"},
            {"key": "zid_integration",             "label": "تكامل Zid"},
            {"key": "advanced_discount_rules",     "label": "قواعد الخصم المتقدمة"},
            {"key": "future_integrations",         "label": "تكاملات مستقبلية"},
        ],
    },
    {
        "slug":       "scale",
        "name":       "Scale",
        "name_ar":    "التوسع",
        "price_sar":  3199,
        "trial_days": 14,
        "badge":      None,
        "conversations_limit": None,
        "campaigns_limit":     None,
        "features": [
            "كل مميزات النمو",
            "محادثات وحملات غير محدودة",
            "ذكاء المتجر المتقدم",
            "تحليلات AI متقدمة + تفصيل الإيرادات",
            "أفضل المنتجات + مصادر الطلبات",
            "تخصيص الذكاء الكامل",
            "قواعد الخصم + التصعيد المتقدمة",
            "طابور تحويل للفريق",
            "تكامل Zid",
            "تكاملات مستقبلية (وصول مبكر)",
        ],
        "locked_features": [],
    },
]

_SLUG_MAP = {p["slug"]: p for p in PLAN_CATALOGUE}

# Salla sometimes sends plan names in English or Arabic — normalise to slug
_PLAN_NAME_TO_SLUG: dict[str, str] = {
    "starter":    "starter",
    "المبتدئ":    "starter",   # kept for backward-compat with old Salla webhooks
    "الأساسية":   "starter",   # new display name
    "growth":     "growth",
    "النمو":      "growth",
    "scale":      "scale",
    "التوسع":     "scale",
}


def _resolve_slug(slug_or_name: str) -> str:
    """Return canonical slug from Salla slug/plan_name field."""
    key = (slug_or_name or "").strip().lower()
    return _PLAN_NAME_TO_SLUG.get(key, key)


# ── GET /salla/subscription/status ───────────────────────────────────────────

@router.get("/salla/subscription/status")
async def get_salla_subscription_status(
    request: Request,
    db: Session = Depends(get_db),
):
    """Return current Salla subscription info for the authenticated tenant."""
    tenant_id = resolve_tenant_id(request)

    from models import Integration  # noqa: PLC0415

    integration = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
        )
        .first()
    )

    cfg = (integration.config or {}) if integration else {}

    return {
        "ok": True,
        "subscription": {
            "salla_subscription_id": cfg.get("salla_subscription_id"),
            "salla_plan_slug":       cfg.get("salla_plan_slug"),
            "salla_plan_name":       cfg.get("salla_plan_name"),
            "billing_status":        cfg.get("billing_status", "none"),
            "salla_valid_till":      cfg.get("salla_valid_till"),
            "billing_updated_at":    cfg.get("billing_updated_at"),
        },
        "plans":         PLAN_CATALOGUE,
        "app_store_url": SALLA_APP_STORE_URL,
    }


# ── Webhook handler (called by webhook_dispatcher) ───────────────────────────

def handle_subscription_webhook(
    db: Session,
    tenant_id: int,
    event_type: str,
    data: dict,
) -> None:
    """
    Update Integration.config with subscription lifecycle info.

    Called from core/webhook_dispatcher.py for:
      subscription.created
      subscription.charge.succeeded
      subscription.charge.failed
      subscription.cancelled
      subscription.updated
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
    if integration is None:
        logger.warning(
            "[SallaSub] No Salla integration for tenant=%s — cannot store subscription",
            tenant_id,
        )
        return

    sub_id    = str(data.get("id") or "")
    slug_raw  = str(data.get("slug") or data.get("plan_name") or "")
    slug      = _resolve_slug(slug_raw)
    plan_info = _SLUG_MAP.get(slug, {})
    plan_name = plan_info.get("name_ar") or slug_raw
    valid_till = str(data.get("valid_till") or data.get("end_date") or "")
    now_iso    = datetime.now(timezone.utc).isoformat()

    # ── Determine billing_status ──────────────────────────────────────────────
    if event_type == "subscription.created":
        # If total amount is 0 and trial_days in meta → trial
        total  = (data.get("total") or {}).get("amount", 1)
        meta   = data.get("meta") or {}
        status = "trial" if (total == 0 and meta.get("trial_days")) else "active"
    elif event_type == "subscription.charge.succeeded":
        status = "active"
    elif event_type == "subscription.charge.failed":
        status = "failed"
    elif event_type == "subscription.cancelled":
        status = "cancelled"
    elif event_type == "subscription.updated":
        # Keep previous status, just update plan info
        status = (integration.config or {}).get("billing_status", "active")
    else:
        status = "active"

    cfg = dict(integration.config or {})
    if sub_id:
        cfg["salla_subscription_id"] = sub_id
    if slug:
        cfg["salla_plan_slug"]  = slug
        cfg["salla_plan_name"]  = plan_name
    cfg["billing_status"]       = status
    if valid_till:
        cfg["salla_valid_till"] = valid_till
    cfg["billing_updated_at"]   = now_iso

    integration.config = cfg
    db.add(integration)
    db.flush()

    logger.info(
        "[SallaSub] %s | tenant=%s sub_id=%s slug=%s status=%s valid_till=%s",
        event_type, tenant_id, sub_id, slug, status, valid_till,
    )
