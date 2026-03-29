from models.models import ConversationCreate, MessageCreate, ConversationResponse, MessageResponse
from repositories.data_access import create_conversation_record, add_message_record, start_handoff


def create_conversation(payload: ConversationCreate) -> ConversationResponse:
    record = create_conversation_record(payload)
    return ConversationResponse(**record)


def add_message(conversation_id: int, payload: MessageCreate) -> MessageResponse:
    record = add_message_record(conversation_id, payload)
    return MessageResponse(**record)


def handoff_to_human(conversation_id: int) -> dict:
    return start_handoff(conversation_id)
