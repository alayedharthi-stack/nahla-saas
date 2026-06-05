"""
clarification/router.py
───────────────────────
Route classified ambiguity to deterministic or generative clarification.

Phase 0: shadow telemetry only (flag off).
Phase 1: ``CONTEXTUAL_CLARIFY_ENABLED=true`` replaces legacy template fallback.
"""
from __future__ import annotations

from typing import Any, Optional

from ..decision.actions import ACTION_CLARIFY, ACTION_LLM_REPLY
from ..types import BrainContext, Decision
from .classifier import classify_missing_information, would_action_for_spec
from .deterministic import build_deterministic_question
from .flags import is_clarification_shadow_enabled, is_contextual_clarify_enabled
from .telemetry import (
    log_clarification_routed,
    log_clarification_shadow,
    log_clarification_skipped,
)
from .types import (
    COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
    COMPOSE_TOPIC_SOLUTION_SEEKING,
    ClarificationSpec,
    RECOVERY_GENERATIVE,
)


def record_clarification_shadow(
    ctx: BrainContext,
    *,
    trigger: str,
    legacy_action: str = "clarify",
    legacy_reason: str = "",
) -> ClarificationSpec:
    """
    Phase 0 — always classify and log; does not change decisions.

    Returns the spec for callers that need it when Phase 1 is enabled.
    """
    spec = classify_missing_information(ctx, trigger=trigger)
    if not is_clarification_shadow_enabled():
        return spec

    log_clarification_shadow(
        tenant_id=getattr(ctx, "tenant_id", None),
        spec=spec,
        legacy_action=legacy_action,
        legacy_reason=legacy_reason,
        would_action=would_action_for_spec(spec),
        preview=ctx.message or "",
        flag_enabled=is_contextual_clarify_enabled(),
    )
    return spec


def _decision_from_spec(
    ctx: BrainContext,
    spec: ClarificationSpec,
    *,
    reason_prefix: str,
) -> Optional[Decision]:
    """Build a Decision from a ClarificationSpec, or None to keep legacy."""
    if spec.is_deterministic:
        question = build_deterministic_question(spec)
        if not question:
            log_clarification_skipped(
                tenant_id=getattr(ctx, "tenant_id", None),
                trigger=spec.trigger,
                reason="deterministic_structured_prompt_insufficient",
                preview=ctx.message or "",
            )
            return None
        dec = Decision(
            action=ACTION_CLARIFY,
            args={"question": question},
            reason=(
                f"{reason_prefix} — contextual clarify deterministic "
                f"({spec.ambiguity_class})"
            ),
            confidence=0.84,
        )
        log_clarification_routed(
            tenant_id=getattr(ctx, "tenant_id", None),
            spec=spec,
            action=dec.action,
            reason=dec.reason,
            preview=ctx.message or "",
        )
        return dec

    if not spec.is_generative:
        return None

    if spec.compose_topic == COMPOSE_TOPIC_SOLUTION_SEEKING:
        axis = str(spec.evidence.get("solution_axis") or "general_attribute")
        dec = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": COMPOSE_TOPIC_SOLUTION_SEEKING,
                "need_category": axis,
                "solution_axis": axis,
            },
            reason=(
                f"{reason_prefix} — contextual clarify generative "
                f"(solution_seeking/{spec.ambiguity_class})"
            ),
            confidence=0.86,
        )
        log_clarification_routed(
            tenant_id=getattr(ctx, "tenant_id", None),
            spec=spec,
            action=dec.action,
            reason=dec.reason,
            preview=ctx.message or "",
        )
        return dec

    dec = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
            "ambiguity_class": spec.ambiguity_class,
            "clarification_evidence": dict(spec.evidence),
            "clarification_trigger": spec.trigger,
        },
        reason=(
            f"{reason_prefix} — contextual clarify generative "
            f"({spec.ambiguity_class})"
        ),
        confidence=0.84,
    )
    log_clarification_routed(
        tenant_id=getattr(ctx, "tenant_id", None),
        spec=spec,
        action=dec.action,
        reason=dec.reason,
        preview=ctx.message or "",
    )
    return dec


def try_contextual_clarification_fallback(
    ctx: BrainContext,
    *,
    trigger: str,
    reason_prefix: str = "discovery_blocked",
) -> Optional[Decision]:
    """
    Replace legacy ``intelligent_need_clarification`` template fallback.

    Phase 0 (flag off): returns ``None`` after shadow log — legacy unchanged.
    Phase 1 (flag on): returns contextual clarify decision when classifiable.
    """
    spec = record_clarification_shadow(
        ctx,
        trigger=trigger,
        legacy_action=ACTION_CLARIFY,
        legacy_reason=f"{reason_prefix} — intelligent need clarification",
    )

    if not is_contextual_clarify_enabled():
        return None

    return _decision_from_spec(ctx, spec, reason_prefix=reason_prefix)


def try_contextual_price_clarification(
    ctx: BrainContext,
    *,
    trigger: str = "price_without_product_context",
) -> Optional[Decision]:
    """
    Optional hook for ``try_price_query_decision`` legacy template path.

    Phase 0: shadow only. Phase 1: generative/deterministic when enabled.
    """
    spec = record_clarification_shadow(
        ctx,
        trigger=trigger,
        legacy_action=ACTION_CLARIFY,
        legacy_reason="price ask without resolved product — clarify instead of catalog",
    )

    if not is_contextual_clarify_enabled():
        return None

    return _decision_from_spec(
        ctx,
        spec,
        reason_prefix="price_clarify",
    )


__all__ = [
    "record_clarification_shadow",
    "try_contextual_clarification_fallback",
    "try_contextual_price_clarification",
]
