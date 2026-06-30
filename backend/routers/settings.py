"""
routers/settings.py
────────────────────
GET  /settings              — return all settings for current tenant
PUT  /settings              — partial-update settings groups
POST /settings/test-whatsapp — test WhatsApp connection
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import require_not_support_impersonation
from core.database import get_db
from core.secrets import apply_masks, restore_secrets
from core.tenant import (
    DEFAULT_AI,
    DEFAULT_NOTIFICATIONS,
    DEFAULT_STORE,
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    merge_defaults,
    merge_ai_defaults,
    resolve_tenant_id,
)
from services.whatsapp_platform.token_manager import get_token_context

router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class WhatsAppSettingsIn(BaseModel):
    business_display_name: str = ""
    phone_number: str = ""
    phone_number_id: str = ""
    access_token: str = ""
    verify_token: str = ""
    webhook_url: str = ""
    store_button_label: str = "زيارة المتجر"
    store_button_url: str = ""
    owner_contact_label: str = "تواصل مع المالك"
    owner_whatsapp_number: str = ""
    auto_reply_enabled: bool = True
    transfer_to_owner_enabled: bool = True


class AISettingsIn(BaseModel):
    store_ai_enabled: Optional[bool] = None
    assistant_name: str = "نحلة"
    assistant_role: str = ""
    reply_tone: str = "friendly"
    reply_length: str = "medium"
    default_language: str = "arabic"
    owner_instructions: str = ""
    coupon_rules: str = ""
    escalation_rules: str = ""
    allowed_discount_levels: str = "10"
    recommendations_enabled: bool = True
    # ── Knowledge Base ────────────────────────────────────────────────────
    # Free-form text the merchant maintains on the dedicated "قاعدة المعرفة"
    # page (separate from owner_instructions which controls *behaviour*).
    # Stored verbatim and injected into the prompt overlay at runtime.
    manual_knowledge_base: str = ""


class StoreSettingsIn(BaseModel):
    store_name: str = ""
    store_logo_url: str = ""
    store_url: str = ""
    platform_type: str = "salla"
    salla_client_id: str = ""
    salla_client_secret: str = ""
    salla_access_token: str = ""
    zid_client_id: str = ""
    zid_client_secret: str = ""
    shopify_shop_domain: str = ""
    shopify_access_token: str = ""
    shipping_provider: str = ""
    google_maps_location: str = ""
    sales_channels: Optional[Dict[str, Any]] = None
    instagram_url: str = ""
    twitter_url: str = ""
    snapchat_url: str = ""
    tiktok_url: str = ""


class NotificationSettingsIn(BaseModel):
    whatsapp_alerts: bool = True
    email_alerts: bool = True
    system_alerts: bool = True
    failed_webhook_alerts: bool = True
    low_balance_alerts: bool = True


class PaymentMethodsSettingsIn(BaseModel):
    bank_transfer_enabled: Optional[bool] = None
    cash_on_delivery_enabled: Optional[bool] = None
    moyasar_enabled: Optional[bool] = None
    manual_payment_enabled: Optional[bool] = None


class AllSettingsIn(BaseModel):
    whatsapp: Optional[WhatsAppSettingsIn] = None
    ai: Optional[AISettingsIn] = None
    store: Optional[StoreSettingsIn] = None
    notifications: Optional[NotificationSettingsIn] = None
    payment_methods: Optional[PaymentMethodsSettingsIn] = None


class WidgetSettingsIn(BaseModel):
    enabled: bool = False
    phone: str = ""
    message: str = "السلام عليكم، أبغى الاستفسار"
    logo_url: str = ""
    position: str = "left"          # "left" | "right"
    scroll_threshold: int = 250


class StoreAISettingsPatch(BaseModel):
    store_ai_enabled: bool


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Return all settings for the current tenant."""
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()

    wa    = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    store = merge_defaults(settings.store_settings,    DEFAULT_STORE)
    from core.merchant_payment_methods import load_merchant_payment_methods  # noqa: PLC0415

    return {
        "whatsapp":      apply_masks(wa,    "whatsapp"),
        "ai":            merge_ai_defaults(settings.ai_settings),
        "store":         apply_masks(store, "store"),
        "notifications": merge_defaults(settings.notification_settings, DEFAULT_NOTIFICATIONS),
        "payment_methods": load_merchant_payment_methods(db, tenant_id).to_dict(),
    }


