"""AGENT3-D2 mode routing + truth repair.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Customer staff-request wording must reach Brain/D2. MODE_SUPPORT_ESCALATION
is current explicit human ownership only. Live Arabic is test evidence only.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: E402
from modules.ai.brain.types import ActionResult, INTENT_TALK_HUMAN  # noqa: E402
from modules.ai.routing.conversation_mode import (  # noqa: E402
    META_KEY,
    MODE_LIVE_CHAT,
    MODE_SUPPORT_ESCALATION,
    resolve_conversation_mode,
)
from test_agent3_d2_prebrain_blocker import (  # noqa: E402
    HANDOFF_ACK_CANNED,
    LIVE_STAFF_REQUEST,
    _assert_no_unauthorized_staff_promises,
    _inbound_rows,
    _merchant_handler_convo,
    _merchant_handler_db,
    _merchant_handler_patch_ctx,
    _natural_chain,
    _run,
)

TURN1_NO_HAMZA = "اريد التحدث مع موظف من المتجر"
TURN2_NO_HAMZA = "نعم اريد موظف يساعدني"
TURN3_PRODUCT = "وش المنتجات المتوفرة عندكم ؟"
WEBHOOK_CANNED_FUTURE = (
    "وصلت رسالتك. تم تحويل المحادثة لفريق المتجر، "
    "وسيرد عليك أحد الموظفين في أقرب وقت."
)


def _webhook_src() -> str:
    path = os.path.join(_BACKEND, "routers", "whatsapp_webhook.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_webhook_has_no_canned_future_followup_owner() -> None:
    src = _webhook_src()
    assert WEBHOOK_CANNED_FUTURE not in src
    assert "وسيرد عليك أحد الموظفين في أقرب وقت." not in src


def test_support_patterns_have_no_runtime_authority() -> None:
    src_path = os.path.join(
        _BACKEND, "modules", "ai", "routing", "conversation_mode.py",
    )
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert "_SUPPORT_PATTERNS" not in src
    assert "if _matches_any(text, _SUPPORT_PATTERNS)" not in src


def test_turn1_no_hamza_does_not_select_support_mode() -> None:
    convo = _merchant_handler_convo()
    decision = resolve_conversation_mode(
        _merchant_handler_db(),
        tenant_id=1,
        convo=convo,
        customer_phone="966500000580",
        text=TURN1_NO_HAMZA,
    )
    assert decision.mode == MODE_LIVE_CHAT
    assert decision.mode != MODE_SUPPORT_ESCALATION


class TestWebhookThreeTurnNaturalPath:
    def test_three_turns_reach_brain_and_skip_canned_ack(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        sent: list[str] = []
        brain_seen: list[str] = []
        d2_calls: list[str] = []

        async def fake_send(*_a, **kwargs):
            sent.append(str(kwargs.get("text") or ""))
            return True

        async def fake_d2(**kwargs):
            d2_calls.append(str(kwargs.get("message") or ""))
            reused = len(d2_calls) > 1
            return ActionResult(
                success=True,
                data={
                    "handoff_session_id": 91,
                    "escalation_status": "queued",
                    "handoff_session_reused": reused,
                    "notification_accepted": False,
                    "notification_sent": False,
                    "future_followup_committed": False,
                    "staff_assigned": False,
                },
            )

        async def fake_process(**kwargs):
            message = str(kwargs.get("message") or "")
            brain_seen.append(message)
            if "موظف" in message:
                await fake_d2(message=message)
                return {
                    "reply": "تمام، وصلت رسالتك.",
                    "handoff": True,
                    "action": ACTION_HANDOFF,
                }
            return {
                "reply": "عندنا حذاء رياضي أبيض وقميص قطني أزرق.",
                "handoff": False,
                "action": "llm_reply",
            }

        with _merchant_handler_patch_ctx(
            convo=convo,
            whatsapp_send_mock=fake_send,
            use_real_mode=True,
        ) as (mock_brain, _state, save_mock), patch(
            "modules.ai.brain.execution.staff_escalation_execution.execute_staff_escalation_for_safety_signal",
            new=fake_d2,
        ):
            mock_brain.return_value.process = AsyncMock(side_effect=fake_process)
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=TURN1_NO_HAMZA,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.t1",
            ))
            assert convo.ai_paused is False
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=TURN2_NO_HAMZA,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.t2",
            ))
            assert convo.ai_paused is False
            convo.extra_metadata[META_KEY] = {
                "mode": MODE_SUPPORT_ESCALATION,
                "previous_mode": MODE_LIVE_CHAT,
                "reason": "stale_customer_text_lease",
                "source": "seed",
                "changed_at": "2026-09-03T09:48:55+00:00",
                "locked_until": "2099-01-01T00:00:00+00:00",
            }
            convo.needs_human = True
            convo.handoff_active = True
            convo.is_human_handoff = True
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=TURN3_PRODUCT,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.t3",
            ))

        assert brain_seen == [TURN1_NO_HAMZA, TURN2_NO_HAMZA, TURN3_PRODUCT]
        assert d2_calls == [TURN1_NO_HAMZA, TURN2_NO_HAMZA]
        assert WEBHOOK_CANNED_FUTURE not in sent
        assert HANDOFF_ACK_CANNED not in sent
        assert convo.ai_paused is False
        inbound = _inbound_rows(save_mock)
        assert len(inbound) == 3

    def test_explicit_takeover_does_not_send_canned_or_call_brain(self) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _merchant_handler_convo(
            taken_over_by="dashboard:handoff",
            ai_paused=True,
            ai_paused_reason="manual_takeover",
        )
        db = _merchant_handler_db()
        sent: list[str] = []

        async def fake_send(*_a, **kwargs):
            sent.append(str(kwargs.get("text") or ""))
            return True

        with _merchant_handler_patch_ctx(
            convo=convo,
            whatsapp_send_mock=fake_send,
            use_real_mode=True,
        ) as (mock_brain, _state, _save):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "should-not-run", "handoff": False},
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966500000580",
                text=TURN3_PRODUCT,
                tenant_id=1,
                db=db,
                wa_msg_id="wamid.takeover",
            ))

        mock_brain.return_value.process.assert_not_called()
        assert WEBHOOK_CANNED_FUTURE not in sent
        assert sent == []


class TestNaturalD2StillOwnsStaffSemantics:
    def test_live_request_still_reaches_action_handoff(self) -> None:
        chain = _natural_chain(LIVE_STAFF_REQUEST)
        try:
            assert chain["intent"].name == INTENT_TALK_HUMAN
            assert chain["decision"].action == ACTION_HANDOFF
            assert chain["handler"] == "_HandoffHandler"
            assert chain["d2_calls"] == 1
            assert chain["result"].success is True
            assert chain["result"].data.get("handoff_session_id") not in (None, "")
        finally:
            chain["db"].close()
            chain["engine"].dispose()

    def test_queue_only_result_cannot_claim_future_followup(self) -> None:
        from modules.ai.brain.postprocess.staff_escalation_semantic_claims import (
            StaffEscalationCandidateClaims,
            capabilities_from_execution_data,
            unsupported_claims,
        )

        data = {
            "handoff_session_id": 91,
            "notification_accepted": False,
            "notification_sent": False,
            "staff_assigned": False,
            "future_followup_committed": False,
        }
        caps = capabilities_from_execution_data(data)
        claims = StaffEscalationCandidateClaims(
            valid_parse=True,
            claims_request_acknowledged=True,
            claims_queued=True,
            claims_future_followup=True,
            claims_staff_notified=True,
            claims_staff_assigned=True,
        )
        blocked = unsupported_claims(claims, caps)
        assert "future_followup" in blocked
        assert "staff_notified" in blocked
        assert "staff_assigned" in blocked
        _assert_no_unauthorized_staff_promises("تمام، وصلت رسالتك.", data)
        assert caps.future_followup_committed is False

