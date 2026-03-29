from models.models import ConversationCreate, MessageCreate


def create_conversation_record(payload: ConversationCreate) -> dict:
    return {
        "id": 1,
        "external_id": "conv-1",
        "status": "open",
        "customer_id": payload.customer_id,
        "tenant_id": payload.tenant_id,
        "is_human_handoff": False,
        "is_urgent": False,
        "paused_by_human": False,
        "metadata": payload.metadata or {},
    }


def add_message_record(conversation_id: int, payload: MessageCreate) -> dict:
    return {
        "id": 1,
        "conversation_id": conversation_id,
        "direction": payload.direction,
        "body": payload.body,
        "event_type": payload.event_type,
        "metadata": payload.metadata or {},
    }


def start_handoff(conversation_id: int) -> dict:
    return {"status": "handoff_started", "conversation_id": conversation_id}
