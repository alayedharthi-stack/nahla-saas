"""
routers/shopify_connection.py
─────────────────────────────
Dormant Shopify secure connection foundation (standalone authorization code,
expiring offline tokens, read-only catalog scope). Off by default: while
``NAHLA_SHOPIFY_CONNECTION_ENABLED`` is not truthy every route answers 404.

  GET  /merchant/integrations/shopify/status      JWT; secret-free connection state
  POST /merchant/integrations/shopify/start       JWT merchant actor (DB-revalidated,
                                                  no support impersonation); body {shop};
                                                  returns Shopify's authorize URL
  GET  /merchant/integrations/shopify/callback    exact JWT-public path; Shopify's browser
                                                  redirect. Verifies query HMAC, timestamp,
                                                  shop and the hashed single-use state,
                                                  binds nothing, and returns to the fixed
                                                  dashboard completion path
  POST /merchant/integrations/shopify/complete    JWT merchant actor; body {handle}; same
                                                  tenant, user and session as /start;
                                                  exchanges the code, verifies the shop
                                                  identity, claims the shop
  POST /merchant/integrations/shopify/disconnect  JWT merchant actor; body {shop}; erases
                                                  credentials, keeps the ownership tombstone
  POST /webhooks/shopify/app-uninstalled          public; bounded raw-body HMAC; persists,
                                                  dedupes, quarantines and acknowledges
                                                  with a durable reconciliation request;
                                                  one background attempt, then the
                                                  flag-gated recovery runner

No route registers Shopify in the store adapter registry or the catalog, and
none touches the AI runtime, Salla, Meta, WhatsApp or Moyasar. Codes, states,
handles, tokens and secrets are never logged, reflected or returned.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from core.auth import PLATFORM_ADMIN_ROLES, get_jwt_tenant_id, require_authenticated
from core.database import get_db
from services.shopify_connection import crypto, lifecycle, recovery
from services.shopify_connection.actor import ActorRejected, VerifiedActor, revalidate_actor
from services.shopify_connection.config import (
    REQUESTED_SCOPES,
    ROUTE_PREFIX,
    WEBHOOK_UNINSTALL_PATH,
    ShopifyConfig,
    dashboard_complete_url,
    evaluate_availability,
    flag_enabled,
    webhook_secret,
)
from services.shopify_connection.oauth import OAuthQueryError, ShopifyApi, verify_callback
from services.shopify_connection.shop_domain import canonical_shop_domain
from services.shopify_connection.webhooks import (
    MAX_BODY_BYTES,
    TOPIC_APP_UNINSTALLED,
    WebhookRejected,
    body_digest,
    parse_uninstall_body,
    verify_webhook_hmac,
)

logger = logging.getLogger("nahla.shopify_connection")

router = APIRouter(prefix=ROUTE_PREFIX, tags=["Shopify Connection (dormant)"])
webhook_router = APIRouter(tags=["Shopify Connection (dormant)"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
C_CALLBACK_INVALID = "callback_invalid"


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found"})


def _require_flag() -> None:
    if not flag_enabled():
        raise _not_found()


def _require_config() -> ShopifyConfig:
    _require_flag()
    availability = evaluate_availability()
    if not availability.available:
        raise HTTPException(status_code=503, detail={"error": "shopify_connection_unavailable",
                                                     "reason": availability.reason})
    return availability.config


def _require_tables(db: Session) -> None:
    if not lifecycle.tables_present(db):
        raise HTTPException(status_code=503, detail={"error": "shopify_connection_unavailable",
                                                     "reason": "storage_unavailable"})


def _actor(request: Request, db: Session) -> VerifiedActor:
    payload = require_authenticated(request)
    try:
        return revalidate_actor(db, payload)
    except ActorRejected as exc:
        logger.warning("[SHOPIFY_CONNECTION] actor refused path=%s code=%s", request.url.path, exc.code)
        raise HTTPException(status_code=403, detail={"error": exc.code}) from None


def _refused(exc: lifecycle.ConnectionRefused) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"error": exc.code})


def _api(config: ShopifyConfig) -> ShopifyApi:
    return ShopifyApi(client_id=config.client_id, client_secret=config.client_secret,
                      api_version=config.api_version)


class ShopBody(BaseModel):
    shop: str = Field(min_length=1, max_length=80)


class CompleteBody(BaseModel):
    handle: str = Field(min_length=32, max_length=128)


def _canonical_shop_or_400(raw: str) -> str:
    shop = canonical_shop_domain(raw)
    if shop is None:
        raise HTTPException(status_code=400, detail={"error": "shop_invalid"})
    return shop


@router.get("/status")
async def shopify_status(request: Request, db: Session = Depends(get_db)):
    _require_flag()
    payload = require_authenticated(request)
    if str(payload.get("role") or "").strip() in PLATFORM_ADMIN_ROLES and not payload.get("impersonation"):
        raise HTTPException(status_code=403, detail={"error": "platform_session_refused"})
    tenant_id = get_jwt_tenant_id(request)
    availability = evaluate_availability()
    body: Dict[str, Any] = {
        "available": availability.available,
        "reason": availability.reason,
        "requested_scopes": list(REQUESTED_SCOPES),
        "connections": [],
    }
    if not lifecycle.tables_present(db):
        body["available"] = False
        body["reason"] = body["reason"] or "storage_unavailable"
    else:
        body["connections"] = lifecycle.connection_summaries(db, tenant_id)
    return JSONResponse(body, headers=_NO_STORE)


@router.post("/start")
async def shopify_start(body: ShopBody, request: Request, db: Session = Depends(get_db)):
    config = _require_config()
    actor = _actor(request, db)
    shop = _canonical_shop_or_400(body.shop)
    _require_tables(db)
    try:
        url, ttl = lifecycle.begin_authorization(db, actor=actor, shop_domain=shop, config=config)
    except lifecycle.ConnectionRefused as exc:
        raise _refused(exc) from None
    logger.info("[SHOPIFY_CONNECTION] authorization started tenant=%s", actor.tenant_id)
    return JSONResponse({"authorize_url": url, "expires_in": ttl}, headers=_NO_STORE)


def _finish_redirect(complete_url: Optional[str], *, code: Optional[str] = None,
                     handle: Optional[str] = None) -> Any:
    """Fixed return target; only a fixed code or the fresh handle goes in the fragment."""
    if not complete_url:
        return JSONResponse({"status": code or lifecycle.C_UNAVAILABLE}, status_code=400, headers=_NO_STORE)
    if handle:
        return RedirectResponse(f"{complete_url}#shopify_handle={handle}", status_code=302, headers=_NO_STORE)
    safe = code if code in lifecycle.RESULT_CODES or code == C_CALLBACK_INVALID else lifecycle.C_ERROR
    return RedirectResponse(f"{complete_url}#shopify_connection={safe}", status_code=302, headers=_NO_STORE)


@router.get("/callback")
async def shopify_callback(request: Request, db: Session = Depends(get_db)):
    _require_flag()
    availability = evaluate_availability()
    complete_url = availability.config.dashboard_complete_url if availability.config else dashboard_complete_url()
    if not availability.available:
        logger.warning("[SHOPIFY_CONNECTION] callback refused reason=%s", availability.reason)
        return _finish_redirect(complete_url, code=lifecycle.C_UNAVAILABLE)
    config = availability.config
    try:
        verified = verify_callback(request.url.query, secret=config.client_secret)
    except OAuthQueryError as exc:
        logger.warning("[SHOPIFY_CONNECTION] callback refused code=%s", exc.code)
        return _finish_redirect(complete_url, code=C_CALLBACK_INVALID)
    if not lifecycle.tables_present(db):
        return _finish_redirect(complete_url, code=lifecycle.C_UNAVAILABLE)
    try:
        handle = lifecycle.accept_callback(
            db, callback=verified, config=config, cipher=crypto.TokenCipher(config.encryption_key),
        )
    except lifecycle.ConnectionRefused as exc:
        logger.warning("[SHOPIFY_CONNECTION] callback refused code=%s", exc.code)
        return _finish_redirect(complete_url, code=exc.code)
    except Exception as exc:  # noqa: BLE001 — never reflect or log request material
        try:
            db.rollback()
        except Exception:  # noqa: silent-ok — best-effort rollback; a fixed error is returned
            pass
        logger.error("[SHOPIFY_CONNECTION] callback failed kind=%s", type(exc).__name__)
        return _finish_redirect(complete_url, code=lifecycle.C_ERROR)
    return _finish_redirect(complete_url, handle=handle)


@router.post("/complete")
async def shopify_complete(body: CompleteBody, request: Request, db: Session = Depends(get_db)):
    config = _require_config()
    actor = _actor(request, db)
    _require_tables(db)
    try:
        summary = await lifecycle.complete_authorization(
            db, actor=actor, handle=body.handle, config=config,
            cipher=crypto.TokenCipher(config.encryption_key), api=_api(config),
        )
    except lifecycle.ConnectionRefused as exc:
        raise _refused(exc) from None
    return JSONResponse({"status": lifecycle.C_CONNECTED, "connection": summary}, headers=_NO_STORE)


@router.post("/disconnect")
async def shopify_disconnect(body: ShopBody, request: Request, db: Session = Depends(get_db)):
    # Erasing credentials needs only the flag, never the client secret or key.
    _require_flag()
    actor = _actor(request, db)
    shop = _canonical_shop_or_400(body.shop)
    _require_tables(db)
    try:
        summary = lifecycle.disconnect(db, actor=actor, shop_domain=shop)
    except lifecycle.ConnectionRefused as exc:
        raise _refused(exc) from None
    return JSONResponse({"connection": summary}, headers=_NO_STORE)


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Stream the body and stop past *limit* bytes, whatever Content-Length
    claims (absent, chunked or dishonest): never buffer an unbounded body."""
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > limit:
            raise WebhookRejected("body_too_large")
    return bytes(received)


