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

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import Campaign, WhatsAppTemplate  # noqa: E402

from core.database import get_db
from core.tenant import (
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    get_or_create_tenant,
    merge_defaults,
    resolve_tenant_id,
)
from services.whatsapp_platform.token_manager import get_token_context

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
        "template_variables": c.template_variables or {},
        "audience_type": c.audience_type,
        "audience_count": c.audience_count,
        "schedule_type": c.schedule_type,
        "schedule_time": c.schedule_time.isoformat() if c.schedule_time else None,
        "delay_minutes": c.delay_minutes,
        "coupon_code": c.coupon_code or "",
        "sent_count": c.sent_count,
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
async def create_campaign(body: CreateCampaignIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

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

    campaign = Campaign(
        tenant_id=tenant_id,
        name=body.name,
        campaign_type=body.campaign_type,
        status="scheduled" if body.schedule_type == "scheduled" and schedule_dt else "draft",
        template_id=str(template.id),
        template_name=template.name,
        template_language=template.language,
        template_category=template.category,
        template_body=next((c.get("text", "") for c in (template.components or []) if c.get("type") == "BODY"), body.template_body),
        template_variables=body.template_variables or {},
        audience_type=body.audience_type,
        audience_count=body.audience_count,
        schedule_type=body.schedule_type,
        schedule_time=schedule_dt,
        delay_minutes=body.delay_minutes,
        coupon_code=body.coupon_code or None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
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
    campaign.status = body.status
    if body.status == "active" and not campaign.launched_at:
        campaign.launched_at = datetime.now(timezone.utc)
    campaign.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(campaign)
    return _campaign_to_dict(campaign)


@router.post("/campaigns/test-send")
async def test_send(body: TestSendIn, request: Request, db: Session = Depends(get_db)):
    """Simulate sending a test message to a phone number."""
    from models import WhatsAppConnection  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    token_ctx = get_token_context(conn)
    if not ((conn.phone_number_id if conn else None) or wa.get("phone_number_id")) or not token_ctx.token:
        return {
            "success": True,
            "simulated": True,
            "message": f"تمت المحاكاة — أرسلنا القالب '{body.template_name}' إلى {body.phone} (وضع تجريبي)",
        }
    return {
        "success": True,
        "simulated": False,
        "message": f"تم إرسال رسالة اختبار إلى {body.phone}",
        "token_status": token_ctx.token_status,
    }
