"""
routers/ai_playground.py
────────────────────────
AI Playground dry-run preview for merchants.

POST /intelligence/playground/dry-run — stateless inbound reply preview.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from services.ai_playground_dry_run import run_playground_dry_run

router = APIRouter()


class PlaygroundOrderContextBody(BaseModel):
    order_status: str = "shipped"
    order_reference: str = ""
    tracking_number: str = ""
    shipping_provider: str = ""


class PlaygroundDryRunBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    mode: str = "stateless"
    context: Optional[PlaygroundOrderContextBody] = None
    test_phone: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Simulated customer phone for store_ai_mode=test previews.",
    )


@router.post("/intelligence/playground/dry-run")
async def playground_dry_run(
    request: Request,
    body: PlaygroundDryRunBody,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Preview AI inbound reply without WhatsApp send or DB mutations."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    ctx_payload: Optional[Dict[str, Any]] = None
    if body.context is not None:
        ctx_payload = body.context.model_dump()

    result = run_playground_dry_run(
        db,
        tenant_id=tenant_id,
        message=body.message,
        mode=body.mode,
        context=ctx_payload,
        test_phone=body.test_phone,
    )
    return result.to_dict()
