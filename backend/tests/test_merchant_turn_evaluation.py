from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.merchant_turn_evaluation import (
    INTERNAL_READ_ONLY_EXECUTION_POLICY,
    LIVE_EXECUTION_POLICY,
    DeferredActionKind,
    MerchantTurnGateways,
    MerchantTurnPreconditions,
    NormalizedMerchantTurnInput,
    RecordingWriteSink,
    RejectingProviderSink,
    evaluate_merchant_turn,
    max_outbound_overlap,
    require_explicit_tenant_id,
    reply_carries_new_signal,
)


def test_require_explicit_tenant_id_rejects_missing() -> None:
    with pytest.raises(ValueError, match="tenant_id is required"):
        require_explicit_tenant_id(None)


def test_require_explicit_tenant_id_accepts_explicit_value() -> None:
    assert require_explicit_tenant_id(90210) == 90210


def _trace() -> SimpleNamespace:
    return SimpleNamespace(
        brain_called=False,
        response_goal="",
        reply_source="",
        fallback_source="",
        handoff_triggered=False,
    )


def _convo() -> SimpleNamespace:
    return SimpleNamespace(
        id=77,
        extra_metadata={"brain_state": {"last_action": "ACTION_LLM_REPLY"}},
        status="active",
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
    )


async def _evaluate(**kwargs):
    pre = kwargs.pop("preconditions", MerchantTurnPreconditions(brain_active=True))
    text = kwargs.pop("text", "استفسار عام عن المنتج")
    convo = kwargs["convo"]
    return await evaluate_merchant_turn(
        phone_id="PH-GENERIC",
        turn_input=NormalizedMerchantTurnInput(
            customer_phone="966500000123",
            text=text,
            history=[],
            preconditions=pre,
            profile={"id": 9, "name": "أحمد سالم"},
            conversation_id=convo.id,
        ),
        brain_active=True,
        **kwargs,
    )


def test_internal_read_only_defers_handoff_writes() -> None:
    brain = MagicMock()
    brain.process = AsyncMock(
        return_value={
            "reply": "reply-from-brain",
            "buttons": [],
            "handoff": True,
            "chosen_path": "llm",
        }
    )
    write_sink = RecordingWriteSink()
    convo = _convo()
    trace = _trace()
    persona = MagicMock()

    result = asyncio.run(
        _evaluate(
            db=MagicMock(),
            tenant_id=90210,
            gateways=MerchantTurnGateways(brain_factory=lambda: brain, write_sink=write_sink),
            convo=convo,
            trace=trace,
            persona_ownership=persona,
            execution_policy=INTERNAL_READ_ONLY_EXECUTION_POLICY,
        )
    )

    assert result.status == "evaluated"
    assert result.brain_handoff is True
    assert any(action.kind == DeferredActionKind.CREATE_HANDOFF_SESSION for action in result.deferred_actions)
    assert any(action.kind == DeferredActionKind.UPDATE_CONVERSATION_HANDOFF for action in result.deferred_actions)
    assert write_sink.attempts
    assert convo.status == "active"
    brain.process.assert_awaited_once()


def test_internal_read_only_rejects_provider_sink_invocation() -> None:
    sink = RejectingProviderSink()
    with pytest.raises(RuntimeError, match="provider dispatch blocked"):
        sink.record_attempt(channel="whatsapp", action="send_text", payload={"tenant_id": 90210})


def test_live_policy_parity_with_injected_brain() -> None:
    brain = MagicMock()
    brain.process = AsyncMock(
        return_value={
            "reply": "catalog-price-answer",
            "buttons": [{"id": "buy"}],
            "handoff": False,
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {"surface": "kb_product_answer", "source": "catalog"},
            "compose_source": "persona_llm",
        }
    )
    convo = _convo()
    trace = _trace()
    persona = MagicMock()

    result = asyncio.run(
        _evaluate(
            db=MagicMock(),
            tenant_id=55001,
            gateways=MerchantTurnGateways(brain_factory=lambda: brain),
            convo=convo,
            trace=trace,
            persona_ownership=persona,
            execution_policy=LIVE_EXECUTION_POLICY,
            text="كم سعر القميص القطني؟",
        )
    )

    assert result.status == "evaluated"
    assert result.reply_text == "catalog-price-answer"
    assert result.brain_buttons
    assert result.provenance.llm_candidate_present is True
    assert result.provenance.chosen_path == "fact_bound_persona_compose"
    brain.process.assert_awaited_once()


def test_skip_ai_precondition_suppresses_evaluation() -> None:
    brain = MagicMock()
    brain.process = AsyncMock(return_value={"reply": "should-not-run", "buttons": []})
    result = asyncio.run(
        _evaluate(
            db=MagicMock(),
            tenant_id=55002,
            gateways=MerchantTurnGateways(brain_factory=lambda: brain),
            convo=SimpleNamespace(id=1, extra_metadata={}),
            trace=_trace(),
            persona_ownership=MagicMock(),
            preconditions=MerchantTurnPreconditions(brain_active=True, skip_ai=True),
        )
    )
    assert result.status == "suppressed"
    brain.process.assert_not_called()


def test_outbound_lock_precondition_blocks_evaluation() -> None:
    brain = MagicMock()
    brain.process = AsyncMock(return_value={"reply": "blocked", "buttons": []})
    result = asyncio.run(
        _evaluate(
            db=MagicMock(),
            tenant_id=55003,
            gateways=MerchantTurnGateways(brain_factory=lambda: brain),
            convo=_convo(),
            trace=_trace(),
            persona_ownership=MagicMock(),
            preconditions=MerchantTurnPreconditions(
                brain_active=True,
                outbound_lock_available=False,
            ),
        )
    )
    assert result.status == "outbound_locked"
    brain.process.assert_not_called()


def test_dedup_helpers_are_signal_aware() -> None:
    history = [
        {"direction": "outbound", "body": "نفس الرد السابق للعميل حول المنتج"},
        {"direction": "inbound", "body": "سؤال"},
    ]
    overlap = max_outbound_overlap("نفس الرد السابق للعميل حول المنتج", history)
    assert overlap >= 0.85
    assert reply_carries_new_signal("تفضل https://example.test/product") is True
