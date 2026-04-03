"""
routers/store_integration.py
─────────────────────────────
Store integration settings and connectivity endpoints.

Routes
  GET    /store-integration/settings
  PUT    /store-integration/settings
  DELETE /store-integration/settings
  GET    /store-integration/test
"""
from __future__ import annotations

import logging
import os
import sys

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
from models import Integration  # noqa: E402

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(prefix="/store-integration", tags=["Store Integration"])


class StoreIntegrationSettingsIn(BaseModel):
    platform:       str  = "salla"
    api_key:        str
    store_id:       str  = ""
    webhook_secret: str  = ""
    enabled:        bool = True


@router.get("/settings")
async def get_store_integration_settings(request: Request, db: Session = Depends(get_db)):
    """Return store integration config for this tenant (api_key masked)."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider.in_(["salla"]),
    ).first()
    if not integration:
        return {"configured": False, "platform": None, "store_id": "", "enabled": False}
    cfg = integration.config or {}
    return {
        "configured": True,
        "platform":   integration.provider,
        "store_id":   cfg.get("store_id", ""),
        "api_key_hint": ("***" + cfg.get("api_key", "")[-4:]) if cfg.get("api_key") else "",
        "enabled":    integration.enabled,
    }


@router.put("/settings")
async def put_store_integration_settings(
    body: StoreIntegrationSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save or update store integration credentials."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider == body.platform,
    ).first()
    new_config = {
        "api_key":       body.api_key,
        "store_id":      body.store_id,
        "webhook_secret": body.webhook_secret,
    }
    if integration:
        integration.config  = new_config
        integration.enabled = body.enabled
    else:
        integration = Integration(
            tenant_id=tenant_id,
            provider=body.platform,
            config=new_config,
            enabled=body.enabled,
        )
        db.add(integration)
    db.commit()
    return {"status": "saved", "platform": body.platform, "enabled": body.enabled}


@router.delete("/settings")
async def delete_store_integration_settings(request: Request, db: Session = Depends(get_db)):
    """Disable store integration for this tenant."""
    tenant_id = resolve_tenant_id(request)
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
    ).first()
    if integration:
        integration.enabled = False
        db.commit()
    return {"status": "disabled"}


@router.get("/test")
async def test_store_integration(request: Request):
    """Test connectivity to the configured store adapter."""
    tenant_id = resolve_tenant_id(request)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from store_integration.registry import get_adapter  # noqa: PLC0415
    adapter = get_adapter(tenant_id)
    if not adapter:
        return {"status": "not_configured", "message": "No store integration configured"}
    try:
        products = await adapter.get_products()
        return {
            "status":         "ok",
            "platform":       adapter.platform,
            "products_found": len(products),
            "sample":         products[0].dict() if products else None,
        }
    except Exception as exc:
        return {"status": "error", "platform": adapter.platform, "error": str(exc)}
