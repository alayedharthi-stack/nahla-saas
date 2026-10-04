"""Explicit, tenant-scoped OTO shipping operations for non-store orders."""
from __future__ import annotations

import os
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.requests import ClientDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.auth import require_merchant_scope
from core.database import get_db
from core.tenant import resolve_tenant_id
from models import Order, OrderShipment
from oto.client import OtoApiError, OtoClient
from oto.crypto import decrypt_secret, encrypt_secret
from oto.models import OtoConnection
from oto.order_data import build_order_payload, oto_order_id, parse_oto_order_id
from oto.security import verify_webhook, webhook_fingerprint
from oto.state import apply_oto_status, safe_url

router = APIRouter(prefix="/oto", tags=["OTO Shipping"])


class ConnectionInput(BaseModel):
    environment: Literal["staging", "production"]
    refresh_token: str = Field(min_length=8)
    pickup_location_code: str | None = None
    pickup_city: str | None = None
    webhook_secret: str | None = None


class QuoteInput(BaseModel):
    environment: Literal["staging", "production"] = "staging"
    origin_city: str = Field(min_length=2)
    destination_city: str = Field(min_length=2)
    weight_kg: float = Field(gt=0)
    width_cm: float = Field(gt=0)
    length_cm: float = Field(gt=0)
    height_cm: float = Field(gt=0)
    cod_amount: float = Field(default=0, ge=0)


class ShipmentInput(BaseModel):
    environment: Literal["staging", "production"] = "staging"
    delivery_option_id: int = Field(gt=0)
    weight_kg: float = Field(gt=0)
    width_cm: float = Field(gt=0)
    length_cm: float = Field(gt=0)
    height_cm: float = Field(gt=0)


class PickupInput(BaseModel):
    environment: Literal["staging", "production"] = "staging"
    code: str = Field(min_length=2, max_length=120)
    name: str = Field(min_length=2)
    mobile: str = Field(min_length=9)
    address: str = Field(min_length=5)
    city: str = Field(min_length=2)
    contact_name: str = Field(min_length=2)
    contact_email: str = Field(min_length=5)
    short_address_code: str | None = None


def _connection(db: Session, tenant_id: int, environment: str) -> OtoConnection:
    row = db.query(OtoConnection).filter_by(tenant_id=tenant_id, environment=environment).first()
    if row is None or not row.enabled:
        raise HTTPException(409, "oto_connection_unavailable")
    return row


def _client(row: OtoConnection) -> OtoClient:
    base = ("https://staging-api.tryoto.com/rest/v2" if row.environment == "staging"
            else "https://api.tryoto.com/rest/v2")
    return OtoClient(decrypt_secret(row.refresh_token_ciphertext), base_url=base)


def _order(db: Session, tenant_id: int, order_id: int, *, lock: bool = False) -> Order:
    query = db.query(Order).filter(Order.tenant_id == tenant_id, Order.id == order_id)
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        raise HTTPException(404, "order_not_found")
    if row.source not in {"whatsapp", "manual"}:
        raise HTTPException(409, "oto_requires_nahlah_order")
    return row


def _shipment(db: Session, tenant_id: int, order_id: int) -> OrderShipment:
    row = db.query(OrderShipment).filter_by(tenant_id=tenant_id, order_id=order_id, provider="oto").first()
    if row is None:
        raise HTTPException(404, "oto_shipment_not_found")
    return row


def oto_integration_enabled() -> bool:
    """The one activation switch for every OTO surface (default off).

    ``OTO_EXTERNAL_EGRESS_ENABLED=1`` is the documented activation step. Until it
    is set, credential storage, status reads, the public webhook and the
    WhatsApp label notice answer as if the integration did not exist — before
    any database read, so a deployment without migration 0117 or
    ``OTO_TOKEN_ENC_KEY`` stays inert and quiet rather than failing with 500s.
    """
    return os.getenv("OTO_EXTERNAL_EGRESS_ENABLED") == "1"


def _require_integration_enabled() -> None:
    if not oto_integration_enabled():
        raise HTTPException(409, "oto_integration_disabled")


# Signed OTO callbacks are small JSON objects; anything larger is refused.
WEBHOOK_BODY_LIMIT = 32 * 1024
_CLOSE = {"Connection": "close"}


