"""
routers/store_sync.py
──────────────────────
Store Knowledge Sync API endpoints.

Routes
  POST /store-sync/trigger       — merchant triggers a full sync
  GET  /store-sync/status        — current sync state + entity counts
  GET  /store-sync/knowledge     — AI-ready knowledge overview
  POST /store-sync/webhook/:type — internal endpoint for incremental updates
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import StoreKnowledgeSnapshot, StoreSyncJob  # noqa: E402

from core.database import get_db
from core.tenant import resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(prefix="/store-sync", tags=["Store Sync"])


# ── Background task wrapper ────────────────────────────────────────────────────

async def _run_full_sync(tenant_id: int):
    """Background task — runs in a separate async call after HTTP response."""
    import sys, os  # noqa: PLC0415,E401
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from core.database import get_db as _get_db  # noqa: PLC0415
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    db = next(_get_db())
    try:
        svc = StoreSyncService(db, tenant_id)
        await svc.full_sync(triggered_by="merchant")
    except Exception as exc:
        logger.error("Background full_sync error tenant=%s: %s", tenant_id, exc)
    finally:
        db.close()


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/trigger")
async def trigger_full_sync(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger a full store sync in the background.
    Returns immediately with job_id; poll /status for progress.
    """
    tenant_id = resolve_tenant_id(request)

    # Prevent duplicate concurrent syncs
    running = (
        db.query(StoreSyncJob)
        .filter_by(tenant_id=tenant_id, status="running")
        .first()
    )
    if running:
        return {
            "status":  "already_running",
            "job_id":  running.id,
            "message": "مزامنة جارية بالفعل. انتظر حتى تكتمل.",
        }

    # Create a pending job row so we can return the ID immediately
    job = StoreSyncJob(
        tenant_id    = tenant_id,
        status       = "pending",
        sync_type    = "full",
        triggered_by = "merchant",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(_run_full_sync, tenant_id)

    return {
        "status":  "started",
        "job_id":  job.id,
        "message": "بدأت عملية المزامنة. ستظهر النتائج خلال ثوانٍ.",
    }


@router.get("/status")
async def sync_status(request: Request, db: Session = Depends(get_db)):
    """Return current sync status and entity counts."""
    tenant_id = resolve_tenant_id(request)

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    svc = StoreSyncService(db, tenant_id)
    return svc.get_status()


@router.get("/knowledge")
async def get_knowledge_overview(request: Request, db: Session = Depends(get_db)):
    """
    Return a summary of the AI-ready knowledge snapshot for this tenant.
    Does NOT include full product lists — for dashboard overview only.
    """
    tenant_id = resolve_tenant_id(request)
    snap = (
        db.query(StoreKnowledgeSnapshot)
        .filter_by(tenant_id=tenant_id)
        .first()
    )
    if not snap:
        return {
            "ready":          False,
            "message":        "لم يتم مزامنة بيانات المتجر بعد. اضغط 'مزامنة الآن' لتهيئة نحلة.",
            "product_count":  0,
            "category_count": 0,
            "order_count":    0,
            "coupon_count":   0,
        }

    store_profile = snap.store_profile or {}
    catalog       = snap.catalog_summary or {}
    coupons       = snap.coupon_summary  or {}

    return {
        "ready":           True,
        "store_name":      store_profile.get("store_name", ""),
        "store_url":       store_profile.get("store_url", ""),
        "product_count":   snap.product_count,
        "category_count":  snap.category_count,
        "categories":      catalog.get("categories", [])[:10],
        "order_count":     snap.order_count,
        "coupon_count":    snap.coupon_count,
        "active_coupons":  (coupons.get("coupons") or [])[:5],
        "last_full_sync":  snap.last_full_sync_at.isoformat() if snap.last_full_sync_at else None,
        "last_inc_sync":   snap.last_incremental_sync_at.isoformat() if snap.last_incremental_sync_at else None,
        "sync_version":    snap.sync_version,
    }


@router.post("/webhook/{event_type}")
async def store_webhook_incremental(
    event_type: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Internal incremental update endpoint — called when store platform
    sends a webhook for product/order/coupon changes.
    """
    tenant_id = resolve_tenant_id(request)
    payload   = await request.json()

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    svc = StoreSyncService(db, tenant_id)

    if event_type == "product":
        await svc.handle_product_webhook(payload)
        return {"status": "ok", "event": "product_updated"}

    return {"status": "ignored", "event": event_type}
