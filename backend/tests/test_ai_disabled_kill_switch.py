"""
tests/test_ai_disabled_kill_switch.py
─────────────────────────────────────
P0 — AI disabled conversation must not receive any automated reply.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in [_BACKEND, os.path.join(_BACKEND, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.ai_pause_guard import REASON_MANUAL_PAUSE
from core.ai_disabled_gate import (
    disabled_reason_for_conversation,
    is_ai_disabled_for_conversation,
)


def _run(coro):
    return asyncio.run(coro)


def _convo(**kwargs):
    defaults = dict(
        id=42,
        tenant_id=33,
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
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestAIDisabledGateAggregate:
    def test_any_sibling_paused_row_disables(self) -> None:
        active = _convo(id=1, ai_paused=False)
        paused = _convo(id=2, ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = MagicMock()

        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[active, paused],
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )

        assert decision.disabled is True
        assert decision.reason == REASON_MANUAL_PAUSE
        assert decision.conversation.id == 2

    def test_manual_pause_reason(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        assert disabled_reason_for_conversation(convo) == REASON_MANUAL_PAUSE


class TestMerchantWebhookKillSwitch:
    def _call_handle_merchant_message(self, *, ai_paused: bool, text: str):
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _convo(ai_paused=ai_paused, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = MagicMock()
        db.commit = MagicMock()
        db.rollback = MagicMock()
        db.add = MagicMock()
        db.flush = MagicMock()

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.X"}]}

        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "core.conversation_engine.StateManager.save_message",
        ) as mock_save, patch(
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
        ):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "should not send", "buttons": []},
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966551459303",
                text=text,
                tenant_id=33,
                db=db,
            ))

        return posted, mock_save, mock_brain

    def test_disabled_conversation_suppresses_ai(self) -> None:
        posted, mock_save, mock_brain = self._call_handle_merchant_message(
            ai_paused=True,
            text="العسل خفيف ومو مثل أول",
        )
        assert posted == []
        mock_save.assert_called()
        mock_brain.return_value.process.assert_not_called()

    def test_disabled_checkout_does_not_continue(self) -> None:
        posted, mock_save, mock_brain = self._call_handle_merchant_message(
            ai_paused=True,
            text="أنا في الطائف",
        )
        assert posted == []
        mock_save.assert_called()
        mock_brain.return_value.process.assert_not_called()

    def test_disabled_social_does_not_reply(self) -> None:
        posted, mock_save, mock_brain = self._call_handle_merchant_message(
            ai_paused=True,
            text="وصل والله يبيض وجهك",
        )
        assert posted == []
        mock_save.assert_called()
        mock_brain.return_value.process.assert_not_called()


class TestBrainProcessKillSwitch:
    def test_brain_process_skips_when_paused(self) -> None:
        from modules.ai.brain.pipeline import MerchantBrain

        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = MagicMock()

        brain = MerchantBrain(
            classifier=MagicMock(),
            state_store=MagicMock(),
            facts_loader=MagicMock(),
            decision_engine=MagicMock(),
            policy_gate=MagicMock(),
            executor=MagicMock(),
            composer=MagicMock(),
            memory_updater=MagicMock(),
        )

        with patch("core.billing.has_billing_access", return_value=True), patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True, used_total=0, limit=100, reason="", pct=0,
            ),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            result = _run(brain.process(
                db=db,
                tenant_id=33,
                customer_phone="966551459303",
                message="test",
                history=[],
                profile={},
            ))

        assert result.get("skipped") is True
        assert result.get("reason") == "ai_disabled_gate"
        brain._classifier.classify.assert_not_called()


class TestSendLayerProtection:
    def test_post_wa_blocks_when_ai_becomes_disabled(self) -> None:
        from routers.whatsapp_webhook import _post_wa

        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        mock_conn = MagicMock()
        mock_conn.extra_metadata = {}
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_conn

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.X"}]}

        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ), patch(
            "observability.rate_limiter.check_rate_limit",
            return_value=True,
        ), patch(
            "core.outbound_dedup.check_outbound_send",
            return_value=None,
        ):
            ok = _run(_post_wa(
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "966551459303",
                    "type": "text",
                    "text": {"body": "late reply"},
                },
                _tenant_id=33,
                _db=mock_db,
                _blocked_path="brain",
            ))

        assert ok is False
        assert posted == []
