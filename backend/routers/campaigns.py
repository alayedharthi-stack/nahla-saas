"""
routers/campaigns.py
─────────────────────
Campaign management endpoints.

Routes:
  GET  /campaigns                   — list all campaigns
  POST /campaigns                   — create a new campaign
  PUT  /campaigns/{id}/status       — update campaign status
  POST /campaigns/test-send         — simulate a test message send
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import Campaign, WhatsAppTemplate  # noqa: E402

from core.database import get_db
from core.tenant import (
    get_or_create_tenant,
    resolve_tenant_id,
)

logger = logging.getLogger("nahla-backend")

router = APIRouter()


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class CreateCampaignIn(BaseModel):
    name: str
    campaign_type: str
    template_id: str
    template_name: str
    template_language: str = "ar"
    template_category: str = "MARKETING"
    template_body: str = ""
    template_variables: Optional[Dict[str, str]] = None
    audience_type: str = "all"
    audience_count: int = 0
    schedule_type: str = "immediate"
    schedule_time: Optional[str] = None
    delay_minutes: Optional[int] = None
    coupon_code: str = ""
    discount_percent: Optional[int] = None
    auto_coupon: bool = False


class UpdateCampaignStatusIn(BaseModel):
    status: str  # active | paused | completed


class TestSendIn(BaseModel):
    phone: str
    template_id: str
    template_name: str
    template_language: str = "ar"
    variables: Dict[str, str] = {}


# ── Helper functions ───────────────────────────────────────────────────────────

def _campaign_to_dict(c: Campaign) -> Dict[str, Any]:
    tpl_vars = c.template_variables or {}
    auto_coupon = tpl_vars.get("_auto_coupon") == "true"
    discount_pct_raw = tpl_vars.get("_discount_percent")
    discount_pct = int(discount_pct_raw) if discount_pct_raw else None

    failed_count = int(tpl_vars.get("_failed_count", "0") or "0")
    skipped_count = int(tpl_vars.get("_skipped_count", "0") or "0")
    raw_errors = tpl_vars.get("_dispatch_errors", "") or ""
    dispatch_errors = [e for e in raw_errors.split("|") if e] if raw_errors else []

    return {
        "id": c.id,
        "name": c.name,
        "campaign_type": c.campaign_type,
        "status": c.status,
        "template_id": c.template_id,
        "template_name": c.template_name,
        "template_language": c.template_language,
        "template_category": c.template_category,
        "template_status": getattr(c, "template_status", None) or "APPROVED",
        "template_body": c.template_body,
        "template_variables": {k: v for k, v in tpl_vars.items() if not k.startswith("_")},
        "audience_type": c.audience_type,
        "audience_count": c.audience_count,
        "schedule_type": c.schedule_type,
        "schedule_time": c.schedule_time.isoformat() if c.schedule_time else None,
        "delay_minutes": c.delay_minutes,
        "coupon_code": c.coupon_code or "",
        "auto_coupon": auto_coupon,
        "discount_percent": discount_pct,
        "sent_count": c.sent_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "dispatch_errors": dispatch_errors,
        "delivered_count": c.delivered_count,
        "read_count": c.read_count,
        "clicked_count": c.clicked_count,
        "converted_count": c.converted_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "launched_at": c.launched_at.isoformat() if c.launched_at else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/campaigns")
async def list_campaigns(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.tenant_id == tenant_id)
        .order_by(Campaign.created_at.desc())
        .all()
    )
    return {"campaigns": [_campaign_to_dict(c) for c in campaigns]}


@router.post("/campaigns")
async def create_campaign(
    body: CreateCampaignIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    from core.billing import require_billing_access  # noqa: PLC0415
    require_billing_access(db, tenant_id)

    try:
        template_db_id = int(body.template_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="template_id غير صالح")

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_db_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    if template.status != "APPROVED":
        raise HTTPException(
            status_code=422,
            detail="لا يمكن إنشاء حملة إلا باستخدام قالب معتمد من Meta",
        )

    schedule_dt = None
    if body.schedule_time:
        try:
            from datetime import datetime as _dt
            schedule_dt = _dt.fromisoformat(body.schedule_time)
        except ValueError:
            pass

    tpl_vars = dict(body.template_variables or {})
    if body.auto_coupon:
        tpl_vars["_auto_coupon"] = "true"
    if body.discount_percent is not None and body.discount_percent > 0:
        tpl_vars["_discount_percent"] = str(body.discount_percent)

    is_immediate = body.schedule_type == "immediate"
    campaign = Campaign(
        tenant_id=tenant_id,
        name=body.name,
        campaign_type=body.campaign_type,
        status="active" if is_immediate else ("scheduled" if body.schedule_type == "scheduled" and schedule_dt else "draft"),
        template_id=str(template.id),
        template_name=template.name,
        template_language=template.language,
        template_category=template.category,
        template_body=next((c.get("text", "") for c in (template.components or []) if c.get("type") == "BODY"), body.template_body),
        template_variables=tpl_vars,
        audience_type=body.audience_type,
        audience_count=body.audience_count,
        schedule_type=body.schedule_type,
        schedule_time=schedule_dt,
        delay_minutes=body.delay_minutes,
        coupon_code=body.coupon_code or None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        launched_at=datetime.now(timezone.utc) if is_immediate else None,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)

    if is_immediate:
        asyncio.create_task(_dispatch_campaign_async(campaign.id))

    return _campaign_to_dict(campaign)


@router.put("/campaigns/{campaign_id}/status")
async def update_campaign_status(
    campaign_id: int,
    body: UpdateCampaignStatusIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.tenant_id == tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    was_not_active = campaign.status != "active"
    campaign.status = body.status
    if body.status == "active" and not campaign.launched_at:
        campaign.launched_at = datetime.now(timezone.utc)
    campaign.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(campaign)

    if body.status == "active" and was_not_active:
        asyncio.create_task(_dispatch_campaign_async(campaign.id))

    return _campaign_to_dict(campaign)


@router.get("/campaigns/debug-template/{template_id}")
async def debug_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    """Diagnostic endpoint: shows raw template components, the generated
    payload, and any validation issues. Returns JSON with no-cache headers."""
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    from services.campaign_dispatcher import (  # noqa: PLC0415
        _build_send_payload,
        _button_needs_param,
        _extract_param_count,
        _example_param_count,
        validate_template_payload,
    )

    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    payload = _build_send_payload(
        template=tpl,
        to_phone="966500000000",
        customer_name="أحمد",
        store_name="المتجر",
        coupon_code="SAVE10",
    )

    validation_issues = validate_template_payload(tpl, coupon_code="SAVE10")

    tpl_types = [(c.get("type") or "?") for c in (tpl.components or [])]

    button_analysis: List[Dict[str, Any]] = []
    for comp in (tpl.components or []):
        if (comp.get("type") or "").upper() == "BUTTONS":
            for idx, btn in enumerate(comp.get("buttons") or []):
                button_analysis.append({
                    "index": idx,
                    "type": btn.get("type"),
                    "needs_param": _button_needs_param(btn),
                    "url": btn.get("url"),
                    "example": btn.get("example"),
                })

    notes: List[str] = []
    for comp in (tpl.components or []):
        ct = (comp.get("type") or "").upper()
        if ct == "BODY":
            n = _extract_param_count(comp.get("text") or "") or _example_param_count(comp, "body_text")
            notes.append(f"BODY needs {n} text params")
        if ct == "HEADER" and (comp.get("format") or "").upper() == "TEXT":
            n = _extract_param_count(comp.get("text") or "") or _example_param_count(comp, "header_text")
            if n > 0:
                notes.append(f"HEADER needs {n} text params")

    result = {
        "template_id": tpl.id,
        "template_name": tpl.name,
        "language": tpl.language,
        "status": tpl.status,
        "tpl_types": tpl_types,
        "raw_components": tpl.components,
        "button_analysis": button_analysis,
        "generated_payload": payload,
        "payload_comps": payload.get("template", {}).get("components", []),
        "validation_issues": validation_issues,
        "notes": notes,
    }
    return JSONResponse(
        content=result,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/campaigns/test-send")
async def test_send(body: TestSendIn, request: Request, db: Session = Depends(get_db)):
    """Send a real test WhatsApp message for the chosen template.

    Backwards-compatible wrapper around the wizard's `send_test_message`
    so the existing frontend (which still posts to /campaigns/test-send
    with the old payload shape) keeps working while the new wizard
    moves over to /campaigns/wizard/test-send.

    Behaviour change vs. the previous stub:
      * Previously this endpoint only *simulated* a send and always
        returned success. That made the merchant think the campaign
        worked when nothing had actually been delivered to WhatsApp.
      * Now it delegates to the same provider_send_message pipeline
        used by every other transactional template send (COD, cart
        recovery, etc.). When no live WA connection exists it still
        returns `simulated=True` so dev/sandbox UX is preserved, but
        when a connection is live the message is really sent.
    """
    from services.campaign_wizard.test_send import send_test_message  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()

    try:
        template_db_id = int(body.template_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="template_id غير صالح")

    result = await send_test_message(
        db,
        tenant_id=tenant_id,
        template_db_id=template_db_id,
        to_phone=body.phone,
        merchant_vars=body.variables or {},
    )

    # Reshape into the legacy `{success, simulated, message}` envelope
    # the existing frontend expects. The wizard endpoint returns the
    # richer schema directly; only this legacy route does this mapping.
    if result["sent"]:
        if result["simulated"]:
            msg = result["error_message"] or f"تمت المحاكاة — أرسلنا قالب الاختبار إلى {result['to']}"
        else:
            msg = f"تم إرسال رسالة اختبار إلى {result['to']}"
        return {"success": True, "simulated": result["simulated"], "message": msg}
    raise HTTPException(
        status_code=400,
        detail=result["error_message"] or "فشل إرسال رسالة الاختبار",
    )


# ── Campaign dispatch (background) ──────────────────────────────────────────

async def _dispatch_campaign_async(campaign_id: int) -> None:
    """Fire-and-forget async task that dispatches a campaign using a fresh
    DB session.  Runs on the main uvicorn event loop via asyncio.create_task,
    so all async HTTP calls (provider_send_message -> httpx) work natively."""
    from core.database import SessionLocal  # noqa: PLC0415
    from services.campaign_dispatcher import dispatch_campaign  # noqa: PLC0415

    logger.info("[campaigns] dispatching campaign=%d in async task", campaign_id)
    db = SessionLocal()
    try:
        result = await dispatch_campaign(db, campaign_id)
        logger.info(
            "[campaigns] dispatch done campaign=%d: sent=%s failed=%s errors=%s",
            campaign_id, result.get("sent"), result.get("failed"),
            result.get("errors", [])[:3],
        )
    except Exception as exc:
        logger.error(
            "[campaigns] dispatch failed campaign=%d: %s",
            campaign_id, exc, exc_info=True,
        )
        try:
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if campaign and campaign.status not in ("completed", "failed"):
                campaign.status = "failed"
                tpl_vars = dict(campaign.template_variables or {})
                tpl_vars["_dispatch_errors"] = f"خطأ داخلي: {str(exc)[:200]}"
                campaign.template_variables = tpl_vars
                flag_modified(campaign, "template_variables")
                db.commit()
        except Exception:
            logger.error("[campaigns] could not mark campaign=%d as failed", campaign_id, exc_info=True)
    finally:
        db.close()
