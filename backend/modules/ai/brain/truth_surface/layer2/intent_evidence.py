"""
Layer 2 — IntentEvidence shadow contract (PROPOSED / SHADOW CONTRACT).

Structured evidence only. No DB I/O, no customer prose, no execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Tuple

from ._privacy import (
    validate_entities,
    validate_evidence_refs,
    validate_required_domains,
    validate_source_turn_ref,
    validate_trigger_ids,
)
from ._serialization import filter_known_keys, require_schema_version

CONTRACT_STATUS = "PROPOSED / SHADOW CONTRACT"
SCHEMA_VERSION = "1"

_INTENT_EVIDENCE_KEYS: FrozenSet[str] = frozenset({
    "schema_version",
    "confidence",
    "entities",
    "required_domains",
    "evidence_refs",
    "ambiguity_state",
    "trigger_ids",
    "source_turn_ref",
    "shadow_only",
})


class AmbiguityState(str, Enum):
    CLEAR = "clear"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntentEvidence:
    """Immutable turn-level evidence of required trusted-context coverage."""

    confidence: float
    entities: Tuple[Dict[str, str], ...] = ()
    required_domains: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    ambiguity_state: AmbiguityState = AmbiguityState.CLEAR
    trigger_ids: Tuple[str, ...] = ()
    source_turn_ref: str = ""
    schema_version: str = SCHEMA_VERSION
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if not self.shadow_only:
            raise ValueError("IntentEvidence.shadow_only must be True")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0 inclusive")
        if not isinstance(self.ambiguity_state, AmbiguityState):
            object.__setattr__(self, "ambiguity_state", AmbiguityState(self.ambiguity_state))
        object.__setattr__(self, "entities", validate_entities(self.entities))
        object.__setattr__(self, "required_domains", validate_required_domains(self.required_domains))
        object.__setattr__(self, "evidence_refs", validate_evidence_refs(self.evidence_refs))
        object.__setattr__(self, "trigger_ids", validate_trigger_ids(self.trigger_ids))
        object.__setattr__(
            self,
            "source_turn_ref",
            validate_source_turn_ref(self.source_turn_ref),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "confidence": self.confidence,
            "entities": [dict(item) for item in self.entities],
            "required_domains": list(self.required_domains),
            "evidence_refs": list(self.evidence_refs),
            "ambiguity_state": self.ambiguity_state.value,
            "trigger_ids": list(self.trigger_ids),
            "source_turn_ref": self.source_turn_ref,
            "shadow_only": self.shadow_only,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IntentEvidence:
        filtered = filter_known_keys(data, _INTENT_EVIDENCE_KEYS)
        require_schema_version(filtered)
        entities_raw = filtered.get("entities") or ()
        entities = validate_entities(entities_raw)
        return cls(
            confidence=float(filtered["confidence"]),
            entities=entities,
            required_domains=validate_required_domains(filtered.get("required_domains") or ()),
            evidence_refs=validate_evidence_refs(filtered.get("evidence_refs") or ()),
            ambiguity_state=AmbiguityState(filtered.get("ambiguity_state", AmbiguityState.CLEAR.value)),
            trigger_ids=validate_trigger_ids(filtered.get("trigger_ids") or ()),
            source_turn_ref=validate_source_turn_ref(str(filtered.get("source_turn_ref") or "")),
            schema_version=str(filtered.get("schema_version", SCHEMA_VERSION)),
            shadow_only=bool(filtered.get("shadow_only", True)),
        )


__all__ = [
    "AmbiguityState",
    "CONTRACT_STATUS",
    "IntentEvidence",
    "SCHEMA_VERSION",
]
