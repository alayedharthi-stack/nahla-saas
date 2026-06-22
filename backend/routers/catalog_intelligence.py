"""
routers/catalog_intelligence.py
─────────────────────────────────
Catalog Intelligence Phase 1 — merchant CRUD APIs (no dashboard UI).

Groups, group items, product relations, rankings, and tenant catalog settings.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import require_not_support_impersonation
from core.database import get_db
from core.tenant import resolve_tenant_id
from services.catalog_intelligence_service import (
    add_group_item,
    create_product_group,
    create_product_relation,
    delete_group_item,
    delete_product_group,
    delete_product_relation,
    get_catalog_settings,
    get_product_group,
    get_product_ranking,
    list_group_items,
    list_product_groups,
    list_product_relations,
    reorder_product_groups,
    save_catalog_settings,
    save_product_ranking,
    update_group_item,
    update_product_group,
)

router = APIRouter()


class CatalogSettingsIn(BaseModel):
    best_seller_mode: str = "manual"
    max_relations_per_product: int = Field(8, ge=1, le=50)
    default_group_slug: str = ""
    small_catalog_threshold: int = Field(5, ge=1, le=500)
    scoring_weights: Dict[str, float] = Field(default_factory=dict)


class ProductGroupIn(BaseModel):
    slug: str = ""
    label: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    catalog_match: str = ""
    priority: int = 100
    is_active: bool = True
    metadata_json: Dict[str, Any] = Field(default_factory=dict)


class ProductGroupPatch(BaseModel):
    slug: Optional[str] = None
    label: Optional[str] = None
    description: Optional[str] = None
    catalog_match: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    metadata_json: Optional[Dict[str, Any]] = None


class ReorderGroupsIn(BaseModel):
    group_ids: List[int] = Field(default_factory=list)


class GroupItemIn(BaseModel):
    product_id: int
    variant_id: Optional[int] = None
    priority: int = 0
    label_override: str = ""


class GroupItemPatch(BaseModel):
    variant_id: Optional[int] = None
    priority: Optional[int] = None
    label_override: Optional[str] = None


class ProductRelationIn(BaseModel):
    target_product_id: int
    relation_type: str
    priority: int = 0
    source: str = "manual"


class ProductRankingIn(BaseModel):
    is_best_seller: bool = False
    sales_rank: Optional[int] = None
    sales_score: Optional[float] = None
    merchant_priority: int = 0
    stats_source: str = "manual"


def _value_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/settings/catalog-intelligence")
async def get_merchant_catalog_intelligence_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    return {
        "tenant_id": tenant_id,
        "catalog_intelligence": get_catalog_settings(db, tenant_id),
    }


@router.put("/settings/catalog-intelligence")
async def put_merchant_catalog_intelligence_settings(
    body: CatalogSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    saved = save_catalog_settings(db, tenant_id, body.model_dump())
    return {
        "status": "ok",
        "tenant_id": tenant_id,
        "catalog_intelligence": saved,
    }


@router.get("/catalog-intelligence/groups")
async def get_product_groups(
    request: Request,
    db: Session = Depends(get_db),
    include_inactive: bool = False,
):
    tenant_id = resolve_tenant_id(request)
    return {
        "tenant_id": tenant_id,
        "groups": list_product_groups(db, tenant_id, include_inactive=include_inactive),
    }


@router.post("/catalog-intelligence/groups", status_code=201)
async def post_product_group(
    body: ProductGroupIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        group = create_product_group(db, tenant_id, body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc
    return {"status": "ok", "group": group}


@router.get("/catalog-intelligence/groups/{group_id}")
async def get_product_group_detail(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    group = get_product_group(db, tenant_id, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group_not_found")
    return {"tenant_id": tenant_id, "group": group}


@router.patch("/catalog-intelligence/groups/{group_id}")
async def patch_product_group(
    group_id: int,
    body: ProductGroupPatch,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        group = update_product_group(
            db,
            tenant_id,
            group_id,
            body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise _value_error(exc) from exc
    if group is None:
        raise HTTPException(status_code=404, detail="group_not_found")
    return {"status": "ok", "group": group}


@router.delete("/catalog-intelligence/groups/{group_id}")
async def delete_product_group_route(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    if not delete_product_group(db, tenant_id, group_id):
        raise HTTPException(status_code=404, detail="group_not_found")
    return {"status": "ok"}


@router.post("/catalog-intelligence/groups/reorder")
async def post_reorder_product_groups(
    body: ReorderGroupsIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    groups = reorder_product_groups(db, tenant_id, body.group_ids)
    return {"status": "ok", "groups": groups}


@router.get("/catalog-intelligence/groups/{group_id}/items")
async def get_group_items(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    items = list_group_items(db, tenant_id, group_id)
    if items is None:
        raise HTTPException(status_code=404, detail="group_not_found")
    return {"tenant_id": tenant_id, "items": items}


@router.post("/catalog-intelligence/groups/{group_id}/items", status_code=201)
async def post_group_item(
    group_id: int,
    body: GroupItemIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        item = add_group_item(db, tenant_id, group_id, body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="group_not_found")
    return {"status": "ok", "item": item}


@router.patch("/catalog-intelligence/groups/{group_id}/items/{item_id}")
async def patch_group_item(
    group_id: int,
    item_id: int,
    body: GroupItemPatch,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        item = update_group_item(
            db,
            tenant_id,
            group_id,
            item_id,
            body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise _value_error(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="group_item_not_found")
    return {"status": "ok", "item": item}


@router.delete("/catalog-intelligence/groups/{group_id}/items/{item_id}")
async def delete_group_item_route(
    group_id: int,
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    if not delete_group_item(db, tenant_id, group_id, item_id):
        raise HTTPException(status_code=404, detail="group_item_not_found")
    return {"status": "ok"}


@router.get("/catalog-intelligence/products/{product_id}/relations")
async def get_product_relations(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    relation_type: str = "",
):
    tenant_id = resolve_tenant_id(request)
    rows = list_product_relations(db, tenant_id, product_id, relation_type=relation_type)
    if rows is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return {"tenant_id": tenant_id, "relations": rows}


@router.post("/catalog-intelligence/products/{product_id}/relations", status_code=201)
async def post_product_relation(
    product_id: int,
    body: ProductRelationIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    try:
        row = create_product_relation(db, tenant_id, product_id, body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return {"status": "ok", "relation": row}


@router.delete("/catalog-intelligence/products/{product_id}/relations/{relation_id}")
async def delete_product_relation_route(
    product_id: int,
    relation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    if not delete_product_relation(db, tenant_id, product_id, relation_id):
        raise HTTPException(status_code=404, detail="relation_not_found")
    return {"status": "ok"}


@router.get("/catalog-intelligence/products/{product_id}/ranking")
async def get_product_ranking_route(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = get_product_ranking(db, tenant_id, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return {"tenant_id": tenant_id, "ranking": row}


@router.put("/catalog-intelligence/products/{product_id}/ranking")
async def put_product_ranking_route(
    product_id: int,
    body: ProductRankingIn,
    request: Request,
    db: Session = Depends(get_db),
    _no_support: dict = Depends(require_not_support_impersonation),
):
    tenant_id = resolve_tenant_id(request)
    row = save_product_ranking(db, tenant_id, product_id, body.model_dump())
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return {"status": "ok", "ranking": row}
