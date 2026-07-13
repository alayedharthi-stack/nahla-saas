"""
Layer 2 — DecisionPlanShadow contract (PROPOSED / SHADOW CONTRACT).

Telemetry/coverage comparison only. Enforcement impossible by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Tuple

from ._privacy import (
    validate_constraints,
    validate_domain_fact_keys,
    validate_loaded_coverage,
    validate_reason_codes,
    validate_safety_flags,
    validate_snapshot_ref,
)
from ._serialization import filter_known_keys, require_schema_version

CONTRACT_STATUS = "PROPOSED / SHADOW CONTRACT"
SCHEMA_VERSION = "1"

_DECISION_PLAN_KEYS: FrozenSet[str] = frozenset({
    "schema_version",
    "proposed_action",
    "required_facts",
    "missing_facts",
    "loaded_coverage",
    "constraints",
    "safety_flags",
    "reason_codes",
    "snapshot_ref",
    "shadow_only",
})


class ProposedActionKind(str, Enum):
    """Telemetry/coverage labels — never customer clarification or execution."""

    ANSWER_FROM_FACTS = "answer_from_facts"
    CLARIFY_MISSING = "clarify_missing"
    DEFER_UNAVAILABLE = "defer_unavailable"
    NO_OP_SHADOW = "no_op_shadow"


@dataclass(frozen=True)
class DecisionPlanShadow:
    """
    Structured shadow plan — reason codes and coverage metadata only.

    ``clarify_missing`` records missing required coverage for drift telemetry.
    It must never trigger customer clarification, routing, Brain/Compose input,
    loader selection, or lifecycle execution.
    """

    proposed_action: ProposedActionKind
    required_facts: Tuple[str, ...] = ()
    missing_facts: Tuple[str, ...] = ()
    loaded_coverage: Tuple[str, ...] = ()
    constraints: Tuple[str, ...] = ()
    safety_flags: Tuple[str, ...] = ("shadow_only", "no_enforcement")
    reason_codes: Tuple[str, ...] = ()
    snapshot_ref: str = ""
    schema_version: str = SCHEMA_VERSION
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if not self.shadow_only:
            raise ValueError("DecisionPlanShadow.shadow_only must be True")
        if not isinstance(self.proposed_action, ProposedActionKind):
            object.__setattr__(
                self,
                "proposed_action",
                ProposedActionKind(self.proposed_action),
            )
        object.__setattr__(self, "required_facts", validate_domain_fact_keys(self.required_facts))
        object.__setattr__(self, "missing_facts", validate_domain_fact_keys(self.missing_facts))
        object.__setattr__(self, "loaded_coverage", validate_loaded_coverage(self.loaded_coverage))
        object.__setattr__(self, "constraints", validate_constraints(self.constraints))
        object.__setattr__(self, "safety_flags", validate_safety_flags(self.safety_flags))
        object.__setattr__(self, "reason_codes", validate_reason_codes(self.reason_codes))
        object.__setattr__(self, "snapshot_ref", validate_snapshot_ref(self.snapshot_ref))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposed_action": self.proposed_action.value,
            "required_facts": list(self.required_facts),
            "missing_facts": list(self.missing_facts),
            "loaded_coverage": list(self.loaded_coverage),
            "constraints": list(self.constraints),
            "safety_flags": list(self.safety_flags),
            "reason_codes": list(self.reason_codes),
            "snapshot_ref": self.snapshot_ref,
            "shadow_only": self.shadow_only,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DecisionPlanShadow:
        filtered = filter_known_keys(data, _DECISION_PLAN_KEYS)
        require_schema_version(filtered)
        return cls(
            proposed_action=ProposedActionKind(filtered["proposed_action"]),
            required_facts=validate_domain_fact_keys(filtered.get("required_facts") or ()),
            missing_facts=validate_domain_fact_keys(filtered.get("missing_facts") or ()),
            loaded_coverage=validate_loaded_coverage(filtered.get("loaded_coverage") or ()),
            constraints=validate_constraints(filtered.get("constraints") or ()),
            safety_flags=validate_safety_flags(
                filtered.get("safety_flags") or ("shadow_only", "no_enforcement"),
            ),
            reason_codes=validate_reason_codes(filtered.get("reason_codes") or ()),
            snapshot_ref=validate_snapshot_ref(str(filtered.get("snapshot_ref") or "")),
            schema_version=str(filtered.get("schema_version", SCHEMA_VERSION)),
            shadow_only=bool(filtered.get("shadow_only", True)),
        )

    def to_metadata(self) -> Dict[str, Any]:
        """Safe telemetry projection — no facts, prose, or secrets."""
        return self.to_dict()


__all__ = [
    "CONTRACT_STATUS",
    "DecisionPlanShadow",
    "ProposedActionKind",
    "SCHEMA_VERSION",
]
