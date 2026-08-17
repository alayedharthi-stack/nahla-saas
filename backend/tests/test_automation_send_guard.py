"""P0 — automation send guard blocks outbound when AI paused / human takeover."""
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
from core.automation_send_guard import (
    REASON_AI_DISABLED,
    REASON_HUMAN_TAKEOVER,
    should_block_automation_for_conversation,
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


class TestShouldBlockAutomationForConversation:
    def test_ai_paused_blocks(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=33,
            customer_phone="966559968061",
            conversation=convo,
        )
        assert decision.block is True
        assert decision.reason == REASON_AI_DISABLED

    def test_human_takeover_columns_block(self) -> None:
        convo = _convo(taken_over_at=datetime.now(timezone.utc), taken_over_by="dashboard:handoff")
        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=33,
            customer_phone="966559968061",
            conversation=convo,
        )
        assert decision.block is True
        assert decision.reason == REASON_HUMAN_TAKEOVER

    def test_advisory_needs_human_does_not_block(self) -> None:
        convo = _convo(needs_human=True)
        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=33,
            customer_phone="966559968061",
            conversation=convo,
        )
        assert decision.block is False

    def test_leftover_advisory_handoff_flags_do_not_block(self) -> None:
        convo = _convo(
            is_human_handoff=True,
            handoff_active=True,
            needs_human=True,
            status="human",
        )
        with patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = should_block_automation_for_conversation(
                MagicMock(),
                tenant_id=33,
                customer_phone="966559968061",
                conversation=convo,
            )
        assert decision.block is False

    def test_active_conversation_allows(self) -> None:
        convo = _convo()
        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=33,
            customer_phone="966559968061",
            conversation=convo,
        )
        assert decision.block is False


class TestProviderSendAutomationGuard:
    def _provider_call(self, *, allow_manual: bool = False):
        from services.whatsapp_platform.service import provider_send_message

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.blocked"}]}

        conn = MagicMock()
        conn.extra_metadata = {}
        db = MagicMock()
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)

        with patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ) as mock_token, patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.wa_provider",
            return_value="360dialog",
        ), patch(
            "core.automation_send_guard.lookup_conversation_for_phone",
            return_value=convo,
        ):
            resp, _ctx = _run(provider_send_message(
                db,
                conn,
                tenant_id=33,
                operation="send_message",
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "966559968061",
                    "type": "text",
                    "text": {"body": "fallback"},
                },
                allow_manual=allow_manual,
                blocked_path="media_fallback",
            ))

        return resp, posted, mock_token

    def test_ai_paused_customer_does_not_post(self) -> None:
        resp, posted, mock_token = self._provider_call()
        assert posted == []
        mock_token.assert_not_called()
        assert resp.get("_nahla_classification") == "automation_blocked"

    def test_manual_send_bypasses_guard(self) -> None:
        resp, posted, mock_token = self._provider_call(allow_manual=True)
        assert len(posted) == 1
        mock_token.assert_called_once()
        assert "error" not in resp


class TestMediaFallbackBlockedWhenAiPaused:
    def test_media_fallback_does_not_reach_provider(self) -> None:
        from routers.whatsapp_webhook import _handle_media_fallback

        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = MagicMock()
        mock_conn = MagicMock()
        mock_conn.extra_metadata = {}
        db.query.return_value.filter_by.return_value.first.return_value = mock_conn

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.audio"}]}

        with patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "routers.whatsapp_webhook.StateManager",
        ), patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ) as mock_token, patch(
            "observability.rate_limiter.check_rate_limit",
            return_value=True,
        ), patch(
            "core.outbound_dedup.check_outbound_send",
            return_value=None,
        ), patch(
            "core.automation_send_guard.lookup_conversation_for_phone",
            return_value=convo,
        ):
            _run(_handle_media_fallback(
                phone_id="PH1",
                to="966559968061",
                tenant_id=33,
                db=db,
                fallback_reply="لم أتمكن من سماع الرسالة الصوتية",
                inbound_metadata={"normalized_type": "audio"},
            ))

        assert posted == []
        mock_token.assert_not_called()


class TestPostWaIntegration:
    def test_post_wa_blocks_brain_path_when_paused(self) -> None:
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
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ) as mock_token, patch(
            "observability.rate_limiter.check_rate_limit",
            return_value=True,
        ), patch(
            "core.outbound_dedup.check_outbound_send",
            return_value=None,
        ), patch(
            "core.automation_send_guard.lookup_conversation_for_phone",
            return_value=convo,
        ):
            ok = _run(_post_wa(
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "966559968061",
                    "type": "text",
                    "text": {"body": "brain reply"},
                },
                _tenant_id=33,
                _db=mock_db,
                _blocked_path="brain",
            ))

        assert ok is False
        assert posted == []
        mock_token.assert_not_called()

    def test_manual_post_wa_still_sends(self) -> None:
        from routers.whatsapp_webhook import _post_wa

        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        mock_conn = MagicMock()
        mock_conn.extra_metadata = {}
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_conn

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.manual"}]}

        with patch(
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
        ), patch(
            "core.outbound_dedup.record_outbound_result",
        ):
            ok = _run(_post_wa(
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "966559968061",
                    "type": "text",
                    "text": {"body": "رد الموظف"},
                },
                _tenant_id=33,
                _db=mock_db,
                _allow_manual=True,
                _blocked_path="manual_reply",
            ))

        assert ok is True
        assert len(posted) == 1
