"""
routers/discovery_settings.py
──────────────────────────────
Merchant discovery settings API (Phase 4A).

GET/PUT  /settings/discovery
POST     /settings/discovery/collections/reorder
PATCH    /settings/discovery/collections/{collection_id}/enabled
POST     /settings/discovery/collections/{collection_id}/featured
DELETE   /settings/discovery/collections/{collection_id}/featured/{product_id}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import require_not_support_impersonation
from core.database import get_db
from core.tenant import resolve_tenant_id
from services.merchant_discovery_settings_service import (
    assign_featured_product,
    get_discovery_settings,
    remove_featured_product,
    reorder_collections,
    save_discovery_settings,
    set_collection_enabled,
)

router = APIRouter()


class FeaturedProductIn(BaseModel):
    product_id: str = Field(..., min_length=1)
    variant_id: str = ""
    priority: int = 0
    label_override: str = ""


class DiscoveryCollectionIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=120)
    priority: int = 0
    enabled: bool = True
    catalog_match: str = ""
    featured_products: List[FeaturedProductIn] = Field(default_factory=list)


class DiscoverySettingsIn(BaseModel):
    default_mode: str = ""
    initial_product_count: int = Field(3, ge=1, le=20)
    featured_product_ids: List[str] = Field(default_factory=list)
    collections: List[DiscoveryCollectionIn] = Field(default_factory=list)
    guided_question: str = ""
    small_catalog_threshold: int = Field(5, ge=1, le=100)


class ReorderCollectionsIn(BaseModel):
    collection_ids: List[str] = Field(default_factory=list)


class CollectionEnabledIn(BaseModel):
    enabled: bool = True


@router.get("/settings/discovery")
async def get_merchant_discovery_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    return {
        "tenant_id": tenant_id,
        "discovery_settings": get_discovery_settings(db, tenant_id),
    }


@router.put("/settings/discovery")
async def put_merchant_discovery_settings(
    body: DiscoverySettingsIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    raw = body.model_dump()
    try:
        saved = save_discovery_settings(db, tenant_id, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "tenant_id": tenant_id,
        "discovery_settings": saved,
    }


@router.post("/settings/discovery/collections/reorder")
async def post_reorder_discovery_collections(
    body: ReorderCollectionsIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    saved = reorder_collections(db, tenant_id, body.collection_ids)
    return {"status": "ok", "discovery_settings": saved}


@router.patch("/settings/discovery/collections/{collection_id}/enabled")
async def patch_collection_enabled(
    collection_id: str,
    body: CollectionEnabledIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        saved = set_collection_enabled(
            db,
            tenant_id,
            collection_id,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "discovery_settings": saved}


@router.post("/settings/discovery/collections/{collection_id}/featured")
async def post_collection_featured_product(
    collection_id: str,
    body: FeaturedProductIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        saved = assign_featured_product(
            db,
            tenant_id,
            collection_id,
            body.model_dump(),
        )
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "collection_not_found" else 400
        raise HTTPException(status_code=status, detail=code) from exc
    return {"status": "ok", "discovery_settings": saved}


@router.delete("/settings/discovery/collections/{collection_id}/featured/{product_id}")
async def delete_collection_featured_product(
    collection_id: str,
    product_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        saved = remove_featured_product(db, tenant_id, collection_id, product_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "discovery_settings": saved}
