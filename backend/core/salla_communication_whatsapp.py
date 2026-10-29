"""Fail-closed Salla Communication WhatsApp order-status delivery.

The Salla envelope is already signature-checked and durably persisted by the
webhook router. A committed attempt marker means a reclaimed worker may not
send again: Meta does not offer an idempotency key for this endpoint.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _error_code(value: Any) -> str:
    code = re.sub(r"[^a-z0-9_]", "_", str(value or "provider_error").lower())[:64]
    return code or "provider_error"


def inbox_event_type(event_type: str | None, headers: Mapping[str, Any]) -> str | None:
    """A public Salla webhook is observation-only, even with forged headers."""
    return "communication.whatsapp.observed" if event_type == EVENT else event_type


def record_meta_delivery_status(db: Session, *, message_id: str, status: str) -> tuple[bool, bool]:
    """Correlate a verified Meta status with this pilot's accepted send.

    Returns (matched, changed). The caller owns the transaction. A later
    status cannot downgrade read/delivered, and no status triggers a send.
    """
    from models import WebhookEvent

    if not message_id or status not in {"delivered", "read", "failed"}:
        return False, False
    event = (
        db.query(WebhookEvent)
        .filter(
            WebhookEvent.provider == "salla_function",
            WebhookEvent.event_type == EVENT,
            WebhookEvent.communication_provider_message_id == message_id,
        )
        .one_or_none()
    )
    if event is None:
        return False, False
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
    """Send once, or leave a visible failure without pretending delivery."""
    from models import Order, WhatsAppConnection
    from core.commerce_lifecycle.canary_guard import (
        commerce_lifecycle_dispatch_tenant_permitted,
    )
    from core.commerce_lifecycle.window import lifecycle_service_window_is_open
    from services.whatsapp_platform.service import provider_send_message
    from utils.phone_utils import normalize_to_e164

    if (
        event.provider != "salla_function"
        or event.signature_valid is not True
        or event.event_type != EVENT
    ):
        raise RuntimeError("unverified_communication_source")
    if not event.external_event_id:
        raise RuntimeError("communication_identity_missing")
    if str(event.store_id or "") != str(store_id or "") or not store_id:
        raise RuntimeError("communication_store_mismatch")
    state = str(event.communication_send_state or "")
    if state in {"accepted", "delivered", "read"} and event.communication_provider_message_id:
        return  # completion-record retry; no second Meta call
    if state in {"sending", "ambiguous", "failed", "blocked", "delivery_failed"}:
        raise RuntimeError(f"communication_send_{state}_requires_reconciliation")
    if state:
        raise RuntimeError("unknown_communication_send_state")

    # Interpret exactly the body whose HMAC was checked, not a separately
    # editable parsed_payload column or a derived event label.
    notice = parse_notice(json.loads(event.raw_body))
    if not _enabled_for(tenant_id=tenant_id, store_id=store_id, recipient=notice.recipient):
        raise RuntimeError("communication_pilot_not_enabled_for_target")
    if commerce_lifecycle_dispatch_tenant_permitted(tenant_id):
        raise RuntimeError("communication_lifecycle_sender_conflict")

    order = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
            Order.source == "salla",
            Order.integration_connection_id == integration_connection_id,
            Order.external_id == notice.order_external_id,
        )
        .one_or_none()
    )
    if order is None:
        raise RuntimeError("communication_order_not_verified")
    info = order.customer_info if isinstance(order.customer_info, dict) else {}
    order_phone = normalize_to_e164(info.get("mobile") or info.get("phone"))
    if not order_phone or order_phone != notice.recipient:
        raise RuntimeError("communication_order_recipient_mismatch")

    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).one_or_none()
    if (
        conn is None or conn.status != "connected" or not conn.sending_enabled
        or conn.provider != "meta" or not conn.phone_number_id
    ):
        raise RuntimeError("communication_whatsapp_connection_unavailable")
    window_open, _ = lifecycle_service_window_is_open(db, tenant_id, notice.recipient)
    if not window_open:
        # Salla content is not an approved Meta template. Sending it outside
        # the customer service window would violate the provider policy.
        raise RuntimeError("communication_approved_template_required")

    # Irreversible boundary: reserve this event with a conditional database
    # update and COMMIT before Meta. A concurrent worker cannot replace a
    # live reservation; a crash or unknown provider result cannot resend.
    from sqlalchemy import update
    from models import WebhookEvent

    claimed = db.execute(
        update(WebhookEvent)
        .where(
            WebhookEvent.id == event.id,
            WebhookEvent.communication_send_state.is_(None),
        )
        .values(
            communication_send_state="sending",
            communication_send_attempted_at=datetime.now(timezone.utc),
        )
        .returning(WebhookEvent.id)
    ).scalar_one_or_none()
    if claimed is None:
        db.rollback()
        raise RuntimeError("communication_send_already_reserved")
    db.commit()
    db.refresh(event)
    try:
        response, _ = await provider_send_message(
            db,
            conn,
            tenant_id=tenant_id,
            operation="salla_communication_whatsapp",
            phone_id=str(conn.phone_number_id),
            payload={
                "messaging_product": "whatsapp",
                "to": notice.recipient,
                "type": "text",
                "text": {"body": notice.content, "preview_url": False},
            },
            blocked_path="salla_communication_whatsapp",
        )
    except Exception as exc:
        db.rollback()
        event.communication_send_state = "ambiguous"
        event.communication_send_error_code = "provider_outcome_unknown"
        db.add(event)
        db.commit()
        raise RuntimeError("communication_provider_outcome_unknown") from exc

    messages = response.get("messages") if isinstance(response, dict) else None
    message_id = (
        str(messages[0].get("id") or "")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict)
        else ""
    )
    if message_id:
        # A provider message ID proves acceptance, not handset delivery.
        event.communication_send_state = "accepted"
        event.communication_provider_message_id = message_id[:128]
        event.communication_send_error_code = None
        db.add(event)
        db.commit()
        return

    # A structured provider rejection is known not to be accepted; an empty
    # or malformed response is ambiguous. Neither is retried automatically.
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict):
        event.communication_send_state = "failed"
        event.communication_send_error_code = _error_code(error.get("code"))
    else:
        event.communication_send_state = "ambiguous"
        event.communication_send_error_code = "provider_response_without_message_id"
    db.add(event)
    db.commit()
    raise RuntimeError("communication_provider_send_not_confirmed")
