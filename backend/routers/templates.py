"""
routers/templates.py
─────────────────────
WhatsApp template management + AI generation + variable resolution.

Routes:
  GET  /templates                          — list all templates
  POST /templates                          — create + submit to Meta
  PUT  /templates/{id}/status              — update template status
  DELETE /templates/{id}                   — delete a template
  POST /templates/generate                 — AI-generate a draft template
  POST /templates/{id}/submit              — submit DRAFT template to Meta
  GET  /templates/health                   — health scores + recommendations
  PUT  /templates/{id}/recommendation      — merchant acts on recommendation
  GET  /templates/objectives               — supported AI generation objectives
  POST /templates/sync                     — sync templates from Meta Graph API
  GET  /templates/{id}/var-map             — variable → field mapping
  POST /templates/{id}/resolve             — resolve all vars for a customer
  GET  /campaigns/templates                — APPROVED templates for campaign wizard
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.templates")

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import Customer, CustomerProfile, TenantSettings, WhatsAppTemplate  # noqa: E402

from core.database import get_db
from core.tenant import (
    DEFAULT_STORE,
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    get_or_create_tenant,
    merge_defaults,
    resolve_tenant_id,
)
from services.whatsapp_platform.provider_utils import (
    WHATSAPP_CONNECTION_TYPE_DIRECT,
    WHATSAPP_PROVIDER_360DIALOG,
    wa_provider,
)
from services.whatsapp_platform.service import provider_list_templates, provider_submit_template
from services.customer_intelligence import CUSTOMER_STATUS_LABELS

# Default library: AR + EN templates for the 3 core revenue automations
# (cart_abandoned, customer_inactive, vip_customer_upgrade). Single source of
# truth for the named-slot contract enforced by the placeholder validator.
from core.template_library import (
    DEFAULT_AUTOMATION_TEMPLATES as CORE_FEATURE_TEMPLATES,
    iter_template_seeds as _iter_core_template_seeds,
    numeric_var_map_for as _core_lib_var_map_for,
)

router = APIRouter()

SUPPORTED_TEMPLATE_FIELDS: Dict[int, List[str]] = {
    1: ["customer_name"],
    2: ["product_name", "order_id", "cart_url", "store_name"],
    3: ["reorder_url", "order_amount", "discount_pct", "status_label", "coupon_code"],
    4: ["order_tracking_url"],
}

SUPPORTED_FEATURE_RULES: Dict[str, Dict[str, Any]] = {
    "order_status_update": {"category": {"UTILITY"}, "min_vars": 2, "max_vars": 3},
    "predictive_reorder": {"category": {"MARKETING", "UTILITY"}, "min_vars": 2, "max_vars": 3},
    "abandoned_cart": {"category": {"MARKETING", "UTILITY"}, "min_vars": 2, "max_vars": 2},
    "inactive_recovery": {"category": {"MARKETING"}, "min_vars": 2, "max_vars": 3},
}

DEFAULT_TEMPLATE_LIBRARY: Dict[str, Dict[str, Any]] = {
    "welcome_intro": {
        "library_key": "welcome",
        "label": "رسالة ترحيب",
        "objective": "welcome",
        "customer_statuses": ["lead", "new"],
        "rfm_segments": ["lead", "new_customers", "promising"],
    },
    "abandoned_cart_reminder": {
        "library_key": "abandoned_cart",
        "label": "استرداد سلة متروكة",
        "objective": "abandoned_cart",
        "customer_statuses": ["new", "active"],
        "rfm_segments": ["regulars", "potential_loyalists", "promising"],
    },
    "win_back": {
        "library_key": "reactivation",
        "label": "استرجاع العملاء",
        "objective": "reactivation",
        "customer_statuses": ["at_risk", "inactive"],
        "rfm_segments": ["at_risk", "hibernating", "lost_customers", "cant_lose_them"],
    },
    "vip_exclusive": {
        "library_key": "vip_offers",
        "label": "عروض VIP",
        "objective": "vip_offer",
        "customer_statuses": ["vip"],
        "rfm_segments": ["champions", "loyal_customers", "cant_lose_them"],
    },
    "product_recommendations": {
        "library_key": "product_recommendations",
        "label": "توصيات منتجات",
        "objective": "product_recommendations",
        "customer_statuses": ["active", "vip"],
        "rfm_segments": ["champions", "loyal_customers", "potential_loyalists", "regulars"],
    },
    "order_confirmed": {
        "library_key": "order_notifications",
        "label": "إشعارات الطلبات",
        "objective": "order_notifications",
        "customer_statuses": ["new", "active", "vip"],
        "rfm_segments": ["new_customers", "regulars", "champions", "loyal_customers"],
    },
    "predictive_reorder_reminder_ar": {
        "library_key": "product_recommendations",
        "label": "تذكير إعادة الطلب",
        "objective": "predictive_reorder",
        "customer_statuses": ["active", "vip"],
        "rfm_segments": ["loyal_customers", "potential_loyalists", "regulars"],
    },
}


# ── Seed data ─────────────────────────────────────────────────────────────────

SEED_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "welcome_intro",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "أهلًا بك في متجرنا"},
            {"type": "BODY", "text": "مرحباً {{1}}، يسعدنا وجودك معنا. اكتشف أفضل العروض والمنتجات الجديدة من {{2}}."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "abandoned_cart_reminder",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "سلّتك في انتظارك! 🛒"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلاحظنا أنك تركت بعض المنتجات في سلّتك.\nأكمل طلبك الآن قبل نفاد الكمية:\n{{2}}"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
            {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "أكمل الطلب", "url": "{{2}}"}]},
        ],
    },
    {
        "name": "special_offer",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "عرض خاص لك 🎁"},
            {"type": "BODY",   "text": "أهلاً {{1}}،\nاحصل على خصم {{2}} باستخدام كود: *{{3}}*\nالعرض ينتهي قريباً — لا تفوّته!"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "new_arrivals",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "وصل جديد! ✨"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nيسعدنا إعلامك بوصول منتجات جديدة في متجر {{2}}.\nاكتشف أحدث التشكيلة الآن وكن أول من يحصل عليها."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "product_recommendations",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "اخترنا لك هذه التوصية"},
            {"type": "BODY", "text": "مرحباً {{1}}، بناءً على اهتماماتك ننصحك بتجربة {{2}} من متجر {{3}}."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "vip_exclusive",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "👑 عرض VIP حصري"},
            {"type": "BODY",   "text": "{{1}}، أنت من عملائنا المميزين!\nبصفتك عضواً VIP لديك خصم حصري {{2}} على مشترياتك القادمة.\nاستخدم الكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "order_confirmed",
        "language": "ar",
        "category": "UTILITY",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "تأكيد الطلب ✅"},
            {"type": "BODY",   "text": "شكراً {{1}}!\nتم استلام طلبك رقم *{{2}}* بنجاح.\nسيتم التواصل معك قريباً لتأكيد موعد التوصيل."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "win_back",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "اشتقنا إليك! 💙"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلم نرك منذ فترة ونحن نفتقدك!\nعُد إلينا مع خصم خاص {{2}} على طلبك القادم.\nالكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "cod_order_confirmation_ar",
        "language": "ar",
        "category": "UTILITY",
        "status": "APPROVED",
        "components": [
            {"type": "BODY",   "text": "مرحباً {{1}} 🐝\n\nاستلمنا طلبك بنجاح 🍯\n\nالمنتج: {{2}}\nالمبلغ: {{3}} ريال\n\nطريقة الدفع: الدفع عند الاستلام\n\nيرجى تأكيد الطلب بالضغط على الزر أدناه."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
            {"type": "BUTTONS", "buttons": [
                {"type": "QUICK_REPLY", "text": "تأكيد الطلب ✅"},
                {"type": "QUICK_REPLY", "text": "إلغاء الطلب ❌"},
            ]},
        ],
    },
    {
        "name": "predictive_reorder_reminder_ar",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "BODY",   "text": "مرحباً {{1}} 🐝\n\nنتوقع أن {{2}} لديك قد أوشك على النفاد 🍯\n\nاطلب عبوة جديدة الآن:\n\n{{3}}"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
]

TEMPLATE_VAR_MAP: Dict[str, Dict[str, str]] = {
    "welcome_intro": {
        "{{1}}": "customer_name",
        "{{2}}": "store_name",
    },
    "predictive_reorder_reminder_ar": {
        "{{1}}": "customer_name",
        "{{2}}": "product_name",
        "{{3}}": "reorder_url",
    },
    "cod_order_confirmation_ar": {
        "{{1}}": "customer_name",
        "{{2}}": "product_name",
        "{{3}}": "order_amount",
    },
    "abandoned_cart_reminder": {
        "{{1}}": "customer_name",
        "{{2}}": "cart_url",
    },
    "special_offer": {
        "{{1}}": "customer_name",
        "{{2}}": "discount_pct",
        "{{3}}": "coupon_code",
    },
    "win_back": {
        "{{1}}": "customer_name",
        "{{2}}": "discount_pct",
        "{{3}}": "coupon_code",
    },
    "vip_exclusive": {
        "{{1}}": "customer_name",
        "{{2}}": "discount_pct",
        "{{3}}": "coupon_code",
    },
    "new_arrivals": {
        "{{1}}": "customer_name",
        "{{2}}": "store_name",
    },
    "product_recommendations": {
        "{{1}}": "customer_name",
        "{{2}}": "product_name",
        "{{3}}": "store_name",
    },
    "order_confirmed": {
        "{{1}}": "customer_name",
        "{{2}}": "order_id",
    },
}

VAR_FIELD_LABELS: Dict[str, str] = {
    "customer_name":  "اسم العميل",
    "product_name":   "اسم المنتج",
    "reorder_url":    "رابط إعادة الطلب",
    "order_amount":   "مبلغ الطلب (ر.س)",
    "cart_url":       "رابط السلة المتروكة",
    "checkout_url":   "رابط إتمام الطلب",
    "cart_total":     "إجمالي السلة",
    "discount_pct":   "نسبة الخصم",
    "coupon_code":    "كود الكوبون",
    "vip_coupon":     "كوبون VIP",
    "store_name":     "اسم المتجر",
    "store_url":      "رابط المتجر",
    "product_url":    "رابط المنتج",
    "order_id":       "رقم الطلب",
    "discount_code":  "كود الخصم",
    "status_label":   "وصف الحالة",
}

# Splice in the default library's var maps so the variable resolver, the AI
# rewrite assistant, and the placeholder integrity checker all see the new
# AR/EN core templates as first-class. This is additive — merchant-authored
# templates remain merchant-defined.
for _feature in CORE_FEATURE_TEMPLATES.values():
    for _lang_spec in _feature["languages"].values():
        TEMPLATE_VAR_MAP[_lang_spec["template_name"]] = _core_lib_var_map_for(
            _lang_spec["template_name"]
        )

MOCK_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "mock_1",
        "name": "abandoned_cart_reminder",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "سلّتك في انتظارك! 🛒"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلاحظنا أنك تركت بعض المنتجات في سلّتك.\nأكمل طلبك الآن قبل نفاد الكمية: {{2}}"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
            {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "أكمل الطلب", "url": "{{2}}"}]},
        ],
    },
    {
        "id": "mock_2",
        "name": "special_offer",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "عرض خاص لك 🎁"},
            {"type": "BODY",   "text": "أهلاً {{1}}،\nاحصل على خصم {{2}} باستخدام كود: *{{3}}*\nالعرض ينتهي قريباً — لا تفوّته!"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_3",
        "name": "new_arrivals",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "وصل جديد! ✨"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nيسعدنا إعلامك بوصول منتجات جديدة في متجر {{2}}.\nاكتشف أحدث التشكيلة الآن وكن أول من يحصل عليها."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_4",
        "name": "vip_exclusive",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "👑 عرض VIP حصري"},
            {"type": "BODY",   "text": "{{1}}، أنت من عملائنا المميزين!\nبصفتك عضواً VIP لديك خصم حصري {{2}} على مشترياتك القادمة.\nاستخدم الكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_5",
        "name": "order_confirmed",
        "language": "ar",
        "category": "UTILITY",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "تأكيد الطلب ✅"},
            {"type": "BODY",   "text": "شكراً {{1}}!\nتم استلام طلبك رقم *{{2}}* بنجاح.\nسيتم التواصل معك قريباً لتأكيد موعد التوصيل."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_6",
        "name": "win_back",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "اشتقنا إليك! 💙"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلم نرك منذ فترة ونحن نفتقدك!\nعدت إلينا مع خصم خاص {{2}} على طلبك القادم.\nالكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
]


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class TemplateComponentIn(BaseModel):
    type: str
    format: Optional[str] = None
    text: Optional[str] = None
    buttons: Optional[List[Dict[str, Any]]] = None


class CreateTemplateIn(BaseModel):
    name: str
    language: str = "ar"
    category: str
    components: List[TemplateComponentIn]
    auto_submit: bool = False


class UpdateTemplateIn(BaseModel):
    name: Optional[str] = None
    language: Optional[str] = None
    category: Optional[str] = None
    components: Optional[List[TemplateComponentIn]] = None


class UpdateTemplateStatusIn(BaseModel):
    status: str
    rejection_reason: Optional[str] = None
    meta_template_id: Optional[str] = None


class GenerateTemplateIn(BaseModel):
    objective: str
    language: str = "ar"


class TemplateAIRewriteIn(BaseModel):
    """
    Request body for `POST /templates/{id}/ai-rewrite`.

    `mode` selects one of the four canonical assistant actions the merchant
    UI exposes ("Improve message", "Rewrite professionally", "Shorten",
    "Make friendlier"). `apply` controls whether we persist the rewrite or
    just return a preview the merchant can accept/reject in the UI.
    """
    mode: str  # "improve" | "professional" | "shorten" | "friendlier"
    language: Optional[str] = None    # falls back to the template's language
    apply: bool = False               # save when True, preview-only when False


class RecommendationActionIn(BaseModel):
    action: str  # accepted | dismissed


# ── Helper functions ───────────────────────────────────────────────────────────

def _seed_templates_if_empty(db: Session, tenant_id: int) -> None:
    """
    Seed demo templates ONLY when:
    - The tenant has zero templates in DB
    - AND the tenant has no connected WhatsApp number
    If WhatsApp is connected, real templates must come from Meta via /templates/sync.
    """
    from models import WhatsAppConnection  # noqa: PLC0415
    count = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id).count()
    if count > 0:
        return
    # Do NOT seed if the tenant already has a connected WhatsApp number
    wa_connected = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant_id,
        WhatsAppConnection.status    == "connected",
    ).first()
    if wa_connected:
        logger.info(
            "[templates] Skipping seed for tenant=%s — WhatsApp is connected, use /templates/sync",
            tenant_id,
        )
        return

    # Combine legacy seeds with the canonical default-library seeds (AR + EN
    # for the 3 core revenue automations). Library seeds win on name conflict
    # so the canonical numeric body and named-slot contract take precedence.
    library_seeds: List[Dict[str, Any]] = []
    for lang in ("ar", "en"):
        library_seeds.extend(_iter_core_template_seeds(lang))
    library_names = {s["name"] for s in library_seeds}
    combined_seeds: List[Dict[str, Any]] = [
        s for s in SEED_TEMPLATES if s["name"] not in library_names
    ] + library_seeds

    for seed in combined_seeds:
        library_meta = DEFAULT_TEMPLATE_LIBRARY.get(seed["name"], {})
        tpl = WhatsAppTemplate(
            tenant_id=tenant_id,
            meta_template_id=f"seed_{seed['name']}",
            name=seed["name"],
            language=seed["language"],
            category=seed["category"],
            status=seed["status"],
            components=seed["components"],
            source="library_default",
            objective=library_meta.get("objective"),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            synced_at=datetime.now(timezone.utc),
        )
        db.add(tpl)
    db.flush()


def _fetch_meta_templates(waba_id: str, access_token: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch ALL templates from Meta Graph API with pagination."""
    try:
        import json as _json
        import urllib.parse
        import urllib.request
        fields = "id,name,language,category,status,components,quality_score,rejected_reason"
        url = (
            f"https://graph.facebook.com/v20.0/{waba_id}/message_templates"
            f"?access_token={urllib.parse.quote(access_token)}&limit=100&fields={fields}"
        )
        results: List[Dict[str, Any]] = []
        while url:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = _json.loads(resp.read())
            results.extend(data.get("data", []))
            paging = data.get("paging") or {}
            url = paging.get("next")
        return results
    except Exception as exc:
        logger.warning("[templates] _fetch_meta_templates failed waba=%s: %s", waba_id, exc)
        return None


