"""Reject a Meta message ID already claimed by another outbound family.

This module covers the sources present in the Communication base. A future
sender family must be added here before it can share the Meta status route.
"""

from sqlalchemy import or_

from models import (
    AutomationExecution, CampaignSendAttempt, CampaignSendLog,
    CommerceLifecycleNotificationLedger, MessageEvent,
)


class CommunicationReceiptConflict(RuntimeError):
    """The callback cannot be attributed to one Communication send."""


def competing_receipt_owner(db, message_id: str) -> bool:
    """Read-only check for a non-Communication owner of the same Meta ID."""
    if not isinstance(message_id, str) or not message_id:
        return True
    return any((
        db.query(CampaignSendAttempt.id)
        .filter(CampaignSendAttempt.provider_message_id == message_id).first(),
        db.query(CampaignSendLog.id)
        .filter(CampaignSendLog.provider_message_id == message_id).first(),
        db.query(MessageEvent.id)
        .filter(
            MessageEvent.direction == "outbound",
            or_(
                MessageEvent.extra_metadata["wa_message_id"].as_string() == message_id,
                MessageEvent.extra_metadata["provider_send"]["wamid"].as_string() == message_id,
            ),
        ).first(),
        db.query(AutomationExecution.id)
        .filter(AutomationExecution.action_taken["wa_message_id"].as_string() == message_id)
        .first(),
        db.query(CommerceLifecycleNotificationLedger.id)
        .filter(CommerceLifecycleNotificationLedger.provider_message_id == message_id)
        .first(),
    ))
