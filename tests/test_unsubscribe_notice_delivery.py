"""Consent wire regression: store test/off mode must not swallow opt-out notices.

Real _post_wa -> provider_send_message -> recipient safety; only network, token
resolution and unrelated persistence are mocked. No model or provider calls.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.unsubscribe import (
    CANCELLED_UNSUB_MSG_AR,
    FINAL_UNSUBSCRIBED_MSG_AR,
    build_confirmation_payload,
    build_confirmation_fallback_payload,
    build_text_payload,
    is_unsubscribe_notice_payload,
)

PHONE = "+966500000123"


def notices():
    return [
        build_confirmation_payload(PHONE),
        build_confirmation_fallback_payload(PHONE),
        build_text_payload(PHONE, FINAL_UNSUBSCRIBED_MSG_AR),
        build_text_payload(PHONE, CANCELLED_UNSUB_MSG_AR),
    ]


@pytest.mark.parametrize("payload", notices())
@pytest.mark.parametrize("mode", ["test", "off"])
def test_consent_notices_reach_wire_with_ai_disabled(payload, mode):
    ok, posted = send(payload, mode=mode)
    assert ok is True
    assert posted.await_count == 1
    sent = posted.call_args.kwargs["json"]
    expected = deepcopy(payload)
    expected["to"] = PHONE.lstrip("+")
    assert sent == expected


def send(payload, *, mode="test", notice=True, paused=False, blocked=False,
         quota=True, safety_error=False, direct=False, provider_response=None):
    from routers.whatsapp_webhook import _post_wa
    from services.whatsapp_platform.service import provider_send_message
    from core.ai_disabled_gate import store_ai_mode_allows

    convo = SimpleNamespace(id=9, tenant_id=77, customer_id=8,
                            ai_paused=paused, ai_paused_reason="manual_pause")
    conn = SimpleNamespace(provider="meta", connection_type="direct", extra_metadata={})
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = conn
    response = provider_response or {
        "messages": [{"id": "wamid.consent"}],
        "_nahla_classification": "ok", "_nahla_wamid": "wamid.consent",
    }
    posted = AsyncMock(return_value=response)
    with ExitStack() as stack:
        def mock(name, **kwargs):
            return stack.enter_context(patch(name, **kwargs))
        mock("core.ai_disabled_gate.is_ai_allowed_by_store_mode",
             return_value=store_ai_mode_allows({"store_ai_mode": mode,
                                               "ai_test_allowed_numbers": []}, PHONE))
        mock("core.ai_disabled_gate._find_conversations_for_phone", return_value=[convo])
        mock("core.automation_send_guard.is_internal_or_blocked",
             side_effect=RuntimeError("safety database unavailable") if safety_error else None,
             return_value=(blocked, "blocked" if blocked else None))
        mock("core.wa_usage.check_limit", return_value=SimpleNamespace(
            allowed=quota, used_total=5, limit=5, reason="quota"))
        mock("services.whatsapp_platform.service._resolve_token", new=AsyncMock(
            return_value=SimpleNamespace(token="test", source="test")))
        mock("services.whatsapp_platform.service.provider_post_with_context", new=posted)
        mock("observability.rate_limiter.check_rate_limit", return_value=True)
        mock("core.outbound_dedup.check_outbound_send", return_value=None)
        mock("core.outbound_dedup.record_outbound_result")
        mock("core.outbound_send_status.stamp_outbound_send_status", return_value=None)
        if direct:
            result, _ = asyncio.run(provider_send_message(
                db, conn, tenant_id=77, operation="send_message", phone_id="PH1",
                payload=payload, unsubscribe_notice=notice,
            ))
            ok = "error" not in result
        else:
            ok = asyncio.run(_post_wa(
                phone_id="PH1", payload=payload, _tenant_id=77, _db=db,
                _store_name="متجر تجريبي عام", _unsubscribe_notice=notice,
            ))
    return ok, posted


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("mode", ["test", "off"])
def test_normal_ai_reply_stays_blocked(mode, direct):
    ok, posted = send(build_text_payload(PHONE, "generic product answer"),
                      mode=mode, notice=False, direct=direct)
    assert ok is False
    posted.assert_not_awaited()


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("constraint", ["paused", "blocked", "quota", "safety_error"])
def test_consent_preserves_recipient_safety_and_quota(direct, constraint):
    settings = {constraint: False if constraint == "quota" else True}
    ok, posted = send(build_confirmation_payload(PHONE), direct=direct, **settings)
    assert ok is False
    posted.assert_not_awaited()


@pytest.mark.parametrize("direct", [False, True])
def test_notice_flag_cannot_send_arbitrary_text(direct):
    ok, posted = send(build_text_payload(PHONE, "unexpected promotion"), direct=direct)
    assert ok is False
    posted.assert_not_awaited()


def test_closed_notice_payload_rejects_changed_buttons_and_extra_fields():
    payload = build_confirmation_payload(PHONE)
    payload["interactive"]["action"]["buttons"][0]["reply"]["id"] = "buy_now"
    assert not is_unsubscribe_notice_payload(payload)
    payload = build_confirmation_payload(PHONE)
    payload["image"] = {"link": "https://example.com/promotion.png"}
    assert not is_unsubscribe_notice_payload(payload)
    assert not is_unsubscribe_notice_payload(None)


def test_provider_failure_is_not_reported_as_success():
    ok, posted = send(build_confirmation_payload(PHONE), provider_response={
        "error": {"code": 131000, "message": "send failed"},
        "_nahla_classification": "provider_error_field",
    })
    assert ok is False
    posted.assert_awaited_once()


@pytest.mark.parametrize("outcomes, saved_count, stamped", [
    ([True], 1, True),
    ([False, True], 1, True),
    ([False, False], 0, False),
])
def test_real_webhook_prompt_fallback_and_success_stamp(outcomes, saved_count, stamped):
    # Compile the actual nested helper so its fallback/persistence sequence is
    # exercised without bootstrapping unrelated media/AI webhook infrastructure.
    import ast
    import logging
    from pathlib import Path

    source = Path("backend/routers/whatsapp_webhook.py").read_text()
    tree = ast.parse(source)
    helper = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "_send_unsub_confirmation_prompt")
    posted = AsyncMock(side_effect=outcomes)
    saved, stamp = MagicMock(), MagicMock()
    namespace = dict(
        _post_wa=posted, _save_unsub_msg=saved, mark_pending_prompt_sent=stamp,
        phone_number_id="PH1", normalized_sender=PHONE, resolved_tenant_id=77,
        db=MagicMock(), _lead=SimpleNamespace(id=8), logger=logging.getLogger(__name__),
        build_confirmation_payload=build_confirmation_payload,
        build_confirmation_fallback_payload=build_confirmation_fallback_payload,
    )
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "webhook_helper", "exec"), namespace)
    result = asyncio.run(namespace["_send_unsub_confirmation_prompt"]())
    assert result is stamped
    assert saved.call_count == saved_count
    assert stamp.called is stamped
    for call in posted.call_args_list:
        assert call.kwargs["_unsubscribe_notice"] is True
        assert is_unsubscribe_notice_payload(call.kwargs["payload"])
