"""
routers/handoff.py
───────────────────
Human handoff management endpoints.

Routes
  GET  /handoff/settings
  PUT  /handoff/settings
  GET  /handoff/sessions
  PUT  /handoff/sessions/{session_id}/resolve
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import HandoffSession  # noqa: E402

from core.database import get_db
from core.tenant import get_or_create_settings, get_or_create_tenant, merge_defaults, resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(prefix="/handoff", tags=["Handoff"])

DEFAULT_HANDOFF: Dict[str, Any] = {
    "notification_method": "webhook",   # "webhook" | "whatsapp" | "both" | "none"
    "webhook_url": "",
    "staff_whatsapp": "",
    "auto_pause_ai": True,
}


def get_handoff_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Return handoff config for a tenant, merged with defaults."""
    s = get_or_create_settings(db, tenant_id)
    meta = s.extra_metadata or {}
    return merge_defaults(meta.get("handoff_settings", {}), DEFAULT_HANDOFF)


class HandoffSettingsIn(BaseModel):
    notification_method: str  = "webhook"
    webhook_url:         str  = ""
    staff_whatsapp:      str  = ""
    auto_pause_ai:       bool = True


@router.get("/settings")
async def get_handoff_settings_endpoint(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    return {"settings": get_handoff_settings(db, tenant_id)}


@router.put("/settings")
async def put_handoff_settings_endpoint(
    body: HandoffSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    s = get_or_create_settings(db, tenant_id)
    meta = dict(s.extra_metadata or {})
    meta["handoff_settings"] = body.dict()
    s.extra_metadata = meta
    db.add(s)
    db.commit()
    return {"settings": body.dict()}


@router.get("/sessions")
async def list_handoff_sessions(
    request: Request,
    db: Session = Depends(get_db),
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
):
    """List handoff sessions for the staff queue."""
    tenant_id = resolve_tenant_id(request)
    query = db.query(HandoffSession).filter(HandoffSession.tenant_id == tenant_id)
    if status in ("active", "resolved"):
        query = query.filter(HandoffSession.status == status)
    total = query.count()
    rows = query.order_by(HandoffSession.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "sessions": [
            {
                "id":                r.id,
                "customer_phone":    r.customer_phone,
                "customer_name":     r.customer_name or "—",
                "status":            r.status,
                "handoff_reason":    r.handoff_reason,
                "last_message":      r.last_message,
                "notification_sent": r.notification_sent,
                "resolved_by":       r.resolved_by,
                "resolved_at":       r.resolved_at.isoformat() if r.resolved_at else None,
                "created_at":        r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.put("/sessions/{session_id}/resolve")
async def resolve_handoff_session_endpoint(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Mark a handoff session as resolved and resume AI responses for the customer."""
    tenant_id = resolve_tenant_id(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    resolved_by = body.get("resolved_by", "staff")

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from handoff.manager import resolve_handoff_session  # noqa: PLC0415
    session = resolve_handoff_session(db, session_id, tenant_id, resolved_by)
    if not session:
        raise HTTPException(status_code=404, detail="Handoff session not found")

    from observability.event_logger import log_event  # noqa: PLC0415
    log_event(
        db, tenant_id,
        category="handoff",
        event_type="handoff.resolved",
        summary=f"تم حل التحويل #{session_id} بواسطة {resolved_by}",
        severity="info",
        payload={"session_id": session_id, "resolved_by": resolved_by},
        reference_id=str(session_id),
    )
    db.commit()
    return {
        "session_id":  session.id,
        "status":      session.status,
        "resolved_by": session.resolved_by,
        "resolved_at": session.resolved_at.isoformat() if session.resolved_at else None,
    }
