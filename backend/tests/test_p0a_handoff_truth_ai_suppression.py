"""
P0-A — Handoff truth wire scrub + AI suppression fail-closed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in [_BACKEND, os.path.join(_BACKEND, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.ai_pause_guard import REASON_MANUAL_PAUSE
from core.ai_disabled_gate import (
    REASON_HANDOFF_SESSION,
    REASON_HUMAN_OWNERSHIP,
    REASON_HUMAN_SUPERVISION,
    disabled_reason_for_conversation,
    is_ai_disabled_for_conversation,
)
from core.handoff_truth import (
    REASON_GATE_VERIFY_FAILED,
    evaluate_gate_error_fail_closed,
    resolve_handoff_truth_active,
)
from core.ownership_state import OWNERSHIP_HUMAN_ACTIVE


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


class TestHandoffTruthPredicate:
    def test_no_truth_when_only_soft_needs_human(self) -> None:
        db = MagicMock()
        convo = _convo(needs_human=True)
        with patch(
            "core.handoff_truth._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.handoff_truth._get_active_handoff_session",
            return_value=None,
        ), patch(
            "core.handoff_truth.conversation_handoff_active",
            return_value=False,
        ):
            result = resolve_handoff_truth_active(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert result.active is False
        assert result.verify_failed is False

    def test_truth_when_active_handoff_session(self) -> None:
        db = MagicMock()
        session = SimpleNamespace(id=9, status="active")
        with patch(
            "core.handoff_truth._get_active_handoff_session",
            return_value=session,
        ):
            result = resolve_handoff_truth_active(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert result.active is True
        assert result.source == "handoff_session_active"

    def test_truth_when_conversation_flags_backed(self) -> None:
        db = MagicMock()
        convo = _convo(
            needs_human=True,
            handoff_active=True,
            is_human_handoff=True,
            status="human",
        )
        with patch(
            "core.handoff_truth._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.handoff_truth._get_active_handoff_session",
            return_value=None,
        ), patch(
            "core.handoff_truth.conversation_handoff_active",
            return_value=False,
        ):
            result = resolve_handoff_truth_active(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert result.active is True
        assert "conversation" in result.source

    def test_truth_from_action_handoff_path(self) -> None:
        db = MagicMock()
        with patch(
            "core.handoff_truth._find_conversations_for_phone",
            return_value=[],
        ), patch(
            "core.handoff_truth._get_active_handoff_session",
            return_value=None,
        ):
            result = resolve_handoff_truth_active(
                db,
                tenant_id=33,
                customer_phone="966551459303",
                chosen_path="ACTION_HANDOFF",
            )
        assert result.active is True


class TestWireLayerHandoffScrub:
    def test_scrubs_promise_without_truth(self) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload

        payload = {
            "messaging_product": "whatsapp",
            "to": "966551459303",
            "type": "text",
            "text": {
                "body": "سأحوّل المحادثة لفريق المتجر الآن.",
            },
        }
        db = MagicMock()
        with patch(
            "core.handoff_truth.resolve_handoff_truth_active",
            return_value=SimpleNamespace(active=False, source="no_handoff_truth", verify_failed=False),
        ):
            out, scrubbed = sanitize_outbound_payload(
                payload,
                tenant_id=33,
                recipient="966551459303",
                db=db,
            )
        assert scrubbed is True
        assert "سأحوّل" not in out["text"]["body"]

    def test_allows_promise_with_truth(self) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload

        text = "سأحوّل المحادثة لفريق المتجر الآن."
        payload = {
            "messaging_product": "whatsapp",
            "to": "966551459303",
            "type": "text",
            "text": {"body": text},
        }
        db = MagicMock()
        with patch(
            "core.handoff_truth.resolve_handoff_truth_active",
            return_value=SimpleNamespace(active=True, source="handoff_session_active", verify_failed=False),
        ):
            out, scrubbed = sanitize_outbound_payload(
                payload,
                tenant_id=33,
                recipient="966551459303",
                db=db,
            )
        assert scrubbed is False
        assert out["text"]["body"] == text

    def test_scrubs_interactive_payload_body(self) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload

        payload = {
            "messaging_product": "whatsapp",
            "to": "966551459303",
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "سأحوّلك للفريق الآن."},
                "action": {"buttons": [{"type": "reply", "reply": {"id": "1", "title": "نعم"}}]},
            },
        }
        db = MagicMock()
        with patch(
            "core.handoff_truth.resolve_handoff_truth_active",
            return_value=SimpleNamespace(active=False, source="no_handoff_truth", verify_failed=False),
        ):
            out, scrubbed = sanitize_outbound_payload(
                payload,
                tenant_id=33,
                recipient="966551459303",
                db=db,
            )
        assert scrubbed is True
        assert "أحوّلك" not in out["interactive"]["body"]["text"]

    def test_suppresses_send_when_scrub_empties_body(self) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload

        payload = {
            "messaging_product": "whatsapp",
            "to": "966551459303",
            "type": "text",
            "text": {"body": "سأحوّلك للفريق"},
        }
        db = MagicMock()
        with patch(
            "core.handoff_truth.resolve_handoff_truth_active",
            return_value=SimpleNamespace(active=False, source="no_handoff_truth", verify_failed=False),
        ):
            out, scrubbed = sanitize_outbound_payload(
                payload,
                tenant_id=33,
                recipient="966551459303",
                db=db,
            )
        assert scrubbed is True
        assert out.get("_nahla_suppress_send") == "handoff_promise_scrub_empty"


class TestAISuppression:
    def _mock_db_no_handoff_session(self) -> MagicMock:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_ai_paused_disables(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = self._mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is True
        assert decision.reason == REASON_MANUAL_PAUSE

    def test_status_human_disables(self) -> None:
        convo = _convo(status="human")
        db = self._mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is False
        assert disabled_reason_for_conversation(convo) == ""

    def test_human_ownership_disables(self) -> None:
        convo = _convo()
        db = self._mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=True,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is True
        assert decision.reason == REASON_HUMAN_OWNERSHIP

    def test_active_handoff_session_disables(self) -> None:
        convo = _convo()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=5, status="active",
        )
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is True
        assert decision.reason == REASON_HANDOFF_SESSION

    def test_needs_human_alone_does_not_disable_ai(self) -> None:
        convo = _convo(needs_human=True)
        db = self._mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is False

    def test_customer_request_notify_session_does_not_disable_ai(self) -> None:
        convo = _convo(needs_human=True)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=5, status="active", handoff_reason="customer_request",
        )
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is False

    def test_notify_handoff_flags_do_not_disable_ai(self) -> None:
        convo = _convo(
            needs_human=True,
            handoff_active=True,
            is_human_handoff=True,
            status="active",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=75, status="active", handoff_reason="customer_request",
        )
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="test"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=1,
                customer_phone="966500000001",
            )
        assert decision.disabled is False

    def test_genuine_dashboard_takeover_still_disables(self) -> None:
        from datetime import datetime, timezone  # noqa: PLC0415

        convo = _convo(
            status="human",
            paused_by_human=True,
            taken_over_at=datetime.now(timezone.utc),
            taken_over_by="dashboard:handoff",
        )
        db = self._mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=1,
                customer_phone="966500000001",
            )
        assert decision.disabled is True
        assert decision.reason == REASON_HUMAN_SUPERVISION

    def test_sibling_paused_row_disables(self) -> None:
        active = _convo(id=1, ai_paused=False)
        paused = _convo(id=2, ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = self._mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[active, paused],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
                conversation=active,
            )
        assert decision.disabled is True
        assert decision.conversation.id == 2

    def test_normal_chat_continues(self) -> None:
        convo = _convo()
        db = self._mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is False


class TestAIDisabledGateVerifyFailure:
    def _gate_patches(self, convos):
        return (
            patch(
                "core.ai_disabled_gate._find_conversations_for_phone",
                return_value=convos,
            ),
            patch(
                "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
                return_value=SimpleNamespace(allowed=True, mode="on"),
            ),
            patch(
                "core.ownership_state.conversation_handoff_active",
                return_value=False,
            ),
        )

    def test_handoff_session_lookup_error_fail_closed_with_signals(self) -> None:
        """Session query fails but sibling shows human-ownership signals → suppress."""
        convo = _convo(needs_human=True, handoff_active=True)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = RuntimeError(
            "handoff session db down",
        )
        p_find, p_mode, p_own = self._gate_patches([convo])
        with p_find, p_mode, p_own:
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is True
        assert decision.reason in {
            REASON_HUMAN_SUPERVISION,
            REASON_GATE_VERIFY_FAILED,
        }

    def test_handoff_session_lookup_error_fail_open_without_signals(self) -> None:
        """Session query fails with no ownership signals → fail-open."""
        convo = _convo()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = RuntimeError(
            "handoff session db down",
        )
        p_find, p_mode, p_own = self._gate_patches([convo])
        with p_find, p_mode, p_own, patch(
            "core.handoff_truth.aggregate_possible_human_ownership_signals",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is False

    def test_ownership_lookup_error_fail_closed_with_human_active(self) -> None:
        """Ownership check fails but human_active signal present → suppress."""
        convo = _convo()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        p_find, p_mode, _p_own = self._gate_patches([convo])
        with p_find, p_mode, patch(
            "core.ownership_state.conversation_handoff_active",
            side_effect=RuntimeError("ownership db down"),
        ), patch(
            "core.handoff_truth.aggregate_possible_human_ownership_signals",
            return_value=True,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )
        assert decision.disabled is True
        assert decision.reason == REASON_GATE_VERIFY_FAILED


class TestFailClosedPolicy:
    def test_fail_closed_when_handoff_signals_present(self) -> None:
        db = MagicMock()
        convo = _convo(status="human")
        with patch(
            "core.handoff_truth.aggregate_possible_human_ownership_signals",
            return_value=True,
        ):
            blocked = evaluate_gate_error_fail_closed(
                db,
                tenant_id=33,
                customer_phone="966551459303",
                conversation=convo,
                gate="ai_pause_guard",
                error=RuntimeError("db timeout"),
            )
        assert blocked is True

    def test_fail_open_without_handoff_signals(self) -> None:
        db = MagicMock()
        with patch(
            "core.handoff_truth.aggregate_possible_human_ownership_signals",
            return_value=False,
        ):
            blocked = evaluate_gate_error_fail_closed(
                db,
                tenant_id=33,
                customer_phone="966551459303",
                gate="merchant_webhook_entry",
                error=RuntimeError("db timeout"),
            )
        assert blocked is False


class TestWebhookIntegration:
    def _call_handle_merchant_message(self, *, convo, text: str, gate_raises: bool = False):
        from routers.whatsapp_webhook import _handle_merchant_message

        db = MagicMock()
        db.commit = MagicMock()
        db.rollback = MagicMock()
        db.add = MagicMock()
        db.flush = MagicMock()

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.X"}]}

        gate_patch = patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            side_effect=RuntimeError("gate down") if gate_raises else None,
        )
        if not gate_raises:
            gate_patch = patch(
                "core.ai_disabled_gate.is_ai_disabled_for_conversation",
                return_value=SimpleNamespace(
                    disabled=False,
                    reason="",
                    conversation=convo,
                ),
            )

        with gate_patch, patch(
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
        ), patch(
            "core.handoff_truth.aggregate_possible_human_ownership_signals",
            return_value=convo.status == "human",
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

    def test_verification_failure_under_handoff_suppresses_ai(self) -> None:
        convo = _convo(status="human")
        posted, mock_save, mock_brain = self._call_handle_merchant_message(
            convo=convo,
            text="مرحبا",
            gate_raises=True,
        )
        assert posted == []
        mock_save.assert_called()
        mock_brain.return_value.process.assert_not_called()

    def test_normal_chat_not_suppressed_on_gate_error(self) -> None:
        convo = _convo()
        from routers.whatsapp_webhook import _handle_merchant_message

        db = MagicMock()
        db.commit = MagicMock()
        db.rollback = MagicMock()
        db.add = MagicMock()
        db.flush = MagicMock()

        with patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            side_effect=RuntimeError("gate down"),
        ), patch(
            "core.handoff_truth.aggregate_possible_human_ownership_signals",
            return_value=False,
        ), patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "core.conversation_engine.StateManager.save_message",
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
            "services.whatsapp_platform.service.provider_post_with_context",
            new=AsyncMock(return_value={"messages": [{"id": "wamid.X"}]}),
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ):
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "مرحبا", "buttons": []},
            )
            # Should not raise — fail-open without handoff signals
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966551459303",
                text="مرحبا",
                tenant_id=33,
                db=db,
            ))


class TestPostWaScrub:
    def test_post_wa_suppresses_false_handoff_promise(self) -> None:
        from routers.whatsapp_webhook import _post_wa

        mock_db = MagicMock()
        mock_conn = MagicMock()
        mock_conn.extra_metadata = {}
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_conn

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.X"}]}

        with patch(
            "core.handoff_truth.resolve_handoff_truth_active",
            return_value=SimpleNamespace(active=False, source="no_handoff_truth", verify_failed=False),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[_convo()],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=100, reason="", pct=0),
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
                    "text": {"body": "سأحوّلك للفريق"},
                },
                _tenant_id=33,
                _db=mock_db,
                _blocked_path="brain",
            ))

        assert ok is False
        assert posted == []
