"""Evidence helpers for human-escalation claims (AGENTS.md claim rule)."""
from __future__ import annotations

from typing import Any, Optional


def has_documented_human_escalation(
    *,
    store_has_live_agent: bool = False,
    handoff_active: bool = False,
    needs_human: bool = False,
    handoff_session_exists: bool = False,
) -> bool:
    """True when we may honestly tell the customer a human team will respond.

    Requires both merchant live-agent capability AND persisted escalation
    state — not merely a customer request or a brain crash.
    """
    if not store_has_live_agent:
        return False
    return bool(handoff_active or needs_human or handoff_session_exists)


def escalation_from_conversation(convo: Any) -> bool:
    if convo is None:
        return False
    return bool(
        getattr(convo, "handoff_active", False)
        or getattr(convo, "needs_human", False)
        or getattr(convo, "is_human_handoff", False)
    )


__all__ = [
    "escalation_from_conversation",
    "has_documented_human_escalation",
]
