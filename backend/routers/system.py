"""
routers/system.py
──────────────────
System observability endpoints.

Routes
  GET /system/health
  GET /system/events
  GET /conversations/trace/{customer_phone}
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from models import ConversationTrace, SystemEvent  # noqa: E402

from core.billing import get_moyasar_settings
from core.config import ENVIRONMENT, IS_PRODUCTION, ORCHESTRATOR_URL
from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["System"])


@router.get("/system/health")
async def system_health(request: Request, db: Session = Depends(get_db)):
    """Comprehensive health check for all system components."""
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from observability.health import (  # noqa: PLC0415
        check_database, check_orchestrator, check_moyasar, check_salla, overall_status,
    )
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    moyasar_cfg = get_moyasar_settings(db, tenant_id)

    components = {
        "database":     await check_database(db),
        "orchestrator": await check_orchestrator(ORCHESTRATOR_URL),
        "moyasar":      check_moyasar(moyasar_cfg),
        "salla":        check_salla(tenant_id),
    }

    return {
        "status":      overall_status(components),
        "environment": ENVIRONMENT,
        "production":  IS_PRODUCTION,
        "components":  components,
        "timestamp":   datetime.now(timezone.utc).isoformat() + "Z",
    }


@router.get("/system/events")
async def list_system_events(
    request: Request,
    db: Session = Depends(get_db),
    category: str = "",
    severity: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """Return paginated System Event Timeline for this tenant."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    query = db.query(SystemEvent).filter(SystemEvent.tenant_id == tenant_id)
    if category:
        query = query.filter(SystemEvent.category == category)
    if severity:
        query = query.filter(SystemEvent.severity == severity)

    total = query.count()
    rows = (
        query.order_by(SystemEvent.created_at.desc())
        .offset(offset)
        .limit(min(limit, 200))
        .all()
    )

    return {
        "events": [
            {
                "id":           r.id,
                "category":     r.category,
                "event_type":   r.event_type,
                "severity":     r.severity,
                "summary":      r.summary,
                "reference_id": r.reference_id,
                "payload":      r.payload,
                "created_at":   r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total":  total,
        "offset": offset,
        "limit":  limit,
    }


@router.get("/conversations/trace/{customer_phone}")
async def get_conversation_trace(
    customer_phone: str,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """Return conversation trace turns for a specific customer (latest first)."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    rows = (
        db.query(ConversationTrace)
        .filter(
            ConversationTrace.tenant_id == tenant_id,
            ConversationTrace.customer_phone == customer_phone,
        )
        .order_by(ConversationTrace.created_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "customer_phone": customer_phone,
        "turns": [
            {
                "id":                  r.id,
                "session_id":          r.session_id,
                "turn":                r.turn,
                "message":             r.message,
                "detected_intent":     r.detected_intent,
                "confidence":          r.confidence,
                "response_type":       r.response_type,
                "response_text":       r.response_text,
                "orchestrator_used":   r.orchestrator_used,
                "model_used":          r.model_used,
                "fact_guard_modified": r.fact_guard_modified,
                "fact_guard_claims":   r.fact_guard_claims,
                "actions_triggered":   r.actions_triggered,
                "order_started":       r.order_started,
                "payment_link_sent":   r.payment_link_sent,
                "handoff_triggered":   r.handoff_triggered,
                "latency_ms":          r.latency_ms,
                "created_at":          r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
