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
    logger.info(
        "[campaign_dispatcher] starting campaign=%d tenant=%d template_id=%s audience=%s",
        campaign_id, tenant_id, campaign.template_id, campaign.audience_type,
    )

    template = _load_template(db, campaign)
    if not template:
        err = "لم يتم العثور على القالب أو لم تتم الموافقة عليه"
        logger.warning("[campaign_dispatcher] campaign=%d: template not found or not APPROVED (id=%s)", campaign_id, campaign.template_id)
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, [err])
        db.commit()
        return {"sent": 0, "failed": 0, "skipped": 0, "errors": [err]}

    wa_conn = _get_wa_connection(db, tenant_id)
    if not wa_conn:
        err = "لا يوجد اتصال واتساب نشط"
        logger.warning("[campaign_dispatcher] campaign=%d: no active WhatsApp connection", campaign_id)
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, [err])
        db.commit()
        return {"sent": 0, "failed": 0, "skipped": 0, "errors": [err]}

    logger.info("[campaign_dispatcher] campaign=%d: WA conn found phone_id=%s", campaign_id, getattr(wa_conn, 'phone_number_id', '?'))

    customers = _resolve_audience(db, tenant_id, campaign.audience_type)
    if not customers:
        err = "لا يوجد عملاء يمكن الوصول إليهم في هذه الشريحة"
        logger.warning("[campaign_dispatcher] campaign=%d: no reachable customers for segment=%s", campaign_id, campaign.audience_type)
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, [err])
        db.commit()
        return {"sent": 0, "failed": 0, "skipped": 0, "errors": [err]}

    logger.info("[campaign_dispatcher] campaign=%d: found %d customers to send to", campaign_id, len(customers))

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

    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

    for i, customer in enumerate(customers):
        phone = getattr(customer, "normalized_phone", None) or ""
        if not phone:
            skipped += 1
            logger.debug("[campaign_dispatcher] campaign=%d: customer=%d skipped (no phone)", campaign_id, customer.id)
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

            logger.info(
                "[campaign_dispatcher] campaign=%d: sending to %s template=%s payload_components=%s",
                campaign_id, phone, template.name,
                payload.get("template", {}).get("components", []),
            )

            response, _ctx = await provider_send_message(
                db,
                wa_conn,
                tenant_id=tenant_id,
                operation="campaign_send",
                phone_id=wa_conn.phone_number_id,
                payload=payload,
            )

            resp = response or {}
            logger.info(
                "[campaign_dispatcher] campaign=%d: Meta response for %s → %s",
                campaign_id, phone, str(resp)[:500],
            )
            meta_err = resp.get("error") if isinstance(resp, dict) else None
            if meta_err:
                failed += 1
                err_msg = meta_err.get("message", "Unknown Meta error")
                err_code = meta_err.get("code", "")
                err_sub = meta_err.get("error_subcode", "")
                logger.warning(
                    "[campaign_dispatcher] campaign=%d: Meta error for %s code=%s sub=%s msg=%s payload=%s",
                    campaign_id, phone, err_code, err_sub, err_msg,
                    payload.get("template", {}).get("components", []),
                )
                detail = f"{phone}: ({err_code}) {err_msg[:100]}"
                if err_code in (131008, "131008"):
                    sent_comps = payload.get("template", {}).get("components", [])
                    detail += f" [sent {len(sent_comps)} components]"
                if len(errors) < 10:
                    errors.append(detail)
            else:
                sent += 1
                logger.info("[campaign_dispatcher] campaign=%d: sent OK to %s", campaign_id, phone)
        except Exception as exc:
            failed += 1
            logger.error(
                "[campaign_dispatcher] campaign=%d: exception sending to %s: %s",
                campaign_id, phone, exc, exc_info=True,
            )
            if len(errors) < 10:
                errors.append(f"{phone}: {str(exc)[:120]}")

        campaign.sent_count = sent
        if i % 10 == 0:
            db.commit()

        if i < len(customers) - 1:
            await asyncio.sleep(INTER_MESSAGE_DELAY)

    final_status = "completed" if sent > 0 else ("failed" if failed > 0 else "completed")
    campaign.sent_count = sent
    campaign.status = final_status
    campaign.updated_at = datetime.now(timezone.utc)

    _persist_dispatch_result(campaign, sent, failed, skipped, errors)
    db.commit()

    logger.info(
        "[campaign_dispatcher] campaign=%d tenant=%d status=%s sent=%d failed=%d skipped=%d total=%d errors=%s",
        campaign_id, tenant_id, final_status, sent, failed, skipped, len(customers), errors[:3],
    )
    return {"sent": sent, "failed": failed, "skipped": skipped, "errors": errors}


def _persist_dispatch_result(
    campaign: Campaign,
    sent: int,
    failed: int,
    skipped: int,
    errors: List[str],
) -> None:
    """Store dispatch metrics in the campaign's JSONB template_variables
    under private underscore keys so they survive without a migration."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    tpl_vars = dict(campaign.template_variables or {})
    tpl_vars["_failed_count"] = str(failed)
    tpl_vars["_skipped_count"] = str(skipped)
    tpl_vars["_dispatch_errors"] = "|".join(errors[:10]) if errors else ""
    campaign.template_variables = tpl_vars
    flag_modified(campaign, "template_variables")


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

    slot_values = [
        customer_name,
        store_name,
        coupon_code or store_name,
        store_name,
        coupon_code or "",
        store_name,
    ]

    def _extract_param_count(text: str) -> int:
        if not text:
            return 0
        matches = re.findall(r"\{\{(\d+)\}\}", text)
        return max((int(m) for m in matches), default=0)

    def _example_param_count(comp: Dict[str, Any], key: str) -> int:
        """Fallback: derive parameter count from the example field."""
        ex = comp.get("example") or {}
        vals = ex.get(key) or []
        if isinstance(vals, list) and vals:
            inner = vals[0] if isinstance(vals[0], list) else vals
            return len(inner)
        return 0

    def _make_params(count: int) -> List[Dict[str, str]]:
        params: List[Dict[str, str]] = []
        for i in range(count):
            val = slot_values[i] if i < len(slot_values) else store_name
            params.append({"type": "text", "text": str(val).strip() or " "})
        return params

    components: List[Dict[str, Any]] = []

    for comp in (template.components or []):
        ctype = (comp.get("type") or "").upper()
        text = comp.get("text") or ""

        if ctype == "HEADER":
            fmt = (comp.get("format") or "").upper()
            if fmt == "TEXT":
                count = _extract_param_count(text) or _example_param_count(comp, "header_text")
                if count > 0:
                    components.append({"type": "header", "parameters": _make_params(count)})

        elif ctype == "BODY":
            count = _extract_param_count(text) or _example_param_count(comp, "body_text")
            if count > 0:
                components.append({"type": "body", "parameters": _make_params(count)})

        elif ctype == "BUTTONS":
            for idx, btn in enumerate(comp.get("buttons") or []):
                if (btn.get("type") or "").upper() != "URL":
                    continue
                url_tpl = btn.get("url") or ""
                if "{{1}}" in url_tpl:
                    components.append({
                        "type": "button",
                        "sub_type": "url",
                        "index": str(idx),
                        "parameters": [{"type": "text", "text": "shop"}],
                    })

    logger.debug(
        "[_build_send_payload] template=%s to=%s components=%s raw_tpl_components=%s",
        template.name, to_phone, components, template.components,
    )

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
