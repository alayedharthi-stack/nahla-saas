"""Agents SDK Session adapter over Nahla Conversation + MessageEvent."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agents.memory import Session, SessionSettings

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.security.tenant_isolation import TenantIsolationLayer


class ConversationMessageSession:
    """Read-through session keyed by ``tenant_id + conversation_id``.

    The active outbound owner remains the canonical writer of customer-visible
    messages. SDK additions are kept only for this in-flight run; the shadow
    path never writes a second assistant message into the live transcript. The
    next V2 run reconstructs history from canonical MessageEvent rows.
    """

    session_settings = SessionSettings(limit=30)

    def __init__(self, context: CommerceAgentContext) -> None:
        self._context = context
        self.session_id = f"commerce-v2:{context.tenant_id}:{context.conversation_id}"
        self._ephemeral_items: list[Any] = []

    async def get_items(self, limit: int | None = None) -> list[Any]:
        from models import Conversation, MessageEvent

        self._context.assert_scope()
        self._context.begin_session_history_query()
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
        resolved_limit = limit if limit is not None else self.session_settings.limit
        if resolved_limit is not None and int(resolved_limit) <= 0:
            return []
        # Fetch newest rows first so PostgreSQL applies the bounded history
        # window. One extra row allows the already-persisted current inbound
        # WAMID to be excluded without shrinking normal history by one.
        directions = (
            ("internal_e2e_inbound", "internal_e2e_outbound")
            if self._context.channel == "internal_e2e"
            else ("in", "inbound", "out", "outbound")
        )
        query = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.conversation_id == self._context.conversation_id,
                MessageEvent.tenant_id == self._context.tenant_id,
                MessageEvent.direction.in_(directions),
            )
            .order_by(MessageEvent.created_at.desc(), MessageEvent.id.desc())
        )
        if resolved_limit is not None:
            canonical_budget = max(
                0,
                int(resolved_limit) - len(self._ephemeral_items),
            )
            query = query.limit(canonical_budget + 1)
        rows = list(reversed(query.all()))
        canonical: list[dict[str, str]] = []
        for row in rows:
            TenantIsolationLayer.assert_belongs(row, self._context.tenant_context)
            metadata = dict(getattr(row, "extra_metadata", None) or {})
            if self._context.channel == "internal_e2e":
                from modules.ai.commerce_agent_v2.internal_e2e_identity import (
                    metadata_matches_internal_e2e_identity,
                )

                if self._context.synthetic_customer_alias is None or not (
                    str(getattr(row, "direction", "") or "") in directions
                    and metadata_matches_internal_e2e_identity(
                        metadata,
                        tenant_id=self._context.tenant_id,
                        alias=self._context.synthetic_customer_alias,
                    )
                ):
                    raise TenantIsolationViolation(
                        "internal_e2e_session_history_provenance_invalid"
                    )
                self._context.record_session_history_row(
                    message_id=int(row.id),
                    tenant_id=int(row.tenant_id),
                    conversation_id=int(row.conversation_id),
                    direction=str(row.direction),
                    metadata=metadata,
                )
            # The SDK receives the current turn as Runner input. Exclude the
            # already-persisted webhook row so it is not sent twice.
            persisted_inbound_id = str(
                metadata.get("wa_message_id")
                or metadata.get("internal_message_id")
                or ""
            )
            if persisted_inbound_id == self._context.inbound_trace_id:
                continue
            body = self._context.redact_unexposed_customer_identity(
                str(getattr(row, "body", "") or "")
            ).strip()
            if not body:
                continue
            direction = str(getattr(row, "direction", "") or "").lower()
            inbound_directions = {"in", "inbound", "internal_e2e_inbound"}
            canonical.append(
                {
                    "role": "user" if direction in inbound_directions else "assistant",
                    "content": body,
                }
            )
        combined = canonical + deepcopy(self._ephemeral_items)
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
