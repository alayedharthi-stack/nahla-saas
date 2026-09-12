"""Agents SDK Session adapter over Nahla Conversation + MessageEvent."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agents.memory import Session, SessionSettings

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.security.tenant_isolation import TenantIsolationLayer


class ConversationMessageSession:
    """Read-through session keyed by ``tenant_id + conversation_id``.

    V1 remains the canonical writer of customer and merchant-visible messages.
    SDK additions are kept only for this in-flight run; Phase 1 never writes a
    second assistant message into the live transcript. The next V2 shadow run
    reconstructs history from the canonical MessageEvent rows written by V1.
    """

    session_settings = SessionSettings(limit=30)

    def __init__(self, context: CommerceAgentContext) -> None:
        self._context = context
        self.session_id = f"commerce-v2:{context.tenant_id}:{context.conversation_id}"
        self._ephemeral_items: list[Any] = []

    async def get_items(self, limit: int | None = None) -> list[Any]:
        from models import Conversation, MessageEvent

        self._context.assert_scope()
        db = self._context.db
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.id == self._context.conversation_id,
                Conversation.tenant_id == self._context.tenant_id,
            )
            .one_or_none()
        )
        if conversation is None:
            return []
        TenantIsolationLayer.assert_belongs(conversation, self._context.tenant_context)
        rows = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.conversation_id == self._context.conversation_id,
                MessageEvent.tenant_id == self._context.tenant_id,
                MessageEvent.direction.in_(("in", "inbound", "out", "outbound")),
            )
            .order_by(MessageEvent.created_at.asc(), MessageEvent.id.asc())
            .all()
        )
        canonical: list[dict[str, str]] = []
        for row in rows:
            TenantIsolationLayer.assert_belongs(row, self._context.tenant_context)
            metadata = dict(getattr(row, "extra_metadata", None) or {})
            # The SDK receives the current turn as Runner input. Exclude the
            # already-persisted webhook row so it is not sent twice.
            if str(metadata.get("wa_message_id") or "") == self._context.inbound_trace_id:
                continue
            body = str(getattr(row, "body", "") or "").strip()
            if not body:
                continue
            direction = str(getattr(row, "direction", "") or "").lower()
            canonical.append(
                {
                    "role": "user" if direction in {"in", "inbound"} else "assistant",
                    "content": body,
                }
            )
        combined = canonical + deepcopy(self._ephemeral_items)
        resolved_limit = limit if limit is not None else self.session_settings.limit
        if resolved_limit is not None:
            combined = combined[-max(0, int(resolved_limit)) :]
        return combined

    async def add_items(self, items: list[Any]) -> None:
        # Official Session protocol mutation, deliberately process-local in
        # shadow mode. Persisting these would create a second outbound bubble.
        self._ephemeral_items.extend(deepcopy(list(items or [])))

    async def pop_item(self) -> Any | None:
        if not self._ephemeral_items:
            return None
        return self._ephemeral_items.pop()

    async def clear_session(self) -> None:
        self._ephemeral_items.clear()


def is_agents_session(value: object) -> bool:
    return isinstance(value, Session)
