"""
routers/addons.py
─────────────────────────────────────────────────────────────────────
Merchant Addons — pluggable feature modules for merchant stores.

Endpoints:
  GET  /merchant/addons                     — list all addons for tenant
  POST /merchant/addons/{key}/toggle        — enable / disable an addon
  PUT  /merchant/addons/{key}/settings      — update addon settings
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import resolve_tenant_id

logger = logging.getLogger("nahla.addons")

router = APIRouter()

# ── Addon Registry ────────────────────────────────────────────────────────────
# Adding a new addon = add one entry here.  Frontend picks it up automatically.

ADDON_REGISTRY: Dict[str, Dict[str, Any]] = {
    "widget": {
        "name_ar":        "ويدجت نحلة",
        "description_ar": "زر واتساب عائم في متجرك يسهّل تواصل الزوار مع نحلة",
        "badge":          "free",        # free | paid | coming_soon
        "has_settings":   True,
        "default_settings": {
            "phone":            "",
            "message":          "السلام عليكم، أبغى الاستفسار",
            "logo_url":         "",
            "position":         "left",
            "scroll_threshold": 250,
        },
    },
    "discount_popup": {
        "name_ar":        "نافذة خصم",
        "description_ar": "نافذة تظهر للزائر بعرض خاص لتحفيزه على الشراء",
        "badge":          "free",
        "has_settings":   True,
        "default_settings": {
            "title":          "عرض خاص لك!",
            "body_text":      "احصل على خصم حصري على طلبك الآن",
            "discount_type":  "percentage",   # percentage | fixed
            "discount_value": 10,
            "delay_seconds":  5,
            "show_once":      True,
        },
    },
    "first_order_coupon": {
        "name_ar":        "كوبون أول طلب",
        "description_ar": "كوبون خصم تلقائي يُقدَّم للعملاء الجدد عند أول طلب",
        "badge":          "free",
        "has_settings":   True,
        "default_settings": {
            "coupon_code":       "",
            "discount_type":     "percentage",
            "discount_value":    10,
            "min_order_value":   0,
            "validity_days":     30,
            "new_customers_only": True,
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_addon(db: Session, tenant_id: int, addon_key: str):
    """Return existing MerchantAddon row, creating a default if missing."""
    from models import MerchantAddon  # noqa: PLC0415
    row = (
        db.query(MerchantAddon)
        .filter(MerchantAddon.tenant_id == tenant_id, MerchantAddon.addon_key == addon_key)
        .first()
    )
    if row is None:
        meta = ADDON_REGISTRY.get(addon_key, {})
        row = MerchantAddon(
            tenant_id=tenant_id,
            addon_key=addon_key,
            is_enabled=False,
            settings_json=dict(meta.get("default_settings", {})),
        )
        db.add(row)
        db.flush()
    return row


def _serialize_addon(row, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a MerchantAddon row + registry meta into an API response dict."""
    settings = dict(row.settings_json or {})
    defaults = dict(meta.get("default_settings", {}))
    merged   = {**defaults, **settings}   # settings override defaults
    return {
        "key":          row.addon_key,
        "name":         meta.get("name_ar", row.addon_key),
        "description":  meta.get("description_ar", ""),
        "badge":        meta.get("badge", "free"),
        "has_settings": meta.get("has_settings", False),
        "is_enabled":   row.is_enabled,
        "settings":     merged,
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class ToggleBody(BaseModel):
    enabled: bool


class AddonSettingsBody(BaseModel):
    settings: Dict[str, Any]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/merchant/addons")
async def list_addons(request: Request, db: Session = Depends(get_db)):
    """Return all known addons enriched with this tenant's state."""
    tenant_id = resolve_tenant_id(request)
    from models import MerchantAddon  # noqa: PLC0415

    # Load all existing rows for this tenant in one query
    rows = {
        r.addon_key: r
        for r in db.query(MerchantAddon)
                   .filter(MerchantAddon.tenant_id == tenant_id)
                   .all()
    }

    result = []
    for key, meta in ADDON_REGISTRY.items():
        if key not in rows:
            # Create default row lazily
            rows[key] = _get_or_create_addon(db, tenant_id, key)

        result.append(_serialize_addon(rows[key], meta))

    db.commit()
    return {"addons": result}


@router.post("/merchant/addons/{addon_key}/toggle")
async def toggle_addon(
    addon_key: str,
    body: ToggleBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable or disable an addon for the current tenant."""
    if addon_key not in ADDON_REGISTRY:
        raise HTTPException(status_code=404, detail=f"addon '{addon_key}' not found")

    tenant_id = resolve_tenant_id(request)
    row = _get_or_create_addon(db, tenant_id, addon_key)
    row.is_enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        "[addons/toggle] tenant=%s addon=%s enabled=%s",
        tenant_id, addon_key, body.enabled,
    )
    return _serialize_addon(row, ADDON_REGISTRY[addon_key])


@router.put("/merchant/addons/{addon_key}/settings")
async def update_addon_settings(
    addon_key: str,
    body: AddonSettingsBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update settings for a specific addon."""
    if addon_key not in ADDON_REGISTRY:
        raise HTTPException(status_code=404, detail=f"addon '{addon_key}' not found")

    tenant_id = resolve_tenant_id(request)
    row = _get_or_create_addon(db, tenant_id, addon_key)

    # Merge incoming settings over existing (partial update)
    current = dict(row.settings_json or {})
    current.update(body.settings)
    row.settings_json = current
    row.updated_at    = datetime.now(timezone.utc)
    db.commit()

    logger.info("[addons/settings] tenant=%s addon=%s", tenant_id, addon_key)
    return _serialize_addon(row, ADDON_REGISTRY[addon_key])
