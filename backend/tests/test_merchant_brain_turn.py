from __future__ import annotations

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.merchant_brain_turn import (
    LiveMerchantBrainPreconditions,
    LiveMerchantBrainTurnInput,
    _build_provenance,
    _resolve_response_mode,
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
    assert result.provenance.response_mode == "persona"
    assert result.provenance.llm_candidate_present is True
    assert result.reply_text == result.brain_reply_candidate
    assert result.provenance.final_text_transformed is False
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


def test_live_handoff_persistence_failure_keeps_brain_reply() -> None:
    convo = _convo()
    db = MagicMock()
    with patch(
        "handoff.manager.create_handoff_session",
        side_effect=RuntimeError("handoff-write-failed"),
    ):
        result, *_ = _run(
            {
                "reply": "brain handoff candidate",
                "buttons": [],
                "handoff": True,
            },
            convo=convo,
            db=db,
        )

    assert result.status == "evaluated"
    assert result.reply_text == "brain handoff candidate"
    assert result.brain_handoff is True
    assert convo.status == "active"


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


def test_live_brain_exception_returns_to_webhook_without_rollback() -> None:
    db = MagicMock()
    result, brain, *_ = _run(RuntimeError("brain-failed"), db=db)

    assert result.status == "brain_exception"
    assert isinstance(result.brain_exception, RuntimeError)
    brain.process.assert_awaited_once()
    db.rollback.assert_not_called()


def test_webhook_safe_fallback_send_failure_is_fail_open_and_traced() -> None:
    from routers.whatsapp_webhook import _handle_merchant_message
    from services import turn_trace as turn_trace_service
    from services.fallback_policy import FALLBACK_KIND_SOFT_RETRY

    db = MagicMock()
    convo = SimpleNamespace(
        id=42,
        tenant_id=55001,
        customer_id=7,
        ai_paused=False,
        ai_paused_reason=None,
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        taken_over_by=None,
        status="active",
        extra_metadata={},
    )
    state = SimpleNamespace(turn=0, stage="active", order_prep={})
    mode = SimpleNamespace(
        mode="live_chat",
        identity_topic="",
        source="",
        lease=SimpleNamespace(locked_until=None),
        to_log_dict=lambda: {},
    )
    brain = MagicMock()
    brain.process = AsyncMock(side_effect=RuntimeError("brain-failed"))
    send = AsyncMock(side_effect=RuntimeError("provider-send-failed"))
    captured_traces = []
    real_new_trace = turn_trace_service.new_trace

    def capture_trace(**kwargs):
        trace = real_new_trace(**kwargs)
        captured_traces.append(trace)
        return trace

    fallback = SimpleNamespace(
        kind=FALLBACK_KIND_SOFT_RETRY,
        text="registered-safe-fallback",
        response_goal="clarify",
        rationale="brain_exception",
    )

    with ExitStack() as stack:
        stack.enter_context(patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None, conversation=convo),
        ))
        stack.enter_context(patch(
            "modules.operations.structured_admin_contact_policy.evaluate_structured_admin_contact_policy",
            return_value=None,
        ))
        stack.enter_context(patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.StateManager.save_message"))
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.load_history",
            return_value=[{"direction": "inbound", "body": "generic inquiry"}],
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.load",
            return_value=state,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.StateManager.save"))
        stack.enter_context(patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True,
                used_total=0,
                limit=1000,
                reason="",
            ),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.conversational_priority.has_payment_outbound_consent",
            return_value=False,
        ))
        stack.enter_context(patch(
            "modules.ai.routing.conversation_mode.resolve_conversation_mode",
            return_value=mode,
        ))
        stack.enter_context(patch("modules.ai.routing.conversation_mode.save_lease"))
        stack.enter_context(patch(
            "core.ownership_state.resolve_ownership_state",
            return_value=SimpleNamespace(state="ai_active", takeover_class=""),
        ))
        stack.enter_context(patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=SimpleNamespace(released=False, reason=""),
        ))
        stack.enter_context(patch(
            "core.ai_pause_guard.should_skip_ai",
            return_value=(False, None),
        ))
        stack.enter_context(patch(
            "modules.ai.order_flow_v2.owner.try_handle_order_flow_v2",
            return_value=SimpleNamespace(handled=False, reason="not_handled"),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.inbound_fragment_guard.evaluate_duplicate_fragment_turn",
            return_value=SimpleNamespace(
                process_turn=True,
                send_clarification_once=False,
                reason="",
            ),
        ))
        customer_service = stack.enter_context(patch(
            "services.customer_intelligence.CustomerIntelligenceService",
        ))
        customer_service.return_value.upsert_lead_customer.return_value = SimpleNamespace(
            id=7,
            name="",
            email="",
        )
        customer_service.return_value.ensure_profile.return_value = SimpleNamespace(
            segment="",
            customer_status="",
            rfm_segment="",
            is_returning=False,
            total_orders=0,
            total_spend_sar=0.0,
            last_order_at=None,
        )
        stack.enter_context(patch(
            "modules.ai.brain.pipeline.get_brain",
            return_value=brain,
        ))
        stack.enter_context(patch(
            "core.billing.has_billing_access",
            return_value=True,
        ))
        stack.enter_context(patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_shadow_enabled",
            return_value=False,
        ))
        stack.enter_context(patch(
            "services.fallback_policy.choose_intent_aware_fallback",
            return_value=fallback,
        ))
        stack.enter_context(patch(
            "core.store_knowledge.build_ai_context",
            return_value={},
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook._send_whatsapp_message",
            new=send,
        ))
        stack.enter_context(patch(
            "services.turn_trace.new_trace",
            side_effect=capture_trace,
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook.MERCHANT_BRAIN_ENABLED",
            True,
        ))

        asyncio.run(_handle_merchant_message(
            phone_id="PH-GENERIC",
            to="966500000123",
            text="generic inquiry",
            tenant_id=55001,
            db=db,
        ))

    assert send.await_count == 1
    assert captured_traces
    trace = captured_traces[0]
    assert trace.outbound_error == "RuntimeError"
    assert trace.reply_source == turn_trace_service.SOURCE_BRAIN_EXCEPTION
    assert trace.outbound_sent is True