def _normalize_provider_template_list(conn: Any, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        return list(payload.get("waba_templates") or [])
    return list(payload.get("data") or [])


def _tpl_to_dict(t: WhatsAppTemplate) -> Dict[str, Any]:
    meta = dict(getattr(t, "ai_generation_metadata", None) or {})
    compatibility = meta.get("meta_compatibility") or _compute_template_compatibility(
        t.components or [],
        category=t.category,
        language=t.language,
        status=t.status,
        template_name=t.name,
    )
    library_meta = DEFAULT_TEMPLATE_LIBRARY.get(t.name, {})
    return {
        "id": t.id,
        "meta_template_id": t.meta_template_id,
        "name": t.name,
        "language": t.language,
        "category": t.category,
        "status": t.status,
        "workflow_status": "pending_approval" if t.status == "PENDING" else str(t.status or "DRAFT").lower(),
        "status_raw": meta.get("meta_status_raw", t.status),
        "rejection_reason": t.rejection_reason,
        "components": t.components or [],
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "synced_at": t.synced_at.isoformat() if t.synced_at else None,
        "source": getattr(t, "source", "merchant") or "merchant",
        "objective": getattr(t, "objective", None),
        "usage_count": getattr(t, "usage_count", 0) or 0,
        "last_used_at": t.last_used_at.isoformat() if getattr(t, "last_used_at", None) else None,
        "health_score": getattr(t, "health_score", None),
        "recommendation_state": getattr(t, "recommendation_state", "none") or "none",
        "recommendation_note": getattr(t, "recommendation_note", None),
        "ai_generation_metadata": getattr(t, "ai_generation_metadata", None),
        "editable": t.status in {"DRAFT", "REJECTED"},
        "submittable": t.status in {"DRAFT", "REJECTED"},
        "library": library_meta or None,
        "compatibility": compatibility,
    }


def _tpl_bump_usage(db: Session, template_id: int, tenant_id: int | None = None) -> None:
    """Increment usage_count and set last_used_at."""
    q = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id)
    if tenant_id is not None:
        q = q.filter(WhatsAppTemplate.tenant_id == tenant_id)
    tpl = q.first()
    if tpl:
        tpl.usage_count = (getattr(tpl, "usage_count", 0) or 0) + 1
        tpl.last_used_at = datetime.now(timezone.utc)


