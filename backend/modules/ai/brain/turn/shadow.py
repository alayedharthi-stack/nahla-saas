"""
turn/shadow.py
──────────────
Phase 1/2A — Turn Understanding + Turn Arbiter shadow trace and pre-decide prep.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..types import BrainContext, Decision
from .arbiter import arbitrate_turn
from .contract import TurnArbitration, TurnShadowTelemetry, TurnUnderstanding
from .flags import is_turn_arbiter_shadow_enabled, should_prepare_turn_arbitration
from .telemetry import build_shadow_telemetry, telemetry_to_log_dict
from .understanding import synthesize_turn_understanding

logger = logging.getLogger("nahla.brain.turn_shadow")


def prepare_turn_arbitration(ctx: BrainContext) -> Optional[TurnUnderstanding]:
    """
    Synthesize understanding + arbitration before ``decide()``.

    Runs when shadow **or** enforce is enabled. Attaches artifacts to ctx.
    """
    if not should_prepare_turn_arbitration():
        return None

    try:
        understanding = synthesize_turn_understanding(ctx)
        arbitration = arbitrate_turn(understanding, ctx)
        ctx.turn_understanding_shadow = understanding  # type: ignore[attr-defined]
        ctx.turn_arbitration_shadow = arbitration  # type: ignore[attr-defined]
        return understanding
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — turn arbiter prep must not block decide
        logger.debug(
            "[TURN_ARBITER_SHADOW] pre_decide failed tenant=%s err=%s",
            getattr(ctx, "tenant_id", None),
            exc,
        )
        return None


def run_turn_shadow_before_decide(ctx: BrainContext) -> Optional[TurnUnderstanding]:
    """Backward-compatible alias for ``prepare_turn_arbitration``."""
    return prepare_turn_arbitration(ctx)


def complete_turn_shadow_telemetry(
    ctx: BrainContext,
    decision: Decision,
    *,
    enforce_result: Any = None,
) -> Optional[TurnShadowTelemetry]:
    """
    Log arbiter vs legacy owner comparison after ``decide()``.

    Pass the **pre-enforce** legacy ``decision``. Optional ``enforce_result``
    from Phase 2A is appended to the log payload when present.
    """
    if not is_turn_arbiter_shadow_enabled():
        return None

    understanding: Optional[TurnUnderstanding] = getattr(
        ctx, "turn_understanding_shadow", None,
    )
    arbitration: Optional[TurnArbitration] = getattr(
        ctx, "turn_arbitration_shadow", None,
    )
    if understanding is None or arbitration is None:
        return None

    try:
        if enforce_result is None:
            enforce_result = getattr(ctx, "turn_enforce_result", None)
        enforced = bool(getattr(enforce_result, "enforced", False))

        telemetry = build_shadow_telemetry(understanding, arbitration, decision)
        payload = telemetry_to_log_dict(telemetry)
        if enforced and enforce_result is not None:
            payload["enforced"] = True
            payload["enforce"] = enforce_result.to_dict()

        logger.info(
            "[TURN_ARBITER_SHADOW] tenant=%s proposed_owner=%s legacy_owner=%s "
            "legacy_action=%s mismatch=%s mismatch_type=%s enforced=%s suspend_stale=%s "
            "conflicts=%d slot_replay=%s confidence=%.2f "
            "intent=%s topic=%s goal=%s candidate=%r reason=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            payload["proposed_owner"],
            payload["legacy_owner"],
            payload["legacy_action"],
            str(payload["owner_mismatch"]).lower(),
            payload["mismatch_type"],
            str(payload.get("enforced", False)).lower(),
            str(payload["should_suspend_stale_state"]).lower(),
            payload["conflicts_with_state_count"],
            str(payload["slot_replay_approved"]).lower(),
            payload["confidence"],
            payload["current_intent"],
            payload["current_topic"],
            payload["customer_goal"],
            payload["active_objective_candidate"],
            payload["proposed_reason"],
            (getattr(ctx, "raw_message", None) or ctx.message or "")[:80],
        )
        ctx.turn_shadow_telemetry = telemetry  # type: ignore[attr-defined]
        return telemetry
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — turn shadow telemetry must not block decide
        logger.debug(
            "[TURN_ARBITER_SHADOW] post_decide failed tenant=%s err=%s",
            getattr(ctx, "tenant_id", None),
            exc,
        )
        return None


__all__ = [
    "complete_turn_shadow_telemetry",
    "prepare_turn_arbitration",
    "run_turn_shadow_before_decide",
]
