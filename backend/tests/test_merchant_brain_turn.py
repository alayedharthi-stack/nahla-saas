from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.merchant_brain_turn import (
    LiveMerchantBrainPreconditions,
    LiveMerchantBrainTurnInput,
    evaluate_live_merchant_brain_turn,
    require_explicit_tenant_id,
)


def _trace() -> SimpleNamespace:
    return SimpleNamespace(
        brain_called=False,
        brain_silent=False,
        response_goal="",
        response_mode="",
        reply_source="",
        fallback_source="",
        chosen_path="",
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


def _run(
    brain_result,
    *,
    tenant_id: int | None = 55001,
    preconditions: LiveMerchantBrainPreconditions | None = None,
    convo: SimpleNamespace | None = None,
    db: MagicMock | None = None,
):
    brain = MagicMock()
    if isinstance(brain_result, BaseException):
        brain.process = AsyncMock(side_effect=brain_result)
    else:
        brain.process = AsyncMock(return_value=brain_result)
    conversation = convo or _convo()
    session = db or MagicMock()
    trace = _trace()
    persona = MagicMock()

    async def invoke():
        return await evaluate_live_merchant_brain_turn(
            db=session,
            tenant_id=tenant_id,
            phone_id="PH-GENERIC",
            turn_input=LiveMerchantBrainTurnInput(
                customer_phone="966500000123",
                text="generic product inquiry",
                conversation_id=conversation.id,
                history=[],
                preconditions=preconditions or LiveMerchantBrainPreconditions(),
                profile={"id": 9, "name": "Generic Customer"},
            ),
            convo=conversation,
            trace=trace,
            persona_ownership=persona,
            brain_factory=lambda: brain,
            brain_active=True,
        )

    result = asyncio.run(invoke())
    return result, brain, session, trace, persona, conversation


def test_explicit_tenant_id_is_required_without_fallback() -> None:
    with pytest.raises(ValueError, match="tenant_id is required"):
        require_explicit_tenant_id(None)

    with pytest.raises(ValueError, match="tenant_id is required"):
        _run({"reply": "unused"}, tenant_id=None)


def test_live_normal_reply_buttons_and_provenance_parity() -> None:
    result, brain, *_ = _run(
        {
            "reply": "catalog answer candidate",
            "buttons": [{"id": "catalog-item"}],
            "handoff": False,
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {
                "surface": "kb_product_answer",
                "source": "catalog",
            },
            "compose_source": "persona_llm",
        }
    )

    assert result.status == "evaluated"
    assert result.reply_text == "catalog answer candidate"
    assert result.brain_buttons == [{"id": "catalog-item"}]
    assert result.provenance.compose_source == "persona_llm"
    assert result.provenance.chosen_path == "fact_bound_persona_compose"
    assert result.provenance.llm_candidate_present is True
    brain.process.assert_awaited_once()


def test_live_billing_denied_is_silent() -> None:
    result, *_ = _run(
        {
            "skipped": True,
            "reason": "billing_access_denied",
            "reply": None,
        }
    )

    assert result.status == "evaluated"
    assert result.billing_denied is True
    assert result.reply_text == ""
    assert result.provenance.fallback_source == "billing_access_denied"


def test_live_brain_silent_uses_existing_registered_fallback() -> None:
    with patch(
        "services.merchant_brain_turn._empty_reply_fallback",
        return_value="registered-emergency-fallback",
    ):
        result, *_ = _run({"reply": "", "buttons": [], "handoff": False})

    assert result.status == "evaluated"
    assert result.brain_silent is True
    assert result.reply_text == "registered-emergency-fallback"
    assert result.provenance.final_text_transformed is False


def test_live_handoff_executes_existing_persistence_mutations() -> None:
    convo = _convo()
    db = MagicMock()
    with patch("handoff.manager.create_handoff_session") as create_handoff:
        result, *_ = _run(
            {
                "reply": "brain handoff candidate",
                "buttons": [],
                "handoff": True,
            },
            convo=convo,
            db=db,
        )

    assert result.brain_handoff is True
    create_handoff.assert_called_once()
    db.flush.assert_called_once()
    assert convo.status == "human"
    assert convo.is_human_handoff is True
    assert convo.needs_human is True
    assert convo.handoff_active is True


def test_live_outbound_lock_blocks_brain_call() -> None:
    result, brain, *_ = _run(
        {"reply": "must-not-run"},
        preconditions=LiveMerchantBrainPreconditions(
            outbound_lock_available=False,
        ),
    )

    assert result.status == "outbound_locked"
    brain.process.assert_not_called()


def test_live_pause_precondition_skips_brain_call() -> None:
    result, brain, *_ = _run(
        {"reply": "must-not-run"},
        preconditions=LiveMerchantBrainPreconditions(
            skip_ai=True,
            skip_reason="human_takeover",
        ),
    )

    assert result.status == "suppressed"
    brain.process.assert_not_called()


def test_live_brain_exception_rolls_back_for_webhook_fallback() -> None:
    db = MagicMock()
    result, brain, *_ = _run(RuntimeError("brain-failed"), db=db)

    assert result.status == "brain_exception"
    assert isinstance(result.brain_exception, RuntimeError)
    brain.process.assert_awaited_once()
    db.rollback.assert_called_once()

