"""
turn/telemetry.py
─────────────────
Build shadow telemetry payloads for logs and tests.

Read-only — no side effects on reply routing or conversation state.
"""
from __future__ import annotations

from typing import Any, Optional

from ..types import Decision
from .contract import TurnArbitration, TurnShadowTelemetry, TurnUnderstanding
from .legacy_owner import legacy_owner_from_decision, owners_compatible
from .mismatch import classify_owner_mismatch


def build_shadow_telemetry(
    understanding: TurnUnderstanding,
    arbitration: TurnArbitration,
    decision: Decision,
) -> TurnShadowTelemetry:
    """Assemble a full shadow telemetry record from understanding + legacy decision."""
    legacy_action = str(getattr(decision, "action", "") or "")
    legacy_owner = legacy_owner_from_decision(decision)
    proposed_owner = arbitration.turn_owner
    owner_mismatch = not owners_compatible(proposed_owner, legacy_owner)
    mismatch_type = classify_owner_mismatch(
        proposed_owner,
        legacy_owner,
        owner_mismatch=owner_mismatch,
    )

    return TurnShadowTelemetry(
        current_intent=understanding.current_intent,
        current_topic=understanding.current_topic,
        customer_goal=understanding.customer_goal,
        active_objective_candidate=understanding.active_objective_candidate,
        proposed_owner=proposed_owner,
        proposed_reason=arbitration.reason,
        legacy_owner=legacy_owner,
        legacy_action=legacy_action,
        owner_mismatch=owner_mismatch,
        mismatch_type=mismatch_type,
        confidence=understanding.confidence,
        should_suspend_stale_state=understanding.should_suspend_stale_state,
        conflicts_with_state_count=len(understanding.conflicts_with_state),
        suspend_scope=understanding.suspend_scope,
        slot_replay_approved=arbitration.slot_replay_approved,
        has_state_conflict=bool(understanding.conflicts_with_state),
        understanding=understanding.to_dict(),
        arbitration=arbitration.to_dict(),
    )


def telemetry_to_log_dict(telemetry: TurnShadowTelemetry) -> dict[str, Any]:
    """Flat dict suitable for structured logs and test assertions."""
    return telemetry.to_dict()


def telemetry_from_context(
    ctx: Any,
    decision: Optional[Decision] = None,
) -> Optional[TurnShadowTelemetry]:
    """
    Build telemetry from a BrainContext when shadow artifacts are present.

    If ``decision`` is supplied, legacy owner is derived from it.
    Otherwise uses any pre-built ``ctx.turn_shadow_telemetry``.
    """
    existing = getattr(ctx, "turn_shadow_telemetry", None)
    if decision is None:
        return existing

    understanding: Optional[TurnUnderstanding] = getattr(
        ctx, "turn_understanding_shadow", None,
    )
    arbitration: Optional[TurnArbitration] = getattr(
        ctx, "turn_arbitration_shadow", None,
    )
    if understanding is None or arbitration is None:
        return None

    return build_shadow_telemetry(understanding, arbitration, decision)


__all__ = [
    "build_shadow_telemetry",
    "telemetry_from_context",
    "telemetry_to_log_dict",
]
