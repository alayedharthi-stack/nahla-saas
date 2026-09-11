from __future__ import annotations

import os
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.outbound_provenance import extract_outbound_provenance  # noqa: E402


def test_extracts_general_llm_and_final_wire_ownership() -> None:
    provenance = extract_outbound_provenance(
        {
            "requested_model": "model-a",
            "actual_model": "model-b",
            "escalation_reason": "technical_failure",
            "compose_source": "llm",
            "final_customer_text_source": "llm_postprocess",
            "final_expression_owner": "final_visual_dispatch",
            "final_transform_reasons": ["final_visual_dispatch"],
            "outbound_text_policy": {
                "text_source": "llm",
                "final_delivery_type": "text",
            },
        }
    )

    assert provenance["requested_model"] == "model-a"
    assert provenance["actual_model"] == "model-b"
    assert provenance["escalation_reason"] == "technical_failure"
    assert provenance["final_expression_owner"] == "final_visual_dispatch"
    assert provenance["final_transform_reasons"] == ["final_visual_dispatch"]


def test_extracts_persona_route_models_without_inference_from_text() -> None:
    provenance = extract_outbound_provenance(
        {
            "persona_compose": {
                "source": "persona_llm",
                "route_model": "model-requested",
                "model": "model-actual",
            },
            "persona_ownership": {"expression_owner": "persona_composer"},
        }
    )

    assert provenance["requested_model"] == "model-requested"
    assert provenance["actual_model"] is None
    assert provenance["attempted_model"] == "model-actual"
    assert provenance["model_identity_source"] == "unknown"
    assert provenance["compose_source"] == "persona_llm"
    assert provenance["final_expression_owner"] == "persona_composer"


def test_extracts_final_suppression_reason() -> None:
    provenance = extract_outbound_provenance(
        {
            "final_wire_body_kind": "symbols_only",
            "outbound_suppression": {
                "reason": "non_substantive_commerce_reply",
                "final_stage": "pre_provider_send",
            },
        }
    )

    assert provenance["final_wire_body_kind"] == "symbols_only"
    assert provenance["suppression"]["reason"] == "non_substantive_commerce_reply"
