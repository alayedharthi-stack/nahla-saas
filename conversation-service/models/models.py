from pydantic import BaseModel
from typing import Optional, Dict, Any

class ConversationCreate(BaseModel):
    customer_id: Optional[int]
    tenant_id: int
    metadata: Optional[Dict[str, Any]] = None

class MessageCreate(BaseModel):
    direction: str
    body: str
    event_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class ConversationResponse(ConversationCreate):
    id: int
    status: str
    is_human_handoff: bool
    is_urgent: bool
    paused_by_human: bool

class MessageResponse(MessageCreate):
    id: int
    conversation_id: int
