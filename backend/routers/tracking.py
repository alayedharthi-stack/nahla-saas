"""
routers/tracking.py
────────────────────
Storefront event tracking endpoint (receives events from the Nahla JS snippet).

Routes
  POST /track/event
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import AutomationEvent, Customer  # noqa: E402

from core.automation_triggers import AutomationTrigger
from core.database import get_db
from core.tenant import get_or_create_tenant
from services.customer_intelligence import CustomerIntelligenceService, normalize_phone

logger = logging.getLogger("nahla-backend")

router = APIRouter(prefix="/track", tags=["Tracking"])


class StorefrontEventIn(BaseModel):
    event_type: str
    tenant_id:  Optional[str] = None
    store_id:   Optional[str] = None
    payload:    Optional[Dict[str, Any]] = None
    url:        Optional[str] = None
    referrer:   Optional[str] = None
    ts:         Optional[int] = None


@router.post("/event")
async def track_storefront_event(
    body: StorefrontEventIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Receive storefront events from the Nahla snippet.

    Supported event types:
      page_view, product_view, add_to_cart, cart_update,
      begin_checkout, order_created, cart_abandon

    cart_abandon events are forwarded to the autopilot engine
    as abandoned_cart signals for WhatsApp recovery flows.
    """
    raw_tid = body.tenant_id or request.headers.get("X-Tenant-ID", "1")
    try:
        tenant_id = int(raw_tid)
    except (ValueError, TypeError):
        tenant_id = 1

    get_or_create_tenant(db, tenant_id)

    payload = body.payload or {}
    payload["url"]      = body.url
    payload["referrer"] = body.referrer
    payload["store_id"] = body.store_id

    event = AutomationEvent(
        tenant_id=tenant_id,
        event_type=f"storefront_{body.event_type}",
        customer_id=None,
        payload=payload,
        processed=False,
    )
    db.add(event)
    db.commit()

    logger.info(
        "[Snippet] tenant=%s event=%s store=%s url=%s",
        tenant_id, body.event_type, body.store_id, body.url,
    )

    if body.event_type == "cart_abandon":
        customer_phone = payload.get("customer_phone")
        if customer_phone:
            service = CustomerIntelligenceService(db, tenant_id)
            normalized_phone = normalize_phone(customer_phone)
            customer = service.find_customer_by_phone(normalized_phone or customer_phone)
            if customer is None and normalized_phone:
                customer = service.upsert_lead_customer(
                    phone=normalized_phone,
                    source="tracking_lead",
                    extra_metadata={"source": "tracking", "origin_event": "cart_abandon"},
                    commit=True,
                )
            cart_event = AutomationEvent(
                tenant_id=tenant_id,
                event_type=AutomationTrigger.CART_ABANDONED.value,
                customer_id=customer.id if customer else None,
                payload={
                    "source":     "storefront_snippet",
                    "cart_total": payload.get("cart_total"),
                    "items":      payload.get("items"),
                    "phone":      customer_phone,
                    "url":        body.url,
                },
                processed=False,
            )
            db.add(cart_event)
            db.commit()
            logger.info(
                "[Snippet] cart_abandon → %s event tenant=%s phone=%s",
                AutomationTrigger.CART_ABANDONED.value, tenant_id, customer_phone,
            )

    return {"received": True, "event_type": body.event_type}
