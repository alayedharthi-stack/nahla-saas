"""Fail-closed Salla Communication WhatsApp order-status delivery.

The Salla envelope is already signature-checked and durably persisted by the
webhook router. A committed attempt marker means a reclaimed worker may not
send again: Meta does not offer an idempotency key for this endpoint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy.orm import Session

EVENT = "communication.whatsapp.send"
REASON = "order.status.updated"
_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Notice:
    order_external_id: str
    recipient: str
    content: str


def _allowlist(name: str) -> frozenset[str]:
    return frozenset(part.strip() for part in os.getenv(name, "").split(",") if part.strip())


def _enabled_for(*, tenant_id: int, store_id: str, recipient: str) -> bool:
    """Pilot configuration is inert unless all three allowlists are explicit."""
    from utils.phone_utils import normalize_to_e164

    allowed_recipients = frozenset(
        phone for raw in _allowlist("SALLA_COMM_WHATSAPP_RECIPIENTS")
        if (phone := normalize_to_e164(raw))
    )
    return (
        os.getenv("SALLA_COMM_WHATSAPP_ENABLED", "").strip().lower() in _TRUE
        and str(tenant_id) in _allowlist("SALLA_COMM_WHATSAPP_TENANTS")
        and store_id in _allowlist("SALLA_COMM_WHATSAPP_STORES")
        and recipient in allowed_recipients
    )


def parse_notice(payload: Mapping[str, Any]) -> Notice:
    """Only Salla's signed order-status communication shape is in this pilot."""
    from utils.phone_utils import normalize_to_e164

    if payload.get("event") != EVENT:
        raise ValueError("unexpected_communication_event")
    if not isinstance(payload.get("created_at"), str) or not payload["created_at"].strip():
        raise ValueError("missing_communication_event_timestamp")
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("type") != REASON:
        raise ValueError("unexpected_communication_reason")
    entity = data.get("entity")
    if not isinstance(entity, Mapping) or entity.get("type") != "order":
        raise ValueError("invalid_communication_entity")
    order_id = str(entity.get("id") or "").strip()
    if not order_id or len(order_id) > 64:
        raise ValueError("missing_communication_order")
    recipients = data.get("notifiable")
    if not isinstance(recipients, list) or len(recipients) != 1:
        raise ValueError("unexpected_communication_recipients")
    recipient = normalize_to_e164(recipients[0]) if isinstance(recipients[0], str) else None
    if not recipient:
        raise ValueError("invalid_communication_recipient")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 4096:
        raise ValueError("invalid_communication_content")
    # The signed, pre-composed Salla message is kept verbatim, including its
    # language and formatting. No URL is opened and no payload instruction runs.
    return Notice(order_external_id=order_id, recipient=recipient, content=content)


def inbox_event_type(event_type: str | None, headers: Mapping[str, Any]) -> str | None:
    """A public Salla webhook is observation-only, even with forged headers."""
    return "communication.whatsapp.observed" if event_type == EVENT else event_type


def record_meta_delivery_status(
    db: Session, *, message_id: str, status: str, phone_number_id: str,
    receipt_payload: dict, receipt_context: object,
) -> tuple[bool, bool]:
    """Correlate a verified Meta status with this pilot's accepted send.

    Returns (matched, changed). The caller owns the transaction. A later
    status cannot downgrade read/delivered, and no status triggers a send.
    """
    import hashlib
    import hmac
    import json

    from models import WebhookEvent, WhatsAppConnection
    from services.meta_receipt_context import permits_receipt
    from services.salla_communication_receipt_owner import (
        CommunicationReceiptConflict, competing_receipt_owner,
    )
    from utils.phone_utils import normalize_to_e164

    if not message_id or status not in {"delivered", "read", "failed"}:
        return False, False
    if not permits_receipt(receipt_context, receipt_payload, phone_number_id):
        return False, False
    if receipt_payload.get("id") != message_id or receipt_payload.get("status") != status:
        return False, False
    event = (
        db.query(WebhookEvent)
        .filter(
            WebhookEvent.provider == "salla_function",
            WebhookEvent.event_type == EVENT,
            WebhookEvent.signature_valid.is_(True),
            WebhookEvent.communication_provider_message_id == message_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if event is None:
        return False, False
    connection = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.phone_number_id == phone_number_id,
            WhatsAppConnection.provider == "meta",
        )
        .one_or_none()
    )
    if connection is None or connection.tenant_id != event.tenant_id:
        raise CommunicationReceiptConflict("communication_receipt_tenant_mismatch")
    digest = str((event.raw_headers or {}).get("x-nahla-comm-body-sha256") or "")
    if not isinstance(event.raw_body, str) or not hmac.compare_digest(
        digest, hashlib.sha256(event.raw_body.encode("utf-8")).hexdigest()
    ):
        raise CommunicationReceiptConflict("communication_receipt_source_mismatch")
    try:
        notice = parse_notice(json.loads(event.raw_body))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommunicationReceiptConflict("communication_receipt_source_invalid") from exc
    recipient = normalize_to_e164(str(receipt_payload.get("recipient_id") or ""))
    if not recipient or recipient != notice.recipient:
        raise CommunicationReceiptConflict("communication_receipt_recipient_mismatch")
    if competing_receipt_owner(db, message_id):
        raise CommunicationReceiptConflict("communication_receipt_owner_ambiguous")
    current = event.communication_send_state
    next_state = {
        ("accepted", "delivered"): "delivered",
        ("accepted", "read"): "read",
        ("accepted", "failed"): "delivery_failed",
        ("delivered", "read"): "read",
    }.get((current, status))
    if next_state is None:
        return True, False
    event.communication_send_state = next_state
    db.add(event)
    return True, True


async def dispatch_communication_whatsapp(
    db: Session,
    event: Any,
    *,
    tenant_id: int,
    store_id: str,
    integration_connection_id: int,
) -> None:
    """Delegate the full verified send boundary to the canonical engine."""
    from core.automation_engine import send_reserved_salla_communication
    await send_reserved_salla_communication(
        db, event, tenant_id=tenant_id, store_id=store_id,
        integration_connection_id=integration_connection_id,
    )
