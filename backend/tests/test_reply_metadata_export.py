"""Unit tests for the closed Brain reply-metadata export contract."""
from __future__ import annotations

from modules.ai.compose.reply_metadata_export import (
    apply_persona_nested_compose_source_to_event,
    extract_reply_metadata_export,
    map_persona_nested_source_to_compose_source,
    stamp_general_llm_compose_metadata,
)


def test_map_persona_nested_source_only_approved_closed_values() -> None:
    assert map_persona_nested_source_to_compose_source("persona_llm") == "persona_llm"
    assert map_persona_nested_source_to_compose_source("catalog_deterministic_fallback") == ""
    assert map_persona_nested_source_to_compose_source("template") == ""


def test_apply_persona_nested_source_never_upgrades_deterministic() -> None:
    event = {"compose_source": "fallback_deterministic"}
    apply_persona_nested_compose_source_to_event(
        event,
        {"source": "persona_llm"},
    )
    assert event["compose_source"] == "fallback_deterministic"


def test_stamp_general_llm_compose_metadata_sets_producer_fields() -> None:
    data: dict = {}
    stamp_general_llm_compose_metadata(
        data,
        llm_candidate="provider-backed candidate",
        chosen_path="llm",
    )
    assert data["compose_source"] == "llm"
    assert data["response_mode"] == "llm"
    assert data["chosen_path"] == "llm"
    assert data["llm_candidate_present"] is True
    assert data["final_text_transformed"] is False
    assert data["final_transform_reasons"] == []
    assert data["final_customer_text_source"] == "llm"


def test_extract_reply_metadata_export_includes_fallback_only_for_fallback_source() -> None:
    llm_export = extract_reply_metadata_export(
        {
            "compose_source": "llm",
            "response_mode": "llm",
            "chosen_path": "llm",
            "llm_candidate_present": True,
            "final_text_transformed": False,
            "final_transform_reasons": [],
            "final_customer_text_source": "llm",
            "fallback_reason": "should-not-export",
        }
    )
    assert "fallback_reason" not in llm_export

    fallback_export = extract_reply_metadata_export(
        {
            "compose_source": "fallback_deterministic",
            "response_mode": "template",
            "chosen_path": "track_order_need_order_number",
            "llm_candidate_present": False,
            "final_text_transformed": False,
            "final_transform_reasons": [],
            "final_customer_text_source": "fallback_deterministic",
            "fallback_reason": "compose_failed_or_empty",
            "fallback_action_type": "track_order_need_identifiers",
        }
    )
    assert fallback_export["fallback_reason"] == "compose_failed_or_empty"
    assert fallback_export["fallback_action_type"] == "track_order_need_identifiers"
