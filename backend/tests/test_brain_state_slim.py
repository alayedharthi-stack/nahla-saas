"""Tests for Phase 2b — general / non-commerce BrainStateJSON slimming."""
from __future__ import annotations

import json
import logging

import pytest

from modules.ai.brain.compose.brain_state_slim import (
    is_slim_general_brain_state_enabled,
    prepare_brain_state_dict_with_telemetry,
    should_slim_general_brain_state,
    slim_brain_state_dict_for_general,
)
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.types import (
    INTENT_ASK_PRICE,
    INTENT_ASK_SHIPPING,
    INTENT_GENERAL,
    BrainReplyState,
)


def _heavy_state(**overrides) -> BrainReplyState:
    base = dict(
        store_name="متجر",
        tone="neutral",
        stage="exploring",
        intent_name=INTENT_GENERAL,
        identity_already_introduced=True,
        conversation_summary="ملخص المحادثة",
        recent_turns=["user: مرحبا", "assistant: هلا"],
        customer_memory={"name": "سارة", "segment": "returning"},
        known_facts={
            "store_name": "متجر",
            "shipping_policy": "شحن 25",
            "checkout_preparation": {"order_status": "none"},
        },
        store_knowledge={"store_name": "متجر"},
        selected_product=None,
        coupon_policy={"has_coupons": True},
        merchant_context={
            "tenant_id": 10,
            "ai_settings": {"manual_knowledge_base": "KB " * 5000},
            "structured_facts_block": "facts",
            "products": [{"id": 1, "title": "منتج", "price": 99}],
            "policies": {"shipping_policy": "مجاني"},
            "faq_approved": [{"question": "Q", "answer": "A"}],
            "resolver_overlay": "overlay",
            "conversation": {"recent_messages": [{"body": "msg " * 100}]},
        },
    )
    base.update(overrides)
    return BrainReplyState(**base)


def test_gate_general_turn_eligible() -> None:
    ok, reason = should_slim_general_brain_state(_heavy_state())
    assert ok is True
    assert reason == "intent_general"


def test_gate_blocks_commerce_intent() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(intent_name=INTENT_ASK_PRICE),
    )
    assert ok is False
    assert "commerce_intent" in reason


def test_gate_blocks_shipping_intent() -> None:
    ok, _ = should_slim_general_brain_state(
        _heavy_state(intent_name=INTENT_ASK_SHIPPING),
    )
    assert ok is False


def test_gate_blocks_order_stage() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(stage="checkout"),
    )
    assert ok is False
    assert "order_stage" in reason


def test_gate_blocks_product_focus() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(selected_product={"id": 1, "title": "X"}),
    )
    assert ok is False
    assert reason == "selected_product_focus"


def test_gate_blocks_active_checkout() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(
            known_facts={
                "checkout_preparation": {"order_status": "awaiting_payment"},
            },
        ),
    )
    assert ok is False
    assert reason == "active_order_flow"


def test_slim_removes_operational_fields() -> None:
    from dataclasses import asdict

    d = asdict(_heavy_state())
    slimmed, removed = slim_brain_state_dict_for_general(d)
    blob = json.dumps(slimmed, ensure_ascii=False)
    assert "manual_knowledge_base" not in blob
    assert "products" not in blob or slimmed.get("merchant_context") == {"tenant_id": 10}
    assert "known_facts" not in slimmed
    assert "coupon_policy" not in slimmed
    assert "recent_turns" in slimmed
    assert "conversation_summary" in slimmed
    assert "customer_memory" in slimmed
    assert removed


def test_prompt_json_slimmed_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    assert is_slim_general_brain_state_enabled()
    prompt = build_brain_reply_prompt(_heavy_state())
    assert "manual_knowledge_base" not in prompt.split("BrainStateJSON:")[-1]
    assert "recent_turns" in prompt
    # JSON section should be much smaller than full heavy state would be
    json_part = prompt.split("BrainStateJSON:", 1)[1]
    assert len(json_part) < 8000


def test_prompt_not_slimmed_for_commerce_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    prompt = build_brain_reply_prompt(
        _heavy_state(intent_name=INTENT_ASK_PRICE),
    )
    json_part = prompt.split("BrainStateJSON:", 1)[1]
    assert "manual_knowledge_base" in json_part or "known_facts" in json_part