def _normalize_template_status(raw_status: Any) -> str:
    raw = str(raw_status or "PENDING").strip().upper()
    status_map = {
        "DRAFT": "DRAFT",
        "APPROVED": "APPROVED",
        "ACTIVE": "APPROVED",
        "PENDING": "PENDING",
        "IN_REVIEW": "PENDING",
        "PENDING_REVIEW": "PENDING",
        "REJECTED": "REJECTED",
        "DISABLED": "DISABLED",
        "PAUSED": "PAUSED",
        "ARCHIVED": "ARCHIVED",
        "DELETED": "ARCHIVED",
        "LIMIT_EXCEEDED": "LIMIT_EXCEEDED",
    }
    return status_map.get(raw, raw or "PENDING")


def _normalize_template_language(raw_language: Any) -> str:
    lang = str(raw_language or "ar").strip()
    return lang.replace("-", "_")


def _normalize_template_category(raw_category: Any) -> str:
    category = str(raw_category or "MARKETING").strip().upper()
    return category if category in {"MARKETING", "UTILITY", "AUTHENTICATION"} else "MARKETING"


def _placeholder_sort_key(value: str) -> tuple[int, Any]:
    raw = str(value or "").strip()
    inner = raw[2:-2].strip() if raw.startswith("{{") and raw.endswith("}}") else raw
    return (0, int(inner)) if inner.isdigit() else (1, inner)


def _extract_placeholders_from_text(text: str) -> List[str]:
    import re
    return sorted(set(re.findall(r"\{\{[^{}]+\}\}", text or "")), key=_placeholder_sort_key)


def _extract_template_placeholders(components: List[Dict[str, Any]]) -> List[str]:
    placeholders: set[str] = set()
    for comp in components or []:
        text = str(comp.get("text") or "")
        placeholders.update(_extract_placeholders_from_text(text))
        for btn in comp.get("buttons") or []:
            placeholders.update(_extract_placeholders_from_text(str(btn.get("text") or "")))
            placeholders.update(_extract_placeholders_from_text(str(btn.get("url") or "")))
    return sorted(placeholders, key=_placeholder_sort_key)


