"""P0 — trace-only AI candidates must not appear as sent dashboard replies."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.conversation_timeline import merge_trace_rows_into_timeline
from core.outbound_abort_audit import log_outbound_candidate_abort


def _ts(offset_sec: int = 0) -> datetime:
    return datetime(2026, 6, 12, 12, 16, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_sec)


class TestConversationTimelineTracePolicy:
    def test_trace_outbound_not_surfaced_when_no_message_event(self) -> None:
        trace = SimpleNamespace(
            message="نبغى نشتري عاسبة",
            response_text="النفظ (العاسبة) متوفر حالياً 🌷",
            created_at=_ts(),
            orchestrator_used=True,
        )
        messages: list = []
        merge_trace_rows_into_timeline(messages, [trace], set(), include_trace_outbound=False)
        outbound = [m for m in messages if m.get("direction") == "out"]
        assert outbound == []

    def test_normal_outbound_message_event_unchanged(self) -> None:
        sent_at = _ts()
        messages = [{
            "id": "29331",
            "direction": "out",
            "body": "ياهلا 🌷",
            "time": sent_at.isoformat(),
            "isAI": True,
            "eventType": "ai",
            "sendStatus": "sent",
            "wamid": "wamid.TEST",
            "_ts": sent_at,
        }]
        trace = SimpleNamespace(
            message="https://vt.tiktok.com/x",
            response_text="ياهلا 🌷",
            created_at=sent_at,
            orchestrator_used=True,
        )
        merge_trace_rows_into_timeline(
            messages,
            [trace],
            {sent_at},
            include_trace_outbound=False,
        )
        ai_out = [m for m in messages if m.get("direction") == "out" and m.get("isAI")]
        assert len(ai_out) == 1
        assert ai_out[0]["sendStatus"] == "sent"
        assert ai_out[0]["wamid"] == "wamid.TEST"

    def test_failed_provider_send_preserved_on_message_event(self) -> None:
        sent_at = _ts(5)
        messages = [{
            "id": "99",
            "direction": "out",
            "body": "reply body",
            "time": sent_at.isoformat(),
            "isAI": True,
            "eventType": "ai",
            "sendStatus": "failed",
            "sendError": {"key": "not_on_whatsapp", "labelAr": "تعذّر"},
            "_ts": sent_at,
        }]
        merge_trace_rows_into_timeline(messages, [], set(), include_trace_outbound=False)
        assert messages[0]["sendStatus"] == "failed"
        assert messages[0]["sendError"]["key"] == "not_on_whatsapp"

    def test_merchant_echo_not_marked_ai_via_trace_merge(self) -> None:
        sent_at = _ts(10)
        messages = [{
            "id": "29337",
            "direction": "out",
            "body": "*وسائل الدفع:*",
            "time": sent_at.isoformat(),
            "isAI": False,
            "eventType": "manual",
            "_ts": sent_at,
        }]
        trace = SimpleNamespace(
            message="",
            response_text="should-not-appear",
            created_at=sent_at + timedelta(seconds=8),
            orchestrator_used=True,
        )
        merge_trace_rows_into_timeline(
            messages,
            [trace],
            {sent_at},
            include_trace_outbound=False,
        )
        assert all(not m.get("isAI") for m in messages if m["direction"] == "out")
        assert all("should-not-appear" not in m.get("body", "") for m in messages)


class TestOutboundCandidateAbortAudit:
    def test_emits_structured_log_when_candidate_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="nahla.outbound_abort_audit"):
            log_outbound_candidate_abort(
                tenant_id=33,
                conversation_id=9368,
                customer_id=8810,
                generated_candidate_non_empty=True,
                final_response_empty=True,
                abort_reason="skip_persist",
                final_stage="pre_persist",
                suppressor="chat_dedup_hard",
                candidate_preview="النفظ متوفر",
            )
        assert any("[OUTBOUND_CANDIDATE_ABORT]" in r.message for r in caplog.records)
        payload = json.loads(caplog.records[-1].message.split("[OUTBOUND_CANDIDATE_ABORT] ", 1)[1])
        assert payload["generated_candidate_non_empty"] is True
        assert payload["final_response_empty"] is True
        assert payload["suppressor"] == "chat_dedup_hard"

    def test_webhook_helper_skips_when_final_reply_present(self) -> None:
        from routers.whatsapp_webhook import _maybe_log_outbound_candidate_abort

        with patch("core.outbound_abort_audit.log_outbound_candidate_abort") as mock_log:
            _maybe_log_outbound_candidate_abort(
                tenant_id=1,
                conversation_id=1,
                customer_id=1,
                brain_candidate="candidate",
                final_reply="sent",
                abort_reason="skip_persist",
                final_stage="pre_persist",
            )
            mock_log.assert_not_called()

    def test_webhook_helper_logs_when_candidate_non_empty_final_empty(self) -> None:
        from routers.whatsapp_webhook import _maybe_log_outbound_candidate_abort

        with patch("core.outbound_abort_audit.log_outbound_candidate_abort") as mock_log:
            _maybe_log_outbound_candidate_abort(
                tenant_id=1,
                conversation_id=1,
                customer_id=1,
                brain_candidate="النفظ متوفر",
                final_reply="",
                abort_reason="skip_wire_send",
                final_stage="pre_provider_send",
                suppressor="chat_dedup_hard",
            )
            mock_log.assert_called_once()
            assert mock_log.call_args.kwargs["generated_candidate_non_empty"] is True
            assert mock_log.call_args.kwargs["final_response_empty"] is True
