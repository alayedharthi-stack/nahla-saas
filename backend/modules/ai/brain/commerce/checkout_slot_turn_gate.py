"""Current-turn authorization for late checkout slot recovery."""
from __future__ import annotations

from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER


def current_turn_allows_checkout_slot_fallback(decision_action: str = "") -> bool:
    """Return true only when the final decision assigned this turn to checkout."""
    return str(decision_action or "") == ACTION_PROPOSE_DRAFT_ORDER


__all__ = ["current_turn_allows_checkout_slot_fallback"]
