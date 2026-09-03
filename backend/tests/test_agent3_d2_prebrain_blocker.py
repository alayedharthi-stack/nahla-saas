"""AGENT3-D2 PRE-BRAIN blocker — persist-first + unified D2 execution.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Generic commerce fixtures. Live Arabic is test evidence only.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.handoff_detector import (  # noqa: E402
    GENERIC_HANDOFF_TIER,
    HANDOFF_ACK_TEXT_AR,
    is_handoff_request,
)
from core.inbound_dedup import is_duplicate_inbound, reset_cache  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: E402
from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: E402
    action_handoff_already_executed,
    execute_staff_escalation_for_safety_signal,
    should_defer_generic_prebrain_execution,
)
from modules.ai.brain.types import ActionResult  # noqa: E402
from models import HandoffSession, Tenant  # noqa: E402

LIVE_STAFF_REQUEST = "أريد التحدث مع موظف من المتجر"
LIVE_STAFF_PARAPHRASE = "نعم أريد موظف يساعدني"
GENERIC_SHOE_QUESTION = "كم سعر الحذاء الرياضي الأبيض"
HANDOFF_ACK_CANNED = HANDOFF_ACK_TEXT_AR


def _run(coro):
    return asyncio.run(coro)


def _sqlite_db():
    engine = create_engine("sqlite:///:memory:")
    for table in (Tenant.__table__, HandoffSession.__table__):
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    Tenant.__table__.create(engine, checkfirst=True)
    HandoffSession.__table__.create(engine, checkfirst=True)
    return sessionmaker(bind=engine)(), engine


def _seed_tenant(db, name: str) -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _merchant_handler_db() -> MagicMock:
    db = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


def _merchant_handler_convo(**kwargs) -> SimpleNamespace:
    defaults = dict(
        id=10107,
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
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@contextmanager
def _merchant_handler_patch_ctx(
    *,
    convo,
    whatsapp_send_mock=None,
    save_message_side_effect=None,
):
    state = SimpleNamespace(turn=0, stage="active", order_prep={})
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
        save_patch = stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.save_message",
            side_effect=save_message_side_effect,
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.load_history",
            return_value=[],
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.load",
            return_value=state,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.StateManager.save"))
        stack.enter_context(patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.conversational_priority.has_payment_outbound_consent",
            return_value=False,
        ))
        mock_brain = stack.enter_context(patch("modules.ai.brain.pipeline.get_brain"))
        stack.enter_context(patch(
            "modules.ai.routing.conversation_mode.resolve_conversation_mode",
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
        stack.enter_context(patch("core.store_knowledge.build_ai_context", return_value={}))
        stack.enter_context(patch(
            "core.whatsapp_ai_live.is_inbound_before_ai_live_since",
            return_value=False,
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.staff_contact_policy.evaluate_staff_contact_policy",
            return_value=None,
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.staff_contact_policy.evaluate_generic_handoff_contact_policy",
            return_value=None,
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook._send_whatsapp_message",
            new=whatsapp_send_mock or AsyncMock(return_value=True),
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook._maybe_deliver_generic_handoff_vcard",
            new=AsyncMock(return_value=None),
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=AsyncMock(return_value={"messages": [{"id": "wamid.test"}]}),
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ))
        cis_mock = stack.enter_context(patch(
            "services.customer_intelligence.CustomerIntelligenceService",
        ))
        cis_mock.return_value.upsert_lead_customer.return_value = SimpleNamespace(
            id=7, name="أحمد سالم", email="",
        )
        cis_mock.return_value.ensure_profile.return_value = SimpleNamespace(
            segment="",
            customer_status="",
            rfm_segment="",
            is_returning=False,
            total_orders=0,
            total_spend_sar=0.0,
            last_order_at=None,
        )
        stack.enter_context(patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_shadow_enabled",
            return_value=False,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.MERCHANT_BRAIN_ENABLED", True))
        stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
        yield mock_brain, state, save_patch


def _inbound_rows(save_mock) -> List[Any]:
    rows = []
    for call in save_mock.call_args_list:
        args = call.args
        kwargs = call.kwargs
        direction = kwargs.get("direction")
        if direction is None and len(args) >= 4:
            direction = args[3]
        if str(direction) == "inbound":
            rows.append(call)
    return rows


class TestDeferContract:
    def test_generic_tier_defers(self) -> None:
        assert should_defer_generic_prebrain_execution(
            is_handoff=True,
            is_owner_contact=False,
            is_post_pay_mod=False,
            tier=GENERIC_HANDOFF_TIER,
        ) is True

    def test_owner_and_post_pay_still_execute(self) -> None:
        assert should_defer_generic_prebrain_execution(
            is_handoff=True,
            is_owner_contact=True,
            is_post_pay_mod=False,
            tier="clear",
        ) is False
        assert should_defer_generic_prebrain_execution(
            is_handoff=True,
            is_owner_contact=False,
            is_post_pay_mod=True,
            tier=GENERIC_HANDOFF_TIER,
        ) is False

    def test_live_phrases_match_existing_detector_only(self) -> None:
        assert is_handoff_request(LIVE_STAFF_REQUEST) is True
        assert is_handoff_request(LIVE_STAFF_PARAPHRASE) is True
        assert is_handoff_request(GENERIC_SHOE_QUESTION) is False

    def test_action_handoff_detection(self) -> None:
        assert action_handoff_already_executed(
            brain_handoff=True, decision_action="llm_reply",
        ) is True
        assert action_handoff_already_executed(
            brain_handoff=False, decision_action=ACTION_HANDOFF,
        ) is True
        assert action_handoff_already_executed(
            brain_handoff=False, decision_action="llm_reply",
        ) is False


class TestD2SafetyCore:
    def test_safety_wrapper_uses_same_executor_and_reuses_session(self) -> None:
        db, engine = _sqlite_db()
        try:
            tenant = _seed_tenant(db, "متجر تجريبي عام")
            phone = "966500000201"
            convo = SimpleNamespace(needs_human=False, id=42)
            settings = {"notification_method": "none", "webhook_url": ""}
            with patch(
                "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
                return_value=settings,
            ):
                first = _run(execute_staff_escalation_for_safety_signal(
                    db=db,
                    tenant_id=tenant.id,
                    customer_phone=phone,
                    message=LIVE_STAFF_REQUEST,
                    convo=convo,
                ))
                second = _run(execute_staff_escalation_for_safety_signal(
                    db=db,
                    tenant_id=tenant.id,
                    customer_phone=phone,
                    message=LIVE_STAFF_PARAPHRASE,
                    convo=convo,
                ))
            db.commit()
            assert first.success is True
            assert second.success is True
            assert first.data["handoff_session_id"] == second.data["handoff_session_id"]
            assert second.data["handoff_session_reused"] is True
            assert db.query(HandoffSession).filter(
                HandoffSession.tenant_id == tenant.id,
                HandoffSession.customer_phone == phone,
                HandoffSession.status == "active",
            ).count() == 1
            assert convo.needs_human is True
        finally:
            db.close()
            engine.dispose()

    def test_handoff_handler_still_thin_adapter(self) -> None:
        from modules.ai.brain.execution.executor import _HandoffHandler
        src = inspect_handle_source()
        assert "execute_staff_escalation" in src
        assert "create_handoff_session" not in src


def inspect_handle_source() -> str:
    import inspect
    from modules.ai.brain.execution.executor import _HandoffHandler
    return inspect.getsource(_HandoffHandler.handle)


class TestWebhookPersistAndBrain:
    def test_live_staff_request_persists_once_and_reaches_brain_d2(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        sent: list[str] = []
        d2_calls = {"n": 0}

        async def fake_send(*_a, **kwargs):
            sent.append(str(kwargs.get("text") or ""))
            return True

        async def fake_d2(**_kwargs):
            d2_calls["n"] += 1
            return ActionResult(
                success=True,
                data={
                    "handoff_session_id": 85,
                    "escalation_status": "queued",
                    "handoff_session_reused": False,
                },
            )

        async def fake_process(**_kwargs):
            await fake_d2()
            return {
                "reply": "وصلت طلبك، الفريق يشوفه الحين.",
                "handoff": True,
                "action": ACTION_HANDOFF,
            }

        with _merchant_handler_patch_ctx(
            convo=convo, whatsapp_send_mock=fake_send,
        ) as (mock_brain, _state, save_mock), patch(
            "modules.ai.brain.execution.staff_escalation_execution.execute_staff_escalation_for_safety_signal",
            new=fake_d2,
        ):
            mock_brain.return_value.process = AsyncMock(side_effect=fake_process)
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=LIVE_STAFF_REQUEST,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.turn1",
            ))

        mock_brain.return_value.process.assert_called_once()
        assert d2_calls["n"] == 1
        inbound = _inbound_rows(save_mock)
        assert len(inbound) == 1
        inbound_kwargs = inbound[0].kwargs
        assert inbound_kwargs.get("tenant_id") == 1
        assert inbound_kwargs.get("conversation_id") == 10107
        assert inbound_kwargs.get("extra_metadata", {}).get("wa_message_id") == "wamid.turn1"
        assert HANDOFF_ACK_CANNED not in sent
        assert convo.ai_paused is False

    def test_paraphrase_same_architecture(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        with _merchant_handler_patch_ctx(convo=convo) as (mock_brain, _state, save_mock):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "تم تسجيل الطلب للدعم", "handoff": True},
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=LIVE_STAFF_PARAPHRASE,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.turn2",
            ))
        mock_brain.return_value.process.assert_called_once()
        assert len(_inbound_rows(save_mock)) == 1
        assert convo.ai_paused is False

    def test_second_new_wamid_persists_again(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        with _merchant_handler_patch_ctx(convo=convo) as (mock_brain, _state, save_mock):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "تم", "handoff": True},
            )
            _run(_handle_merchant_message(
                phone_id="PH1", to="966500000580", text=LIVE_STAFF_REQUEST,
                tenant_id=1, db=db, wa_msg_id="wamid.a",
            ))
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=LIVE_STAFF_PARAPHRASE,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.b",
            ))
        assert len(_inbound_rows(save_mock)) == 2

    def test_brain_exception_uses_d2_and_existing_fallback(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        sent: list[str] = []
        d2_n = {"n": 0}

        async def fake_send(*_a, **kwargs):
            sent.append(str(kwargs.get("text") or ""))
            return True

        async def boom(**_kwargs):
            raise RuntimeError("injected brain failure")

        async def fake_d2(**_kwargs):
            d2_n["n"] += 1
            return ActionResult(
                success=True,
                data={"handoff_session_id": 85, "escalation_status": "queued"},
            )

        with _merchant_handler_patch_ctx(
            convo=convo, whatsapp_send_mock=fake_send,
        ) as (mock_brain, _state, save_mock), patch(
            "modules.ai.brain.execution.staff_escalation_execution.execute_staff_escalation_for_safety_signal",
            new=fake_d2,
        ):
            mock_brain.return_value.process = AsyncMock(side_effect=boom)
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=LIVE_STAFF_REQUEST,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.exc",
            ))
        assert len(_inbound_rows(save_mock)) == 1
        assert d2_n["n"] == 1
        assert HANDOFF_ACK_CANNED not in sent
        assert convo.ai_paused is False

    def test_semantic_miss_uses_d2_not_prebrain_session(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        d2_n = {"n": 0}

        async def fake_d2(**_kwargs):
            d2_n["n"] += 1
            return ActionResult(
                success=True,
                data={"handoff_session_id": 91, "escalation_status": "queued"},
            )

        async def fake_compose(**_kwargs):
            return "وصلت طلبك بدون وعد إشعار."

        with _merchant_handler_patch_ctx(convo=convo) as (mock_brain, _state, save_mock), patch(
            "core.handoff_detector.is_handoff_request",
            return_value=True,
        ), patch(
            "core.handoff_detector.is_owner_contact_request",
            return_value=False,
        ), patch(
            "core.handoff_detector.is_post_payment_modification_request",
            return_value=False,
        ), patch(
            "modules.ai.brain.execution.staff_escalation_execution.execute_staff_escalation_for_safety_signal",
            new=fake_d2,
        ), patch(
            "modules.ai.brain.execution.staff_escalation_execution.compose_staff_escalation_with_verifier",
            new=fake_compose,
        ):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "الحذاء الرياضي الأبيض متوفر", "handoff": False},
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=GENERIC_SHOE_QUESTION,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.miss",
            ))
        assert d2_n["n"] == 1
        assert len(_inbound_rows(save_mock)) == 1
        assert convo.ai_paused is False

    def test_normal_question_unchanged(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        with _merchant_handler_patch_ctx(convo=convo) as (mock_brain, _state, save_mock):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "سعر الحذاء 199 ريال", "handoff": False},
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=GENERIC_SHOE_QUESTION,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.normal",
            ))
        mock_brain.return_value.process.assert_called_once()
        assert len(_inbound_rows(save_mock)) == 1
        assert convo.needs_human is False
        assert convo.ai_paused is False

    def test_owner_contact_still_prebrain(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        with _merchant_handler_patch_ctx(convo=convo) as (mock_brain, _state, _save):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "should not run", "handoff": False},
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text="أبي أتواصل مع المالك",
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.owner",
            ))
        mock_brain.return_value.process.assert_not_called()

    def test_ai_remains_on_after_staff_request_then_normal_question(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        seen: list[str] = []

        async def fake_process(**kwargs):
            seen.append(str(kwargs.get("message") or ""))
            if kwargs.get("message") == LIVE_STAFF_REQUEST:
                return {"reply": "ok", "handoff": True}
            return {"reply": "الشحن خلال يومين", "handoff": False}

        with _merchant_handler_patch_ctx(convo=convo) as (mock_brain, _state, _save):
            mock_brain.return_value.process = AsyncMock(side_effect=fake_process)
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=LIVE_STAFF_REQUEST,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.staff",
            ))
            assert convo.ai_paused is False
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=GENERIC_SHOE_QUESTION,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.followup",
            ))
        assert seen == [LIVE_STAFF_REQUEST, GENERIC_SHOE_QUESTION]
        assert convo.ai_paused is False


class TestDuplicateWamid:
    def test_same_wamid_is_duplicate_before_merchant_handler(self) -> None:
        reset_cache()
        assert is_duplicate_inbound(
            phone_number_id="PH1", msg_id="wamid.HBgMOTY2NTYxMDUyNTgwFQIAEhgUMkFENDVFRDFCQ0QyN0M5ODlERDgA",
        ) is False
        assert is_duplicate_inbound(
            phone_number_id="PH1", msg_id="wamid.HBgMOTY2NTYxMDUyNTgwFQIAEhgUMkFENDVFRDFCQ0QyN0M5ODlERDgA",
        ) is True
        reset_cache()
