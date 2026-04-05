"""
EventLogger
───────────
Write SystemEvent records for the unified Event Timeline.
Called from all major subsystems. Silent on failure so it never blocks business logic.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.observability")


def log_event(
    db,
    tenant_id: int,
    category: str,
    event_type: str,
    summary: str,
    severity: str = "info",
    payload: Optional[Dict[str, Any]] = None,
    reference_id: Optional[str] = None,
) -> None:
    """
    Append a SystemEvent row. Caller commits the DB session.
    category: payment | ai_sales | handoff | order | orchestrator | system
    severity: info | warning | error
    """
    try:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
        from database.models import SystemEvent
        event = SystemEvent(
            tenant_id=tenant_id,
            category=category,
            event_type=event_type,
            severity=severity,
            summary=summary[:250] if summary else "",
            payload=payload or {},
            reference_id=str(reference_id) if reference_id else None,
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
    except Exception as exc:
        logger.warning(f"[EventLogger] Failed to persist event {event_type}: {exc}")


def write_trace(
    db,
    tenant_id: int,
    customer_phone: str,
    message: str,
    detected_intent: str,
    confidence: float,
    response_type: str,
    response_text: str,
    orchestrator_used: bool,
    model_used: str,
    fact_guard_modified: bool,
    fact_guard_claims: list,
    actions_triggered: list,
    order_started: bool,
    payment_link_sent: bool,
    handoff_triggered: bool,
    latency_ms: int,
) -> None:
    """Append a ConversationTrace row for replay and debugging."""
    try:
        from database.models import ConversationTrace
        import datetime as dt_mod
        today = dt_mod.date.today().isoformat()
        session_id = f"{tenant_id}:{customer_phone}:{today}"

        # Compute turn number
        last = (
            db.query(ConversationTrace)
            .filter(
                ConversationTrace.tenant_id == tenant_id,
                ConversationTrace.session_id == session_id,
            )
            .order_by(ConversationTrace.turn.desc())
            .first()
        )
        turn = (last.turn + 1) if last else 1

        trace = ConversationTrace(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            session_id=session_id,
            turn=turn,
            message=message[:1000] if message else "",
            detected_intent=detected_intent,
            confidence=confidence,
            response_type=response_type,
            response_text=response_text[:1000] if response_text else "",
            orchestrator_used=orchestrator_used,
            model_used=model_used or "",
            fact_guard_modified=fact_guard_modified,
            fact_guard_claims=fact_guard_claims or [],
            actions_triggered=actions_triggered or [],
            order_started=order_started,
            payment_link_sent=payment_link_sent,
            handoff_triggered=handoff_triggered,
            latency_ms=latency_ms,
            created_at=datetime.now(timezone.utc),
        )
        db.add(trace)
    except Exception as exc:
        logger.warning(f"[EventLogger] Failed to write conversation trace: {exc}")