def _guess_field_candidates(placeholder: str, *, category: str, template_name: str) -> List[str]:
    inner = placeholder.strip("{}").strip()
    if inner and not inner.isdigit():
        return [inner]
    try:
        idx = int(placeholder.strip("{}"))
    except Exception:
        idx = 0
    candidates = list(SUPPORTED_TEMPLATE_FIELDS.get(idx, []))
    lower_name = (template_name or "").lower()
    if "order" in lower_name and "order_id" not in candidates:
        candidates.insert(0, "order_id")
    if "status" in lower_name and "status_label" not in candidates:
        candidates.insert(0, "status_label")
    if "cart" in lower_name and "cart_url" not in candidates:
        candidates.insert(0, "cart_url")
    if category == "UTILITY" and "status_label" not in candidates and idx == 3:
        candidates.insert(0, "status_label")
    # preserve order and uniqueness
    deduped: List[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _infer_var_map(
    components: List[Dict[str, Any]],
    *,
    category: str,
    template_name: str,
) -> Dict[str, str]:
    name_map = TEMPLATE_VAR_MAP.get(template_name, {})
    placeholders = _extract_template_placeholders(components)
    inferred: Dict[str, str] = {}
    for placeholder in placeholders:
        if placeholder in name_map:
            inferred[placeholder] = name_map[placeholder]
            continue
        candidates = _guess_field_candidates(placeholder, category=category, template_name=template_name)
        if candidates:
            inferred[placeholder] = candidates[0]
    return inferred


def _validate_placeholder_integrity(
    *,
    old_components: List[Dict[str, Any]],
    new_components: List[Dict[str, Any]],
) -> None:
    old_placeholders = _extract_template_placeholders(old_components)
    new_placeholders = _extract_template_placeholders(new_components)
    if old_placeholders and old_placeholders != new_placeholders:
        raise HTTPException(
            status_code=422,
            detail=(
                "لا يمكن حذف أو إعادة تسمية placeholders في القالب. "
                "يمكنك تعديل النص المحيط فقط مع الإبقاء على نفس المتغيرات."
            ),
        )


def _compute_template_compatibility(
    components: List[Dict[str, Any]],
    *,
    category: str,
    language: str,
    status: str,
    template_name: str,
) -> Dict[str, Any]:
    placeholders = _extract_template_placeholders(components)
    has_body_text = any(comp.get("type") == "BODY" and comp.get("text") for comp in (components or []))
    var_map = _infer_var_map(components, category=category, template_name=template_name)
    status_norm = _normalize_template_status(status)
    issues: List[str] = []
    if not has_body_text:
        issues.append("القالب لا يحتوي على BODY نصي")
    if status_norm != "APPROVED":
        issues.append(f"حالة Meta الحالية: {status_norm}")
    supported_features: List[str] = []
    for feature, rule in SUPPORTED_FEATURE_RULES.items():
        if category not in rule["category"]:
            continue
        count = len(placeholders)
        if count < rule["min_vars"] or count > rule["max_vars"]:
            continue
        supported_features.append(feature)
    compatibility = "compatible" if has_body_text and bool(supported_features) else "review_needed"
    if status_norm != "APPROVED":
        compatibility = "pending_meta"
    return {
        "compatibility": compatibility,
        "placeholder_count": len(placeholders),
        "placeholders": placeholders,
        "var_map": var_map,
        "supported_features": supported_features,
        "issues": issues,
        "has_body_text": has_body_text,
        "language_normalized": _normalize_template_language(language),
        "category_normalized": _normalize_template_category(category),
        "status_normalized": status_norm,
    }


async def _submit_template_to_meta(
    *,
    db: Session,
    conn: Any,
    tenant_id: int,
    waba_id: str,
    name: str,
    language: str,
    category: str,
    components: List[Dict[str, Any]],
) -> Optional[str]:
    """Submit a new template using the active WhatsApp provider."""
    try:
        payload = {
            "name": name,
            "language": language,
            "category": category,
            "components": components,
        }
        result, _token_ctx = await provider_submit_template(
            db,
            conn,
            tenant_id=tenant_id,
            waba_id=waba_id,
            payload=payload,
            prefer_platform=bool(conn and getattr(conn, "connection_type", None) == WHATSAPP_CONNECTION_TYPE_DIRECT),
        )
        return str(result.get("id") or result.get("external_id") or "")
    except Exception:
        return None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(
    request: Request,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List all WhatsApp templates for this tenant, optionally filtered by status."""
    from models import WhatsAppConnection  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    # ── Auto-clean: if WhatsApp is connected, seed templates are fake — remove them ──
    wa_conn = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant_id,
        WhatsAppConnection.status.in_(["connected", "pending"]),
    ).first()
    if wa_conn:
        deleted = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.meta_template_id.like("seed_%"),
            )
            .delete(synchronize_session=False)
        )
        if deleted:
            logger.info(
                "[templates/list] Auto-removed %d seed templates for tenant=%s (WA connected)",
                deleted, tenant_id,
            )
    else:
        _seed_templates_if_empty(db, tenant_id)

    db.commit()

    q = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id)
    if status:
        q = q.filter(WhatsAppTemplate.status == status.upper())
    templates = q.order_by(WhatsAppTemplate.created_at.desc()).all()
    return {"templates": [_tpl_to_dict(t) for t in templates]}


@router.post("/templates")
async def create_template(body: CreateTemplateIn, request: Request, db: Session = Depends(get_db)):
    """Create a new template locally as DRAFT, with optional immediate submit."""
    from models import WhatsAppConnection  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    settings = get_or_create_settings(db, tenant_id)
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    wa_conn = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant_id,
        WhatsAppConnection.status.in_(["connected", "pending", "review_pending", "activation_pending"]),
    ).first()
    waba_id = (
        (wa_conn.whatsapp_business_account_id if wa_conn else None)
        or wa.get("whatsapp_business_account_id", "")
    )
    components = [c.model_dump(exclude_none=True) for c in body.components]
    normalized_language = _normalize_template_language(body.language)
    normalized_category = _normalize_template_category(body.category)
    compatibility = _compute_template_compatibility(
        components,
        category=normalized_category,
        language=normalized_language,
        status="DRAFT",
        template_name=body.name,
    )

    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        meta_template_id=None,
        name=body.name,
        language=normalized_language,
        category=normalized_category,
        status="DRAFT",
        components=components,
        source="merchant",
        ai_generation_metadata={"meta_compatibility": compatibility},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(tpl)
    db.flush()

    meta_id = None
    if body.auto_submit and waba_id:
        meta_id = await _submit_template_to_meta(
            db=db,
            conn=wa_conn,
            tenant_id=tenant_id,
            waba_id=waba_id,
            name=tpl.name,
            language=tpl.language,
            category=tpl.category,
            components=tpl.components or [],
        )
        if meta_id:
            tpl.meta_template_id = meta_id
            tpl.status = "PENDING"
            tpl.synced_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(tpl)
    return _tpl_to_dict(tpl)


@router.put("/templates/{template_id}")
async def update_template(
    template_id: int,
    body: UpdateTemplateIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Edit a non-approved template. Any edit moves the workflow back to DRAFT."""
    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.status == "APPROVED":
        raise HTTPException(
            status_code=400,
            detail="لا يمكن تعديل قالب معتمد مباشرةً. أنشئ نسخة جديدة كمسودة إذا أردت تغيير محتواه.",
        )

    new_components = [c.model_dump(exclude_none=True) for c in body.components] if body.components is not None else (tpl.components or [])
    _validate_placeholder_integrity(
        old_components=tpl.components or [],
        new_components=new_components,
    )

    if body.name is not None:
        tpl.name = body.name
    if body.language is not None:
        tpl.language = _normalize_template_language(body.language)
    if body.category is not None:
        tpl.category = _normalize_template_category(body.category)
    tpl.components = new_components
    tpl.status = "DRAFT"
    tpl.rejection_reason = None
    tpl.meta_template_id = None
    tpl.updated_at = datetime.now(timezone.utc)
    tpl.ai_generation_metadata = {
        **(tpl.ai_generation_metadata or {}),
        "meta_compatibility": _compute_template_compatibility(
            tpl.components or [],
            category=tpl.category,
            language=tpl.language,
            status=tpl.status,
            template_name=tpl.name,
        ),
    }
    db.commit()
    db.refresh(tpl)
    return _tpl_to_dict(tpl)


@router.put("/templates/{template_id}/status")
async def update_template_status(
    template_id: int,
    body: UpdateTemplateStatusIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update template status (called by webhook or manually for testing)."""
    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.status = body.status.upper()
    if body.rejection_reason:
        tpl.rejection_reason = body.rejection_reason
    if body.meta_template_id:
        tpl.meta_template_id = body.meta_template_id
    tpl.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(tpl)
    return _tpl_to_dict(tpl)


@router.delete("/templates/{template_id}")
async def delete_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a template (only allowed for PENDING/REJECTED/DISABLED)."""
    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.status == "APPROVED":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete an APPROVED template — disable it from Meta Business Manager first",
        )
    db.delete(tpl)
    db.commit()
    return {"deleted": True}


_AI_REWRITE_MODES: Dict[str, Dict[str, str]] = {
    # Each mode carries a system-prompt instruction in both AR and EN. The AI
    # call uses the language matching the template, so the model is asked to
    # rewrite in the same language it sees.
    "improve": {
        "ar": "حسّن صياغة الرسالة لتكون أكثر وضوحًا وجاذبية مع الحفاظ على نفس المعنى والطول التقريبي.",
        "en": "Improve the wording for clarity and engagement while keeping the same meaning and roughly the same length.",
    },
    "professional": {
        "ar": "أعد صياغة الرسالة بأسلوب احترافي ومهذّب يناسب التواصل التجاري الرسمي.",
        "en": "Rewrite the message in a polished, professional tone suitable for business communication.",
    },
    "shorten": {
        "ar": "اختصر الرسالة لتكون أقصر بنسبة 30-40% مع الحفاظ على المعنى الأساسي والمتغيرات.",
        "en": "Shorten the message by about 30–40% while keeping the core meaning and the variables intact.",
    },
    "friendlier": {
        "ar": "أعد كتابة الرسالة بأسلوب أكثر ودًا ودفئًا مع الحفاظ على الاحترافية.",
        "en": "Rewrite the message in a warmer, friendlier tone while staying professional.",
    },
}


def _placeholder_protect(text: str) -> tuple[str, Dict[str, str]]:
    """
    Replace each `{{N}}` (or named `{{slot}}`) placeholder in `text` with a
    sentinel like `__NHVAR1__` that LLMs do not touch, and return the
    sentinel→placeholder mapping so we can restore them after the rewrite.

    This is significantly more reliable than asking the LLM to "preserve
    `{{1}}`" — Anthropic and OpenAI both occasionally drop or normalise
    Arabic-adjacent placeholders even with explicit instructions.
    """
    import re  # noqa: PLC0415

    mapping: Dict[str, str] = {}
    counter = {"i": 0}

    def _swap(match):
        counter["i"] += 1
        sentinel = f"__NHVAR{counter['i']}__"
        mapping[sentinel] = match.group(0)
        return sentinel

    protected = re.sub(r"\{\{[^{}]+\}\}", _swap, text or "")
    return protected, mapping


def _placeholder_restore(text: str, mapping: Dict[str, str]) -> str:
    out = text or ""
    for sentinel, original in mapping.items():
        out = out.replace(sentinel, original)
    return out


def _ai_rewrite_body_text(
    *,
    body_text: str,
    mode: str,
    language: str,
    tenant_id: int,
    store_name: str,
) -> str:
    """
    Run the merchant-facing rewrite assistant on a single BODY string.

    Strategy:
      1. Mask every `{{…}}` placeholder with a sentinel that the LLM cannot
         meaningfully alter. This guarantees the contract — even if the
         model rewrites everything else, the placeholders survive verbatim.
      2. Build a tight system prompt containing the mode instruction and a
         hard rule about not touching the sentinels.
      3. Call the existing AI orchestration adapter so we benefit from the
         provider chain (Anthropic → OpenAI fallback) already in place.
      4. Restore placeholders.

    Raises HTTPException with a clear merchant-facing message on failure.
    """
    instruction = _AI_REWRITE_MODES.get(mode, {}).get(language) or _AI_REWRITE_MODES["improve"][language]
    masked, sentinel_map = _placeholder_protect(body_text)

    system_prompt = (
        "You are a careful copy editor for WhatsApp Business marketing messages.\n"
        "Rules you MUST follow:\n"
        "  1. Never modify, translate, remove, or re-order tokens that match `__NHVAR\\d+__`.\n"
        "  2. Keep approximately the same number of lines; do not add bullet lists or HTML.\n"
        "  3. Stay within WhatsApp Business policy — no spammy claims, no shortened links, "
        "no excessive emojis, no all-caps shouting.\n"
        "  4. Output ONLY the rewritten body text. Do not add any preface, explanation, or quotes.\n"
        f"\nLanguage of the message: {language}\n"
        f"Store name (for context — do not insert it unless already present): {store_name}\n"
        f"\nTask: {instruction}"
    )

    full_prompt = f"{system_prompt}\n\nORIGINAL MESSAGE:\n{masked}\n\nREWRITTEN MESSAGE:\n"

    try:
        import sys as _sys  # noqa: PLC0415
        _sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from modules.ai.orchestrator.adapter import generate_ai_reply  # noqa: PLC0415

        payload = generate_ai_reply(
            tenant_id=tenant_id,
            customer_phone="",
            message=masked,
            store_name=store_name,
            channel="system",
            locale=language,
            context_metadata={"mode": "template_rewrite", "rewrite_mode": mode},
            prompt_overrides={"__full_system_prompt": full_prompt},
        )
        rewritten_masked = (payload.reply_text or "").strip()
    except Exception as exc:
        logger.warning("[templates/ai-rewrite] orchestrator failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="تعذّر الاتصال بمساعد الذكاء الاصطناعي حاليًا. حاول مرة أخرى بعد قليل.",
        )

    if not rewritten_masked:
        raise HTTPException(
            status_code=502,
            detail="لم يُرجع المساعد أي نص. حاول مرة أخرى أو استخدم وضعًا مختلفًا.",
        )

    rewritten = _placeholder_restore(rewritten_masked, sentinel_map)

    # Defensive contract check: every sentinel must round-trip back into
    # the final text. Surfacing this as a 422 lets the dashboard show a
    # specific error and offer the merchant the un-rewritten body.
    missing = [orig for sentinel, orig in sentinel_map.items() if orig not in rewritten]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                "المساعد فقد بعض المتغيرات أثناء إعادة الصياغة: "
                + ", ".join(missing)
                + ". لم نطبّق التعديل."
            ),
        )

    return rewritten


