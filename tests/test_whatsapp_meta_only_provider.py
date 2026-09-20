"""Meta is the only WhatsApp provider, and a leftover row never becomes Meta.

360dialog was removed. Its connection rows were not — a merchant's history,
number and connection record are not ours to delete — so the platform still
meets rows whose ``provider`` column names an integration this code no longer
speaks. The failure to prevent is quiet promotion: answering "meta" for such a
row would send one provider's credential to another provider's API, on a number
Meta may never have had.

Three properties are pinned here:

* the retired inbound URLs accept nothing — no parse, no persistence, no
  background work, no path into the Meta pipeline;
* no send, read, health check or status answer treats an unsupported row as
  Meta, and each refusal is **definite** rather than ambiguous, so nothing
  resends on the strength of it;
* a row that says Meta, or says nothing at all (every Meta row written before
  the column existed), keeps working exactly as it did.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR, REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from services.whatsapp_platform import provider_utils as pu  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _conn(provider):
    return SimpleNamespace(id=7, provider=provider, phone_number_id="PID-1",
                           connection_type="embedded", access_token="tok")


def _ctx(token="EAAJ_meta_token"):
    return SimpleNamespace(token=token, source="merchant_oauth", token_status="healthy")


# ── 1. The provider question itself ──────────────────────────────────────────


@pytest.mark.parametrize("stored", ["meta", "META", " meta ", "", None])
def test_a_row_that_says_meta_or_says_nothing_is_meta(stored):
    assert pu.wa_provider(_conn(stored)) == pu.WHATSAPP_PROVIDER_META
    assert pu.provider_is_supported(_conn(stored)) is True


@pytest.mark.parametrize("stored", ["dialog360", "360dialog", "d360", "twilio", "whatever"])
def test_no_other_stored_value_is_ever_promoted_to_meta(stored):
    assert pu.wa_provider(_conn(stored)) == pu.WHATSAPP_PROVIDER_UNSUPPORTED
    assert pu.provider_is_supported(_conn(stored)) is False
    assert pu.raw_provider(_conn(stored)) == stored.strip().lower()
    with pytest.raises(pu.UnsupportedWhatsAppProvider):
        pu.require_supported_provider(_conn(stored))


def test_the_url_and_header_builders_refuse_rather_than_default_to_meta():
    from services.whatsapp_platform.service import _provider_base_url, _provider_headers

    assert _provider_base_url(_conn("meta")).startswith("https://graph.facebook.com/")
    assert "Authorization" in _provider_headers(_conn("meta"), _ctx())
    with pytest.raises(pu.UnsupportedWhatsAppProvider):
        _provider_base_url(_conn("dialog360"))
    with pytest.raises(pu.UnsupportedWhatsAppProvider):
        _provider_headers(_conn("dialog360"), _ctx())


# ── 2. Nothing is sent, and the refusal is definite ──────────────────────────


def test_a_send_for_an_unsupported_row_never_reaches_the_wire():
    from core.wa_provider_observability import get_recent_attempts, reset_for_tests
    from services.whatsapp_platform.service import provider_post_with_context

    reset_for_tests()
    client = MagicMock()
    client.post = AsyncMock(side_effect=AssertionError("no request may be built"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("services.whatsapp_platform.service.httpx.AsyncClient", return_value=cm):
        data = _run(provider_post_with_context(
            _conn("dialog360"), _ctx(token="d360-key"), tenant_id=33,
            operation="send_message", path="messages", json={"to": "+966500000000"},
        ))

    assert data["error"]["_nahla_unsupported_provider"] is True
    assert "not supported" in data["error"]["message"]
    client.post.assert_not_awaited()

    latest = get_recent_attempts(33)[0]
    # Definite, not ambiguous: "exception" is the classification that tells
    # callers a send may still have been accepted. A refusal is not that.
    assert latest["classification"] == "provider_error_field"
    assert latest["response_status"] is None


def test_a_read_for_an_unsupported_row_never_reaches_the_wire():
    from services.whatsapp_platform.service import provider_get_with_context

    client = MagicMock()
    client.get = AsyncMock(side_effect=AssertionError("no request may be built"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("services.whatsapp_platform.service.httpx.AsyncClient", return_value=cm):
        data = _run(provider_get_with_context(
            _conn("dialog360"), _ctx(token="d360-key"), tenant_id=33,
            operation="fetch_phone_tier", path="PID-1",
        ))
    assert data["error"]["_nahla_unsupported_provider"] is True
    client.get.assert_not_awaited()


def test_a_meta_row_still_sends_and_is_classified_ok():
    from core.wa_provider_observability import get_recent_attempts, reset_for_tests
    from services.whatsapp_platform.service import provider_post_with_context

    reset_for_tests()
    response = MagicMock()
    response.status_code = 200
    response.text = "{}"
    response.json.return_value = {"messages": [{"id": "wamid.OK"}]}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("services.whatsapp_platform.service.httpx.AsyncClient", return_value=cm):
        data = _run(provider_post_with_context(
            _conn("meta"), _ctx(), tenant_id=34, operation="send_message",
            path="PID-1/messages", json={"to": "+966500000000"},
        ))
    assert data["messages"][0]["id"] == "wamid.OK"
    assert get_recent_attempts(34)[0]["classification"] == "ok"


# ── 3. Health and status never render a retired row as working ───────────────


def test_connection_health_reports_the_row_as_unsupported_not_valid():
    from services.whatsapp_platform.wa_token_validation import validate_connection_health

    result = _run(validate_connection_health(_conn("dialog360")))
    assert result.is_valid is False
    assert result.production_ready is False
    assert result.token_status == "unsupported_provider"
    assert result.error_code == "unsupported_provider"


def test_the_guardian_does_not_stamp_a_retired_row_as_webhook_verified():
    from core import webhook_guardian as wg

    assert wg._provider_is_retired(_conn("dialog360")) is True
    assert wg._provider_is_retired(_conn("meta")) is False
    assert wg._provider_is_retired(_conn(None)) is False
    # Graph cannot speak for it either, so the subscribe probe is skipped.
    assert wg._is_meta_graph_compatible(provider="dialog360", connection_type="embedded") is False
    assert wg._is_meta_graph_compatible(provider="meta", connection_type="embedded") is True


# ── 4. The retired inbound URLs accept nothing ───────────────────────────────


@pytest.mark.parametrize("path", [
    "/webhook/whatsapp/360dialog",
    "/webhook/whatsapp/360dialog/coexistence",
    "/webhook/whatsapp/360dialog/status",
])
def test_a_retired_inbound_url_refuses_without_reading_or_scheduling_anything(path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import whatsapp_webhook as wh

    app = FastAPI()
    app.include_router(wh.router)

    spawned = []
    with patch.object(wh, "spawn_background", create=True, new=lambda *a, **k: spawned.append(a)):
        with TestClient(app) as client:
            response = client.post(path, json={"entry": [{"changes": [
                {"field": "messages", "value": {"messages": [{"id": "wamid.X", "from": "+9665"}]}}]}]})

    assert response.status_code == 410
    body = response.json()
    assert body["status"] == "gone"
    assert body["reason"] == wh.RETIRED_PROVIDER_REASON
    assert spawned == []


def test_the_module_keeps_no_handler_for_the_retired_provider():
    from routers import whatsapp_webhook as wh

    for name in ("_handle_360dialog_body", "_safe_360dialog_ack",
                 "_classify_360dialog_field", "_scope_accepts"):
        assert not hasattr(wh, name), name


# ── 4. The token boundary: a retired row's credential is never processed ─────
#
# The reviewer's reproduction: a connection left over from the retired provider
# still holds a stored credential, and that credential is past its expiry. On
# the reviewed head ``provider_send_message`` resolved the token first — reading
# the credential, exchanging it at Meta's OAuth endpoint because it was expired,
# rewriting the row's expiry on Meta's 190 and committing token state onto it —
# and only then reached the refusal. Every one of those has to be zero.

import copy
from datetime import datetime, timedelta, timezone


def _retired_row_with_expired_credential():
    return SimpleNamespace(
        id=7, tenant_id=33, provider="dialog360", phone_number_id="PID-1",
        connection_type="embedded", access_token="retired-provider-key-expired",
        token_type="retired", token_expires_at=datetime.now(timezone.utc) - timedelta(days=3),
        extra_metadata={"token_status": "expired", "oauth_session_status": "expired"},
    )


class _NoNetwork:
    """An ``httpx.AsyncClient`` that fails the case the moment it is constructed."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("no HTTP client may be opened for a retired provider's row")


