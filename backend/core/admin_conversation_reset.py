"""Admin-only clean-room reset for an explicitly selected test conversation.

The reset preserves customer identity, messages, orders, payments, and prior
audit rows. It creates a new empty conversation as the latest session owner,
removes the customer-level rolling conversation summary, and resolves active
handoff state that would otherwise intercept the new test turn.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session


class ConversationContextResetError(RuntimeError):
    """Raised when the requested reset cannot be scoped safely."""


def reset_conversation_context(
    db: Session,
    *,
    tenant_id: int,
    conversation_id: int,
    actor: str,
    apply: bool,
) -> dict[str, Any]:
    """Preview or create a clean conversation for one tenant-bound customer."""
    from models import (
        Conversation,
        ConversationHistorySummary,
        Customer,
        HandoffSession,
        MessageEvent,
    )

    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == int(conversation_id),
            Conversation.tenant_id == int(tenant_id),
        )
        .one_or_none()
    )
    if conversation is None:
        raise ConversationContextResetError("conversation_not_in_tenant_scope")
    if conversation.customer_id is None:
        raise ConversationContextResetError("conversation_has_no_customer")
    customer = (
        db.query(Customer)
        .filter(
            Customer.id == int(conversation.customer_id),
            Customer.tenant_id == int(tenant_id),
        )
        .one_or_none()
    )
    if customer is None:
        raise ConversationContextResetError("customer_not_in_tenant_scope")

    message_count = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == int(tenant_id),
            MessageEvent.conversation_id == int(conversation.id),
        )
        .count()
    )
    summary_count = (
        db.query(ConversationHistorySummary)
        .filter(
            ConversationHistorySummary.tenant_id == int(tenant_id),
            ConversationHistorySummary.customer_id == int(customer.id),
        )
        .count()
    )
    phones = {
        str(value).strip()
        for value in (
            getattr(customer, "phone", None),
            getattr(customer, "normalized_phone", None),
        )
        if str(value or "").strip()
    }
    active_handoff_count = (
        db.query(HandoffSession)
        .filter(
            HandoffSession.tenant_id == int(tenant_id),
            HandoffSession.customer_phone.in_(phones),
            HandoffSession.status == "active",
        )
        .count()
        if phones
        else 0
    )
    result = {
        "applied": False,
        "tenant_id": int(tenant_id),
        "source_conversation_id": int(conversation.id),
        "customer_id": int(customer.id),
        "preserved_message_count": int(message_count),
        "removed_history_summary_count": int(summary_count),
        "resolved_handoff_count": int(active_handoff_count),
        "new_conversation_id": None,
        "preserved_business_records": True,
    }
    if not apply:
        return result

    now = datetime.now(timezone.utc)
    try:
        if summary_count:
            (
                db.query(ConversationHistorySummary)
                .filter(
                    ConversationHistorySummary.tenant_id == int(tenant_id),
                    ConversationHistorySummary.customer_id == int(customer.id),
                )
                .delete(synchronize_session=False)
            )
        if phones:
            active_handoffs = (
                db.query(HandoffSession)
                .filter(
                    HandoffSession.tenant_id == int(tenant_id),
                    HandoffSession.customer_phone.in_(phones),
                    HandoffSession.status == "active",
                )
                .all()
            )
            for handoff in active_handoffs:
                handoff.status = "resolved"
                handoff.resolved_by = str(actor or "admin")[:120]
                handoff.resolved_at = now

        clean_conversation = Conversation(
            tenant_id=int(tenant_id),
            customer_id=int(customer.id),
            status="active",
            is_human_handoff=False,
            is_urgent=False,
            paused_by_human=False,
            ai_paused=False,
            ai_paused_reason=None,
            ai_paused_at=None,
            ai_paused_by=None,
            needs_human=False,
            handoff_active=False,
            extra_metadata={
                "customer_phone": str(
                    getattr(customer, "normalized_phone", None)
                    or getattr(customer, "phone", None)
                    or ""
                ),
                "brain_state": {},
                "test_context_reset": {
                    "source_conversation_id": int(conversation.id),
                    "reset_at": now.isoformat(),
                    "reset_by": str(actor or "admin")[:120],
                },
            },
        )
        db.add(clean_conversation)
        db.flush()
        result["new_conversation_id"] = int(clean_conversation.id)
        result["applied"] = True
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result
