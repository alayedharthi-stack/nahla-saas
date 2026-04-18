"""
brain/protocols.py
──────────────────
Structural Protocol definitions for every Brain layer.

Each Protocol is the "slot" in MerchantBrain.__init__. Phase 1 ships a
default implementation for every slot. Future layers replace the default
without changing the pipeline or calling code.

Using typing.Protocol (structural sub-typing) means we never need ABC
inheritance — any object with the right method signatures satisfies the
protocol, making testing with simple mocks trivial.
"""
from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable

from .types import (
    ActionResult,
    BrainContext,
    SuggestionSnapshot,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


@runtime_checkable
class IntentClassifier(Protocol):
    """Determine customer intent and extract entity slots from a message."""
    async def classify(
        self,
        message: str,
        history: List[Dict[str, Any]],
        state: MerchantConversationState,
    ) -> Intent: ...


@runtime_checkable
class StateStore(Protocol):
    """Persist and retrieve per-conversation state."""
    def load(self, db: Any, tenant_id: int, customer_phone: str) -> MerchantConversationState: ...
    def save(self, db: Any, tenant_id: int, customer_phone: str, state: MerchantConversationState) -> None: ...
    def transition(
        self,
        state: MerchantConversationState,
        intent: Intent,
        decision: Decision,
    ) -> MerchantConversationState: ...


@runtime_checkable
class FactsLoader(Protocol):
    """Load real-world store facts before each turn."""
    def load(self, db: Any, tenant_id: int) -> CommerceFacts: ...


@runtime_checkable
class DecisionMaker(Protocol):
    """Choose the next best action given the full BrainContext."""
    def decide(self, ctx: BrainContext) -> Decision: ...


@runtime_checkable
class PolicyGate(Protocol):
    """
    Validate / modify a Decision before execution.
    Phase 1 implementation: pass-through (no changes).
    Phase 2+: policy rules, merchant config gates, frequency caps, etc.
    """
    def gate(self, decision: Decision, ctx: BrainContext) -> Decision: ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Execute the approved Decision and return structured results."""
    async def execute(self, decision: Decision, ctx: BrainContext) -> ActionResult: ...


@runtime_checkable
class SuggestionEngine(Protocol):
    """Recommend the next best step after decision/execution."""
    def suggest(
        self,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
    ) -> SuggestionSnapshot: ...


@runtime_checkable
class Composer(Protocol):
    """Turn an ActionResult into a human-readable Arabic reply string."""
    async def compose(
        self,
        decision: Decision,
        result: ActionResult,
        ctx: BrainContext,
    ) -> str: ...


@runtime_checkable
class MemoryUpdater(Protocol):
    """
    Persist the turn outcome back to DB (ConversationTrace, history summary, …).
    Phase 1: writes ConversationTrace row only.
    Phase 2+: update ConversationHistorySummary, ProductAffinity, PriceSensitivity.
    """
    def update(
        self,
        db: Any,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
        reply: str,
        stage_before: str,
        latency_ms: int,
    ) -> None: ...
