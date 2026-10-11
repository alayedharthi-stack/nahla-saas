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
  408  body not received within RECEIPT_BODY_READ_SECONDS
  401  client secret missing (fail closed), missing / repeated / malformed
       ``X-Shopify-Hmac-Sha256``, or a raw-body HMAC mismatch
  415  not ``application/json`` (after authentication)
  400  topic header missing / unknown / disagreeing with the route; body not
       strict JSON; body shape not that topic's (signed-body classification);
       signed shop identity not canonical; ``X-Shopify-Shop-Domain`` not equal
       to the signed domain
  503  no receipt slot free, storage absent, the durable receipt failed, or it
       did not commit within RECEIPT_BUDGET_SECONDS (Shopify redelivers)
  200  only after the receipt committed within the budget

Event loop and budget
  The handler never touches the database on the event loop. The durable
  receipt — pool checkout, connect, schema inspection, receipt SQL and
  commit — runs on a dedicated bounded thread pool with its own small engine
  (``store.dedicated_session_factory``: shared URL, separate pool, tight
  checkout / connect / statement / lock timeouts; the shared engine is not
  changed). A slot is taken without waiting (no backlog: a full pool answers
  503 at once) and released only when the thread finishes. The answer waits
  at most RECEIPT_BUDGET_SECONDS. A Python thread cannot be cancelled: a
  receipt still running at the deadline keeps its slot and its connection
  until its own statement timeout ends it, and it checks the deadline again
  before committing (rolling back when passed). A commit already in flight
  at the deadline may still complete after the 503 — Shopify's redelivery
  then attaches to the same open request (idempotent), so the outcome is a
  duplicate delivery count, never a lost or falsely acknowledged receipt.
  The HTTP timeout does not terminate a database thread: a thread stalled on
  the network (no answer from the server) is not ended by the server-side
  statement timeout and may stay blocked until the operating system gives up
  on the connection. The four-slot bound and the dedicated pool keep such
  threads from multiplying (further deliveries are answered 503 at once) —
  a mitigation, not cancellation or a hard network termination.

