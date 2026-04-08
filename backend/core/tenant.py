"""
core/tenant.py
──────────────
Tenant resolution and settings helpers shared across all routers.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from models import Tenant, TenantSettings  # noqa: E402


# ── Default configuration values ──────────────────────────────────────────────
# These are merged over stored JSONB values so new keys always have a value.

DEFAULT_WHATSAPP: Dict[str, Any] = {
    "business_display_name":    "",
    "phone_number":             "",
    "phone_number_id":          "",
    "access_token":             "",
    "verify_token":             "",
    "webhook_url":              "https://api.nahlah.ai/webhook/whatsapp",
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


_tenant_logger = logging.getLogger("nahla.tenant")


def resolve_tenant_id(request: Request) -> int:
    """
    Resolve tenant_id for the current request.

    Priority (authoritative → fallback)
    ------------------------------------
    1. JWT payload claim   — set by jwt_enforcement_middleware; always present on
                             authenticated routes after the middleware fix.
    2. request.state       — set by multi_tenant_middleware from X-Tenant-ID header
                             (dev/testing only).
    3. Hard fallback = 1   — NEVER acceptable in production; logged as CRITICAL.

    The hard fallback exists only to prevent a 500 crash in edge cases.
    Every call that hits the fallback indicates a middleware or auth bug.
    """
    # Path 1 — JWT claim (expected path for all authenticated requests)
    jwt_payload = getattr(request.state, "jwt_payload", None)
    if jwt_payload:
        tid = jwt_payload.get("tenant_id")
        if tid is not None:
            return int(tid)
        # JWT present but missing tenant_id — should have been caught by middleware
        _tenant_logger.critical(
            "[tenant] JWT has no tenant_id! path=%s sub=%s role=%s — using 1 (DATA ISOLATION AT RISK)",
            request.url.path, jwt_payload.get("sub"), jwt_payload.get("role"),
        )
        return 1

    # Path 2 — header/state (dev testing only)
    try:
        tid = int(request.state.tenant_id)
        if tid > 0:
            _tenant_logger.warning(
                "[tenant] resolve_tenant_id used X-Tenant-ID header fallback — path=%s tid=%s "
                "(only acceptable in dev; JWT middleware should set this in production)",
                request.url.path, tid,
            )
            return tid
    except (ValueError, AttributeError):
        pass

    # Path 3 — hard fallback (SHOULD NEVER REACH HERE)
    _tenant_logger.critical(
        "[tenant] resolve_tenant_id fell back to 1! path=%s — THIS IS A BUG. "
        "Data isolation is compromised. Check JWT middleware.",
        request.url.path,
    )
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
            created_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(settings)
        db.flush()
    return settings
