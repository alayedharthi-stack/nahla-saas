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

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

router = APIRouter()


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
    """Seed demo templates into the DB if this tenant has none."""
    count = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id).count()
    if count == 0:
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
    """Try to fetch templates from Meta Graph API. Returns None on failure."""
    try:
        import json as _json
        import urllib.request
        url = (
            f"https://graph.facebook.com/v18.0/{waba_id}/message_templates"
            f"?access_token={access_token}&limit=50&status=APPROVED"
        )
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json.loads(resp.read())
            return data.get("data", [])
    except Exception:
        return None


def _tpl_to_dict(t: WhatsAppTemplate) -> Dict[str, Any]:
    return {
        "id": t.id,
        "meta_template_id": t.meta_template_id,
        "name": t.name,
        "language": t.language,
        "category": t.category,
        "status": t.status,
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


def _submit_template_to_meta(waba_id: str, token: str, body: "CreateTemplateIn") -> Optional[str]:
    """Submit a new template to Meta Graph API. Returns the Meta template ID or None."""
    try:
        import json as _json
        import urllib.parse
        import urllib.request
        url = f"https://graph.facebook.com/v18.0/{waba_id}/message_templates"
        payload = _json.dumps({
            "name": body.name,
            "language": body.language,
            "category": body.category,
            "components": [c.model_dump(exclude_none=True) for c in body.components],
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
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
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
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
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    settings = get_or_create_settings(db, tenant_id)
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    waba_id = wa.get("phone_number_id", "")
    token = wa.get("access_token", "")

    meta_id = None
    if waba_id and token:
        meta_id = _submit_template_to_meta(waba_id, token, body)

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
        waba_id = wa.get("phone_number_id", "")
        token = wa.get("access_token", "")
        if waba_id and token:
            meta_id = await _submit_template_to_meta(tpl.name, tpl.language, tpl.category, tpl.components or [], waba_id, token)
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
    waba_id = wa.get("phone_number_id", "")
    token = wa.get("access_token", "")

    if not waba_id or not token:
        raise HTTPException(
            status_code=422,
            detail="بيانات WhatsApp Business غير مُعدَّة. أضف Phone Number ID و Access Token في الإعدادات.",
        )

    meta_id = await _submit_template_to_meta(tpl.name, tpl.language, tpl.category, tpl.components or [], waba_id, token)
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
    """Pull all templates from Meta Graph API and upsert them into the local DB."""
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()

    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    waba_id = wa.get("phone_number_id", "")
    token = wa.get("access_token", "")

    if not waba_id or not token:
        return {"synced": 0, "message": "يجب إدخال Phone Number ID و Access Token في الإعدادات أولاً"}

    live = _fetch_meta_templates(waba_id, token)
    if live is None:
        return {"synced": 0, "message": "تعذّر الاتصال بـ Meta. تأكد من صحة بيانات الاعتماد."}

    synced = 0
    for item in live:
        meta_id = str(item.get("id", ""))
        existing = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.tenant_id == tenant_id,
            WhatsAppTemplate.meta_template_id == meta_id,
        ).first()
        if existing:
            existing.status = item.get("status", existing.status)
            existing.components = item.get("components", existing.components)
            existing.synced_at = datetime.now(timezone.utc)
            existing.updated_at = datetime.now(timezone.utc)
        else:
            tpl = WhatsAppTemplate(
                tenant_id=tenant_id,
                meta_template_id=meta_id,
                name=item.get("name", ""),
                language=item.get("language", "ar"),
                category=item.get("category", "MARKETING"),
                status=item.get("status", "PENDING"),
                components=item.get("components", []),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                synced_at=datetime.now(timezone.utc),
            )
            db.add(tpl)
        synced += 1

    db.commit()
    return {"synced": synced, "message": f"تمت مزامنة {synced} قالب من Meta"}


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

    raw_map = TEMPLATE_VAR_MAP.get(tpl.name, {})
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
        "is_default": tpl.name in ("cod_order_confirmation_ar", "predictive_reorder_reminder_ar"),
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

    var_map = TEMPLATE_VAR_MAP.get(tpl.name, {})
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
    for var_placeholder, field in sorted(var_map.items()):
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
