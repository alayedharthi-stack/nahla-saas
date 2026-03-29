from fastapi import APIRouter
from models.models import ConversationCreate, MessageCreate, ConversationResponse, MessageResponse
from services.business_logic import create_conversation as create_conversation_logic, add_message as add_message_logic, handoff_to_human as handoff_to_human_logic

router = APIRouter(prefix="/conversations", tags=["conversations"])

@router.post("/", response_model=ConversationResponse)
async def create_conversation(payload: ConversationCreate):
    return create_conversation_logic(payload)

@router.post("/{conversation_id}/messages", response_model=MessageResponse)
async def add_message(conversation_id: int, payload: MessageCreate):
    return add_message_logic(conversation_id, payload)

@router.post("/{conversation_id}/handoff")
async def handoff_to_human(conversation_id: int):
    return handoff_to_human_logic(conversation_id)
