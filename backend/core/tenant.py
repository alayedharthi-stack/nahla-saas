"""
core/tenant.py
──────────────
Tenant resolution and settings helpers shared across all routers.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import Request
from sqlalchemy.orm import Session

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
from models import Tenant, TenantSettings  # noqa: E402


# ── Default configuration values ──────────────────────────────────────────────
# These are merged over stored JSONB values so new keys always have a value.

DEFAULT_WHATSAPP: Dict[str, Any] = {
    "business_display_name":    "",
    "phone_number":             "",
    "phone_number_id":          "",
    "access_token":             "",
    "verify_token":             "",
    "webhook_url":              "https://app.nahla.ai/webhook/whatsapp",
    "store_button_label":       "زيارة المتجر",
    "store_button_url":         "",
    "owner_contact_label":      "تواصل مع المالك",
    "owner_whatsapp_number":    "",
    "auto_reply_enabled":       True,
    "transfer_to_owner_enabled": True,
    # draft_approval — save as DRAFT, merchant reviews before Meta submission
    # auto_submit    — generate and submit to Meta immediately
    "template_submission_mode": "draft_approval",
}

DEFAULT_AI: Dict[str, Any] = {
    "assistant_name":           "نحلة",
    "assistant_role":           "مساعدة ذكية لخدمة عملاء المتجر",
    "reply_tone":               "friendly",
    "reply_length":             "medium",
    "default_language":         "arabic",
    "owner_instructions":       "",
    "coupon_rules":             "",
    "escalation_rules":         "",
    "allowed_discount_levels":  "10",
    "recommendations_enabled":  True,
}

DEFAULT_STORE: Dict[str, Any] = {
    "store_name":           "",
    "store_logo_url":       "",
    "store_url":            "",
    "platform_type":        "salla",
    "salla_client_id":      "",
    "salla_client_secret":  "",
    "salla_access_token":   "",
    "zid_client_id":        "",
    "zid_client_secret":    "",
    "shopify_shop_domain":  "",
    "shopify_access_token": "",
    "shipping_provider":    "",
    "google_maps_location": "",
    "instagram_url":        "",
    "twitter_url":          "",
    "snapchat_url":         "",
    "tiktok_url":           "",
}

DEFAULT_NOTIFICATIONS: Dict[str, Any] = {
    "whatsapp_alerts":       True,
    "email_alerts":          True,
    "system_alerts":         True,
    "failed_webhook_alerts": True,
    "low_balance_alerts":    True,
}


def merge_defaults(stored: Optional[Dict], defaults: Dict) -> Dict:
    """Merge stored values over defaults so new keys always have a fallback."""
    result = dict(defaults)
    if stored:
        result.update(stored)
    return result


def resolve_tenant_id(request: Request) -> int:
    """
    Resolve tenant_id for the current request.
    Priority: JWT payload (authoritative) → X-Tenant-ID header (dev) → 1.
    """
    jwt_payload = getattr(request.state, "jwt_payload", None)
    if jwt_payload:
        return int(jwt_payload.get("tenant_id", 1))
    try:
        return int(request.state.tenant_id)
    except (ValueError, AttributeError):
        return 1


def get_or_create_tenant(db: Session, tenant_id: int) -> Tenant:
    """Fetch existing tenant or create a default placeholder (dev only)."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        tenant = Tenant(
            id=tenant_id,
            name=f"متجر رقم {tenant_id}",
            domain=f"store-{tenant_id}.nahla.sa",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db.add(tenant)
        db.flush()
    return tenant


def get_or_create_settings(db: Session, tenant_id: int) -> TenantSettings:
    """Fetch existing TenantSettings or create with defaults."""
    get_or_create_tenant(db, tenant_id)
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    if not settings:
        settings = TenantSettings(
            tenant_id=tenant_id,
            show_nahla_branding=True,
            branding_text="🐝 Powered by Nahla",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(settings)
        db.flush()
    return settings
