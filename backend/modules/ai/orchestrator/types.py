"""
backend/modules/ai/orchestrator/types.py
─────────────────────────────────────────
Shared types for the canonical Nahla AI orchestration entrypoint.

This file defines the stable request/response contract used by the canonical
adapter -> pipeline -> engine stack and by compatibility callers that route
through it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


AIChannel = Literal["whatsapp", "campaigns", "conversations", "widgets", "system"]
AIIntent = Literal["reply", "summarize", "recommend", "classify", "draft", "other"]
AIProvider = Literal["anthropic", "openai_compatible", "gemini", "mock", "unknown"]


@dataclass(slots=True)
class AIMessage:
    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AIContext:
    tenant_id: Optional[int] = None
    customer_id: Optional[int] = None
    customer_phone: str = ""
    store_name: str = ""
    channel: AIChannel = "system"
    intent: AIIntent = "reply"
    locale: str = "ar"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AIOrchestrationRequest:
    """
    Canonical input contract for all future AI calls.
    """

    context: AIContext
    message: str = ""
    history: List[AIMessage] = field(default_factory=list)
    tools_requested: List[str] = field(default_factory=list)
    tool_definitions: List[Dict[str, Any]] = field(default_factory=list)
    prompt_overrides: Dict[str, Any] = field(default_factory=dict)
    provider_hint: Optional[AIProvider] = None


@dataclass(slots=True)
class AIReplyPayload:
    """
    Final normalized AI output payload.
    """

    reply_text: str = ""
    provider_used: AIProvider = "unknown"
    prompt_used: str = ""
    allowed_actions: List[str] = field(default_factory=list)
    blocked_actions: List[str] = field(default_factory=list)
    policy_notes: List[str] = field(default_factory=list)
    raw_model_output: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