async def _read_bounded_body(request: Request, limit: int = WEBHOOK_BODY_LIMIT) -> bytes:
    """Read at most ``limit`` bytes of the request body, streaming.

    A declared ``Content-Length`` above the limit is refused before any body
    byte is read; without one (chunked transfer) the body is read chunk by
    chunk and refused as soon as the running total passes the limit, so an
    oversized body is never buffered whole. A malformed ``Content-Length`` is
    refused outright (ASCII digits only). Refusals carry ``Connection: close``
    so the server drops the connection instead of draining the rest of an
    unread body. A client that disconnects mid-body is answered 400, not an
    unhandled error.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        if not (declared.isascii() and declared.strip().isdigit()):
            raise HTTPException(400, "invalid_content_length", headers=_CLOSE)
        if int(declared) > limit:
            raise HTTPException(413, "payload_too_large", headers=_CLOSE)
    received = bytearray()
    try:
        async for chunk in request.stream():
            if len(received) + len(chunk) > limit:
                raise HTTPException(413, "payload_too_large", headers=_CLOSE)
            received += chunk
    except ClientDisconnect:
        raise HTTPException(400, "client_disconnected", headers=_CLOSE) from None
    return bytes(received)


def _egress_allowed(environment: str, tenant_id: int) -> None:
    from core.acceptance_execution_context import deny_external_egress

    deny_external_egress(egress_kind="shipping", operation="oto_api", tenant_id=tenant_id)
    if not oto_integration_enabled():
        raise HTTPException(409, "oto_egress_disabled")
    if environment == "production" and os.getenv("OTO_PRODUCTION_ENABLED") != "1":
        raise HTTPException(409, "oto_production_disabled")


@router.get("/availability")
async def availability(_user: dict = Depends(require_merchant_scope)):
    """Whether OTO is switched on for this deployment. No database read."""
    return {"enabled": oto_integration_enabled()}


@router.put("/connection")
async def save_connection(body: ConnectionInput, request: Request, db: Session = Depends(get_db),
                          _user: dict = Depends(require_merchant_scope)):
    """Provision a merchant token acquired out of band; never return its value."""
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    if _user.get("impersonation"):
        raise HTTPException(403, "oto_credentials_require_merchant")
    row = db.query(OtoConnection).filter_by(tenant_id=tenant_id, environment=body.environment).first()
    if row is None:
        row = OtoConnection(tenant_id=tenant_id, environment=body.environment)
        db.add(row)
    row.refresh_token_ciphertext = encrypt_secret(body.refresh_token)
    if body.webhook_secret:
        row.webhook_secret_ciphertext = encrypt_secret(body.webhook_secret)
    row.pickup_location_code = body.pickup_location_code
    row.pickup_city = body.pickup_city
    row.enabled = False  # Credentials are not proven until OTO confirms them.
    db.commit()
    return {"configured": True, "verified": False, "environment": body.environment,
            "pickup_location_code": row.pickup_location_code, "webhook_configured": bool(row.webhook_secret_ciphertext)}


@router.get("/connection")
async def connection_status(request: Request, db: Session = Depends(get_db),
                            _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    rows = db.query(OtoConnection).filter_by(tenant_id=tenant_id).all()
    return [{"environment": x.environment, "enabled": x.enabled,
             "pickup_location_code": x.pickup_location_code,
             "pickup_city": x.pickup_city,
             "webhook_configured": bool(x.webhook_secret_ciphertext)} for x in rows]


@router.post("/connection/{environment}/verify")
async def verify_connection(environment: Literal["staging", "production"], request: Request,
                            db: Session = Depends(get_db), _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    if _user.get("impersonation"):
        raise HTTPException(403, "oto_verification_requires_merchant")
    _egress_allowed(environment, tenant_id)
    row = db.query(OtoConnection).filter_by(tenant_id=tenant_id, environment=environment).first()
    if row is None:
        raise HTTPException(404, "oto_credentials_not_configured")
    try:
        result = await _client(row).list_pickup_locations()
    except OtoApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    if result.get("success") is not True:
        raise HTTPException(502, "oto_connection_not_verified")
    if row.pickup_location_code:
        locations = (result.get("warehouses") or []) + (result.get("branches") or [])
        if not any(isinstance(x, dict) and x.get("code") == row.pickup_location_code
                   for x in locations):
            raise HTTPException(409, "oto_pickup_code_not_in_account")
    row.enabled = True
    db.commit()
    return {"verified": True, "environment": environment}


@router.post("/pickup")
async def create_pickup(body: PickupInput, request: Request, db: Session = Depends(get_db),
                        _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    if _user.get("impersonation"):
        raise HTTPException(403, "oto_pickup_requires_merchant")
    _egress_allowed(body.environment, tenant_id)
    conn = _connection(db, tenant_id, body.environment)
    payload = {"code": body.code, "name": body.name, "mobile": body.mobile,
               "address": body.address, "city": body.city, "country": "SA",
               "contactName": body.contact_name, "contactEmail": body.contact_email,
               "type": "warehouse"}
    if body.short_address_code:
        payload["shortAddressCode"] = body.short_address_code
    try:
        await _client(conn).create_pickup_location(payload)
    except OtoApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    conn.pickup_location_code = body.code
    conn.pickup_city = body.city
    db.commit()
    return {"pickup_location_code": body.code, "city": body.city}


@router.post("/quotes")
async def quote(body: QuoteInput, request: Request, db: Session = Depends(get_db),
                _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    _egress_allowed(body.environment, tenant_id)
    row = _connection(db, tenant_id, body.environment)
    if row.pickup_city and row.pickup_city.casefold() != body.origin_city.casefold():
        raise HTTPException(409, "oto_origin_city_mismatch")
    payload = {"originCity": body.origin_city, "destinationCity": body.destination_city,
               "weight": body.weight_kg, "width": body.width_cm, "length": body.length_cm,
               "height": body.height_cm, "totalDue": body.cod_amount, "currency": "SAR"}
    try:
        result = await _client(row).check_oto_delivery_fee(payload)
    except OtoApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"quotes": result.get("deliveryCompany") or result.get("deliveryCompanies") or [],
            "source": "oto", "environment": body.environment}


@router.post("/orders/{order_id}/shipments")
async def create_shipment(order_id: int, body: ShipmentInput, request: Request,
                          db: Session = Depends(get_db), _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    from core.order_shipment_service import evaluate_create_shipment, resolve_tenant_cod_enabled
    from core.internal_e2e_safety import assert_external_order_eligible
    from core.acceptance_execution_context import deny_external_egress

    tenant_id = resolve_tenant_id(request)
    if _user.get("impersonation"):
        raise HTTPException(403, "oto_shipment_requires_merchant")
    _egress_allowed(body.environment, tenant_id)
    conn = _connection(db, tenant_id, body.environment)
    order = _order(db, tenant_id, order_id, lock=True)
    try:
        assert_external_order_eligible(order, operation="oto_create_shipment")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    deny_external_egress(egress_kind="shipping", operation="oto_create_shipment", tenant_id=tenant_id)
    existing = db.query(OrderShipment).filter_by(order_id=order.id).first()
    gate = evaluate_create_shipment(order, cod_enabled=resolve_tenant_cod_enabled(db, tenant_id),
                                    existing_shipment=existing)
    if not gate.allowed:
        raise HTTPException(409, gate.reason_key)
    try:
        payload = build_order_payload(order, tenant_id=tenant_id,
                                      pickup_code=conn.pickup_location_code or "",
                                      weight_kg=body.weight_kg, width_cm=body.width_cm,
                                      length_cm=body.length_cm, height_cm=body.height_cm)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    client = _client(conn)
    shipment = OrderShipment(tenant_id=tenant_id, order_id=order.id, provider="oto",
                             status="oto_order_pending", tracking_data_source="oto",
                             extra_metadata={"oto_order_id": oto_order_id(tenant_id, order.id),
                                             "environment": body.environment,
                                             "delivery_option_id": body.delivery_option_id})
    db.add(shipment)
    try:
        db.commit()  # Reserve the single shipment slot before any external call.
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "shipment_exists") from exc
    try:
        await client.create_order(payload)
        shipment.status = "oto_order_created"
        db.commit()
        accepted = await client.create_shipment(payload["orderId"], body.delivery_option_id)
        shipment.status = "oto_shipment_requested"
        external_id = accepted.get("shipmentId") or accepted.get("shipmentNumber")
        if external_id:
            shipment.external_shipment_id = str(external_id)[:120]
        db.commit()
    except OtoApiError as exc:
        # A timeout/5xx may mean OTO acted; an operator must reconcile first.
        shipment.status = "oto_needs_reconciliation"
        db.commit()
        raise HTTPException(502, str(exc)) from exc
    return {"shipment_id": shipment.id, "status": shipment.status,
            "oto_order_id": payload["orderId"], "confirmed": False}


@router.post("/orders/{order_id}/sync")
async def sync_shipment(order_id: int, request: Request, db: Session = Depends(get_db),
                        _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    order = _order(db, tenant_id, order_id)
    shipment = _shipment(db, tenant_id, order.id)
    meta = shipment.extra_metadata or {}
    environment = meta.get("environment")
    _egress_allowed(environment, tenant_id)
    conn = _connection(db, tenant_id, environment)
    try:
        result = await _client(conn).order_status(oto_order_id(tenant_id, order.id))
    except OtoApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    if isinstance(data, dict) and data.get("status"):
        apply_oto_status(order, shipment, data)
        db.commit()
    return {"status": shipment.status, "tracking_number": shipment.tracking_number,
            "tracking_url": shipment.tracking_url, "label_url": shipment.label_url}


@router.post("/orders/{order_id}/label")
async def retrieve_label(order_id: int, request: Request, db: Session = Depends(get_db),
                         _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    order = _order(db, tenant_id, order_id)
    shipment = _shipment(db, tenant_id, order.id)
    environment = (shipment.extra_metadata or {}).get("environment")
    _egress_allowed(environment, tenant_id)
    conn = _connection(db, tenant_id, environment)
    try:
        result = await _client(conn).print_awb(oto_order_id(tenant_id, order.id))
    except OtoApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    url = safe_url(result.get("url") or result.get("printAWBURL") or result.get("awbUrl"))
    if not url:
        raise HTTPException(502, "oto_label_url_unavailable")
    shipment.label_url = url
    db.commit()
    return {"label_url": url}


@router.post("/orders/{order_id}/send-whatsapp")
async def send_shipping_whatsapp(order_id: int, request: Request, db: Session = Depends(get_db),
                                 _user: dict = Depends(require_merchant_scope)):
    """Merchant-initiated document + tracking notice in an open WA service window."""
    from core.wa_usage import has_open_service_window
    from core.wa_notify import _normalize_phone
    from models import WhatsAppConnection
    from routers.whatsapp_webhook import _post_wa

    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    if _user.get("impersonation"):
        raise HTTPException(403, "oto_notice_requires_merchant")
    order = _order(db, tenant_id, order_id, lock=True)
    shipment = _shipment(db, tenant_id, order.id)
    if not shipment.label_url or not safe_url(shipment.label_url):
        raise HTTPException(409, "oto_label_not_available")
    if not shipment.tracking_number and not shipment.tracking_url:
        raise HTTPException(409, "oto_tracking_not_available")
    if shipment.status.lower() in {"returned", "cancelled", "canceled", "oto_cancellation_requested",
                                   "oto_cancellation_needs_reconciliation"}:
        raise HTTPException(409, "oto_shipment_not_deliverable")
    info = order.customer_info if isinstance(order.customer_info, dict) else {}
    recipient = _normalize_phone(str(info.get("mobile") or info.get("phone") or ""))
    if not recipient or not has_open_service_window(db, tenant_id, recipient):
        raise HTTPException(409, "whatsapp_service_window_closed")
    connection = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id, status="connected",
                                                         sending_enabled=True).first()
    if not connection or not connection.phone_number_id:
        raise HTTPException(409, "merchant_whatsapp_unavailable")
    meta = dict(shipment.extra_metadata or {})
    if meta.get("customer_wa_notice_state") in {"sending", "accepted", "unknown"}:
        raise HTTPException(409, "oto_whatsapp_notice_already_attempted")
    meta["customer_wa_notice_state"] = "sending"
    shipment.extra_metadata = meta
    db.commit()
    caption = "بوليصة شحنتك من نحلة"
    if shipment.tracking_number:
        caption += f"\nرقم التتبع: {shipment.tracking_number}"
    if shipment.tracking_url:
        caption += f"\nالتتبع: {shipment.tracking_url}"
    message = {"messaging_product": "whatsapp", "to": recipient, "type": "document",
               "document": {"link": shipment.label_url, "filename": f"AWB-{order.id}.pdf",
                            "caption": caption[:1024]}}
    try:
        sent = await _post_wa(connection.phone_number_id, message, _tenant_id=tenant_id, _db=db,
                              _allow_manual=True, _blocked_path="oto_shipping_notice")
    except Exception:
        meta["customer_wa_notice_state"] = "unknown"
        shipment.extra_metadata = meta
        db.commit()
        raise HTTPException(502, "whatsapp_notice_status_unknown") from None
    meta["customer_wa_notice_state"] = "accepted" if sent else "failed"
    shipment.extra_metadata = meta
    db.commit()
    return {"accepted_by_whatsapp": sent, "delivered_to_customer": False}


@router.post("/orders/{order_id}/cancel")
async def cancel_shipment(order_id: int, request: Request, db: Session = Depends(get_db),
                          _user: dict = Depends(require_merchant_scope)):
    _require_integration_enabled()
    tenant_id = resolve_tenant_id(request)
    if _user.get("impersonation"):
        raise HTTPException(403, "oto_cancellation_requires_merchant")
    order = _order(db, tenant_id, order_id, lock=True)
    shipment = _shipment(db, tenant_id, order.id)
    if (shipment.extra_metadata or {}).get("cancellation_attempted"):
        raise HTTPException(409, "oto_cancellation_already_attempted")
    if shipment.status.lower() in {"oto_cancellation_requested", "oto_cancellation_needs_reconciliation",
                                   "cancelled", "canceled"}:
        raise HTTPException(409, "oto_cancellation_already_attempted")
    if shipment.status.lower() in {"pickedup", "outfordelivery", "delivered", "returned"}:
        raise HTTPException(409, "oto_cancellation_too_late")
    if not shipment.external_shipment_id:
        raise HTTPException(409, "oto_shipment_id_unavailable")
    environment = (shipment.extra_metadata or {}).get("environment")
    _egress_allowed(environment, tenant_id)
    conn = _connection(db, tenant_id, environment)
    meta = dict(shipment.extra_metadata or {})
    meta["cancellation_attempted"] = True
    shipment.extra_metadata = meta
    shipment.status = "oto_cancellation_needs_reconciliation"
    db.commit()  # A timeout may mean OTO accepted cancellation; block automatic retries.
    try:
        await _client(conn).cancel_shipment(oto_order_id(tenant_id, order.id), shipment.external_shipment_id)
    except OtoApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    shipment.status = "oto_cancellation_requested"
    db.commit()
    return {"status": shipment.status, "confirmed": False}


@router.post("/webhooks/{environment}/{event_type}")
async def oto_webhook(environment: Literal["staging", "production"],
                      event_type: Literal["orderStatus", "shipmentError"], request: Request,
                      db: Session = Depends(get_db)):
    if not oto_integration_enabled():
        # Switched off: the endpoint does not exist. Nothing is read, parsed or looked up.
        raise HTTPException(404, "not_found")
    raw = await _read_bounded_body(request)
    try:
        payload: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "invalid_json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid_payload")
    identity = parse_oto_order_id(payload.get("orderId"))
    if identity is None:
        raise HTTPException(400, "invalid_order_id")
    tenant_id, order_id = identity
    conn = db.query(OtoConnection).filter_by(tenant_id=tenant_id, environment=environment, enabled=True).first()
    if conn is None or not conn.webhook_secret_ciphertext:
        raise HTTPException(401, "oto_webhook_unconfigured")
    if not verify_webhook(payload, secret=decrypt_secret(conn.webhook_secret_ciphertext), event_type=event_type):
        raise HTTPException(401, "oto_webhook_invalid_signature")
    order = _order(db, tenant_id, order_id, lock=True)
    shipment = _shipment(db, tenant_id, order.id)
    if (shipment.extra_metadata or {}).get("environment") != environment:
        raise HTTPException(404, "oto_shipment_not_found")
    fingerprint = webhook_fingerprint(payload, event_type)
    meta = dict(shipment.extra_metadata or {})
    if fingerprint in meta.get("webhook_fingerprints", []):
        return {"ok": True, "duplicate": True}
    changed = apply_oto_status(order, shipment, payload, event_type=event_type)
    if changed:
        meta["webhook_fingerprints"] = (meta.get("webhook_fingerprints", []) + [fingerprint])[-25:]
        shipment.extra_metadata = meta
        db.commit()
    return {"ok": True, "applied": changed}