@router.post("/templates/{template_id}/ai-rewrite")
async def ai_rewrite_template_body(
    template_id: int,
    body: TemplateAIRewriteIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Merchant assistant — rewrite a template's BODY text in one of four modes:

      • improve       — clearer / more engaging
      • professional  — more formal business tone
      • shorten       — ~30–40% shorter
      • friendlier    — warmer, more personal

    Guarantees
    ──────────
    The rewrite NEVER drops or renames placeholders. Every `{{…}}` token in
    the original body is preserved verbatim in the result; if the AI fails
    to honour that contract, we return 422 and do not save anything.

    `apply=false` (default) returns a preview the merchant can accept in
    the UI. `apply=true` saves the change and resets the template back to
    DRAFT (the merchant must re-submit it to Meta for approval — that is
    handled by the existing `PUT /templates/{id}` write path).
    """
    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if body.mode not in _AI_REWRITE_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"الوضع '{body.mode}' غير مدعوم. الأوضاع المتاحة: {', '.join(_AI_REWRITE_MODES.keys())}",
        )

    if tpl.status == "APPROVED" and body.apply:
        raise HTTPException(
            status_code=400,
            detail="لا يمكن تعديل قالب معتمد مباشرةً. اطلب معاينة فقط أو أنشئ نسخة جديدة كمسودة.",
        )

    language = (body.language or tpl.language or "ar").lower()
    if language not in ("ar", "en"):
        language = "ar"

    settings = get_or_create_settings(db, tenant_id)
    store = merge_defaults(settings.store_settings, DEFAULT_STORE)
    store_name = store.get("store_name") or "متجرنا"

    components = list(tpl.components or [])
    body_idx: Optional[int] = next(
        (i for i, c in enumerate(components) if str(c.get("type")).upper() == "BODY"),
        None,
    )
    if body_idx is None or not (components[body_idx].get("text") or "").strip():
        raise HTTPException(
            status_code=422,
            detail="هذا القالب لا يحتوي على نص BODY قابل لإعادة الصياغة.",
        )

    original_body_text = components[body_idx]["text"]
    rewritten_body_text = _ai_rewrite_body_text(
        body_text=original_body_text,
        mode=body.mode,
        language=language,
        tenant_id=int(tenant_id),
        store_name=store_name,
    )

    proposed_components = [dict(c) for c in components]
    proposed_components[body_idx] = {**proposed_components[body_idx], "text": rewritten_body_text}

    # Re-run the placeholder integrity check end-to-end. Belt-and-braces:
    # the body-level sentinel check above already guarantees this, but the
    # template might also have placeholders inside BUTTONS/HEADER and we
    # want a single, authoritative invariant for the whole template.
    _validate_placeholder_integrity(
        old_components=components,
        new_components=proposed_components,
    )

    if not body.apply:
        return {
            "template_id":         tpl.id,
            "mode":                body.mode,
            "language":            language,
            "original_body":       original_body_text,
            "rewritten_body":      rewritten_body_text,
            "proposed_components": proposed_components,
            "applied":             False,
        }

    # apply=True: persist the rewrite and reset to DRAFT so re-submission
    # can be triggered. We do this here directly (rather than POST /update)
    # so the merchant sees an atomic "rewritten and saved" outcome.
    tpl.components = proposed_components
    tpl.status = "DRAFT"
    tpl.rejection_reason = None
    tpl.meta_template_id = None
    tpl.updated_at = datetime.now(timezone.utc)
    tpl.ai_generation_metadata = {
        **(tpl.ai_generation_metadata or {}),
        "last_ai_rewrite": {
            "mode": body.mode,
            "language": language,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    }
    db.commit()
    db.refresh(tpl)

    return {
        "template_id":         tpl.id,
        "mode":                body.mode,
        "language":            language,
        "original_body":       original_body_text,
        "rewritten_body":      rewritten_body_text,
        "proposed_components": proposed_components,
        "applied":             True,
        "template":            _tpl_to_dict(tpl),
    }


@router.post("/templates/generate")
async def generate_template(body: GenerateTemplateIn, request: Request, db: Session = Depends(get_db)):
    """AI-generate a WhatsApp template draft for a given objective."""
    import sys as _sys
    _sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from template_ai.generator import SUPPORTED_OBJECTIVES, generate_template_draft
    from template_ai.policy_validator import validate_draft

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    if body.objective not in SUPPORTED_OBJECTIVES:
        raise HTTPException(
            status_code=422,
            detail=f"الهدف '{body.objective}' غير مدعوم. الأهداف المتاحة: {', '.join(SUPPORTED_OBJECTIVES)}",
        )

    draft = generate_template_draft(objective=body.objective, language=body.language)

    existing = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id).all()
    validation = validate_draft(draft, existing)

    if not validation.passed and validation.action == "block":
        return {
            "generated": False,
            "action": "block",
            "issues": validation.issues,
            "draft": draft,
        }

    if validation.action == "merge":
        return {
            "generated": False,
            "action": "merge",
            "issues": validation.issues,
            "merge_with_id": validation.merge_with_id,
            "merge_with_name": validation.merge_with_name,
            "draft": draft,
        }

    settings = get_or_create_settings(db, tenant_id)
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    submission_mode = wa.get("template_submission_mode", "draft_approval")

    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=draft["name"],
        language=draft["language"],
        category=draft["category"],
        status="DRAFT",
        components=draft["components"],
        source="ai_generated",
        objective=draft["objective"],
        usage_count=0,
        ai_generation_metadata=draft["ai_generation_metadata"],
    )
    db.add(tpl)
    db.flush()

    submitted = False
    meta_id = None
    if submission_mode == "auto_submit":
        waba_id = wa.get("whatsapp_business_account_id", "")
        from models import WhatsAppConnection  # noqa: PLC0415
        wa_conn = db.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == tenant_id).first()
        if waba_id:
            meta_id = await _submit_template_to_meta(
                db=db,
                conn=wa_conn,
                tenant_id=tenant_id,
                waba_id=waba_id,
                name=tpl.name,
                language=tpl.language,
                category=tpl.category,
                components=tpl.components or [],
            )
            if meta_id:
                tpl.meta_template_id = meta_id
                tpl.status = "PENDING"
                tpl.synced_at = datetime.now(timezone.utc)
                submitted = True

    db.commit()
    db.refresh(tpl)

    from observability.event_logger import log_event
    log_event(db, tenant_id, "ai_sales", "template.generated",
              f"قالب AI جديد: {tpl.name} (هدف: {body.objective})",
              payload={"template_id": tpl.id, "submitted": submitted, "mode": submission_mode})
    db.commit()

    return {
        "generated": True,
        "action": "auto_submitted" if submitted else "saved_as_draft",
        "template": _tpl_to_dict(tpl),
        "validation_issues": validation.issues,
        "submission_mode": submission_mode,
    }


@router.post("/templates/{template_id}/submit")
async def submit_template_to_meta(template_id: int, request: Request, db: Session = Depends(get_db)):
    """Submit a DRAFT template to Meta for approval."""
    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.status not in ("DRAFT", "REJECTED"):
        raise HTTPException(status_code=400, detail=f"لا يمكن إرسال قالب بحالة '{tpl.status}' إلى Meta")

    settings = get_or_create_settings(db, tenant_id)
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    waba_id = wa.get("whatsapp_business_account_id", "")
    if not waba_id:
        raise HTTPException(
            status_code=422,
            detail="بيانات WhatsApp Business غير مُعدَّة. أضف WABA ID أو أكمل ربط واتساب أولاً.",
        )

    from models import WhatsAppConnection  # noqa: PLC0415
    wa_conn = db.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == tenant_id).first()
    meta_id = await _submit_template_to_meta(
        db=db,
        conn=wa_conn,
        tenant_id=tenant_id,
        waba_id=waba_id,
        name=tpl.name,
        language=tpl.language,
        category=tpl.category,
        components=tpl.components or [],
    )
    if not meta_id:
        raise HTTPException(status_code=502, detail="فشل إرسال القالب إلى Meta. تحقق من بيانات الاعتماد وحاول مرة أخرى.")

    tpl.meta_template_id = meta_id
    tpl.status = "PENDING"
    tpl.synced_at = datetime.now(timezone.utc)
    tpl.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(tpl)

    from observability.event_logger import log_event
    log_event(db, tenant_id, "ai_sales", "template.submitted",
              f"تم إرسال القالب '{tpl.name}' إلى Meta للمراجعة",
              payload={"template_id": tpl.id, "meta_template_id": meta_id})
    db.commit()

    return {"submitted": True, "template": _tpl_to_dict(tpl)}


@router.get("/templates/health")
async def get_template_health(request: Request, db: Session = Depends(get_db)):
    """Evaluate health scores for all tenant templates and return recommendations."""
    import sys as _sys
    _sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from template_ai.health_evaluator import evaluate_templates, health_summary

    tenant_id = resolve_tenant_id(request)
    templates = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id).all()

    if not templates:
        return {"total": 0, "healthy": 0, "needs_attention": 0, "avg_health_score": 0.0, "details": []}

    results = evaluate_templates(templates)

    tpl_map = {t.id: t for t in templates}
    for r in results:
        t = tpl_map.get(r["template_id"])
        if t:
            t.health_score = r["health_score"]
            if r["recommendation_state"] != "none":
                if getattr(t, "recommendation_state", None) not in ("accepted", "dismissed"):
                    t.recommendation_state = r["recommendation_state"]
                    t.recommendation_note = r["recommendation_note"]
    db.commit()

    return health_summary(templates)


@router.put("/templates/{template_id}/recommendation")
async def action_template_recommendation(
    template_id: int,
    body: RecommendationActionIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Merchant acts on a template health recommendation."""
    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if body.action not in ("accepted", "dismissed"):
        raise HTTPException(status_code=422, detail="action يجب أن يكون 'accepted' أو 'dismissed'")

    tpl.recommendation_state = body.action
    tpl.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(tpl)

    return {"updated": True, "template": _tpl_to_dict(tpl)}


@router.get("/templates/objectives")
async def list_template_objectives(request: Request):
    """Return the list of supported AI generation objectives."""
    import sys as _sys
    _sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from template_ai.generator import SUPPORTED_OBJECTIVES
    labels = {
        "abandoned_cart":       "استرداد سلة متروكة",
        "reorder":              "تذكير بإعادة الطلب",
        "winback":              "استعادة عميل غير نشط",
        "back_in_stock":        "إشعار توفر منتج",
        "price_drop":           "إشعار انخفاض السعر",
        "order_followup":       "متابعة طلب",
        "quote_followup":       "متابعة عرض سعر",
        "promotion":            "حملة ترويجية",
        "transactional_update": "تحديث معاملة",
    }
    return {
        "objectives": [
            {"value": obj, "label": labels.get(obj, obj)}
            for obj in SUPPORTED_OBJECTIVES
        ]
    }


@router.get("/templates/library")
async def list_template_library(request: Request):
    """Return the default template library metadata used by Nahla automations."""
    _ = request
    return {
        "templates": [
            {"template_name": template_name, **meta}
            for template_name, meta in DEFAULT_TEMPLATE_LIBRARY.items()
        ]
    }


@router.post("/templates/sync")
async def sync_templates_from_meta(request: Request, db: Session = Depends(get_db)):
    """
    Pull all templates from Meta Graph API and upsert them into the local DB.

    Source of truth for credentials
    ─────────────────────────────────
    Uses WhatsAppConnection (the record created during the number registration
    wizard) to get the real WABA ID and the platform-level WA_TOKEN.
    Falls back to tenant settings for backward compatibility.
    """
    from models import WhatsAppConnection  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)

    # ── Source of truth: WhatsAppConnection row for this tenant ───────────────
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter_by(tenant_id=tenant_id)
        .first()
    )

    sync_conn = wa_conn
    waba_id = (wa_conn.whatsapp_business_account_id if wa_conn else None) or ""

    if not waba_id:
        return {
            "synced":  0,
            "message": "لم يتم العثور على WABA ID. تأكد من ربط واتساب أولاً.",
        }

    try:
        live_payload, _token_ctx = await provider_list_templates(
            db,
            sync_conn,
            tenant_id=tenant_id,
            waba_id=waba_id,
            prefer_platform=bool(sync_conn and getattr(sync_conn, "connection_type", None) == WHATSAPP_CONNECTION_TYPE_DIRECT),
        )
    except Exception:
        return {
            "synced": 0,
            "message": "لم يتم العثور على توكن تشغيل صالح لمزامنة القوالب. أعد ربط واتساب أو تحقّق من إعدادات المنصة.",
        }

    live = _normalize_provider_template_list(sync_conn, live_payload or {})
    if live is None:
        return {"synced": 0, "message": "تعذّر الاتصال بـ Meta. تأكد من صحة بيانات الاعتماد."}

    # ── Delete seed/demo templates that were never real ───────────────────────
    deleted = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == tenant_id,
            WhatsAppTemplate.meta_template_id.like("seed_%"),
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        logger.info("[templates/sync] Removed %d seed templates for tenant=%s", deleted, tenant_id)

    # ── Upsert real templates from Meta ───────────────────────────────────────
    synced = 0
    for item in live:
        meta_id = str(item.get("id", ""))
        if not meta_id:
            continue
        existing = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.tenant_id        == tenant_id,
            WhatsAppTemplate.meta_template_id == meta_id,
        ).first()
        now = datetime.now(timezone.utc)
        raw_status = str(item.get("status") or "PENDING").upper()
        normalized_status = _normalize_template_status(raw_status)
        normalized_language = _normalize_template_language(item.get("language", "ar"))
        normalized_category = _normalize_template_category(item.get("category", "MARKETING"))
        rejection = item.get("rejected_reason") or None
        compatibility = _compute_template_compatibility(
            item.get("components", []),
            category=normalized_category,
            language=normalized_language,
            status=normalized_status,
            template_name=item.get("name", ""),
        )
        sync_metadata = {
            "meta_status_raw": raw_status,
            "meta_language_raw": item.get("language", "ar"),
            "meta_category_raw": item.get("category", "MARKETING"),
            "meta_quality_score": item.get("quality_score"),
            "meta_compatibility": compatibility,
        }
        if existing:
            existing.name             = item.get("name", existing.name)
            existing.language         = normalized_language
            existing.category         = normalized_category
            existing.status           = normalized_status
            existing.components       = item.get("components", existing.components)
            existing.rejection_reason = rejection
            existing.ai_generation_metadata = {
                **(existing.ai_generation_metadata or {}),
                **sync_metadata,
            }
            existing.synced_at        = now
            existing.updated_at       = now
        else:
            db.add(WhatsAppTemplate(
                tenant_id=tenant_id,
                meta_template_id=meta_id,
                name=item.get("name", ""),
                language=normalized_language,
                category=normalized_category,
                status=normalized_status,
                components=item.get("components", []),
                rejection_reason=rejection,
                ai_generation_metadata=sync_metadata,
                created_at=now,
                updated_at=now,
                synced_at=now,
            ))
        synced += 1

    db.commit()
    logger.info("[templates/sync] tenant=%s synced=%d from waba=%s", tenant_id, synced, waba_id)
    return {
        "synced":       synced,
        "deleted_seeds": deleted,
        "message": f"تمت مزامنة {synced} قالب من Meta" + (
            f" (وحُذف {deleted} قالب تجريبي)" if deleted else ""
        ),
    }