def test_prompt_blocks_unchanged_except_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "false")
    p_off = build_brain_reply_prompt(_heavy_state())
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    p_on = build_brain_reply_prompt(_heavy_state())
    off_pre, off_json = p_off.split("BrainStateJSON:", 1)
    on_pre, on_json = p_on.split("BrainStateJSON:", 1)
    assert off_pre == on_pre
    assert len(on_json) < len(off_json)


def test_slim_log_emitted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    caplog.set_level(logging.INFO)
    build_brain_reply_prompt(_heavy_state())
    lines = [r for r in caplog.records if "[BRAIN_STATE_SLIM]" in r.message]
    assert lines
    payload = json.loads(lines[-1].message.split("[BRAIN_STATE_SLIM] ", 1)[1])
    assert payload["was_slimmed"] is True
    assert payload["old_json_chars"] > payload["new_json_chars"]


def test_gate_general_turn_without_operational_facts_remains_eligible() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(
            known_facts={
                "store_name": "متجر",
                "checkout_preparation": {"order_status": "none"},
            },
        ),
    )
    assert ok is True
    assert reason == "intent_general"


def test_gate_blocks_social_turn_with_authoritative_fact_contract() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(
            intent_name="social",
            known_facts={
                "answer_contract": {
                    "fact_kind": "shipping_companies",
                    "status": "KNOWN_VALUE",
                    "claimable_values": ["Dev Company"],
                },
                "checkout_preparation": {"order_status": "none"},
            },
        ),
    )
    assert ok is False
    assert reason == "authoritative_fact_contract"


def test_gate_blocks_general_turn_with_structured_shipping_knowledge() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(
            known_facts={
                "shipping_knowledge": {
                    "city": "الرياض",
                    "fee_sar": 25.0,
                    "source": "kb",
                    "need_city": False,
                },
                "checkout_preparation": {"order_status": "none"},
            },
        ),
    )
    assert ok is False
    assert reason == "shipping_knowledge"


def test_gate_blocks_general_turn_with_need_city_shipping_knowledge() -> None:
    ok, reason = should_slim_general_brain_state(
        _heavy_state(
            known_facts={
                "shipping_knowledge": {
                    "need_city": True,
                    "source": "kb",
                },
                "checkout_preparation": {"order_status": "none"},
            },
        ),
    )
    assert ok is False
    assert reason == "shipping_knowledge"


def test_prompt_retains_structured_shipping_facts_on_general_city_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    shipping_facts = {
        "city": "الرياض",
        "fee_sar": 25.0,
        "source": "kb",
        "need_city": False,
    }
    state = _heavy_state(
        known_facts={
            "shipping_knowledge": shipping_facts,
            "checkout_preparation": {"order_status": "none"},
        },
    )
    prompt = build_brain_reply_prompt(state)
    json_part = prompt.split("BrainStateJSON:", 1)[1]
    assert "shipping_knowledge" in json_part
    assert "fee_sar" in json_part
    assert "25" in json_part
    assert "الرياض" in json_part


def test_prepare_brain_state_retains_shipping_facts_when_slim_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    from dataclasses import asdict

    shipping_facts = {
        "city": "جدة",
        "fee_sar": 35.0,
        "source": "kb",
        "need_city": False,
    }
    state = _heavy_state(
        known_facts={
            "shipping_knowledge": shipping_facts,
            "checkout_preparation": {"order_status": "none"},
        },
    )
    raw = asdict(state)
    result = prepare_brain_state_dict_with_telemetry(state, raw)
    retained = (result.get("known_facts") or {}).get("shipping_knowledge") or {}
    assert retained.get("city") == "جدة"
    assert retained.get("fee_sar") == 35.0
    assert retained.get("source") == "kb"


def test_prompt_retains_need_city_shipping_facts_when_slim_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED", "true")
    state = _heavy_state(
        known_facts={
            "shipping_knowledge": {
                "need_city": True,
                "source": "kb",
            },
            "checkout_preparation": {"order_status": "none"},
        },
    )
    prompt = build_brain_reply_prompt(state)
    json_part = prompt.split("BrainStateJSON:", 1)[1]
    assert "shipping_knowledge" in json_part
    assert "need_city" in json_part
    assert "true" in json_part.lower()
