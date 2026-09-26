"""Persist a lifecycle send in the merchant conversation after Meta accepts it.

The send ledger owns retry/idempotence. This projection records the accepted
wire payload and its provider identity; it never initiates another send.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from core.message_presentation import (
    RESPONSE_BUNDLE_VERSION,
    presentation_from_provider_payload,
)

logger = logging.getLogger("nahla.order_update_timeline")


def persist_accepted_lifecycle_send(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    phone: str,
    customer_name: str,
    service_key: str,
    template_name: str,
    send_method: str,
    provider_message_id: Optional[str],
    wire_payload: Any,
) -> bool:
    """Write one accepted outbound presentation immediately after send finalization.

    A failure here leaves the already-finalized ledger intact and is logged as
    a separate visibility defect. Never retry the WhatsApp send for this.
    """
    if not provider_message_id or not isinstance(wire_payload, Mapping):
        logger.error(
            "[LifecycleTimeline] missing accepted wire evidence tenant=%s order=%s",
            tenant_id, order_id,
        )
        return False

    try:
        from routers.conversations import record_outbound_message  # noqa: PLC0415

        presentation = presentation_from_provider_payload(
            dict(wire_payload), db=db, tenant_id=int(tenant_id),
        )
        body = str((presentation or {}).get("body") or "").strip()
        if not body:
            logger.error(
                "[LifecycleTimeline] empty accepted presentation tenant=%s order=%s",
                tenant_id, order_id,
            )
            return False
        bundle = {
            "version": RESPONSE_BUNDLE_VERSION,
            "presentations": [presentation],
            "delivery": {
                "state": "sent",
                "wamid": provider_message_id,
                "error": None,
            },
        }
        event_id = record_outbound_message(
            db,
            int(tenant_id),
            phone,
            body,
            event_type="automation",
            customer_name=customer_name,
            extra={
                "message_origin": "lifecycle_dispatch",
                "template_name": template_name,
                "order_id": int(order_id),
                "service_key": service_key,
                "send_method": send_method,
                "wa_message_id": provider_message_id,
                "provider_send": {"status": "sent", "wamid": provider_message_id},
                "response_bundle": bundle,
            },
        )
        if not event_id:
            logger.error(
                "[LifecycleTimeline] outbound row failed tenant=%s order=%s",
                tenant_id, order_id,
            )
            return False
        db.commit()
        logger.info(
            "[LifecycleTimeline] recorded tenant=%s order=%s message_event=%s",
            tenant_id, order_id, event_id,
        )
        return True
    except Exception:
        db.rollback()
        logger.exception(
            "[LifecycleTimeline] persistence failed tenant=%s order=%s",
            tenant_id, order_id,
        )
        return False