def test_resolve_response_mode_prefers_brain_result_then_persona_event() -> None:
    trace = _trace()
    trace.response_goal = "answer"
    assert _resolve_response_mode(
        brain_result={"response_mode": "llm"},
        brain_persona_compose_event={"response_mode": "persona"},
        trace=trace,
        compose_source="persona_llm",
        chosen_path="fact_bound_persona_compose",
        llm_candidate_present=True,
    ) == "llm"
    assert _resolve_response_mode(
        brain_result={},
        brain_persona_compose_event={"response_mode": "trusted_coupon_offer_answer"},
        trace=trace,
        compose_source="persona_llm",
        chosen_path="trusted_coupon_offer_compose",
        llm_candidate_present=True,
    ) == "trusted_coupon_offer_answer"


def test_resolve_response_mode_does_not_alias_response_goal() -> None:
    trace = _trace()
    trace.response_goal = "answer"
    assert _resolve_response_mode(
        brain_result={},
        brain_persona_compose_event=None,
        trace=trace,
        compose_source="persona_llm",
        chosen_path="fact_bound_persona_compose",
        llm_candidate_present=True,
    ) == "persona"


def test_resolve_response_mode_derives_llm_and_persona_defaults() -> None:
    trace = _trace()
    assert _resolve_response_mode(
        brain_result={},
        brain_persona_compose_event=None,
        trace=trace,
        compose_source="llm",
        chosen_path="llm_general_reply",
        llm_candidate_present=True,
    ) == "llm"
    assert _resolve_response_mode(
        brain_result={},
        brain_persona_compose_event=None,
        trace=trace,
        compose_source="persona_llm",
        chosen_path="generic_catalog_answer",
        llm_candidate_present=True,
    ) == "persona"