def _snapshot(conn):
    return (copy.deepcopy(conn.extra_metadata), conn.token_expires_at, conn.access_token,
            conn.token_type)


def test_an_expired_retired_credential_is_neither_refreshed_nor_rewritten_by_a_send():
    from core.wa_provider_observability import get_recent_attempts, reset_for_tests
    from services.whatsapp_platform import service, token_manager
    from services.whatsapp_platform.service import provider_send_message

    reset_for_tests()
    conn = _retired_row_with_expired_credential()
    before = _snapshot(conn)
    db = MagicMock()
    with (
        patch.object(service.httpx, "AsyncClient", _NoNetwork),
        patch.object(token_manager.httpx, "AsyncClient", _NoNetwork),
        patch.object(token_manager, "read_access_token",
                     side_effect=AssertionError("the stored credential must not be read")),
    ):
        data, ctx = _run(provider_send_message(
            db, conn, tenant_id=33, operation="send_message", phone_id="PID-1",
            payload={"to": "+966500000000", "type": "text", "text": {"body": "مرحبا"}},
            automation_guard=False,
        ))

    assert data["error"]["_nahla_unsupported_provider"] is True
    assert ctx.token == ""
    assert ctx.token_status == token_manager.TOKEN_STATUS_UNSUPPORTED_PROVIDER
    assert _snapshot(conn) == before                       # zero metadata mutation
    db.commit.assert_not_called()                          # nothing persisted
    latest = get_recent_attempts(33)[0]
    assert latest["classification"] == "provider_error_field"   # definite, not "exception"
    assert latest["response_status"] is None


