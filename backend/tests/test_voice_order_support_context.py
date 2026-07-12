"""Voice + history-aware order-support ownership over stale checkout."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: E402
from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    should_block_order_draft_injection,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    build_order_support_follow_up_args,
    is_order_reference_continuity_active,
    try_order_reference_continuity_decision,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402
from modules.ai.media.routing_guard import (  # noqa: E402
    is_audio_without_trusted_transcript,
    resolve_inbound_semantic_routing,
    resolve_semantic_customer_message,
    should_route_unclear_audio_to_existing_order_support,
)
from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: E402
    EXISTING_ORDER_SUPPORT,
    evaluate_stale_checkout_suppression,
    should_yield_to_existing_order_support,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402

GENERIC_ORDER_REF = "284719365"
GENERIC_ORDER_REF_OLD = "111222333"
GENERIC_ORDER_REF_NEW = "444555666"
GENERIC_PRODUCT = "حذاء رياضي أبيض"
VOICE_SHIPPING = "الطلب متأخر والشحن ما وصل"
AUDIO_FALLBACK = "ما قدرنا نسمع الرسالة الصوتية"


def _run(coro):
    return asyncio.run(coro)


def _pending_history() -> list[dict]:
    return [{"direction": "in", "body": GENERIC_ORDER_REF}]


def _stale_prep() -> dict:
    return {
        "draft_order_id": "draft-16",
        "draft_order_reference": "NHL-1-000016",
        "order_creation_status": "created",
        "order_status": "pending_customer_info",
        "line_items": [{"name": GENERIC_PRODUCT, "qty": 1}],
    }


def _not_found_follow_up_history() -> list[dict]:
    return [
        {"direction": "in", "body": GENERIC_ORDER_REF},
        {"direction": "out", "body": "لم نجد الطلب"},
    ]


def _expired_ref_history() -> list[dict]:
    filler = [{"direction": "in", "body": f"سؤال عام {i}"} for i in range(10)]
    return [{"direction": "in", "body": GENERIC_ORDER_REF_OLD}] + filler


def _inactive_ofv2_patches(*, history: list | None = None):
    return (
        patch(
            "modules.ai.order_flow_v2.owner._load_brain_state",
            return_value=(SimpleNamespace(id=9), {"order_prep": _stale_prep()}),
        ),
        patch(
            "modules.ai.order_flow_v2.owner.operational_tuple",
            return_value=(True, False, ""),
        ),
        patch(
            "modules.ai.order_flow_v2.owner.load_local_draft_evidence",
            return_value=None,
        ),
        patch(
            "modules.ai.order_flow_v2.owner.rehydrate_order_prep_patch",
            return_value={},
        ),
        patch(
            "modules.ai.order_flow_v2.owner.active_whatsapp_checkout",
            return_value=False,
        ),
        patch(
            "modules.ai.order_flow_v2.owner.checkout_has_items",
            return_value=True,
        ),
        patch(
            "modules.ai.order_flow_v2.owner.pending_order_exists",
            return_value=True,
        ),
        patch(
            "core.conversation_engine.StateManager.load_history",
            return_value=history or _pending_history(),
        ),
    )


def _brain_ordering_ctx(
    message: str,
    *,
    history: list | None = None,
    intent_name: str = INTENT_ASK_PRODUCT,
    inbound_metadata: dict | None = None,
) -> BrainContext:
    st = MerchantConversationState()
    st.stage = STAGE_ORDERING
    st.current_product_focus = {
        "title": GENERIC_PRODUCT,
        "external_id": "prod-generic-1",
    }
    op = OrderPreparationState()
    for key, value in _stale_prep().items():
        setattr(op, key, value)
    st.order_prep = op
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000099",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.85,
            slots={},
            raw_message=message,
            extraction_method="rules",
        ),
        state=st,
        facts=CommerceFacts(orderable=True, has_products=True),
        history=history or _pending_history(),
        profile={"inbound_metadata": inbound_metadata or {}},
    )


def _audio_meta(*, transcript: str = "", transcript_status: str = "empty") -> dict:
    meta = {"type": "audio", "transcript_status": transcript_status}
    if transcript:
        meta["transcript"] = transcript
    return meta


def _suppress(
    message: str,
    *,
    history: list | None = None,
    inbound_metadata: dict | None = None,
) -> bool:
    decision = evaluate_stale_checkout_suppression(
        message=message,
        inbound_metadata=inbound_metadata,
        order_prep=_stale_prep(),
        brain_state={"order_prep": _stale_prep()},
        history=history or [],
        checkout_active=True,
        draft_active=True,
    )
    return decision.suppress


def _dispatch_routing(
    *,
    brain_text: str = "",
    inbound_metadata: dict | None = None,
    normalized_type: str = "audio",
    history: list | None = None,
):
    return resolve_inbound_semantic_routing(
        brain_text=brain_text,
        inbound_metadata=inbound_metadata,
        inbound_normalized_type=normalized_type,
        history=history or [],
    )


def _would_take_media_fallback(routing, *, fallback_reply_ar: str = AUDIO_FALLBACK) -> bool:
    return (
        not routing.semantic_text
        and not routing.route_unclear_audio_order_support
        and bool(fallback_reply_ar)
    )


class TestDispatchSemanticRouting:
    def test_t1_transcript_reaches_dispatch_and_ofv2_layers(self) -> None:
        routing = _dispatch_routing(
            inbound_metadata={"type": "audio", "transcript_text": VOICE_SHIPPING},
            history=_pending_history(),
        )
        assert routing.semantic_text == VOICE_SHIPPING
        assert routing.route_unclear_audio_order_support is False
        assert not _would_take_media_fallback(routing)

        db = MagicMock()
        draft_ev = SimpleNamespace(
            active=True,
            order_id="draft-16",
            reference="NHL-1-000016",
            external_order_number="NHL-1-000016",
        )
        with patch(
            "modules.ai.order_flow_v2.owner._load_brain_state",
            return_value=(SimpleNamespace(id=9), {"order_prep": _stale_prep()}),
        ), patch(
            "modules.ai.order_flow_v2.owner.operational_tuple",
            return_value=(True, False, ""),
        ), patch(
            "modules.ai.order_flow_v2.owner.load_local_draft_evidence",
            return_value=draft_ev,
        ), patch(
            "modules.ai.order_flow_v2.owner.rehydrate_order_prep_patch",
            return_value={},
        ), patch(
            "modules.ai.order_flow_v2.owner.active_whatsapp_checkout",
            return_value=True,
        ), patch(
            "modules.ai.order_flow_v2.owner.checkout_has_items",
            return_value=True,
        ), patch(
            "modules.ai.order_flow_v2.owner.pending_order_exists",
            return_value=True,
        ), patch(
            "core.conversation_engine.StateManager.load_history",
            return_value=_pending_history(),
        ):
            result = try_handle_order_flow_v2(
                db,
                tenant_id=1,
                customer_phone="966500000099",
                message="",
                inbound_metadata={"type": "audio", "transcript_text": VOICE_SHIPPING},
                inbound_normalized_type="audio",
            )
        assert result.handled is False
        assert "explicit_intent_suppressed" in (result.reason or "")

        ctx = SimpleNamespace(
            message=routing.semantic_text,
            history=_pending_history(),
            state=SimpleNamespace(
                draft_order_id="draft-16",
                order_prep=SimpleNamespace(**_stale_prep()),
            ),
            commerce_bundle={},
            profile={"inbound_metadata": {"type": "audio", "transcript_text": VOICE_SHIPPING}},
            tenant_id=1,
        )
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "existing_order_support"

    def test_t2_text_body_unchanged(self) -> None:
        msg = f"الطلب فيه {GENERIC_PRODUCT}"
        routing = _dispatch_routing(
            brain_text=msg,
            inbound_metadata={"normalized_type": "text"},
            normalized_type="text",
        )
        assert routing.semantic_text == msg
        assert routing.route_unclear_audio_order_support is False


class TestOrderFlowV2HistoryAwareSuppression:
    def test_t3_pending_ref_voice_shipping_suppresses_checkout(self) -> None:
        assert _suppress(
            VOICE_SHIPPING,
            history=_pending_history(),
            inbound_metadata=_audio_meta(transcript=VOICE_SHIPPING),
        )

    def test_t4_pending_ref_text_shipping_suppresses_checkout(self) -> None:
        assert _suppress(VOICE_SHIPPING, history=_pending_history())

    def test_t5_pending_ref_product_clarification_suppresses_checkout(self) -> None:
        msg = f"الطلب فيه {GENERIC_PRODUCT}"
        assert _suppress(msg, history=_pending_history())

    def test_t6_pending_ref_placed_order_statement_suppresses_checkout(self) -> None:
        assert _suppress("خلاص طلبت", history=_pending_history())

    def test_t7_dispatch_unclear_audio_pending_ref_reaches_order_support(self) -> None:
        routing = _dispatch_routing(
            inbound_metadata=_audio_meta(),
            history=_pending_history(),
        )
        assert routing.semantic_text == ""
        assert routing.route_unclear_audio_order_support is True
        assert not _would_take_media_fallback(routing)

        assert _suppress(
            "",
            history=_pending_history(),
            inbound_metadata=_audio_meta(),
        )

        ctx = SimpleNamespace(
            message="",
            history=_pending_history(),
            state=SimpleNamespace(
                draft_order_id="draft-16",
                order_prep=SimpleNamespace(**_stale_prep()),
            ),
            commerce_bundle={},
            profile={"inbound_metadata": _audio_meta()},
            tenant_id=1,
        )
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "existing_order_support"
        assert dec.args.get("unclear_audio") is True

    def test_t8_no_ref_social_audio_keeps_media_fallback(self) -> None:
        routing = _dispatch_routing(
            inbound_metadata=_audio_meta(),
            history=[],
        )
        assert routing.route_unclear_audio_order_support is False
        assert _would_take_media_fallback(routing)

        decision = evaluate_stale_checkout_suppression(
            message="هلا كيفك",
            inbound_metadata=_audio_meta(transcript="هلا كيفك"),
            order_prep=_stale_prep(),
            history=[],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.detected_intent != EXISTING_ORDER_SUPPORT

    def test_t10_stale_draft_no_ref_tamam_continues_checkout(self) -> None:
        assert not _suppress("تمام", history=[])


class TestBrainAndDraftGuards:
    def test_t9_paid_order_shipping_post_order_preserved(self) -> None:
        op = OrderPreparationState()
        op.payment_receipt_received = True
        op.order_status = "processing"
        st = MerchantConversationState()
        st.order_prep = op
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000099",
            message="اي فرع ارسلتو طلبي في سمسا",
            intent=Intent(
                name=INTENT_ASK_SHIPPING,
                confidence=0.9,
                slots={},
                raw_message="اي فرع ارسلتو طلبي في سمسا",
                extraction_method="rules",
            ),
            state=st,
            facts=CommerceFacts(),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.args.get("topic") == "shipping_post_order"

    def test_t11_draft_injection_blocked_after_order_support(self) -> None:
        history = _pending_history()
        prep = _stale_prep()
        state = SimpleNamespace(
            draft_order_id="draft-16",
            order_prep=SimpleNamespace(**prep),
        )
        state.to_dict = lambda: {  # type: ignore[attr-defined]
            "draft_order_id": "draft-16",
            "order_prep": prep,
        }
        blocked = should_block_order_draft_injection(
            brain_state=state,
            customer_message=VOICE_SHIPPING,
            history=history,
        )
        assert blocked is True
        reply = maybe_inject_draft_flow_reply(
            reply="",
            order_prep=state.order_prep,
            brain_state=state,
            customer_message=VOICE_SHIPPING,
            history=history,
        )
        assert reply == ""

    def test_t11_draft_injection_blocked_for_unclear_audio_path(self) -> None:
        history = _pending_history()
        prep = _stale_prep()
        state = SimpleNamespace(
            draft_order_id="draft-16",
            order_prep=SimpleNamespace(**prep),
        )
        state.to_dict = lambda: {  # type: ignore[attr-defined]
            "draft_order_id": "draft-16",
            "order_prep": prep,
        }
        meta = {**_audio_meta(), "route_unclear_audio_order_support": True}
        assert should_block_order_draft_injection(
            brain_state=state,
            customer_message="",
            history=history,
            inbound_metadata=meta,
        )
        assert maybe_inject_draft_flow_reply(
            reply="",
            order_prep=state.order_prep,
            brain_state=state,
            customer_message="",
            history=history,
        ) == ""

    def test_brain_continuity_routes_voice_shipping_to_order_support(self) -> None:
        history = _pending_history()
        ctx = SimpleNamespace(
            message=VOICE_SHIPPING,
            history=history,
            state=SimpleNamespace(
                draft_order_id="draft-16",
                order_prep=SimpleNamespace(**_stale_prep()),
            ),
            commerce_bundle={},
            profile={"inbound_metadata": _audio_meta(transcript=VOICE_SHIPPING)},
            tenant_id=1,
        )
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "existing_order_support"


def _merchant_handler_db() -> MagicMock:
    db = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


def _merchant_handler_convo() -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        tenant_id=1,
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


class TestMerchantWebhookUnclearAudioRoute:
    def test_merchant_handler_non_empty_text_reaches_save_message(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        save_mock = MagicMock()

        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "routers.whatsapp_webhook.StateManager.save_message",
            save_mock,
        ), patch(
            "routers.whatsapp_webhook.StateManager.load_history",
            return_value=[],
        ), patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        ), patch(
            "modules.ai.brain.pipeline.get_brain",
        ) as mock_brain, patch(
            "modules.ai.routing.conversation_mode.resolve_conversation_mode",
        ), patch(
            "modules.ai.routing.conversation_mode.save_lease",
        ), patch(
            "core.ownership_state.resolve_ownership_state",
            return_value=SimpleNamespace(state="ai_active", takeover_class=""),
        ), patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=SimpleNamespace(released=False, reason=""),
        ), patch(
            "core.ai_pause_guard.should_skip_ai",
            return_value=(False, None),
        ), patch(
            "modules.ai.order_flow_v2.owner.try_handle_order_flow_v2",
            return_value=SimpleNamespace(handled=False, reason="not_handled"),
        ):
            mock_brain.return_value.process = AsyncMock(
                return_value={
                    "reply": "",
                    "buttons": [],
                    "decision": SimpleNamespace(action=ACTION_LLM_REPLY, args={}),
                },
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000099",
                text=GENERIC_ORDER_REF,
                tenant_id=1,
                db=db,
            ))

        save_mock.assert_called()
        first_call = save_mock.call_args_list[0][0]
        assert first_call[2] == GENERIC_ORDER_REF
        assert first_call[3] == "inbound"

    def test_merchant_handler_bare_order_ref_reaches_checkout_suppression(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        ofv2_mock = MagicMock(
            return_value=SimpleNamespace(
                handled=False,
                reason="explicit_intent_suppressed:existing_order_support",
            ),
        )

        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "routers.whatsapp_webhook.StateManager.save_message",
        ), patch(
            "routers.whatsapp_webhook.StateManager.load_history",
            return_value=_pending_history(),
        ), patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        ), patch(
            "modules.ai.brain.pipeline.get_brain",
        ) as mock_brain, patch(
            "modules.ai.routing.conversation_mode.resolve_conversation_mode",
        ), patch(
            "modules.ai.routing.conversation_mode.save_lease",
        ), patch(
            "core.ownership_state.resolve_ownership_state",
            return_value=SimpleNamespace(state="ai_active", takeover_class=""),
        ), patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=SimpleNamespace(released=False, reason=""),
        ), patch(
            "core.ai_pause_guard.should_skip_ai",
            return_value=(False, None),
        ), patch(
            "modules.ai.order_flow_v2.owner.try_handle_order_flow_v2",
            ofv2_mock,
        ):
            mock_brain.return_value.process = AsyncMock(
                return_value={
                    "reply": "",
                    "buttons": [],
                    "decision": SimpleNamespace(
                        action=ACTION_LLM_REPLY,
                        args={"topic": "existing_order_support"},
                    ),
                },
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000099",
                text=GENERIC_ORDER_REF,
                tenant_id=1,
                db=db,
            ))

        ofv2_mock.assert_called_once()

    def test_merchant_handler_allows_empty_text_for_order_support(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = SimpleNamespace(
            id=42,
            tenant_id=1,
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
        db = MagicMock()
        db.commit = MagicMock()
        db.rollback = MagicMock()
        db.add = MagicMock()
        db.flush = MagicMock()
        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.X"}]}

        meta = {**_audio_meta(), "route_unclear_audio_order_support": True}

        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "core.conversation_engine.StateManager.save_message",
        ), patch(
            "core.conversation_engine.StateManager.load_history",
            return_value=_pending_history(),
        ), patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        ), patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ), patch(
            "modules.ai.brain.pipeline.get_brain",
        ) as mock_brain, patch(
            "modules.ai.routing.conversation_mode.resolve_conversation_mode",
        ), patch(
            "modules.ai.routing.conversation_mode.save_lease",
        ), patch(
            "core.ownership_state.resolve_ownership_state",
            return_value=SimpleNamespace(state="ai_active", takeover_class=""),
        ), patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=SimpleNamespace(released=False, reason=""),
        ), patch(
            "core.ai_pause_guard.should_skip_ai",
            return_value=(False, None),
        ), patch(
            "modules.ai.order_flow_v2.owner.try_handle_order_flow_v2",
            return_value=SimpleNamespace(
                handled=False,
                reason="explicit_intent_suppressed:existing_order_support",
            ),
        ):
            mock_brain.return_value.process = AsyncMock(
                return_value={
                    "reply": "نحتاج توضيح بسيط عن طلبك",
                    "buttons": [],
                    "decision": SimpleNamespace(
                        action=ACTION_LLM_REPLY,
                        args={"topic": "existing_order_support", "unclear_audio": True},
                    ),
                },
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000099",
                text="",
                tenant_id=1,
                db=db,
                inbound_metadata=meta,
            ))

        mock_brain.return_value.process.assert_called_once()


class TestSuppressionIntentLabel:
    def test_detected_intent_is_existing_order_support(self) -> None:
        decision = evaluate_stale_checkout_suppression(
            message=VOICE_SHIPPING,
            history=_pending_history(),
            order_prep=_stale_prep(),
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is True
        assert decision.detected_intent == EXISTING_ORDER_SUPPORT


class TestRoutingGuardHelpers:
    def test_unclear_audio_detector_ignores_non_audio(self) -> None:
        assert not is_audio_without_trusted_transcript(
            {"normalized_type": "text"},
            semantic_message="",
            inbound_normalized_type="text",
        )

    def test_pending_ref_required_for_unclear_audio_route(self) -> None:
        assert should_route_unclear_audio_to_existing_order_support(
            inbound_metadata=_audio_meta(),
            semantic_message="",
            inbound_normalized_type="audio",
            history=_pending_history(),
        )
        assert not should_route_unclear_audio_to_existing_order_support(
            inbound_metadata=_audio_meta(),
            semantic_message="",
            inbound_normalized_type="audio",
            history=[],
        )

    def test_stale_draft_alone_does_not_route_unclear_audio_without_history_ref(self) -> None:
        stale_state = {"order_prep": _stale_prep(), "draft_order_id": "draft-16"}
        assert not should_route_unclear_audio_to_existing_order_support(
            inbound_metadata=_audio_meta(),
            semantic_message="",
            inbound_normalized_type="audio",
            history=[],
            brain_state=stale_state,
        )

    def test_spoofed_route_flag_without_audio_evidence_is_ignored(self) -> None:
        meta = {
            "normalized_type": "text",
            "route_unclear_audio_order_support": True,
        }
        assert not should_route_unclear_audio_to_existing_order_support(
            inbound_metadata=meta,
            semantic_message="",
            inbound_normalized_type="text",
            history=_pending_history(),
        )


class TestInactiveCheckoutExistingOrderSupport:
    """Production-shaped path: inactive checkout + stale prep + recent ref."""

    def test_inactive_checkout_voice_shipping_ofv2_yields(self) -> None:
        db = MagicMock()
        patches = _inactive_ofv2_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            result = try_handle_order_flow_v2(
                db,
                tenant_id=1,
                customer_phone="966500000099",
                message=VOICE_SHIPPING,
                inbound_metadata=_audio_meta(transcript=VOICE_SHIPPING),
                inbound_normalized_type="audio",
            )
        assert result.handled is False
        assert result.reason == "explicit_intent_suppressed:existing_order_support"

    def test_inactive_checkout_text_shipping_ofv2_yields(self) -> None:
        db = MagicMock()
        patches = _inactive_ofv2_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            result = try_handle_order_flow_v2(
                db,
                tenant_id=1,
                customer_phone="966500000099",
                message=VOICE_SHIPPING,
                inbound_metadata={"type": "text"},
                inbound_normalized_type="text",
            )
        assert result.handled is False
        assert "explicit_intent_suppressed" in (result.reason or "")

    def test_brain_safety_net_routes_to_existing_order_support(self) -> None:
        ctx = _brain_ordering_ctx(
            VOICE_SHIPPING,
            inbound_metadata=_audio_meta(transcript=VOICE_SHIPPING),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("topic") == "existing_order_support"

    def test_not_found_ref_shipping_follow_up_stays_unverified(self) -> None:
        ownership = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=_not_found_follow_up_history(),
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is True
        assert ownership.order_verified is False
        assert ownership.follow_up_args.get("order_verified") is False

        ctx = _brain_ordering_ctx(VOICE_SHIPPING, history=_not_found_follow_up_history())
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "existing_order_support"
        assert decision.args.get("order_verified") is False
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_verified_order_fixture_preserves_structured_evidence(self) -> None:
        bundle = {
            "active_order_context": {
                "reference": GENERIC_ORDER_REF,
                "order_status": "processing",
                "order_id": "ord-verified-1",
            }
        }
        ownership = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=_pending_history(),
            brain_state={"order_prep": _stale_prep()},
            commerce_bundle=bundle,
        )
        assert ownership.should_yield is True
        assert ownership.order_verified is True

    def test_no_ref_active_checkout_tamam_does_not_yield(self) -> None:
        ownership = should_yield_to_existing_order_support(
            "تمام",
            history=[],
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is False

    def test_no_ref_generic_product_question_unchanged(self) -> None:
        msg = f"كم سعر {GENERIC_PRODUCT}؟"
        ownership = should_yield_to_existing_order_support(
            msg,
            history=[],
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is False

    def test_recent_ref_placed_order_statement_blocked(self) -> None:
        ownership = should_yield_to_existing_order_support(
            "خلاص طلبت",
            history=_pending_history(),
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is True
        assert ownership.detected_intent == EXISTING_ORDER_SUPPORT

    def test_expired_ref_does_not_hijack_product_question(self) -> None:
        msg = f"كم سعر {GENERIC_PRODUCT}؟"
        ownership = should_yield_to_existing_order_support(
            msg,
            history=_expired_ref_history(),
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is False

        ctx = _brain_ordering_ctx(msg, history=_expired_ref_history())
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.args.get("topic") != "existing_order_support"

    def test_explicit_new_order_after_expired_ref_may_checkout(self) -> None:
        ownership = should_yield_to_existing_order_support(
            f"ابي اطلب {GENERIC_PRODUCT}",
            history=_expired_ref_history(),
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is False

    def test_newer_reference_supersedes_older(self) -> None:
        history = [
            {"direction": "in", "body": GENERIC_ORDER_REF_OLD},
            {"direction": "in", "body": "سؤال"},
            {"direction": "in", "body": GENERIC_ORDER_REF_NEW},
        ]
        ownership = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=history,
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is True
        assert ownership.order_reference == GENERIC_ORDER_REF_NEW

    def test_voice_transcript_route_remains_semantic(self) -> None:
        routing = _dispatch_routing(
            inbound_metadata={"type": "audio", "transcript_text": VOICE_SHIPPING},
            history=_pending_history(),
        )
        assert routing.semantic_text == VOICE_SHIPPING

    def test_unclear_audio_with_recent_ref_yields_support(self) -> None:
        ownership = should_yield_to_existing_order_support(
            "",
            inbound_metadata=_audio_meta(),
            history=_pending_history(),
            brain_state={"order_prep": _stale_prep()},
        )
        assert ownership.should_yield is True
        assert ownership.follow_up_args.get("unclear_audio") is True


class TestArchitecturalCorrections:
    """PR #565 review fixes: continuity, parity, not-found args."""

    def _stale_state_dict(self) -> dict:
        return {"order_prep": _stale_prep(), "draft_order_id": "draft-16"}

    def _stale_state_object(self) -> MerchantConversationState:
        st = MerchantConversationState()
        op = OrderPreparationState()
        for key, value in _stale_prep().items():
            setattr(op, key, value)
        st.order_prep = op
        st.draft_order_id = "draft-16"
        return st

    def test_dict_and_object_state_parity(self) -> None:
        history = _pending_history()
        y_dict = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=history,
            brain_state=self._stale_state_dict(),
        )
        y_obj = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=history,
            brain_state=self._stale_state_object(),
        )
        assert y_dict.should_yield == y_obj.should_yield
        assert y_dict.should_yield is True

    def test_outbound_turns_do_not_consume_inbound_window(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        for _ in range(7):
            history.append({"direction": "out", "body": "رد تلقائي"})
        history.append({"direction": "in", "body": VOICE_SHIPPING})
        assert is_order_reference_continuity_active(history) is True

    def test_ref_plus_six_social_then_shipping_stays_active(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        history.extend({"direction": "in", "body": "هلا كيفك"} for _ in range(6))
        history.append({"direction": "in", "body": VOICE_SHIPPING})
        assert is_order_reference_continuity_active(history) is True
        ownership = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=history,
            brain_state=self._stale_state_dict(),
        )
        assert ownership.should_yield is True
        dec = try_order_reference_continuity_decision(
            SimpleNamespace(
                message=VOICE_SHIPPING,
                history=history,
                state=self._stale_state_object(),
                commerce_bundle={},
                profile={},
                tenant_id=1,
            )
        )
        assert dec is not None
        assert dec.args.get("topic") == "existing_order_support"

    def test_ref_plus_seven_social_then_shipping_expires(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        history.extend({"direction": "in", "body": "هلا كيفك"} for _ in range(7))
        history.append({"direction": "in", "body": VOICE_SHIPPING})
        assert is_order_reference_continuity_active(history) is False

    def test_expired_after_eight_newer_inbound_turns(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF_OLD}]
        history.extend({"direction": "in", "body": f"رسالة {i}"} for i in range(8))
        ownership = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=history,
            brain_state=self._stale_state_dict(),
        )
        assert ownership.should_yield is False
        dec = try_order_reference_continuity_decision(
            SimpleNamespace(
                message=VOICE_SHIPPING,
                history=history,
                state=self._stale_state_object(),
                commerce_bundle={},
                profile={},
                tenant_id=1,
            )
        )
        assert dec is None

    def test_not_found_follow_up_args_exclude_stale_status(self) -> None:
        history = _not_found_follow_up_history()
        args = build_order_support_follow_up_args(
            message=VOICE_SHIPPING,
            state=self._stale_state_object(),
            history=history,
            commerce_bundle={},
        )
        assert args["order_verified"] is False
        assert args.get("order_status") in ("", None)
        assert args["order_reference"] == GENERIC_ORDER_REF

    def test_verified_structured_order_preserves_status(self) -> None:
        bundle = {
            "active_order_context": {
                "reference": GENERIC_ORDER_REF,
                "order_status": "processing",
                "order_id": "ord-verified-1",
            }
        }
        args = build_order_support_follow_up_args(
            message=VOICE_SHIPPING,
            state=self._stale_state_object(),
            history=_pending_history(),
            commerce_bundle=bundle,
        )
        assert args["order_verified"] is True
        assert args["order_status"] == "processing"

    def test_no_ref_shipping_stale_prep_does_not_yield(self) -> None:
        ownership = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=[],
            brain_state=self._stale_state_dict(),
        )
        assert ownership.should_yield is False
        ownership_obj = should_yield_to_existing_order_support(
            VOICE_SHIPPING,
            history=[],
            brain_state=self._stale_state_object(),
        )
        assert ownership_obj.should_yield is False
