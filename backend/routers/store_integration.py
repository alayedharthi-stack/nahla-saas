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


@router.get("/debug")
async def debug_salla_integration(request: Request, db: Session = Depends(get_db)):
    """Owner-only diagnostic: shows integration health for current tenant."""
    tenant_id = resolve_tenant_id(request)

    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider == "salla",
    ).first()

    if not integration:
        return {
            "tenant_id": tenant_id,
            "integration_found": False,
            "message": "لا يوجد ربط سلة لهذا التاجر.",
        }

    cfg = integration.config or {}
    has_token = bool(cfg.get("api_key"))
    has_refresh = bool(cfg.get("refresh_token"))
    store_id = cfg.get("store_id", "")

    from models import Product, Order, Coupon  # noqa: PLC0415
    local_products = db.query(Product).filter_by(tenant_id=tenant_id).count()
    local_orders = db.query(Order).filter_by(tenant_id=tenant_id).count()

    from models import StoreSyncJob  # noqa: PLC0415
    last_job = (
        db.query(StoreSyncJob)
        .filter_by(tenant_id=tenant_id)
        .order_by(StoreSyncJob.id.desc())
        .first()
    )

    salla_products_count = None
    salla_orders_count = None
    salla_api_error = None
    if has_token:
        try:
            import httpx  # noqa: PLC0415
            async with httpx.AsyncClient(timeout=10) as client:
                headers = {"Authorization": f"Bearer {cfg['api_key']}", "Accept": "application/json"}
                p_resp = await client.get(
                    "https://api.salla.dev/admin/v2/products",
                    headers=headers, params={"per_page": 1},
                )
                if p_resp.status_code == 200:
                    p_data = p_resp.json()
                    salla_products_count = (p_data.get("pagination") or p_data.get("meta") or {}).get("total", len(p_data.get("data", [])))
                else:
                    salla_api_error = f"products → {p_resp.status_code}: {p_resp.text[:200]}"

                o_resp = await client.get(
                    "https://api.salla.dev/admin/v2/orders",
                    headers=headers, params={"per_page": 1},
                )
                if o_resp.status_code == 200:
                    o_data = o_resp.json()
                    salla_orders_count = (o_data.get("pagination") or o_data.get("meta") or {}).get("total", len(o_data.get("data", [])))
                elif not salla_api_error:
                    salla_api_error = f"orders → {o_resp.status_code}: {o_resp.text[:200]}"
        except Exception as exc:
            salla_api_error = str(exc)

    return {
        "tenant_id":            tenant_id,
        "integration_found":    True,
        "integration_id":       integration.id,
        "enabled":              integration.enabled,
        "store_id":             store_id,
        "has_access_token":     has_token,
        "has_refresh_token":    has_refresh,
        "token_hint":           (cfg["api_key"][:8] + "...") if has_token else None,
        "connected_at":         cfg.get("connected_at"),
        "app_type":             cfg.get("app_type", "production"),
        "last_sync_job":        {
            "id": last_job.id,
            "status": last_job.status,
            "sync_type": last_job.sync_type,
            "started_at": last_job.started_at.isoformat() if last_job.started_at else None,
            "finished_at": last_job.finished_at.isoformat() if last_job.finished_at else None,
            "products_synced": last_job.products_synced,
            "orders_synced": last_job.orders_synced,
        } if last_job else None,
        "local_products_count": local_products,
        "local_orders_count":   local_orders,
        "salla_api_products":   salla_products_count,
        "salla_api_orders":     salla_orders_count,
        "salla_api_error":      salla_api_error,
    }


@router.post("/copy-from-tenant")
async def copy_integration_from_tenant(
    request: Request,
    db: Session = Depends(get_db),
):
    """Copy Salla integration (tokens) from another tenant to the current one."""
    tenant_id = resolve_tenant_id(request)
    body = await request.json()
    source_tenant = int(body.get("source_tenant", 1))

    source = db.query(Integration).filter(
        Integration.tenant_id == source_tenant,
        Integration.provider == "salla",
    ).first()
    if not source or not source.config:
        return {"status": "error", "message": f"No salla integration found for tenant {source_tenant}"}

    get_or_create_tenant(db, tenant_id)
    target = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider == "salla",
    ).first()

    if target:
        target.config = dict(source.config)
        target.enabled = True
    else:
        target = Integration(
            tenant_id=tenant_id,
            provider="salla",
            config=dict(source.config),
            enabled=True,
        )
        db.add(target)

    db.commit()
    logger.info("Copied salla integration from tenant=%s to tenant=%s", source_tenant, tenant_id)
    return {"status": "ok", "copied_from": source_tenant, "to_tenant": tenant_id}
