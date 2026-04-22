"""
routers/campaign_wizard.py
──────────────────────────
Endpoints that back the new "smart" campaign creation wizard:

    GET  /campaigns/wizard/goals
    GET  /campaigns/wizard/segments
    GET  /campaigns/wizard/segments/{key}/sample
    GET  /campaigns/wizard/templates
    POST /campaigns/wizard/test-send

All endpoints are tenant-scoped via `resolve_tenant_id`. The router
itself is intentionally thin — every non-trivial decision lives in
`services/campaign_wizard/`.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id

from services.campaign_wizard.goals import get_goal, list_goals
from services.campaign_wizard.recommender import recommend_templates
from services.campaign_wizard.segments import (
    get_segment, list_segments_with_counts, sample_segment, count_segment,
)
from services.campaign_wizard.test_send import send_test_message

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────


class WizardTestSendIn(BaseModel):
    template_id: int = Field(..., description="WhatsAppTemplate.id (int, not the meta id string)")
    to_phone: str = Field(..., min_length=4, max_length=32)
    variables: Dict[str, str] = Field(default_factory=dict)


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/campaigns/wizard/goals")
async def wizard_goals():
    """Step 1: list of selectable campaign goals.

    Static list — no tenant scoping needed. Returned with the same
    Arabic/English labels the frontend renders directly so we don't
    duplicate copy in the React side.
    """
    return {"goals": list_goals()}


@router.get("/campaigns/wizard/segments")
async def wizard_segments(request: Request, db: Session = Depends(get_db)):
    """Step 2: every named segment + the reachable customer count for
    this tenant. Counts are scoped by `tenant_id`."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()
    return {"segments": list_segments_with_counts(db, tenant_id)}


@router.get("/campaigns/wizard/segments/{segment_key}/sample")
async def wizard_segment_sample(
    segment_key: str,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(5, ge=1, le=20),
):
    """Optional preview of the first N customers in a segment, with
    phone/email masked. Used by the merchant to sanity-check who
    they're about to message."""
    tenant_id = resolve_tenant_id(request)
    if get_segment(segment_key) is None:
        raise HTTPException(status_code=404, detail="Unknown segment key")
    return {
        "segment_key":    segment_key,
        "customer_count": count_segment(segment_key, db, tenant_id),
        "sample":         sample_segment(segment_key, db, tenant_id, limit=limit),
    }


@router.get("/campaigns/wizard/templates")
async def wizard_templates(
    request: Request,
    db: Session = Depends(get_db),
    goal: Optional[str] = Query(None),
    segment: Optional[str] = Query(None),
    language: str = Query("ar", min_length=2, max_length=8),
):
    """Step 3: ranked, badged template list for (goal, segment, lang).

    All three filters are optional so the merchant can browse "all
    approved templates" by simply hitting the endpoint without query
    params. When `goal` is provided we additionally validate it is a
    known key — sending an unknown goal is a UI bug, not a 200.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()
    if goal and get_goal(goal) is None:
        raise HTTPException(status_code=422, detail=f"Unknown goal key: {goal}")
    if segment and get_segment(segment) is None:
        raise HTTPException(status_code=422, detail=f"Unknown segment key: {segment}")
    return recommend_templates(
        db, tenant_id=tenant_id,
        goal_key=goal, segment_key=segment, language=language,
    )


@router.post("/campaigns/wizard/test-send")
async def wizard_test_send(
    body: WizardTestSendIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Step 6: actually deliver a single test message via Meta. Returns
    a structured success/failure shape — never raises on send error so
    the wizard stays on-step."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()
    return await send_test_message(
        db,
        tenant_id=tenant_id,
        template_db_id=body.template_id,
        to_phone=body.to_phone,
        merchant_vars=body.variables,
    )
