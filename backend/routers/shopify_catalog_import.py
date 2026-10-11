"""
routers/shopify_catalog_import.py
─────────────────────────────────
Dormant, read-only Shopify catalog import into the provider-scoped mirror.
Off by default: unless both ``NAHLA_SHOPIFY_CONNECTION_ENABLED`` and
``NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED`` are truthy, every route answers 404.

  GET  /merchant/integrations/shopify/catalog-import/status
       JWT of the tenant (merchant, or support impersonation read-only;
       platform staff without impersonation refused). Per connection:
       ``not_started`` | ``importing`` | ``imported`` | ``error`` with a fixed
       reason and the last committed run's evidence (counts, time, API
       version, inventory mode — inventory counts are null when not read).
  POST /merchant/integrations/shopify/catalog-import/start   body {shop}
       DB-revalidated merchant actor (no support impersonation), re-checked
       under row locks inside the transaction that admits the run. Starts
       the connection's single import run and returns 202; the run executes
       in the background and records its own outcome.

"Imported" is the committed Shopify mirror only: nothing here writes the
shared catalog, Salla, Meta, WhatsApp or the AI runtime. Tokens, URLs,
upstream bodies and messages are never logged, reflected or returned.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import PLATFORM_ADMIN_ROLES, get_jwt_tenant_id, require_authenticated
from core.database import get_db
from services.shopify_catalog import store
from services.shopify_catalog.config import CATALOG_API_VERSION, import_routes_enabled
from services.shopify_connection.actor import ActorRejected, revalidate_actor
from services.shopify_connection.config import ROUTE_PREFIX, evaluate_availability
from services.shopify_connection.shop_domain import canonical_shop_domain

logger = logging.getLogger("nahla.shopify_catalog")

router = APIRouter(prefix=f"{ROUTE_PREFIX}/catalog-import", tags=["Shopify Catalog Import (dormant)"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def _require_flags() -> None:
    if not import_routes_enabled():
        raise HTTPException(status_code=404, detail={"error": "not_found"})


class ShopBody(BaseModel):
    shop: str = Field(min_length=1, max_length=80)


@router.get("/status")
async def catalog_import_status(request: Request, db: Session = Depends(get_db)):
    _require_flags()
    payload = require_authenticated(request)
    if str(payload.get("role") or "").strip() in PLATFORM_ADMIN_ROLES and not payload.get("impersonation"):
        raise HTTPException(status_code=403, detail={"error": "platform_session_refused"})
    tenant_id = get_jwt_tenant_id(request)
    availability = evaluate_availability()
    body: Dict[str, Any] = {
        "available": availability.available,
        "reason": availability.reason,
        "api_version": CATALOG_API_VERSION,
        "connections": [],
    }
    if not store.tables_present(db):
        body["available"] = False
        body["reason"] = body["reason"] or "storage_unavailable"
    else:
        body["connections"] = store.import_summaries(db, tenant_id)
    return JSONResponse(body, headers=_NO_STORE)


@router.post("/start")
async def catalog_import_start(body: ShopBody, request: Request, background: BackgroundTasks,
                               db: Session = Depends(get_db)):
    _require_flags()
    availability = evaluate_availability()
    if not availability.available:
        raise HTTPException(status_code=503, detail={"error": "shopify_connection_unavailable",
                                                     "reason": availability.reason})
    payload = require_authenticated(request)
    try:
        actor = revalidate_actor(db, payload)
    except ActorRejected as exc:
        logger.warning("[SHOPIFY_CATALOG] start refused code=%s", exc.code)
        raise HTTPException(status_code=403, detail={"error": exc.code}) from None
    shop = canonical_shop_domain(body.shop)
    if shop is None:
        raise HTTPException(status_code=400, detail={"error": "shop_invalid"})
    if not store.tables_present(db):
        raise HTTPException(status_code=503, detail={"error": "shopify_catalog_unavailable",
                                                     "reason": "storage_unavailable"})
    try:
        handle = store.begin_run(db, actor=actor, shop_domain=shop)
    except store.StartRefused as exc:
        status = {store.S_NOT_FOUND: 404, store.S_ACTOR_REVOKED: 403}.get(exc.code, 409)
        raise HTTPException(status_code=status, detail={"error": exc.code}) from None
    from services.shopify_catalog.importer import run_import_job  # noqa: PLC0415

    background.add_task(run_import_job, handle)
    logger.info("[SHOPIFY_CATALOG] import started tenant=%s run=%s", actor.tenant_id, handle.run_id)
    return JSONResponse({"status": store.ST_IMPORTING, "shop_domain": shop}, status_code=202, headers=_NO_STORE)
