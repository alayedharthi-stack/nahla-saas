"""
HandoffManager
──────────────
Creates and resolves HandoffSession records.
Checks whether AI is currently paused for a given customer.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database.session import SessionLocal
from database.models import HandoffSession

logger = logging.getLogger("nahla.handoff.manager")


def get_active_handoff(
    db,
    tenant_id: int,
    customer_phone: str,
) -> Optional[HandoffSession]:
    """Return the active HandoffSession for this customer, or None."""
    return (
        db.query(HandoffSession)
        .filter(
            HandoffSession.tenant_id == tenant_id,
            HandoffSession.customer_phone == customer_phone,
            HandoffSession.status == "active",
        )
        .first()
    )


def create_handoff_session(
    db,
    tenant_id: int,
    customer_phone: str,
    customer_name: str,
    last_message: str,
    reason: str = "customer_request",
    context_snapshot: Optional[dict] = None,
) -> HandoffSession:
    """
    Create a new HandoffSession and mark AI as paused for this customer.
    If an active session already exists, return it unchanged.
    """
    existing = get_active_handoff(db, tenant_id, customer_phone)
    if existing:
        return existing

    session = HandoffSession(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        customer_name=customer_name,
        status="active",
        handoff_reason=reason,
        last_message=last_message[:500],
        context_snapshot=context_snapshot or {},
        created_at=datetime.utcnow(),
    )
    db.add(session)
    db.flush()
    logger.info(
        f"[Handoff] Created session #{session.id} for tenant={tenant_id} phone={customer_phone}"
    )
    return session


def resolve_handoff_session(
    db,
    session_id: int,
    tenant_id: int,
    resolved_by: str = "staff",
) -> Optional[HandoffSession]:
    """Mark a HandoffSession as resolved, resuming AI responses."""
    session = (
        db.query(HandoffSession)
        .filter(
            HandoffSession.id == session_id,
            HandoffSession.tenant_id == tenant_id,
        )
        .first()
    )
    if not session:
        return None
    session.status = "resolved"
    session.resolved_by = resolved_by
    session.resolved_at = datetime.utcnow()
    db.flush()
    logger.info(f"[Handoff] Resolved session #{session_id} by {resolved_by}")
    return session