def test_token_resolution_refuses_a_retired_row_before_reading_its_credential():
    from services.whatsapp_platform import token_manager

    conn = _retired_row_with_expired_credential()
    before = _snapshot(conn)
    db = MagicMock()
    with (
        patch.object(token_manager.httpx, "AsyncClient", _NoNetwork),
        patch.object(token_manager, "read_access_token",
                     side_effect=AssertionError("the stored credential must not be read")),
    ):
        with pytest.raises(pu.UnsupportedWhatsAppProvider):
            _run(token_manager.get_token_for_operation(
                db, conn, tenant_id=33, operation="send_message"))
    assert _snapshot(conn) == before
    db.commit.assert_not_called()


def test_the_synchronous_token_context_is_unsupported_without_reading_the_credential():
    from services.whatsapp_platform import token_manager

    conn = _retired_row_with_expired_credential()
    with patch.object(token_manager, "read_access_token",
                      side_effect=AssertionError("the stored credential must not be read")):
        ctx = token_manager.get_token_context(conn)
        candidates = token_manager.get_token_candidates(conn)
        state = token_manager.get_oauth_session_state(conn)
    assert ctx.token == ""
    assert ctx.token_status == token_manager.TOKEN_STATUS_UNSUPPORTED_PROVIDER
    assert [c.token_status for c in candidates] == [token_manager.TOKEN_STATUS_UNSUPPORTED_PROVIDER]
    assert state == (token_manager.TOKEN_STATUS_UNSUPPORTED_PROVIDER, None)


def test_the_refresh_never_exchanges_a_retired_credential_at_meta():
    from services.whatsapp_platform import token_manager

    conn = _retired_row_with_expired_credential()
    before = _snapshot(conn)
    with (
        patch.object(token_manager.httpx, "AsyncClient", _NoNetwork),
        patch.object(token_manager, "read_access_token",
                     side_effect=AssertionError("the stored credential must not be read")),
    ):
        assert _run(token_manager._refresh_merchant_long_lived_token(conn)) is None
    assert _snapshot(conn) == before


def test_token_state_is_never_written_onto_a_retired_row():
    from services.whatsapp_platform import token_manager

    conn = _retired_row_with_expired_credential()
    before = _snapshot(conn)
    db = MagicMock()
    token_manager.update_token_state(conn, token_source="platform", token_status="healthy",
                                     oauth_session_status="healthy")
    token_manager.persist_token_context(
        db, conn, tenant_id=33, operation="send_message",
        ctx=token_manager.unsupported_provider_context(conn))
    assert _snapshot(conn) == before
    db.commit.assert_not_called()


@pytest.mark.parametrize("stored", ["meta", ""])
def test_a_meta_row_and_a_pre_column_row_still_resolve_their_token(stored):
    """The compatibility behaviour is untouched: Meta, and the rows written
    before the column existed, resolve and persist exactly as before."""
    from services.whatsapp_platform import token_manager

    conn = SimpleNamespace(
        id=7, tenant_id=33, provider=stored, phone_number_id="PID-1",
        connection_type="embedded", access_token="tok",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30), extra_metadata={})
    db = MagicMock()
    with patch.object(token_manager.httpx, "AsyncClient", _NoNetwork):
        ctx = _run(token_manager.get_token_for_operation(
            db, conn, tenant_id=33, operation="send_message"))
    assert ctx.token == "tok" and ctx.token_status == "healthy"
    assert conn.extra_metadata["token_status"] == "healthy"
    db.commit.assert_called_once()