def test_build_provenance_preserves_llm_candidate_without_transform() -> None:
    trace = _trace()
    trace.response_goal = "answer"
    provenance = _build_provenance(
        brain_result={
            "compose_source": "persona_llm",
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {"surface": "catalog_product_answer"},
        },
        brain_reply_candidate="catalog answer candidate",
        reply_text="catalog answer candidate",
        brain_persona_compose_event={
            "chosen_path": "fact_bound_persona_compose",
            "compose_source": "persona_llm",
            "llm_candidate_present": True,
            "final_text_transformed": False,
            "final_transform_reasons": [],
        },
        trace=trace,
    )
    assert provenance.compose_source == "persona_llm"
    assert provenance.response_mode == "persona"
    assert provenance.chosen_path == "fact_bound_persona_compose"
    assert provenance.llm_candidate_present is True
    assert provenance.final_text_transformed is False
    assert provenance.final_transform_reasons == []


def test_build_provenance_merges_specialized_response_mode_from_brain_result() -> None:
    provenance = _build_provenance(
        brain_result={
            "compose_source": "persona_llm",
            "response_mode": "customer_conditional_coupon_answer",
            "chosen_path": "customer_conditional_coupon_compose",
        },
        brain_reply_candidate="coupon answer candidate",
        reply_text="coupon answer candidate",
        brain_persona_compose_event={
            "chosen_path": "customer_conditional_coupon_compose",
            "compose_source": "persona_llm",
            "response_mode": "customer_conditional_coupon_answer",
            "llm_candidate_present": True,
            "final_text_transformed": False,
            "final_transform_reasons": [],
        },
        trace=_trace(),
    )
    assert provenance.response_mode == "customer_conditional_coupon_answer"
    assert provenance.final_text_transformed is False


def test_build_provenance_does_not_infer_llm_candidate_from_reply_text_alone() -> None:
    provenance = _build_provenance(
        brain_result={},
        brain_reply_candidate="generic outbound candidate",
        reply_text="generic outbound candidate",
        brain_persona_compose_event=None,
        trace=_trace(),
    )
    assert provenance.llm_candidate_present is False
    assert provenance.compose_source == ""
    assert provenance.final_text_transformed is False
    assert provenance.final_transform_reasons == []


def test_build_provenance_preserves_deterministic_emergency_fallback() -> None:
    provenance = _build_provenance(
        brain_result={
            "compose_source": "fallback_deterministic",
            "chosen_path": "track_order_need_identifiers_emergency",
            "fallback_reason": "compose_failure",
            "fallback_action_type": "track_order_need_identifiers",
            "llm_candidate_present": False,
        },
        brain_reply_candidate="deterministic fallback line",
        reply_text="deterministic fallback line",
        brain_persona_compose_event={
            "compose_source": "fallback_deterministic",
            "chosen_path": "track_order_need_identifiers_emergency",
            "llm_candidate_present": False,
            "fallback_reason": "compose_failure",
            "fallback_action_type": "track_order_need_identifiers",
        },
        trace=_trace(),
    )
    assert provenance.compose_source == "fallback_deterministic"
    assert provenance.llm_candidate_present is False
    assert provenance.fallback_reason == "compose_failure"
    assert provenance.fallback_action_type == "track_order_need_identifiers"


def test_build_provenance_merges_guard_transform_reasons_from_live_tracker() -> None:
    provenance = _build_provenance(
        brain_result={
            "compose_source": "persona_llm",
            "chosen_path": "fact_bound_persona_compose",
            "llm_candidate_present": True,
        },
        brain_reply_candidate="compose candidate unchanged",
        reply_text="guard adjusted candidate",
        brain_persona_compose_event={
            "compose_source": "persona_llm",
            "chosen_path": "fact_bound_persona_compose",
            "llm_candidate_present": True,
        },
        trace=_trace(),
        live_provenance_tracker={
            "final_transform_reasons": ["payment_reply_guard"],
        },
    )
    assert provenance.final_text_transformed is True
    assert provenance.final_transform_reasons == ["payment_reply_guard"]
    assert provenance.llm_candidate_present is True
    assert provenance.compose_source == "persona_llm"