def _single_header(request: Request, name: str, *, required: bool) -> Optional[str]:
    values = request.headers.getlist(name)
    if len(values) > 1 or (required and not values):
        raise WebhookRejected("header_invalid")
    return values[0] if values else None


@webhook_router.post(WEBHOOK_UNINSTALL_PATH)
async def shopify_app_uninstalled(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    _require_flag()
    secret = webhook_secret()
    if not secret:
        logger.error("[SHOPIFY_CONNECTION] webhook refused reason=secret_missing")
        return JSONResponse({"error": "webhook_unavailable"}, status_code=503)
    declared = request.headers.getlist("content-length")
    if len(declared) > 1 or (declared and (not declared[0].isdigit() or int(declared[0]) > MAX_BODY_BYTES)):
        return JSONResponse({"error": "body_invalid"}, status_code=413)
    try:
        raw = await _read_bounded_body(request, MAX_BODY_BYTES)
    except WebhookRejected:
        return JSONResponse({"error": "body_invalid"}, status_code=413)
    try:
        signature = _single_header(request, "x-shopify-hmac-sha256", required=True)
        verify_webhook_hmac(raw, signature, secret)
    except WebhookRejected as exc:
        logger.warning("[SHOPIFY_CONNECTION] webhook rejected code=%s", exc.code)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        identity = parse_uninstall_body(raw)
        # Unsigned headers may only agree with the signed body; they never decide.
        if _single_header(request, "x-shopify-topic", required=True) != TOPIC_APP_UNINSTALLED:
            raise WebhookRejected("topic_mismatch")
        if _single_header(request, "x-shopify-shop-domain", required=True) != identity.shop_domain:
            raise WebhookRejected("shop_mismatch")
        webhook_id = _single_header(request, "x-shopify-webhook-id", required=False)
        if webhook_id is not None and not (0 < len(webhook_id) <= 128 and webhook_id.isascii()):
            raise WebhookRejected("header_invalid")
        triggered_at = _single_header(request, "x-shopify-triggered-at", required=False)
    except WebhookRejected as exc:
        logger.warning("[SHOPIFY_CONNECTION] webhook refused code=%s", exc.code)
        return JSONResponse({"error": exc.code}, status_code=400)
    if not lifecycle.tables_present(db):
        return JSONResponse({"error": "webhook_unavailable"}, status_code=503)
    try:
        outcome, reconcile_id = lifecycle.record_uninstall(
            db, identity=identity, webhook_id=webhook_id, body_sha256=body_digest(raw),
            triggered_at_header=triggered_at,
        )
    except DBAPIError as exc:
        # Lock timeout or storage failure: not acknowledged, Shopify redelivers.
        db.rollback()
        logger.warning("[SHOPIFY_CONNECTION] webhook deferred kind=%s", type(exc).__name__)
        return JSONResponse({"error": "retry"}, status_code=503)
    logger.info("[SHOPIFY_CONNECTION] uninstall webhook outcome=%s", outcome)
    if reconcile_id is not None:
        # One immediate attempt after the acknowledgement. The request is
        # already durable (reconcile_next_at); the recovery runner retries it
        # if this attempt never runs, fails or finds configuration missing.
        background.add_task(recovery.reconcile_now, reconcile_id)
    return JSONResponse({"ok": True}, status_code=200)
