"""OrderFlowV2 operational contract.

The contract is deterministic routing/state only. Customer-facing wording
remains outside this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List


@dataclass(frozen=True)
class OrderFlowV2Contract:
    decision: str
    field: str = ""
    reason: str = ""
    facts: Dict[str, Any] = dataclass_field(default_factory=dict)
    forbidden_claims: List[str] = dataclass_field(default_factory=list)

    def to_patch(self) -> Dict[str, Any]:
        return {
            "order_flow_v2_contract": {
                "decision": self.decision,
                "field": self.field,
                "reason": self.reason,
                "facts": dict(self.facts),
                "forbidden_claims": list(self.forbidden_claims),
            },
        }


OPERATIONAL_FORBIDDEN_CLAIMS = [
    "payment_confirmed",
    "payment_paid",
    "shipping_ready",
    "shipping_started",
]


def build_contract(
    *,
    decision: str,
    field: str = "",
    reason: str = "",
    facts: Dict[str, Any] | None = None,
    forbidden_claims: List[str] | None = None,
) -> OrderFlowV2Contract:
    return OrderFlowV2Contract(
        decision=decision,
        field=field,
        reason=reason,
        facts=dict(facts or {}),
        forbidden_claims=list(forbidden_claims or OPERATIONAL_FORBIDDEN_CLAIMS),
    )
