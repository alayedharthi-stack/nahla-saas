"""P0 — WhatsApp send recipient formatting and empty-reply suppression."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.phone_utils import (  # noqa: E402
    format_wa_send_recipient,
    redact_phone_for_log,
)


def _run(coro):
    return asyncio.run(coro)


class TestFormatWaSendRecipient:
    def test_international_digits_unchanged(self) -> None:
        assert format_wa_send_recipient("966564725255") == "966564725255"

    def test_e164_strips_plus(self) -> None:
        assert format_wa_send_recipient("+966564725255") == "966564725255"

    def test_saudi_local_normalizes(self) -> None:
        assert format_wa_send_recipient("0564725255") == "966564725255"

    def test_invalid_returns_none(self) -> None:
        assert format_wa_send_recipient("") is None
        assert format_wa_send_recipient("not-a-phone") is None
        assert format_wa_send_recipient("123") is None

    def test_redact_masks_msisdn(self) -> None:
        assert "564725255" not in redact_phone_for_log("966564725255")
        assert redact_phone_for_log("966564725255").startswith("9665")


class TestProviderSendBoundary:
    def test_invalid_recipient_skips_provider_post_and_token(self) -> None:
        from services.whatsapp_platform.service import provider_send_message

        captured: list = []

        async def fake_post(*args, **kwargs):
            captured.append((args, kwargs))
            return {"messages": [{"id": "wamid.X"}]}

        conn = MagicMock()
        conn.extra_metadata = {}

        with patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(),
        ) as mock_token, patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ):
            resp, _ctx = _run(provider_send_message(
                MagicMock(),
                conn,
                tenant_id=1,
                operation="send_message",
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "not-a-phone",
                    "type": "text",
                    "text": {"body": "hi"},
                },
            ))

        assert captured == []
        mock_token.assert_not_called()
        assert "error" in resp
        assert resp.get("_nahla_classification") == "recipient_invalid"

    def test_plus_prefix_normalized_before_post(self) -> None:
        from services.whatsapp_platform.service import provider_send_message

        posted: List[Dict[str, Any]] = []

        async def fake_post(_conn, _ctx, *, json=None, **kwargs):
            posted.append(dict(json or {}))
            return {"messages": [{"id": "wamid.Y"}]}

        conn = MagicMock()
        conn.extra_metadata = {}

        with patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(),
        ) as mock_token, patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.wa_provider",
            return_value="360dialog",
        ):
            mock_token.return_value = MagicMock(token="tok", source="test")

            _run(provider_send_message(
                MagicMock(),
                conn,
                tenant_id=1,
                operation="send_message",
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "+966564725255",
                    "type": "text",
                    "text": {"body": "hi"},
                },
            ))

        assert posted
        assert posted[0]["to"] == "966564725255"
        assert "+" not in posted[0]["to"]

    def test_digits_only_recipient_unchanged_on_post(self) -> None:
        from services.whatsapp_platform.service import provider_send_message

        posted: List[Dict[str, Any]] = []

        async def fake_post(_conn, _ctx, *, json=None, **kwargs):
            posted.append(dict(json or {}))
            return {"messages": [{"id": "wamid.Z"}]}

        conn = MagicMock()
        conn.extra_metadata = {}

        with patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(),
        ) as mock_token, patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.wa_provider",
            return_value="360dialog",
        ):
            mock_token.return_value = MagicMock(token="tok", source="test")

            _run(provider_send_message(
                MagicMock(),
                conn,
                tenant_id=1,
                operation="send_message",
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "966564725255",
                    "type": "text",
                    "text": {"body": "الله يسلمك"},
                },
            ))

        assert posted[0]["to"] == "966564725255"


class TestPostWaSendBoundary:
    def test_send_whatsapp_message_formats_at_provider_layer(self) -> None:
        from routers.whatsapp_webhook import _send_whatsapp_message

        posted: List[Dict[str, Any]] = []

        async def fake_post(_conn, _ctx, *, json=None, **kwargs):
            posted.append(dict(json or {}))
            return {"messages": [{"id": "wamid.A"}]}

        mock_conn = MagicMock()
        mock_conn.extra_metadata = {}
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_conn

        with patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ), patch(
            "services.whatsapp_platform.service.wa_provider",
            return_value="360dialog",
        ), patch(
            "observability.rate_limiter.check_rate_limit",
            return_value=True,
        ), patch(
            "core.outbound_dedup.check_outbound_send",
            return_value=None,
        ), patch(
            "core.outbound_dedup.record_outbound_result",
        ):
            ok = _run(_send_whatsapp_message(
                phone_id="PH1",
                to="+966564725255",
                text="hi",
                _tenant_id=1,
                _db=mock_db,
            ))

        assert ok is True
        assert posted[0]["to"] == "966564725255"


class TestEmptyReplySuppression:
    def test_should_suppress_empty_reply(self) -> None:
        from routers.whatsapp_webhook import _should_suppress_empty_outbound_reply

        assert _should_suppress_empty_outbound_reply("") is True
        assert _should_suppress_empty_outbound_reply("   ") is True
        assert _should_suppress_empty_outbound_reply("الله يسلمك") is False
        assert _should_suppress_empty_outbound_reply(
            "", brain_buttons=[{"type": "reply"}],
        ) is False

    def test_suppressed_empty_reply_does_not_invoke_send_helper(self) -> None:
        from routers.whatsapp_webhook import (
            _log_empty_outbound_suppressed,
            _should_suppress_empty_outbound_reply,
        )

        send_calls: list = []

        async def fake_send(*args, **kwargs):
            send_calls.append((args, kwargs))
            return True

        reply = ""
        brain_buttons: list = []
        if _should_suppress_empty_outbound_reply(reply, brain_buttons=brain_buttons):
            _log_empty_outbound_suppressed(
                tenant_id=1,
                to="966564725255",
                conversation_id=99,
                reason="skip_wire_send",
            )
        else:
            _run(fake_send())

        assert send_calls == []

    def test_non_empty_social_reply_still_posts(self) -> None:
        from services.whatsapp_platform.service import provider_send_message

        posted: List[Dict[str, Any]] = []

        async def fake_post(_conn, _ctx, *, json=None, **kwargs):
            posted.append(dict(json or {}))
            return {"messages": [{"id": "wamid.social"}]}

        conn = MagicMock()
        conn.extra_metadata = {}

        with patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(),
        ) as mock_token, patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.wa_provider",
            return_value="360dialog",
        ):
            mock_token.return_value = MagicMock(token="tok", source="test")

            resp, _ = _run(provider_send_message(
                MagicMock(),
                conn,
                tenant_id=1,
                operation="send_message",
                phone_id="PH1",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "966564725255",
                    "type": "text",
                    "text": {"body": "الله يسلمك"},
                },
            ))

        assert "error" not in resp
        assert posted[0]["text"]["body"] == "الله يسلمك"
