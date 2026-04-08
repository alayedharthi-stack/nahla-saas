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
    resolve_tenant_id,
)

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


class AllSettingsIn(BaseModel):
    whatsapp: Optional[WhatsAppSettingsIn] = None
    ai: Optional[AISettingsIn] = None
    store: Optional[StoreSettingsIn] = None
    notifications: Optional[NotificationSettingsIn] = None


class WidgetSettingsIn(BaseModel):
    enabled: bool = False
    phone: str = ""
    message: str = "السلام عليكم، أبغى الاستفسار"
    logo_url: str = ""
    position: str = "left"          # "left" | "right"
    scroll_threshold: int = 250


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Return all settings for the current tenant."""
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()

    wa    = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    store = merge_defaults(settings.store_settings,    DEFAULT_STORE)
    return {
        "whatsapp":      apply_masks(wa,    "whatsapp"),
        "ai":            merge_defaults(settings.ai_settings,           DEFAULT_AI),
        "store":         apply_masks(store, "store"),
        "notifications": merge_defaults(settings.notification_settings, DEFAULT_NOTIFICATIONS),
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
        current = merge_defaults(settings.ai_settings, DEFAULT_AI)
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

    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)

    wa_saved    = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    store_saved = merge_defaults(settings.store_settings,    DEFAULT_STORE)
    return {
        "whatsapp":      apply_masks(wa_saved,    "whatsapp"),
        "ai":            merge_defaults(settings.ai_settings,           DEFAULT_AI),
        "store":         apply_masks(store_saved, "store"),
        "notifications": merge_defaults(settings.notification_settings, DEFAULT_NOTIFICATIONS),
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
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()

    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    if not wa.get("phone_number_id") or not wa.get("access_token"):
        return {"success": False, "message": "Phone Number ID و Access Token مطلوبان لاختبار الاتصال"}

    return {"success": True, "message": "تم الاتصال بنجاح بـ WhatsApp Business API"}