Never logged, stored or reflected: the body, raw / unvalidated header values,
the HMAC, the secret, a customer email / phone / id or order ids. The unsigned
webhook id and triggered-at are persisted only after validation, in canonical
UUID / parsed-timestamp form (diagnostics, never evidence).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from services.shopify_connection.webhooks import WebhookRejected, body_digest, verify_webhook_hmac
from services.shopify_privacy import store
from services.shopify_privacy.config import (
    MAX_BODY_BYTES,
    RECEIPT_BODY_READ_SECONDS,
    RECEIPT_BUDGET_SECONDS,
    RECEIPT_WORKERS,
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
from services.shopify_privacy.payloads import PayloadRejected, PrivacyPayload, parse_privacy_body

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
C_BODY_TIMEOUT = "body_timeout"
C_RETRY = "retry"

# Dedicated, bounded receipt execution (never the event loop, never the
# shared pool). Slots == workers, so an admitted receipt starts at once.
_EXECUTOR = ThreadPoolExecutor(max_workers=RECEIPT_WORKERS, thread_name_prefix="shopify-privacy-receipt")
_SLOTS = threading.BoundedSemaphore(RECEIPT_WORKERS)
_session_factory: Optional[Callable[[], Session]] = None
_factory_lock = threading.Lock()


def set_receipt_session_factory(factory: Optional[Callable[[], Session]]) -> None:
    """Test / provisioning seam: the session factory receipts use (None = the
    lazily built dedicated engine)."""
    global _session_factory
    with _factory_lock:
        _session_factory = factory


def _receipt_sessions() -> Callable[[], Session]:
    global _session_factory
    with _factory_lock:
        if _session_factory is None:
            _session_factory = store.dedicated_session_factory()
        return _session_factory


class _StorageAbsent(Exception):
    pass


def _durable_receipt(payload: PrivacyPayload, digest: str, webhook_id: Optional[str], triggered_at,
                     deadline: float) -> Tuple[str, int]:
    """Runs on the receipt pool. Releases its slot when it ends, whatever happened."""
    try:
        if time.monotonic() >= deadline:
            raise store.ReceiptUnavailable("deadline")
        db = _receipt_sessions()()
        try:
            if not store.tables_present(db):
                raise _StorageAbsent()
            if time.monotonic() >= deadline:  # the caller has already answered 503
                raise store.ReceiptUnavailable("deadline")
            return store.record_receipt(db, payload=payload, body_sha256=digest, webhook_id=webhook_id,
                                        triggered_at=triggered_at, deadline=deadline)
        except BaseException:
            try:
                db.rollback()
            except Exception:  # noqa: silent-ok — best-effort rollback; the caller answers 503
                pass
            raise
        finally:
            db.close()
    finally:
        _SLOTS.release()


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


async def _intake(request: Request, path_topic: Optional[str]) -> JSONResponse:
    if not intake_enabled():
        return _reply(C_NOT_FOUND, 404)
    try:
        declared = request.headers.getlist("content-length")
        if len(declared) > 1 or (declared and (not declared[0].isdigit() or int(declared[0]) > MAX_BODY_BYTES)):
            raise _Refused(C_BODY_TOO_LARGE, 413)
        try:
            raw = await asyncio.wait_for(_read_bounded(request), timeout=RECEIPT_BODY_READ_SECONDS)
        except asyncio.TimeoutError:
            raise _Refused(C_BODY_TIMEOUT, 408) from None
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
    if not _SLOTS.acquire(blocking=False):
        logger.warning("[SHOPIFY_PRIVACY] receipt deferred kind=no_receipt_slot")
        return _reply(C_RETRY, 503)
    deadline = time.monotonic() + RECEIPT_BUDGET_SECONDS
    try:
        job = _EXECUTOR.submit(_durable_receipt, payload, body_digest(raw), webhook_id, triggered_at, deadline)
    except Exception as exc:  # noqa: BLE001 — executor unavailable (shutdown)
        _SLOTS.release()
        logger.warning("[SHOPIFY_PRIVACY] receipt deferred kind=%s", type(exc).__name__)
        return _reply(C_RETRY, 503)
    # A job cancelled before it started never ran its own release.
    job.add_done_callback(lambda f: _SLOTS.release() if f.cancelled() else None)
    try:
        outcome, request_id = await asyncio.wait_for(asyncio.wrap_future(job), timeout=RECEIPT_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("[SHOPIFY_PRIVACY] receipt deferred kind=deadline_exceeded")
        return _reply(C_RETRY, 503)
    except _StorageAbsent:
        logger.warning("[SHOPIFY_PRIVACY] receipt deferred kind=storage_unavailable")
        return _reply(C_RETRY, 503)
    except Exception as exc:  # noqa: BLE001 — never acknowledged; Shopify redelivers
        logger.warning("[SHOPIFY_PRIVACY] receipt deferred kind=%s", type(exc).__name__)
        return _reply(C_RETRY, 503)
    logger.info("[SHOPIFY_PRIVACY] receipt outcome=%s request=%s topic=%s", outcome, request_id, payload.topic)
    return _reply(None, 200)


@router.post(TOPIC_PATHS[TOPIC_CUSTOMERS_DATA_REQUEST])
async def shopify_privacy_customers_data_request(request: Request):
    return await _intake(request, TOPIC_CUSTOMERS_DATA_REQUEST)


@router.post(TOPIC_PATHS[TOPIC_CUSTOMERS_REDACT])
async def shopify_privacy_customers_redact(request: Request):
    return await _intake(request, TOPIC_CUSTOMERS_REDACT)


@router.post(TOPIC_PATHS[TOPIC_SHOP_REDACT])
async def shopify_privacy_shop_redact(request: Request):
    return await _intake(request, TOPIC_SHOP_REDACT)


@router.post(ROUTE_PREFIX)
async def shopify_privacy_any_topic(request: Request):
    return await _intake(request, None)
