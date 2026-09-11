"""Read-only projection of persisted outbound provenance metadata."""
from __future__ import annotations

from typing import Any, Dict, Mapping


def extract_outbound_provenance(extra_metadata: object) -> Dict[str, Any]:
    """Return the stable audit fields exposed by the admin trace endpoint."""
    meta: Mapping[str, Any] = (
        extra_metadata if isinstance(extra_metadata, Mapping) else {}
    )
    persona = (
        meta.get("persona_compose")
        if isinstance(meta.get("persona_compose"), Mapping)
        else {}
    )
    ownership = (
        meta.get("persona_ownership")
        if isinstance(meta.get("persona_ownership"), Mapping)
        else {}
    )
    text_policy = (
        meta.get("outbound_text_policy")
        if isinstance(meta.get("outbound_text_policy"), Mapping)
        else {}
    )
    suppression = (
        meta.get("outbound_suppression")
        if isinstance(meta.get("outbound_suppression"), Mapping)
        else None
    )

    reasons = meta.get("final_transform_reasons")
    if not isinstance(reasons, list):
        reasons = []

    return {
        "requested_model": meta.get("requested_model")
        or persona.get("requested_model")
        or persona.get("route_model"),
        "actual_model": meta.get("actual_model")
        or persona.get("actual_model")
        or persona.get("model"),
        "escalation_reason": meta.get("escalation_reason")
        if meta.get("escalation_reason") is not None
        else persona.get("escalation_reason"),
        "compose_source": meta.get("compose_source") or persona.get("source"),
        "response_mode": meta.get("response_mode"),
        "chosen_path": meta.get("chosen_path"),
        "llm_candidate_present": meta.get("llm_candidate_present"),
        "final_text_transformed": meta.get("final_text_transformed"),
        "final_transform_reasons": list(reasons),
        "final_customer_text_source": meta.get("final_customer_text_source"),
        "final_expression_owner": meta.get("final_expression_owner")
        or ownership.get("expression_owner"),
        "text_source": text_policy.get("text_source"),
        "policy_path": text_policy.get("policy_path"),
        "final_delivery_type": text_policy.get("final_delivery_type"),
        "final_wire_body_kind": meta.get("final_wire_body_kind"),
        "final_wire_structured_delivery": meta.get(
            "final_wire_structured_delivery"
        ),
        "suppression": dict(suppression) if suppression is not None else None,
    }


__all__ = ["extract_outbound_provenance"]
