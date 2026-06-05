"""
B0 — unified [BRAIN_STATE_SLIM] v2 telemetry + persona contract shadow metrics.

Observability only: prompt JSON must remain identical to pre-B0 slim behavior.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.brain_state_slim import (  # noqa: E402
    prepare_brain_state_dict_with_telemetry,
    top_json_field_contributors,
)
from modules.ai.brain.compose.persona_json_contract import (  # noqa: E402
    apply_persona_json_contract_shadow,
    is_persona_contract_eligible,
)
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
    slim_brain_state_dict_for_persona,
)
from modules.ai.brain.types import BrainReplyState  # noqa: E402


def _heavy_persona_state_dict() -> dict:
    manual_kb = "x" * 50_000
    products = [{"id": i, "name": f"product-{i}", "price": 99} for i in range(20)]
    return {
        "store_name": "متجر تجريبي",
        "tone": "neutral",
        "stage": "exploring",
        "intent_name": "greeting",
        "identity_already_introduced": True,
        "persona_expression_mode": True,
        "persona_topic": PERSONA_TOPIC_SOCIAL,
        "persona_kind": PERSONA_KIND_GREETING,
        "non_commerce_block_mode": True,
        "response_goal": "persona_social greeting goal",
        "recent_turns": ["customer: هلا", "assistant: أهلاً"],
        "conversation_summary": "محادثة ودية",
        "customer_memory": {
            "first_name": "سارة",
            "last_order_id": 9999,
            "purchase_history": ["sku-1"],
        },
        "known_facts": {"checkout_preparation": {"product_id": "123"}},
        "selected_product": {"id": 1, "name": "عسل"},
        "merchant_context": {
            "tenant_id": 42,
            "ai_settings": {"manual_knowledge_base": manual_kb},
            "products": products,
            "structured_behavior_block": "behavior " * 500,
            "customer": {"display_name": "سارة"},
        },
    }


def _persona_state(**overrides) -> BrainReplyState:
    base = BrainReplyState(
        store_name="متجر تجريبي",
        stage="exploring",
        intent_name="greeting",
        persona_expression_mode=True,
        persona_topic=PERSONA_TOPIC_SOCIAL,
        persona_kind=PERSONA_KIND_GREETING,
        identity_already_introduced=True,
        merchant_context={"tenant_id": 42},
    )
    for key, val in overrides.items():
        setattr(base, key, val)
    return base


def test_persona_contract_eligible_gate():
    assert is_persona_contract_eligible(_persona_state()) is True
    assert is_persona_contract_eligible(
        _persona_state(platform_kb_mode=True)
    ) is False
    assert is_persona_contract_eligible(
        _persona_state(contextual_clarify_mode=True)
    ) is False
    assert is_persona_contract_eligible(
        _persona_state(persona_expression_mode=False)
    ) is False


def test_contract_shadow_drops_commerce_fields():
    raw = _heavy_persona_state_dict()
    contract, omitted = apply_persona_json_contract_shadow(
        raw,
        state=_persona_state(),
    )
    assert "known_facts" in omitted
    assert "selected_product" in omitted
    assert "merchant_context" not in contract or "ai_settings" not in contract.get(
        "merchant_context", {}
    )
    assert contract.get("persona_topic") == PERSONA_TOPIC_SOCIAL
    assert contract.get("customer_memory", {}).get("first_name") == "سارة"
    assert "last_order_id" not in contract.get("customer_memory", {})
    serialized = json.dumps(contract, ensure_ascii=False, indent=2)
    assert len(serialized) < len(json.dumps(raw, ensure_ascii=False, indent=2)) // 5


def test_prepare_telemetry_does_not_change_persona_json():
    raw = _heavy_persona_state_dict()
    expected = slim_brain_state_dict_for_persona(dict(raw))
    state = _persona_state()
    actual = prepare_brain_state_dict_with_telemetry(state, dict(raw))
    assert actual == expected


def test_prepare_telemetry_emits_v2_log(caplog):
    caplog.set_level(logging.INFO, logger="nahla.ai.brain_state_slim")
    raw = _heavy_persona_state_dict()
    state = _persona_state()
    prepare_brain_state_dict_with_telemetry(state, dict(raw))

    slim_lines = [
        r.message for r in caplog.records if "[BRAIN_STATE_SLIM]" in r.message
    ]
    assert len(slim_lines) == 1
    payload = json.loads(slim_lines[0].split("[BRAIN_STATE_SLIM] ", 1)[1])
    assert payload["schema_version"] == 2
    assert payload["slim_profile"] == "persona_contract_shadow"
    assert payload["contract_eligible"] is True
    assert payload["actual_json_chars"] > 0
    assert payload["contract_json_chars"] > 0
    assert payload["after_json_chars"] == payload["actual_json_chars"]
    assert payload["delta_json_chars"] == (
        payload["actual_json_chars"] - payload["contract_json_chars"]
    )
    assert payload["delta_json_chars"] > 0
    assert payload["contract_omitted_fields"]
    assert payload["persona_topic"] == PERSONA_TOPIC_SOCIAL
    assert payload["persona_kind"] == PERSONA_KIND_GREETING
    assert payload["prompt_unchanged"] is True
    assert payload["top_remaining_contributors"]


def test_phase2b_path_emits_none_profile_when_flag_off(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="nahla.ai.brain_state_slim")
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "false")
    raw = {"store_name": "x", "intent_name": "general", "stage": "exploring"}
    state = BrainReplyState(
        intent_name="general",
        stage="exploring",
        merchant_context={"tenant_id": 1},
    )
    result = prepare_brain_state_dict_with_telemetry(state, dict(raw))
    assert result == raw
    slim_lines = [
        r.message for r in caplog.records if "[BRAIN_STATE_SLIM]" in r.message
    ]
    payload = json.loads(slim_lines[0].split("[BRAIN_STATE_SLIM] ", 1)[1])
    assert payload["slim_profile"] == "none"
    assert payload["contract_eligible"] is False
    assert payload["contract_json_chars"] is None


def test_top_json_field_contributors_ranks_merchant_context():
    raw = _heavy_persona_state_dict()
    ranked = top_json_field_contributors(raw, limit=3)
    assert ranked[0]["field"] == "merchant_context"
    assert ranked[0]["merchant_context_top"][0]["field"] == "ai_settings"


def test_phase2b_path_emits_general_profile(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="nahla.ai.brain_state_slim")
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    raw = {
        "store_name": "متجر",
        "intent_name": "general",
        "stage": "exploring",
        "known_facts": {"store_name": "متجر"},
        "merchant_context": {
            "tenant_id": 1,
            "ai_settings": {"manual_knowledge_base": "x" * 10_000},
            "products": [{"id": 1}],
        },
    }
    state = BrainReplyState(
        intent_name="general",
        stage="exploring",
        merchant_context={"tenant_id": 1},
    )
    result = prepare_brain_state_dict_with_telemetry(state, dict(raw))
    assert "known_facts" not in result
    slim_lines = [
        r.message for r in caplog.records if "[BRAIN_STATE_SLIM]" in r.message
    ]
    payload = json.loads(slim_lines[0].split("[BRAIN_STATE_SLIM] ", 1)[1])
    assert payload["slim_profile"] == "phase2b_general"
    assert payload["was_slimmed"] is True
    assert payload["before_json_chars"] > payload["after_json_chars"]
    assert payload["delta_json_chars"] == (
        payload["before_json_chars"] - payload["after_json_chars"]
    )
    assert payload["contract_eligible"] is False
    assert payload["prompt_unchanged"] is True
