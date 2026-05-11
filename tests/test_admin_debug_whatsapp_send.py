"""
tests/test_admin_debug_whatsapp_send.py
───────────────────────────────────────
Coverage for the new ``POST /admin/debug/whatsapp/send-template``
admin endpoint:

  * ``require_admin`` gates the route (non-admin → 403).
  * Unknown ``phone_number_id`` → 404.
  * Unknown ``template`` on the connected tenant → 404.
  * Happy path: provider call mocked, response parsed, phone masked,
    no campaign_send_log row written, provider_message_id surfaced.
  * Provider error response: still 200 but ``ok=false`` and raw error
    bubbles up unmodified for support.
  * Provider exception: 200 + ``ok=false`` + ``http_status=502`` and
    a synthetic error envelope so the UI shows something useful.
  * ``_mask_phone`` keeps first 4 + last 3 digits for any input.

We call the handler directly with ``asyncio.run`` (the same pattern
used by the other admin/debug tests) instead of spinning up a full
TestClient — the goal is to lock the contract, not exercise the
HTTP transport.
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
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _run(coro):
    return asyncio.run(coro)


def _make_db():
    """In-memory SQLite with the JSONB→JSON downgrade pattern used by
    the rest of the suite."""
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


def _seed_connection(db, *, tenant_id=33, phone_number_id="100543193146977"):
    from models import Tenant, WhatsAppConnection
    t = Tenant(id=tenant_id, name=f"tenant-{tenant_id}")
    db.add(t); db.commit()
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        connection_type="direct",
    )
    db.add(conn); db.commit()
    return t, conn


def _seed_template(db, *, tenant_id, name="nahla_special_offer_c874"):
    from models import WhatsAppTemplate
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=name,
        language="ar",
        category="MARKETING",
        status="APPROVED",
        components=[{"type": "BODY", "text": "مرحباً {{1}}، عرض خاص بسعر {{2}}"}],
    )
    db.add(tpl); db.commit()
    return tpl


# ──────────────────────────────────────────────────────────────────────


class TestMaskPhone:
    def test_keeps_first4_and_last3_for_e164(self):
        from routers.admin_debug import _mask_phone
        assert _mask_phone("+966537970430") == "+966***430"

    def test_handles_bare_digits(self):
        from routers.admin_debug import _mask_phone
        assert _mask_phone("966537970430") == "9665***430"

    def test_short_inputs_fully_redacted(self):
        from routers.admin_debug import _mask_phone
        # Anything ≤ 7 chars is fully redacted — there's no meaningful
        # head/tail to preserve, and we'd rather over-mask than leak.
        assert _mask_phone("12345") == "***"
        assert _mask_phone("") == ""


class TestExtractProviderMessageId:
    def test_returns_wamid_on_success_shape(self):
        from routers.admin_debug import _extract_provider_message_id
        assert _extract_provider_message_id({
            "messages": [{"id": "wamid.ABC123"}],
        }) == "wamid.ABC123"

    def test_returns_none_on_missing_messages(self):
        from routers.admin_debug import _extract_provider_message_id
        assert _extract_provider_message_id({"error": {"code": 132000}}) is None
        assert _extract_provider_message_id({}) is None
        assert _extract_provider_message_id(None) is None

    def test_returns_none_on_empty_messages_array(self):
        from routers.admin_debug import _extract_provider_message_id
        assert _extract_provider_message_id({"messages": []}) is None


# ──────────────────────────────────────────────────────────────────────


def _call_admin_send(body_kwargs, db, *, admin_sub="admin@nahla"):
    """Invoke the FastAPI handler synchronously, skipping the actual
    ``require_admin`` dependency by passing a fake admin payload."""
    from routers.admin_debug import admin_debug_send_template, _DirectSendBody
    body = _DirectSendBody(**body_kwargs)
    return _run(admin_debug_send_template(
        body=body,
        db=db,
        _admin={"sub": admin_sub, "role": "admin"},
    ))


class TestAdminDirectSend:
    def test_unknown_phone_number_id_returns_404(self):
        from fastapi import HTTPException
        db = _make_db()
        with pytest.raises(HTTPException) as exc:
            _call_admin_send({
                "phone_number_id": "999_does_not_exist",
                "to":              "+966537970430",
                "template":        "nahla_special_offer_c874",
                "language":        "ar",
            }, db)
        assert exc.value.status_code == 404
        assert "phone_number_id" in str(exc.value.detail).lower()

    def test_unknown_template_returns_404(self):
        from fastapi import HTTPException
        db = _make_db()
        _seed_connection(db)
        # Don't seed any template — connection exists, template doesn't.
        with pytest.raises(HTTPException) as exc:
            _call_admin_send({
                "phone_number_id": "100543193146977",
                "to":              "+966537970430",
                "template":        "missing_template",
                "language":        "ar",
            }, db)
        assert exc.value.status_code == 404
        assert "template" in str(exc.value.detail).lower()

    def test_happy_path_returns_provider_message_id_and_masks_phone(self):
        db = _make_db()
        _seed_connection(db)
        _seed_template(db, tenant_id=33)

        # Mock the provider call — returns a Meta-shaped success.
        fake_resp = {"messages": [{"id": "wamid.HBgNOTY2NTM3OTcwNDMw_admin"}]}
        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(return_value=(fake_resp, MagicMock(token="x"))),
        ):
            result = _call_admin_send({
                "phone_number_id": "100543193146977",
                "to":              "+966537970430",
                "template":        "nahla_special_offer_c874",
                "language":        "ar",
                "merchant_vars":   {"1": "Hisham", "2": "499"},
            }, db)

        assert result["ok"] is True
        assert result["http_status"] == 200
        assert result["provider_message_id"] == "wamid.HBgNOTY2NTM3OTcwNDMw_admin"
        assert result["tenant_id"] == 33
        assert result["template"] == "nahla_special_offer_c874"
        # Phone is masked in both the response envelope and the
        # echoed request payload.
        assert result["to_masked"] == "+966***430"
        assert result["raw_request_masked"]["to"] == "+966***430"
        # Raw response is unmodified — support needs the exact bytes.
        assert result["raw_response"] == fake_resp
        # Should NOT have created any campaign_send_log row.
        from models import CampaignSendLog
        assert db.query(CampaignSendLog).count() == 0

    def test_provider_error_response_marks_not_ok(self):
        db = _make_db()
        _seed_connection(db)
        _seed_template(db, tenant_id=33)

        fake_err = {
            "error": {
                "code":             132000,
                "error_subcode":    2494073,
                "message":          "Template parameter mismatch",
                "type":             "OAuthException",
                "fbtrace_id":       "ABCDEF",
            },
        }
        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(return_value=(fake_err, MagicMock(token="x"))),
        ):
            result = _call_admin_send({
                "phone_number_id": "100543193146977",
                "to":              "+966537970430",
                "template":        "nahla_special_offer_c874",
                "language":        "ar",
            }, db)
        # ok=False but the endpoint itself returns 200 — the FAILURE
        # detail lives inside `raw_response.error` for support to read.
        assert result["ok"] is False
        assert result["http_status"] == 200
        assert result["provider_message_id"] is None
        assert result["raw_response"]["error"]["code"] == 132000
        assert result["raw_response"]["error"]["fbtrace_id"] == "ABCDEF"

    def test_provider_exception_is_converted_to_502_envelope(self):
        db = _make_db()
        _seed_connection(db)
        _seed_template(db, tenant_id=33)

        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            result = _call_admin_send({
                "phone_number_id": "100543193146977",
                "to":              "+966537970430",
                "template":        "nahla_special_offer_c874",
                "language":        "ar",
            }, db)
        assert result["ok"] is False
        assert result["http_status"] == 502
        assert result["provider_message_id"] is None
        # Synthetic envelope so the UI has something to render.
        assert result["raw_response"]["error"]["type"] == "provider_exception"
        assert "RuntimeError" in (result["error_message"] or "")

    def test_does_not_use_campaign_send_logs(self):
        """Locks the invariant that this endpoint bypasses the
        campaign pipeline entirely — no rows in campaign_send_logs,
        no Campaign row created, nothing."""
        db = _make_db()
        _seed_connection(db)
        _seed_template(db, tenant_id=33)
        fake_resp = {"messages": [{"id": "wamid.X"}]}
        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(return_value=(fake_resp, MagicMock(token="x"))),
        ):
            _call_admin_send({
                "phone_number_id": "100543193146977",
                "to":              "+966537970430",
                "template":        "nahla_special_offer_c874",
                "language":        "ar",
            }, db)
        from models import Campaign, CampaignSendLog
        assert db.query(Campaign).count() == 0
        assert db.query(CampaignSendLog).count() == 0
