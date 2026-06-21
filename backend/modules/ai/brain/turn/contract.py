"""
turn/contract.py
────────────────
Contracts for Turn Understanding + Turn Arbiter (Phase 1 shadow).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── Turn owners (exactly one per turn) ───────────────────────────────────────
OWNER_PERSONA_SOCIAL = "persona/social"
OWNER_DISCOVERY = "discovery"
OWNER_ORDERING = "ordering"
OWNER_CHECKOUT = "checkout"
OWNER_PAYMENT = "payment"
OWNER_TRACKING = "tracking"
OWNER_POST_PURCHASE = "post_purchase"
OWNER_SUPPORT = "support"
OWNER_STAFF_ESCALATION = "staff_escalation"

ALL_TURN_OWNERS: Tuple[str, ...] = (
    OWNER_PERSONA_SOCIAL,
    OWNER_DISCOVERY,
    OWNER_ORDERING,
    OWNER_CHECKOUT,
    OWNER_PAYMENT,
    OWNER_TRACKING,
    OWNER_POST_PURCHASE,
    OWNER_SUPPORT,
    OWNER_STAFF_ESCALATION,
)

COMMERCE_OWNERS = frozenset({
    OWNER_DISCOVERY,
    OWNER_ORDERING,
    OWNER_CHECKOUT,
    OWNER_PAYMENT,
})

COMPOSE_MODE_PERSONA = "persona"
COMPOSE_MODE_OPERATIONAL = "operational_payload"
COMPOSE_MODE_HYBRID = "hybrid"


@dataclass(frozen=True)
class OwnerBrief:
    """
    Structured compose guidance for one turn owner.

    Does NOT contain reply text — only goals, constraints, and compose mode.
    """
    owner: str
    customer_goal: str
    reply_goal: str
    forbidden_objectives: Tuple[str, ...] = field(default_factory=tuple)
    required_evidence: Tuple[str, ...] = field(default_factory=tuple)
    tone_guidance: str = ""
    compose_mode: str = COMPOSE_MODE_PERSONA

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner,
            "customer_goal": self.customer_goal,
            "reply_goal": self.reply_goal,
            "forbidden_objectives": list(self.forbidden_objectives),
            "required_evidence": list(self.required_evidence),
            "tone_guidance": self.tone_guidance,
            "compose_mode": self.compose_mode,
        }


@dataclass(frozen=True)
class UnderstandingEvidence:
    """A single piece of evidence contributing to turn understanding."""
    kind: str
    source: str
    ref: str
    summary: str
    weight: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "ref": self.ref,
            "summary": self.summary,
            "weight": round(float(self.weight), 3),
        }


@dataclass(frozen=True)
class StateConflict:
    """Conflict between current-turn meaning and persisted workflow state."""
    state_field: str
    persisted_objective: str
    conflict_reason: str
    severity: str = "hard"  # "hard" | "soft"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_field": self.state_field,
            "persisted_objective": self.persisted_objective,
            "conflict_reason": self.conflict_reason,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class TurnUnderstanding:
    """Semantic summary of the current inbound turn."""
    current_intent: str
    current_topic: str
    customer_goal: str
    active_objective_candidate: Optional[str]
    evidence: Tuple[UnderstandingEvidence, ...]
    confidence: float
    conflicts_with_state: Tuple[StateConflict, ...]
    should_suspend_stale_state: bool
    suspend_scope: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_intent": self.current_intent,
            "current_topic": self.current_topic,
            "customer_goal": self.customer_goal,
            "active_objective_candidate": self.active_objective_candidate,
            "evidence": [e.to_dict() for e in self.evidence],
            "confidence": round(float(self.confidence), 3),
            "conflicts_with_state": [c.to_dict() for c in self.conflicts_with_state],
            "should_suspend_stale_state": self.should_suspend_stale_state,
            "suspend_scope": list(self.suspend_scope),
        }


@dataclass(frozen=True)
class TurnArbitration:
    """Single turn-owner decision for one inbound turn."""
    turn_owner: str
    reason: str
    confidence: float
    owner_brief: OwnerBrief
    slot_replay_approved: bool = False
    approved_proposal: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_owner": self.turn_owner,
            "reason": self.reason,
            "confidence": round(float(self.confidence), 3),
            "owner_brief": self.owner_brief.to_dict(),
            "slot_replay_approved": self.slot_replay_approved,
            "approved_proposal": self.approved_proposal,
        }


@dataclass(frozen=True)
class TurnShadowTelemetry:
    """Shadow-mode comparison between arbiter and legacy pipeline."""
    # ── Flat fields for grep / dashboards ──
    current_intent: str
    current_topic: str
    customer_goal: str
    active_objective_candidate: Optional[str]
    proposed_owner: str
    proposed_reason: str
    legacy_owner: str
    legacy_action: str
    owner_mismatch: bool
    mismatch_type: str
    confidence: float
    should_suspend_stale_state: bool
    conflicts_with_state_count: int
    suspend_scope: Tuple[str, ...]
    slot_replay_approved: bool
    has_state_conflict: bool
    reply_goal: str
    compose_mode: str
    forbidden_objectives: Tuple[str, ...]
    required_evidence: Tuple[str, ...]
    # ── Full nested payloads ──
    understanding: Dict[str, Any]
    arbitration: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_intent": self.current_intent,
            "current_topic": self.current_topic,
            "customer_goal": self.customer_goal,
            "active_objective_candidate": self.active_objective_candidate,
            "proposed_owner": self.proposed_owner,
            "proposed_reason": self.proposed_reason,
            "legacy_owner": self.legacy_owner,
            "legacy_action": self.legacy_action,
            "owner_mismatch": self.owner_mismatch,
            "mismatch_type": self.mismatch_type,
            "confidence": round(float(self.confidence), 3),
            "should_suspend_stale_state": self.should_suspend_stale_state,
            "conflicts_with_state_count": self.conflicts_with_state_count,
            "suspend_scope": list(self.suspend_scope),
            "slot_replay_approved": self.slot_replay_approved,
            "has_state_conflict": self.has_state_conflict,
            "reply_goal": self.reply_goal,
            "compose_mode": self.compose_mode,
            "forbidden_objectives": list(self.forbidden_objectives),
            "required_evidence": list(self.required_evidence),
            "understanding": self.understanding,
            "arbitration": self.arbitration,
            "shadow": True,
        }


__all__ = [
    "ALL_TURN_OWNERS",
    "COMMERCE_OWNERS",
    "OWNER_CHECKOUT",
    "OWNER_DISCOVERY",
    "OWNER_ORDERING",
    "OWNER_PAYMENT",
    "OWNER_PERSONA_SOCIAL",
    "OWNER_POST_PURCHASE",
    "OWNER_STAFF_ESCALATION",
    "OWNER_SUPPORT",
    "OWNER_TRACKING",
    "COMPOSE_MODE_HYBRID",
    "COMPOSE_MODE_OPERATIONAL",
    "COMPOSE_MODE_PERSONA",
    "OwnerBrief",
    "StateConflict",
    "TurnArbitration",
    "TurnShadowTelemetry",
    "TurnUnderstanding",
    "UnderstandingEvidence",
]