@router.get("/templates/{template_id}/var-map")
async def get_template_var_map(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Return the variable → field mapping for a template."""
    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    compatibility = _compute_template_compatibility(
        tpl.components or [],
        category=tpl.category,
        language=tpl.language,
        status=tpl.status,
        template_name=tpl.name,
    )
    raw_map = compatibility["var_map"]
    annotated = {
        var: {"field": field, "label": VAR_FIELD_LABELS.get(field, field)}
        for var, field in raw_map.items()
    }
    return {
        "template_id": template_id,
        "template_name": tpl.name,
        "category": tpl.category,
        "var_map": raw_map,
        "var_map_annotated": annotated,
        "is_default": tpl.name in TEMPLATE_VAR_MAP,
        "compatibility": compatibility,
    }


@router.post("/templates/{template_id}/resolve")
async def resolve_template_vars(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Resolve all template variables for a specific customer."""
    body = await request.json()
    tenant_id = resolve_tenant_id(request)
    customer_id = int(body.get("customer_id", 0))
    extra: Dict[str, str] = body.get("extra", {})

    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    customer = db.query(Customer).filter(
        Customer.id == customer_id,
        Customer.tenant_id == tenant_id,
    ).first()
    profile = None
    if customer:
        profile = db.query(CustomerProfile).filter(
            CustomerProfile.customer_id == customer.id,
            CustomerProfile.tenant_id == tenant_id,
        ).first()

    settings = get_or_create_settings(db, tenant_id)
    store = merge_defaults(settings.store_settings, DEFAULT_STORE)

    field_values: Dict[str, str] = {
        "customer_name": (customer.name if customer else "العميل") or "العميل",
        "store_name": store.get("store_name", "") or "المتجر",
        "status": (profile.customer_status if profile and getattr(profile, "customer_status", None) else profile.segment if profile else "lead"),
        "status_label": CUSTOMER_STATUS_LABELS.get((profile.customer_status if profile and getattr(profile, "customer_status", None) else profile.segment if profile else "lead"), "العميل"),
        "rfm_segment": (getattr(profile, "rfm_segment", None) if profile else None) or "lead",
        "discount_code": extra.get("discount_code", extra.get("coupon_code", "")),
        **extra,
    }

    compatibility = _compute_template_compatibility(
        tpl.components or [],
        category=tpl.category,
        language=tpl.language,
        status=tpl.status,
        template_name=tpl.name,
    )
    var_map = compatibility["var_map"]
    components = tpl.components or []
    resolved_components: List[Dict[str, Any]] = []

    for comp in components:
        comp_copy = dict(comp)
        if comp_copy.get("text"):
            text = comp_copy["text"]
            for var_placeholder, field in var_map.items():
                text = text.replace(var_placeholder, field_values.get(field, var_placeholder))
            comp_copy["text"] = text
        resolved_components.append(comp_copy)

    wa_params = []
    for var_placeholder, field in sorted(var_map.items(), key=lambda item: int(item[0].strip("{}"))):
        wa_params.append({
            "type": "text",
            "text": field_values.get(field, var_placeholder),
        })

    body_text = next(
        (c.get("text", "") for c in resolved_components if c.get("type") == "BODY"), ""
    )

    _tpl_bump_usage(db, tpl.id, tenant_id)
    db.commit()

    return {
        "template_name": tpl.name,
        "resolved_components": resolved_components,
        "rendered_body": body_text,
        "wa_parameters": wa_params,
        "library": DEFAULT_TEMPLATE_LIBRARY.get(tpl.name),
        "compatibility": compatibility,
    }


@router.get("/campaigns/templates")
async def get_campaign_templates(request: Request, db: Session = Depends(get_db)):
    """Return APPROVED templates from DB for campaign wizard."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    _seed_templates_if_empty(db, tenant_id)
    db.commit()

    approved = (
        db.query(WhatsAppTemplate)
        .filter(WhatsAppTemplate.tenant_id == tenant_id, WhatsAppTemplate.status == "APPROVED")
        .order_by(WhatsAppTemplate.created_at.desc())
        .all()
    )
    result = []
    for t in approved:
        result.append({
            "id": str(t.id),
            "name": t.name,
            "language": t.language,
            "category": t.category,
            "status": t.status,
            "components": t.components or [],
            "library": DEFAULT_TEMPLATE_LIBRARY.get(t.name),
        })
    return {"templates": result, "source": "db"}
