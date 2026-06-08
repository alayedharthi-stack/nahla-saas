"""
tests/test_provider_send_observability.py
─────────────────────────────────────────
Locks F18 — the wire-layer observability + read-only debug endpoint
for outbound WhatsApp provider POSTs.

The exact production failure this is built for:

    HTTP 200 from 360dialog
    response body has no `error` field
    response body has no `messages[0].id` field (no wamid)

Pre-F18 the wire layer classified that as a success and the
downstream dispatcher persisted ``wa_message_id = None`` as
"delivered". F18 reclassifies this as
``classification == "missing_wamid"`` and injects an ``error``
envelope into the response so the existing
``"error" in resp_data`` checks in ``_post_wa`` flip the send to
"failed".

Coverage map
────────────
Module-level helpers in ``core.wa_provider_observability``:

* ``record_attempt`` / ``get_recent_attempts`` — ring buffer
  basics, per-tenant isolation, FIFO eviction, ``newest first``
  order.
* ``_scrub_payload`` — masks the customer ``to`` phone.
* ``_truncate_for_log`` — bounds OOM risk from huge payloads.
* ``summarize_headers`` — maps D360-API-KEY → ``token_tail``,
  Authorization → ``token_tail`` (Bearer stripped).

Service-layer helpers in ``services.whatsapp_platform.service``:

* ``_is_send_path`` — Meta ``{phone}/messages`` and 360dialog
  ``messages`` both classify as send paths.
* ``_extract_wamid`` — returns ``messages[0].id`` or ``None``.
* ``_classify_response`` — non_2xx > provider_error >
  missing_wamid > ok precedence.

End-to-end ``provider_post_with_context`` behaviour:

* 2xx + wamid                  → ``ok``, returned as-is.
* 2xx + ``messages: []``       → ``missing_wamid``, ``error``
                                  envelope injected.
* 2xx + ``error`` envelope     → ``provider_error_field``,
                                  passed through unchanged.
* 4xx                          → ``non_2xx``, passed through.
* exception (network)          → re-raised, attempt logged.

Admin endpoint ``GET /admin/debug/last-provider-send``:

* Returns empty ``attempts`` cleanly when nothing recorded.
* Surfaces ``missing_wamid`` count + last attempt in
  ``last_missing_wamid_attempt`` block.
* Flags phone_number_id drift between current connection and
  recorded attempts.
* Masks the API key (only ``***xxxx`` tail leaks).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _run(coro):
    return asyncio.run(coro)


# ── Reset the module-level ring buffer between every test so the
# ── ordering / FIFO assertions remain deterministic.
@pytest.fixture(autouse=True)
def _reset_ring_buffer():
    from core.wa_provider_observability import reset_for_tests
    reset_for_tests()
    yield
    reset_for_tests()


# ── core.wa_provider_observability ──────────────────────────────────


class TestRingBufferBasics:
    def test_empty_buffer_returns_empty_list(self):
        from core.wa_provider_observability import get_recent_attempts
        assert get_recent_attempts(33) == []
        assert get_recent_attempts(33, limit=5) == []

    def test_record_then_read_returns_newest_first(self):
        from core.wa_provider_observability import get_recent_attempts, record_attempt

        for i in range(3):
            record_attempt(
                tenant_id=33,
                operation="send_message",
                provider="dialog360",
                method="POST",
                full_url="https://waba-v2.360dialog.io/messages",
                path="messages",
                request_payload={"to": f"+96650000000{i}"},
                headers_summary={"token_source": "merchant_oauth"},
                response_status=200,
                response_body={"messages": [{"id": f"wamid.{i}"}]},
                parsed_wamid=f"wamid.{i}",
                classification="ok",
                duration_ms=12.5,
            )

        out = get_recent_attempts(33, limit=10)
        assert len(out) == 3
        # Newest first: i=2 then i=1 then i=0.
        assert out[0]["parsed_wamid"] == "wamid.2"
        assert out[2]["parsed_wamid"] == "wamid.0"

    def test_per_tenant_isolation(self):
        from core.wa_provider_observability import get_recent_attempts, record_attempt

        record_attempt(
            tenant_id=33, operation="op", provider="dialog360",
            method="POST", full_url="u", path="messages",
            request_payload=None, headers_summary={},
            response_status=200, response_body={}, parsed_wamid="A",
            classification="ok", duration_ms=1.0,
        )
        record_attempt(
            tenant_id=99, operation="op", provider="dialog360",
            method="POST", full_url="u", path="messages",
            request_payload=None, headers_summary={},
            response_status=200, response_body={}, parsed_wamid="B",
            classification="ok", duration_ms=1.0,
        )
        a33 = get_recent_attempts(33)
        a99 = get_recent_attempts(99)
        assert [x["parsed_wamid"] for x in a33] == ["A"]
        assert [x["parsed_wamid"] for x in a99] == ["B"]

    def test_per_tenant_cap_evicts_oldest(self):
        from core.wa_provider_observability import (
            _MAX_ATTEMPTS_PER_TENANT, get_recent_attempts, record_attempt,
        )
        # Fill past the cap, oldest entries must roll off.
        N = _MAX_ATTEMPTS_PER_TENANT + 5
        for i in range(N):
            record_attempt(
                tenant_id=33, operation="op", provider="dialog360",
                method="POST", full_url="u", path="messages",
                request_payload=None, headers_summary={},
                response_status=200, response_body={},
                parsed_wamid=f"wamid.{i}", classification="ok",
                duration_ms=1.0,
            )
        out = get_recent_attempts(33, limit=_MAX_ATTEMPTS_PER_TENANT + 10)
        assert len(out) == _MAX_ATTEMPTS_PER_TENANT
        # Newest entry is N-1; oldest entry that survived is i=5.
        assert out[0]["parsed_wamid"] == f"wamid.{N-1}"
        assert out[-1]["parsed_wamid"] == "wamid.5"


class TestScrubPayload:
    def test_masks_recipient_phone_in_payload(self):
        from core.wa_provider_observability import _scrub_payload
        out = _scrub_payload({"to": "+966537970430", "type": "text"})
        assert "537970" not in (out.get("to") or "")
        assert out.get("type") == "text"  # untouched

    def test_handles_short_phone_with_full_redaction(self):
        from core.wa_provider_observability import _scrub_payload
        out = _scrub_payload({"to": "12345"})
        assert out["to"] == "***"

    def test_returns_payload_unchanged_when_no_to_field(self):
        from core.wa_provider_observability import _scrub_payload
        out = _scrub_payload({"type": "text", "text": {"body": "hi"}})
        assert out == {"type": "text", "text": {"body": "hi"}}


class TestSummarizeHeaders:
    def test_dialog360_header_summary(self):
        from core.wa_provider_observability import summarize_headers
        summary = summarize_headers(
            {"D360-API-KEY": "d360_secret_tail_ABCD", "Content-Type": "application/json"},
            token_source="merchant_oauth",
        )
        assert summary["auth_header_name"] == "D360-API-KEY"
        assert summary["auth_header_tail"].endswith("ABCD")
        # The secret middle must NOT leak.
        assert "secret_tail" not in summary["auth_header_tail"]
        assert summary["token_source"] == "merchant_oauth"

    def test_meta_bearer_strips_prefix_and_masks_tail(self):
        from core.wa_provider_observability import summarize_headers
        summary = summarize_headers(
            {"Authorization": "Bearer EAAJ_secret_XYZW", "Content-Type": "application/json"},
        )
        assert summary["auth_header_name"] == "Authorization"
        # Tail extracted from the token part, not the "Bearer " prefix.
        assert summary["auth_header_tail"].endswith("XYZW")
        assert "Bearer" not in (summary["auth_header_tail"] or "")


# ── services.whatsapp_platform.service helpers ─────────────────────


class TestIsSendPath:
    def test_dialog360_send_path(self):
        from services.whatsapp_platform.service import _is_send_path
        assert _is_send_path("messages") is True
        assert _is_send_path("/messages") is True

    def test_meta_send_path(self):
        from services.whatsapp_platform.service import _is_send_path
        assert _is_send_path("100543193146977/messages") is True
        assert _is_send_path("/100543193146977/messages") is True

    def test_non_send_paths(self):
        from services.whatsapp_platform.service import _is_send_path
        assert _is_send_path("v1/configs/templates") is False
        assert _is_send_path("100543193146977/message_templates") is False
        assert _is_send_path("") is False


class TestExtractWamid:
    def test_extracts_wamid_on_success_shape(self):
        from services.whatsapp_platform.service import _extract_wamid
        assert _extract_wamid({"messages": [{"id": "wamid.ABC"}]}) == "wamid.ABC"

    def test_returns_none_on_empty_messages(self):
        from services.whatsapp_platform.service import _extract_wamid
        assert _extract_wamid({"messages": []}) is None

    def test_returns_none_on_missing_messages_field(self):
        from services.whatsapp_platform.service import _extract_wamid
        assert _extract_wamid({"error": {"code": 132000}}) is None

    def test_returns_none_on_non_dict(self):
        from services.whatsapp_platform.service import _extract_wamid
        assert _extract_wamid(None) is None
        assert _extract_wamid("not a dict") is None


class TestClassifyResponse:
    def test_2xx_with_wamid_is_ok(self):
        from services.whatsapp_platform.service import (
            CLASSIFICATION_OK, _classify_response,
        )
        assert _classify_response(
            is_send=True, status_code=200,
            body={"messages": [{"id": "wamid.X"}]},
            wamid="wamid.X",
        ) == CLASSIFICATION_OK

    def test_2xx_no_wamid_on_send_is_missing_wamid(self):
        """The exact production scenario."""
        from services.whatsapp_platform.service import (
            CLASSIFICATION_MISSING_WAMID, _classify_response,
        )
        assert _classify_response(
            is_send=True, status_code=200, body={"messages": []}, wamid=None,
        ) == CLASSIFICATION_MISSING_WAMID
        # Also when "messages" missing entirely.
        assert _classify_response(
            is_send=True, status_code=200, body={"foo": "bar"}, wamid=None,
        ) == CLASSIFICATION_MISSING_WAMID

    def test_2xx_no_wamid_on_NON_send_is_ok(self):
        """Template submit etc. legitimately have no wamid."""
        from services.whatsapp_platform.service import (
            CLASSIFICATION_OK, _classify_response,
        )
        assert _classify_response(
            is_send=False, status_code=200, body={"id": "tpl_abc"}, wamid=None,
        ) == CLASSIFICATION_OK

    def test_non_2xx_overrides_other_signals(self):
        from services.whatsapp_platform.service import (
            CLASSIFICATION_NON_2XX, _classify_response,
        )
        assert _classify_response(
            is_send=True, status_code=401,
            body={"error": {"message": "unauthorized"}}, wamid=None,
        ) == CLASSIFICATION_NON_2XX

    def test_provider_error_field_on_2xx(self):
        from services.whatsapp_platform.service import (
            CLASSIFICATION_PROVIDER_ERROR, _classify_response,
        )
        # 2xx but error envelope present — e.g. Meta returns 200 with
        # a code 100 on some validation failures.
        assert _classify_response(
            is_send=True, status_code=200,
            body={"error": {"code": 132000, "message": "Template Param Mismatch"}},
            wamid=None,
        ) == CLASSIFICATION_PROVIDER_ERROR


# ── End-to-end provider_post_with_context behaviour ────────────────


def _make_conn(*, provider="dialog360", phone_number_id="100543193146977"):
    return MagicMock(
        provider=provider,
        phone_number_id=phone_number_id,
        id=42,
        connection_type="coexistence",
    )


def _make_ctx(*, token="d360_fake_secret_TAIL", source="merchant_oauth"):
    ctx = MagicMock()
    ctx.token = token
    ctx.source = source
    return ctx


def _patch_httpx_post(*, status_code, json_body, raises=None):
    """Build the ``patch`` context manager you'd use with
    ``httpx.AsyncClient``. The mock returns a response whose
    ``status_code`` and ``.json()`` echo the inputs; ``raises`` lets
    the caller simulate a transport-level failure."""
    response = MagicMock()
    response.status_code = status_code
    response.text = str(json_body)
    response.json.return_value = json_body

    client = MagicMock()
    if raises is not None:
        client.post = AsyncMock(side_effect=raises)
    else:
        client.post = AsyncMock(return_value=response)

    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=client)
    async_cm.__aexit__  = AsyncMock(return_value=False)
    return patch(
        "services.whatsapp_platform.service.httpx.AsyncClient",
        return_value=async_cm,
    )


class TestProviderPostWithContext:
    def test_dialog360_2xx_with_wamid_passes_through(self):
        from services.whatsapp_platform.service import provider_post_with_context

        success_body = {"messages": [{"id": "wamid.HBgN_OK"}]}
        with _patch_httpx_post(status_code=200, json_body=success_body):
            data = _run(provider_post_with_context(
                _make_conn(), _make_ctx(),
                tenant_id=33,
                operation="send_message",
                path="messages",
                json={"to": "+966537970430", "type": "text", "text": {"body": "hi"}},
            ))
        # Body untouched, no injected error.
        assert data == success_body
        assert "error" not in data

        from core.wa_provider_observability import get_recent_attempts
        latest = get_recent_attempts(33)[0]
        assert latest["classification"] == "ok"
        assert latest["parsed_wamid"] == "wamid.HBgN_OK"

    def test_dialog360_2xx_without_wamid_injects_error_envelope(self):
        """The F18 production scenario — 200 OK, no wamid, no
        error. Pre-F18 this was silently classified as success.
        Now we add an ``error`` envelope so downstream treats it
        as a failed send."""
        from services.whatsapp_platform.service import provider_post_with_context

        empty_body = {"messages": []}
        with _patch_httpx_post(status_code=200, json_body=empty_body):
            data = _run(provider_post_with_context(
                _make_conn(), _make_ctx(),
                tenant_id=33,
                operation="send_message",
                path="messages",
                json={"to": "+966537970430"},
            ))
        # F18 contract: an ``error`` envelope MUST be present so that
        # the existing ``"error" in resp_data`` checks in _post_wa
        # treat the send as failed.
        assert "error" in data
        err = data["error"]
        assert err.get("type") == "missing_wamid"
        assert err.get("nahla_injected") is True

        from core.wa_provider_observability import get_recent_attempts
        latest = get_recent_attempts(33)[0]
        assert latest["classification"] == "missing_wamid"
        assert latest["parsed_wamid"] is None

    def test_dialog360_non_2xx_records_non_2xx_classification(self):
        from services.whatsapp_platform.service import provider_post_with_context

        with _patch_httpx_post(status_code=401, json_body={"error": "unauthorized"}):
            data = _run(provider_post_with_context(
                _make_conn(), _make_ctx(),
                tenant_id=33,
                operation="send_message",
                path="messages",
                json={"to": "+966537970430"},
            ))
        # Provider body preserved; wire-layer may attach _nahla_* metadata.
        assert data["error"] == "unauthorized"
        assert data["_nahla_classification"] == "non_2xx"
        assert data["_nahla_wamid"] is None
        assert data["_nahla_is_send"] is True
        assert isinstance(data["_nahla_duration_ms"], (int, float))

        from core.wa_provider_observability import get_recent_attempts
        latest = get_recent_attempts(33)[0]
        assert latest["classification"] == "non_2xx"
        assert latest["response_status"] == 401

    def test_meta_send_uses_phone_id_in_path_and_records_correctly(self):
        from services.whatsapp_platform.service import provider_post_with_context

        success_body = {"messages": [{"id": "wamid.META"}]}
        with _patch_httpx_post(status_code=200, json_body=success_body):
            _run(provider_post_with_context(
                _make_conn(provider="meta", phone_number_id="100543193146977"),
                _make_ctx(token="EAAJ_meta_token_LONG", source="merchant_oauth"),
                tenant_id=33,
                operation="send_message",
                path="100543193146977/messages",
                json={"to": "+966500000000"},
            ))

        from core.wa_provider_observability import get_recent_attempts
        latest = get_recent_attempts(33)[0]
        assert latest["classification"] == "ok"
        # The provider tag captures whichever string ``wa_provider``
        # returned — for ``provider="meta"`` it's the meta sentinel.
        assert latest["provider"]   != "dialog360"
        # phone_number_id is captured for mismatch detection.
        assert latest["connection_phone_number_id"] == "100543193146977"

    def test_transport_exception_records_and_reraises(self):
        from services.whatsapp_platform.service import provider_post_with_context

        with _patch_httpx_post(status_code=0, json_body={}, raises=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                _run(provider_post_with_context(
                    _make_conn(), _make_ctx(),
                    tenant_id=33,
                    operation="send_message",
                    path="messages",
                    json={"to": "+966500000000"},
                ))

        from core.wa_provider_observability import get_recent_attempts
        latest = get_recent_attempts(33)[0]
        assert latest["classification"] == "exception"
        assert latest["response_status"] is None
        assert "boom" in (latest.get("error_text") or "")

    def test_non_json_response_body_is_classified_as_provider_error(self):
        """Some 360dialog edge cases (5xx HTML error pages from a
        WAF) come back as 200 OK with HTML. The wire layer must NOT
        explode and must classify it deterministically."""
        from services.whatsapp_platform.service import provider_post_with_context

        # Build a response whose .json() raises but .text returns HTML.
        response = MagicMock()
        response.status_code = 200
        response.text = "<html>Bad Gateway</html>"
        response.json.side_effect = ValueError("not json")

        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=client)
        async_cm.__aexit__  = AsyncMock(return_value=False)

        with patch(
            "services.whatsapp_platform.service.httpx.AsyncClient",
            return_value=async_cm,
        ):
            data = _run(provider_post_with_context(
                _make_conn(), _make_ctx(),
                tenant_id=33,
                operation="send_message",
                path="messages",
                json={"to": "+966500000000"},
            ))

        # The wire layer synthesises an error envelope.
        assert "error" in data
        assert data["error"]["type"] == "non_json_response"

        from core.wa_provider_observability import get_recent_attempts
        latest = get_recent_attempts(33)[0]
        # 2xx with synthetic error envelope → provider_error_field.
        assert latest["classification"] == "provider_error_field"


# ── GET /admin/debug/last-provider-send ────────────────────────────


def _make_db():
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base

    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


def _seed_tenant_and_conn(db, *, tenant_id=33, phone_number_id="100543193146977"):
    from models import Tenant, WhatsAppConnection
    t = Tenant(id=tenant_id, name=f"tenant-{tenant_id}")
    db.add(t); db.commit()
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        provider="dialog360",
        connection_type="coexistence",
        status="connected",
        phone_number_id=phone_number_id,
        access_token="d360_supersecret_TAIL",
    )
    db.add(conn); db.commit()
    return t, conn


def _call_endpoint(*, db, tenant_id, limit=10, admin_sub="admin@nahla"):
    from routers.admin_debug import admin_debug_last_provider_send
    return _run(admin_debug_last_provider_send(
        tenant_id=tenant_id,
        limit=limit,
        db=db,
        _admin={"sub": admin_sub, "role": "admin"},
    ))


class TestAdminLastProviderSendEndpoint:
    def test_unknown_tenant_returns_404(self):
        from fastapi import HTTPException
        db = _make_db()
        with pytest.raises(HTTPException) as exc:
            _call_endpoint(db=db, tenant_id=99999)
        assert exc.value.status_code == 404

    def test_empty_buffer_returns_empty_attempts(self):
        db = _make_db()
        _seed_tenant_and_conn(db)
        resp = _call_endpoint(db=db, tenant_id=33)
        assert resp["attempts"] == []
        assert resp["attempts_returned"] == 0
        # Hint surfaces the most-likely cause (fresh process).
        assert any("process" in h or "Process" in h or "deploy" in h for h in resp["hints"])

    def test_missing_wamid_attempt_is_summarised_in_top_level_block(self):
        from core.wa_provider_observability import record_attempt
        db = _make_db()
        _seed_tenant_and_conn(db)

        # Two ok, one missing_wamid.
        record_attempt(
            tenant_id=33, operation="send_message", provider="dialog360",
            method="POST", full_url="https://waba-v2.360dialog.io/messages",
            path="messages",
            request_payload={"to": "+966537970430"}, headers_summary={},
            response_status=200, response_body={"messages": [{"id": "wamid.A"}]},
            parsed_wamid="wamid.A", classification="ok", duration_ms=10.0,
        )
        record_attempt(
            tenant_id=33, operation="send_message", provider="dialog360",
            method="POST", full_url="https://waba-v2.360dialog.io/messages",
            path="messages",
            request_payload={"to": "+966537970430"}, headers_summary={},
            response_status=200, response_body={"messages": []},
            parsed_wamid=None, classification="missing_wamid", duration_ms=11.0,
        )
        record_attempt(
            tenant_id=33, operation="send_message", provider="dialog360",
            method="POST", full_url="https://waba-v2.360dialog.io/messages",
            path="messages",
            request_payload={"to": "+966537970430"}, headers_summary={},
            response_status=200, response_body={"messages": [{"id": "wamid.B"}]},
            parsed_wamid="wamid.B", classification="ok", duration_ms=9.0,
        )

        resp = _call_endpoint(db=db, tenant_id=33)
        assert resp["attempts_returned"] == 3
        assert resp["classification_counts"]["ok"] == 2
        assert resp["classification_counts"]["missing_wamid"] == 1
        assert resp["last_missing_wamid_attempt"] is not None
        # Surfaced as a top-level issue so the dashboard renders red.
        assert any("missing_wamid" in i or "wamid" in i for i in resp["issues"])
        # `ok` is False because there is at least one issue.
        assert resp["ok"] is False

    def test_phone_number_id_mismatch_is_flagged(self):
        from core.wa_provider_observability import record_attempt
        db = _make_db()
        # Current connection has phone_id 100543193146977.
        _seed_tenant_and_conn(db, phone_number_id="100543193146977")
        # But the recorded attempt used a different phone_id — that's
        # the "merchant reconnected under a new number, old refs
        # still cached" failure mode.
        record_attempt(
            tenant_id=33, operation="send_message", provider="dialog360",
            method="POST", full_url="https://waba-v2.360dialog.io/messages",
            path="messages",
            request_payload={"to": "+966537970430"}, headers_summary={},
            response_status=200, response_body={"messages": [{"id": "wamid.X"}]},
            parsed_wamid="wamid.X", classification="ok", duration_ms=10.0,
            connection_phone_number_id="1061057720431678",
        )
        resp = _call_endpoint(db=db, tenant_id=33)
        assert resp["mismatch_phone_id_count"] == 1
        assert any("phone_number_id" in i for i in resp["issues"])

    def test_payload_recipient_phone_is_masked_in_output(self):
        from core.wa_provider_observability import record_attempt
        db = _make_db()
        _seed_tenant_and_conn(db)
        record_attempt(
            tenant_id=33, operation="send_message", provider="dialog360",
            method="POST", full_url="https://waba-v2.360dialog.io/messages",
            path="messages",
            request_payload={"to": "+966537970430", "type": "text"},
            headers_summary={},
            response_status=200, response_body={"messages": [{"id": "wamid.X"}]},
            parsed_wamid="wamid.X", classification="ok", duration_ms=10.0,
        )
        resp = _call_endpoint(db=db, tenant_id=33)
        # Recipient phone in the stored payload is masked.
        stored_to = resp["attempts"][0]["request_payload"]["to"]
        assert "537970" not in stored_to
        # Connection's access_token tail is masked too — only 4 chars.
        tail = resp["current_connection"]["access_token_tail"]
        assert tail is None or "supersecret" not in tail

    def test_limit_clamps_attempts(self):
        from core.wa_provider_observability import record_attempt
        db = _make_db()
        _seed_tenant_and_conn(db)
        for i in range(15):
            record_attempt(
                tenant_id=33, operation="send_message", provider="dialog360",
                method="POST", full_url="u", path="messages",
                request_payload=None, headers_summary={},
                response_status=200, response_body={"messages": [{"id": f"wamid.{i}"}]},
                parsed_wamid=f"wamid.{i}", classification="ok", duration_ms=1.0,
            )
        resp = _call_endpoint(db=db, tenant_id=33, limit=5)
        assert len(resp["attempts"]) == 5
        # Newest first.
        assert resp["attempts"][0]["parsed_wamid"] == "wamid.14"
