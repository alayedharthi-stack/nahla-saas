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

from models import Customer, TenantSettings, WhatsAppTemplate  # noqa: E402

from core.database import get_db
from core.tenant import (
    DEFAULT_STORE,
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    get_or_create_tenant,
    merge_defaults,
    resolve_tenant_id,
)
from services.whatsapp_platform.token_manager import get_token_for_operation

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


# ── Seed data ─────────────────────────────────────────────────────────────────

SEED_TEMPLATES: List[Dict[str, Any]] = [
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
    "discount_pct":   "نسبة الخصم",
    "coupon_code":    "كود الكوبون",
    "store_name":     "اسم المتجر",
    "order_id":       "رقم الطلب",
}

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


class UpdateTemplateStatusIn(BaseModel):
    status: str
    rejection_reason: Optional[str] = None
    meta_template_id: Optional[str] = None


class GenerateTemplateIn(BaseModel):
    objective: str
    language: str = "ar"


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
    for seed in SEED_TEMPLATES:
        tpl = WhatsAppTemplate(
            tenant_id=tenant_id,
            meta_template_id=f"seed_{seed['name']}",
            name=seed["name"],
            language=seed["language"],
            category=seed["category"],
            status=seed["status"],
            components=seed["components"],
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


def _tpl_to_dict(t: WhatsAppTemplate) -> Dict[str, Any]:
    meta = dict(getattr(t, "ai_generation_metadata", None) or {})
    compatibility = meta.get("meta_compatibility") or _compute_template_compatibility(
        t.components or [],
        category=t.category,
        language=t.language,
        status=t.status,
        template_name=t.name,
    )
    return {
        "id": t.id,
        "meta_template_id": t.meta_template_id,
        "name": t.name,
        "language": t.language,
        "category": t.category,
        "status": t.status,
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


def _extract_placeholders_from_text(text: str) -> List[str]:
    import re
    return sorted(set(re.findall(r"\{\{\d+\}\}", text or "")), key=lambda item: int(item.strip("{}")))


def _extract_template_placeholders(components: List[Dict[str, Any]]) -> List[str]:
    placeholders: set[str] = set()
    for comp in components or []:
        text = str(comp.get("text") or "")
        placeholders.update(_extract_placeholders_from_text(text))
        for btn in comp.get("buttons") or []:
            placeholders.update(_extract_placeholders_from_text(str(btn.get("text") or "")))
            placeholders.update(_extract_placeholders_from_text(str(btn.get("url") or "")))
    return sorted(placeholders, key=lambda item: int(item.strip("{}")))


def _guess_field_candidates(placeholder: str, *, category: str, template_name: str) -> List[str]:
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
    """Submit a new template to Meta Graph API using the operational token manager."""
    try:
        import json as _json
        import urllib.request
        token_ctx = await get_token_for_operation(
            db,
            conn,
            tenant_id=tenant_id,
            operation="template_submit",
            prefer_platform=bool(conn and getattr(conn, "connection_type", None) == "direct"),
        )
        url = f"https://graph.facebook.com/v20.0/{waba_id}/message_templates"
        payload = _json.dumps({
            "name": name,
            "language": language,
            "category": category,
            "components": components,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token_ctx.token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            return str(data.get("id", ""))
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
    """Create a new template locally and submit it to Meta for approval."""
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
    meta_id = None
    if waba_id:
        meta_id = await _submit_template_to_meta(
            db=db,
            conn=wa_conn,
            tenant_id=tenant_id,
            waba_id=waba_id,
            name=body.name,
            language=body.language,
            category=body.category,
            components=[c.model_dump(exclude_none=True) for c in body.components],
        )

    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        meta_template_id=meta_id,
        name=body.name,
        language=body.language,
        category=body.category,
        status="PENDING",
        components=[c.model_dump(exclude_none=True) for c in body.components],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(tpl)
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
        token_ctx = await get_token_for_operation(
            db,
            sync_conn,
            tenant_id=tenant_id,
            operation="template_sync",
            prefer_platform=bool(sync_conn and getattr(sync_conn, "connection_type", None) == "direct"),
        )
    except Exception:
        return {
            "synced": 0,
            "message": "لم يتم العثور على توكن تشغيل صالح لمزامنة القوالب. أعد ربط واتساب أو تحقّق من إعدادات المنصة.",
        }

    live = _fetch_meta_templates(waba_id, token_ctx.token)
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

    settings = get_or_create_settings(db, tenant_id)
    store = merge_defaults(settings.store_settings, DEFAULT_STORE)

    field_values: Dict[str, str] = {
        "customer_name": (customer.name if customer else "العميل") or "العميل",
        "store_name": store.get("store_name", "") or "المتجر",
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
        })
    return {"templates": result, "source": "db"}