@router.put("/settings")
async def update_settings(
    body: AllSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    """Update settings for the current tenant (partial update — only provided groups saved)."""
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)

    if body.whatsapp is not None:
        current  = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
        incoming = restore_secrets(body.whatsapp.model_dump(), current, "whatsapp")
        current.update(incoming)
        settings.whatsapp_settings = current

    if body.ai is not None:
        current = merge_ai_defaults(settings.ai_settings)
        current.update(body.ai.model_dump())
        settings.ai_settings = current

    if body.store is not None:
        current  = merge_defaults(settings.store_settings, DEFAULT_STORE)
        incoming = restore_secrets(body.store.model_dump(), current, "store")
        current.update(incoming)
        settings.store_settings = current

    if body.notifications is not None:
        current = merge_defaults(settings.notification_settings, DEFAULT_NOTIFICATIONS)
        current.update(body.notifications.model_dump())
        settings.notification_settings = current

    if body.payment_methods is not None:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        meta = dict(settings.extra_metadata or {})
        pm = dict(meta.get("payment_methods") or {})
        for key, val in body.payment_methods.model_dump(exclude_none=True).items():
            pm[key] = val
        meta["payment_methods"] = pm
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")

    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)

    wa_saved    = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    store_saved = merge_defaults(settings.store_settings,    DEFAULT_STORE)
    from core.merchant_payment_methods import load_merchant_payment_methods  # noqa: PLC0415

    return {
        "whatsapp":      apply_masks(wa_saved,    "whatsapp"),
        "ai":            merge_ai_defaults(settings.ai_settings),
        "store":         apply_masks(store_saved, "store"),
        "notifications": merge_defaults(settings.notification_settings, DEFAULT_NOTIFICATIONS),
        "payment_methods": load_merchant_payment_methods(db, tenant_id).to_dict(),
    }


@router.patch("/settings/ai")
async def patch_store_ai_settings(
    body: StoreAISettingsPatch,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    """Toggle store-wide AI replies without touching per-conversation pause state."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)

    ai_settings = dict(settings.ai_settings or {})
    ai_settings["store_ai_enabled"] = bool(body.store_ai_enabled)
    settings.ai_settings = ai_settings
    flag_modified(settings, "ai_settings")
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)

    ai = merge_ai_defaults(settings.ai_settings)
    return {
        "ok": True,
        "store_ai_enabled": bool(ai.get("store_ai_enabled", True)),
        "ai": ai,
    }


@router.get("/settings/widget")
async def get_widget_settings(request: Request, db: Session = Depends(get_db)):
    """Return WhatsApp widget embed settings for the current tenant."""
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()
    ws = dict((settings.extra_metadata or {}).get("widget_settings", {}))
    return {
        "enabled":          ws.get("enabled",          False),
        "phone":            ws.get("phone",            ""),
        "message":          ws.get("message",          "السلام عليكم، أبغى الاستفسار"),
        "logo_url":         ws.get("logo_url",         ""),
        "position":         ws.get("position",         "left"),
        "scroll_threshold": ws.get("scroll_threshold", 250),
    }


@router.put("/settings/widget")
async def update_widget_settings(
    body: WidgetSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save WhatsApp widget embed settings for the current tenant."""
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    meta = dict(settings.extra_metadata or {})
    meta["widget_settings"] = body.model_dump()
    settings.extra_metadata = meta
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    return meta["widget_settings"]


@router.post("/settings/test-whatsapp")
async def test_whatsapp_connection(request: Request, db: Session = Depends(get_db)):
    """Simulate a WhatsApp API connection test."""
    from models import WhatsAppConnection  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()

    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    token_ctx = get_token_context(conn)
    if not ((conn.phone_number_id if conn else None) or wa.get("phone_number_id")) or not token_ctx.token:
        return {"success": False, "message": "Phone Number ID و Access Token مطلوبان لاختبار الاتصال"}

    return {
        "success": True,
        "message": "تم الاتصال بنجاح بـ WhatsApp Business API",
        "token_status": token_ctx.token_status,
    }
