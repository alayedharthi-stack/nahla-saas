"""
routers/shopify_privacy_webhooks.py
───────────────────────────────────
Dormant intake for Shopify's mandatory privacy compliance webhooks. Off by
default: while ``NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED`` is not truthy every
route answers 404. Registering the subscriptions (app configuration / Dev
Dashboard) is not done here.

  POST /webhooks/shopify/privacy/customers-data-request
  POST /webhooks/shopify/privacy/customers-redact
  POST /webhooks/shopify/privacy/shop-redact
  POST /webhooks/shopify/privacy                 one URI for all three topics

Plural ``/webhooks/``: the middleware's last-resort fallback rewrites failures
under the singular ``/webhook/`` prefix to HTTP 200, which would acknowledge
a delivery that was never stored. Here every failure stays non-2xx.

Order of checks (fixed response codes; bodies are fixed codes only):
  413  declared or streamed body larger than the bound (before anything else)
  401  client secret missing (fail closed), missing / repeated / malformed
       ``X-Shopify-Hmac-Sha256``, or a raw-body HMAC mismatch
  415  not ``application/json`` (after authentication)
  400  topic header missing / unknown / disagreeing with the route; body not
       strict JSON; body shape not that topic's (signed-body classification);
       signed shop identity not canonical; ``X-Shopify-Shop-Domain`` not equal
       to the signed domain
  503  storage absent or the durable receipt failed (Shopify redelivers)
  200  only after the receipt committed

Never logged, stored or reflected: the body, any header value, the HMAC, the
secret, a customer email / phone / id or order ids. Unsigned webhook id and
triggered-at are kept only in canonical UUID / timestamp form.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.database import get_db
from services.shopify_connection.webhooks import WebhookRejected, body_digest, verify_webhook_hmac
from services.shopify_privacy import store
from services.shopify_privacy.config import (
    MAX_BODY_BYTES,
    ROUTE_PREFIX,
    TOPIC_PATHS,
    intake_enabled,
    webhook_secret,
)
from services.shopify_privacy.headers import canonical_webhook_id, parse_triggered_at
from services.shopify_privacy.models import (
    TOPIC_CUSTOMERS_DATA_REQUEST,
    TOPIC_CUSTOMERS_REDACT,
    TOPIC_SHOP_REDACT,
    TOPICS,
)
from services.shopify_privacy.payloads import PayloadRejected, parse_privacy_body

logger = logging.getLogger("nahla.shopify_privacy")

router = APIRouter(tags=["Shopify Privacy (dormant)"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

C_NOT_FOUND = "not_found"
C_BODY_TOO_LARGE = "body_too_large"
C_UNAUTHORIZED = "unauthorized"
C_UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
C_HEADER_INVALID = "header_invalid"
C_TOPIC_INVALID = "topic_invalid"
C_TOPIC_MISMATCH = "topic_mismatch"
C_SHOP_MISMATCH = "shop_mismatch"
C_RETRY = "retry"


class _Refused(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def _reply(code: Optional[str], status: int) -> JSONResponse:
    body = {"ok": True} if code is None else {"error": code}
    return JSONResponse(body, status_code=status, headers=_NO_STORE)


async def _read_bounded(request: Request) -> bytes:
    """Stream the body and stop past the bound, whatever Content-Length claims."""
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY_BYTES:
            raise _Refused(C_BODY_TOO_LARGE, 413)
    return bytes(received)


def _single(request: Request, name: str, *, code: str, status: int) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise _Refused(code, status)
    return values[0]


def _optional_single(request: Request, name: str) -> Optional[str]:
    values = request.headers.getlist(name)
    return values[0] if len(values) == 1 else None


def _json_content_type(value: str) -> bool:
    parts = [p.strip().lower() for p in value.split(";")]
    if parts[0] != "application/json":
        return False
    return all(p in ("charset=utf-8", 'charset="utf-8"') for p in parts[1:])


async def _intake(request: Request, db: Session, path_topic: Optional[str]) -> JSONResponse:
    if not intake_enabled():
        return _reply(C_NOT_FOUND, 404)
    try:
        declared = request.headers.getlist("content-length")
        if len(declared) > 1 or (declared and (not declared[0].isdigit() or int(declared[0]) > MAX_BODY_BYTES)):
            raise _Refused(C_BODY_TOO_LARGE, 413)
        raw = await _read_bounded(request)
        secret = webhook_secret()
        if not secret:
            logger.error("[SHOPIFY_PRIVACY] delivery refused reason=secret_missing")
            raise _Refused(C_UNAUTHORIZED, 401)
        signature = _single(request, "x-shopify-hmac-sha256", code=C_UNAUTHORIZED, status=401)
        try:
            verify_webhook_hmac(raw, signature, secret)
        except WebhookRejected as exc:
            logger.warning("[SHOPIFY_PRIVACY] delivery refused reason=%s", exc.code)
            raise _Refused(C_UNAUTHORIZED, 401) from None
        # Authenticated body from here on; headers are still unsigned.
        if not _json_content_type(_single(request, "content-type", code=C_UNSUPPORTED_MEDIA_TYPE, status=415)):
            raise _Refused(C_UNSUPPORTED_MEDIA_TYPE, 415)
        topic = _single(request, "x-shopify-topic", code=C_TOPIC_INVALID, status=400)
        if topic not in TOPICS:
            raise _Refused(C_TOPIC_INVALID, 400)
        if path_topic is not None and topic != path_topic:
            raise _Refused(C_TOPIC_MISMATCH, 400)
        try:
            payload = parse_privacy_body(raw, topic)
        except PayloadRejected as exc:
            raise _Refused(exc.code, 400) from None
        if _single(request, "x-shopify-shop-domain", code=C_HEADER_INVALID, status=400) != payload.shop_domain:
            raise _Refused(C_SHOP_MISMATCH, 400)
    except _Refused as refused:
        if refused.status != 401:
            logger.warning("[SHOPIFY_PRIVACY] delivery refused code=%s", refused.code)
        return _reply(refused.code, refused.status)

    webhook_id = canonical_webhook_id(_optional_single(request, "x-shopify-webhook-id"))
    triggered_at = parse_triggered_at(_optional_single(request, "x-shopify-triggered-at"))
    if not store.tables_present(db):
        return _reply(C_RETRY, 503)
    try:
        outcome, request_id = store.record_receipt(
            db, payload=payload, body_sha256=body_digest(raw), webhook_id=webhook_id, triggered_at=triggered_at,
        )
    except Exception as exc:  # noqa: BLE001 — never acknowledged; Shopify redelivers
        try:
            db.rollback()
        except Exception:  # noqa: silent-ok — best-effort rollback; a fixed 503 is returned
            pass
        logger.warning("[SHOPIFY_PRIVACY] receipt deferred kind=%s", type(exc).__name__)
        return _reply(C_RETRY, 503)
    logger.info("[SHOPIFY_PRIVACY] receipt outcome=%s request=%s topic=%s", outcome, request_id, payload.topic)
    return _reply(None, 200)


@router.post(TOPIC_PATHS[TOPIC_CUSTOMERS_DATA_REQUEST])
async def shopify_privacy_customers_data_request(request: Request, db: Session = Depends(get_db)):
    return await _intake(request, db, TOPIC_CUSTOMERS_DATA_REQUEST)


@router.post(TOPIC_PATHS[TOPIC_CUSTOMERS_REDACT])
async def shopify_privacy_customers_redact(request: Request, db: Session = Depends(get_db)):
    return await _intake(request, db, TOPIC_CUSTOMERS_REDACT)


@router.post(TOPIC_PATHS[TOPIC_SHOP_REDACT])
async def shopify_privacy_shop_redact(request: Request, db: Session = Depends(get_db)):
    return await _intake(request, db, TOPIC_SHOP_REDACT)


@router.post(ROUTE_PREFIX)
async def shopify_privacy_any_topic(request: Request, db: Session = Depends(get_db)):
    return await _intake(request, db, None)
