"""Read-only, bounded projection of stored conversation diagnostics."""
from datetime import datetime, timezone

from fastapi import HTTPException

from core.outbound_provenance import extract_outbound_provenance


def build_conversation_trace_export(db, *, tenant_id: int, conversation_id: int, limit: int):
    from models import Conversation, MessageEvent

    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.tenant_id == tenant_id,
    ).first()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found in this tenant")

    rows = db.query(MessageEvent).filter(
        MessageEvent.tenant_id == tenant_id,
        MessageEvent.conversation_id == conversation_id,
    ).order_by(MessageEvent.created_at.desc(), MessageEvent.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    messages = []
    for row in reversed(rows[:limit]):
        meta = row.extra_metadata if isinstance(row.extra_metadata, dict) else {}
        normalized = meta.get("normalized_inbound")
        normalized = normalized if isinstance(normalized, dict) else {}
        messages.append({
            "message_event_id": row.id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "direction": row.direction,
            "event_type": row.event_type,
            "body": row.body,
            "wa_message_id": meta.get("wa_message_id") or normalized.get("wa_message_id"),
            "inbound_type": normalized.get("normalized_type") or normalized.get("source_type"),
            "provenance": extract_outbound_provenance(meta)
            if row.direction in ("out", "outbound") else None,
        })

    metadata = conversation.extra_metadata if isinstance(conversation.extra_metadata, dict) else {}
    state = metadata.get("brain_state")
    state = state if isinstance(state, dict) else {}
    # Explicit projection: never dump arbitrary metadata, integration credentials,
    # payment details or the customer's address into a diagnostic export.
    state_keys = (
        "stage", "turn", "last_intent", "last_question_asked", "last_question_type",
        "selected_product_id", "selected_variant_id", "suggested_next_step",
        "order_prep_missing", "fulfillment_locked",
    )
    return {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "message_limit": limit,
        "has_more_messages": has_more,
        "messages": messages,
        "current_state": {
            "source": "conversation.extra_metadata.brain_state",
            "is_historical_turn_snapshot": False,
            "values": {key: state[key] for key in state_keys if key in state},
        },
        "limitations": {
            "raw_model_requests_included": False,
            "historical_facts_snapshots_included": False,
            "missing_model_identity_is_unknown": True,
            "note": "Stored message provenance only; current state is not evidence of past model inputs.",
        },
    }
