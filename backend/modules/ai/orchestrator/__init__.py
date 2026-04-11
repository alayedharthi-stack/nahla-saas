"""
backend/modules/ai/orchestrator
───────────────────────────────
Official orchestration entrypoint for future Nahla AI reply generation.

This package is scaffolding only during the modular monolith migration.
It does not replace existing runtime paths yet.
"""
from modules.ai.orchestrator.engine import AIOrchestratorEngine
from modules.ai.orchestrator.pipeline import AIOrchestrationPipeline
from modules.ai.orchestrator.types import (
    AIContext,
    AIMessage,
    AIOrchestrationRequest,
    AIReplyPayload,
)

__all__ = [
    "AIContext",
    "AIMessage",
    "AIOrchestrationPipeline",
    "AIOrchestratorEngine",
    "AIOrchestrationRequest",
    "AIReplyPayload",
]

