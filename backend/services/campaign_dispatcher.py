"""
services/campaign_dispatcher.py
────────────────────────────────
Bulk campaign dispatch: iterate the audience and send a WhatsApp
template message to each customer.

Called from:
  - POST /campaigns (when schedule_type == "immediate")
  - PUT  /campaigns/{id}/status (when status → "active")
  - The scheduler loop (for schedule_type == "scheduled" / "delayed")
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

_THIS = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_THIS, ".."))
_DB = os.path.abspath(os.path.join(_BACKEND, "..", "database"))
for _p in (_BACKEND, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Campaign, Customer, WhatsAppConnection, WhatsAppTemplate

logger = logging.getLogger("nahla-backend")

INTER_MESSAGE_DELAY = 1.5


async def dispatch_campaign(db: Session, campaign_id: int) -> Dict[str, Any]:
    """Send a campaign's template to every reachable customer in its audience.

    Returns a summary dict: {sent, failed, skipped, errors}.
    """
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return {"sent": 0, "failed": 0, "skipped": 0, "errors": ["Campaign not found"]}

    tenant_id = campaign.tenant_id
    template = _load_template(db, campaign)
    if not template:
        campaign.status = "completed"
        db.commit()
        return {"sent": 0, "failed": 0, "skipped": 0,
                "errors": ["Template not found or not APPROVED"]}

    wa_conn = _get_wa_connection(db, tenant_id)
    if not wa_conn:
        return {"sent": 0, "failed": 0, "skipped": 0,
                "errors": ["No active WhatsApp connection"]}

    customers = _resolve_audience(db, tenant_id, campaign.audience_type)
    if not customers:
        campaign.status = "completed"
        db.commit()
        return {"sent": 0, "failed": 0, "skipped": 0,
                "errors": ["No reachable customers in this segment"]}

    campaign.status = "active"
    if not campaign.launched_at:
        campaign.launched_at = datetime.now(timezone.utc)
    campaign.audience_count = len(customers)
    db.commit()

    tpl_vars = campaign.template_variables or {}
    auto_coupon = tpl_vars.get("_auto_coupon") == "true"
    discount_pct_raw = tpl_vars.get("_discount_percent")
    discount_pct = int(discount_pct_raw) if discount_pct_raw else None

    store_name = _resolve_store_name(db, tenant_id)

    sent = 0
    failed = 0
    skipped = 0
    errors: List[str] = []

    for i, customer in enumerate(customers):
        phone = getattr(customer, "normalized_phone", None) or ""
        if not phone:
            skipped += 1
            continue

        try:
            coupon_code = ""
            if auto_coupon and discount_pct:
                coupon_code = await _get_auto_coupon(
                    db, tenant_id, customer, discount_pct,
                )

            payload = _build_send_payload(
                template=template,
                to_phone=phone,
                customer_name=customer.name or "العميل",
                store_name=store_name,
                coupon_code=coupon_code,
            )

            from services.whatsapp_platform.service import provider_send_message
            response, _ctx = await provider_send_message(
                db,
                wa_conn,
                tenant_id=tenant_id,
                operation="campaign_send",
                phone_id=wa_conn.phone_number_id,
                payload=payload,
            )

            resp = response or {}
            meta_err = resp.get("error") if isinstance(resp, dict) else None
            if meta_err:
                failed += 1
                err_msg = meta_err.get("message", "Unknown Meta error")
                if len(errors) < 5:
                    errors.append(f"{phone}: {err_msg[:100]}")
            else:
                sent += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 5:
                errors.append(f"{phone}: {str(exc)[:100]}")

        campaign.sent_count = sent
        if i % 10 == 0:
            db.commit()

        if i < len(customers) - 1:
            await asyncio.sleep(INTER_MESSAGE_DELAY)

    campaign.sent_count = sent
    campaign.status = "completed"
    campaign.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        "[campaign_dispatcher] campaign=%d tenant=%d sent=%d failed=%d skipped=%d total=%d",
        campaign_id, tenant_id, sent, failed, skipped, len(customers),
    )
    return {"sent": sent, "failed": failed, "skipped": skipped, "errors": errors}


def _load_template(db: Session, campaign: Campaign) -> Optional[WhatsAppTemplate]:
    try:
        tpl_id = int(campaign.template_id)
    except (TypeError, ValueError):
        return None
    tpl = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.id == tpl_id,
            WhatsAppTemplate.tenant_id == campaign.tenant_id,
        )
        .first()
    )
    if tpl and (tpl.status or "").upper() == "APPROVED":
        return tpl
    return None


def _get_wa_connection(db: Session, tenant_id: int) -> Optional[Any]:
    return (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.tenant_id == tenant_id,
            WhatsAppConnection.status == "connected",
        )
        .first()
    )


def _resolve_audience(
    db: Session, tenant_id: int, audience_type: str,
) -> List[Customer]:
    from services.nahla_segments import build_segment_query
    q = build_segment_query(audience_type, db, tenant_id, require_reachable=True)
    if q is None:
        q = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tenant_id,
                Customer.normalized_phone.isnot(None),
                Customer.normalized_phone != "",
            )
        )
    return q.all()


def _resolve_store_name(db: Session, tenant_id: int) -> str:
    try:
        from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_STORE
        settings = get_or_create_settings(db, tenant_id)
        store = merge_defaults(settings.store_settings, DEFAULT_STORE)
        return store.get("store_name", "") or "المتجر"
    except Exception:
        return "المتجر"


def _build_send_payload(
    *,
    template: WhatsAppTemplate,
    to_phone: str,
    customer_name: str,
    store_name: str,
    coupon_code: str = "",
) -> Dict[str, Any]:
    import re
    body = ""
    for c in (template.components or []):
        if (c.get("type") or "").upper() == "BODY":
            body = c.get("text", "") or ""
            break

    placeholders = sorted(
        set(re.findall(r"\{\{\d+\}\}", body)),
        key=lambda s: int(s.strip("{}")),
    )

    slot_values = {
        "{{1}}": customer_name,
        "{{2}}": store_name,
        "{{3}}": coupon_code or store_name,
        "{{4}}": store_name,
        "{{5}}": coupon_code or "",
        "{{6}}": store_name,
    }

    body_params: List[Dict[str, str]] = []
    for ph in placeholders:
        val = slot_values.get(ph, "")
        body_params.append({"type": "text", "text": str(val) or " "})

    components: List[Dict[str, Any]] = []
    if body_params:
        components.append({"type": "body", "parameters": body_params})

    for c in (template.components or []):
        if str(c.get("type") or "").upper() != "BUTTONS":
            continue
        for idx, btn in enumerate(c.get("buttons") or []):
            if str(btn.get("type") or "").upper() != "URL":
                continue
            url_tpl = btn.get("url") or ""
            if "{{1}}" in url_tpl:
                components.append({
                    "type": "button",
                    "sub_type": "url",
                    "index": str(idx),
                    "parameters": [{"type": "text", "text": "shop"}],
                })

    return {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template.name,
            "language": {"code": template.language or "ar"},
            "components": components,
        },
    }


async def _get_auto_coupon(
    db: Session,
    tenant_id: int,
    customer: Customer,
    discount_pct: int,
) -> str:
    try:
        from services.coupon_generator import CouponGeneratorService
        svc = CouponGeneratorService(db, tenant_id)
        segment = getattr(customer, "customer_status", None) or "active"
        coupon = svc.pick_coupon_for_segment(segment)
        if coupon:
            return coupon.code or ""
        coupon = await svc.create_on_demand(segment, discount_pct)
        if coupon:
            return coupon.code or ""
    except Exception as exc:
        logger.warning(
            "[campaign_dispatcher] auto-coupon failed tenant=%d customer=%d: %s",
            tenant_id, customer.id, exc,
        )
    return ""
