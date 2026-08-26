"""Graph transport security tests for whatsapp_connection_service."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from services.whatsapp_connection_service import (  # noqa: E402
    WhatsAppConnectionConflict,
    commit_connection,
    fetch_phone_metadata,
    resolve_waba_for_phone,
)

TOKEN = "SYNTH-GRAPH-TOKEN-877"
PHONE = "PHONE-GRAPH-CANARY-877"
WABA = "WABA-GRAPH-CANARY-877"
TENANT = 990877


@pytest.fixture()
def capture_transport(monkeypatch):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "display_phone_number": "+966500008877",
                "verified_name": "Test",
                "whatsapp_business_account": {"id": WABA},
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Client(transport=transport).get(url, **kwargs))
    return captured


def test_fetch_phone_metadata_bearer_only(capture_transport):
    out = fetch_phone_metadata(PHONE, TOKEN, TENANT)
    assert out["verified_name"] == "Test"
    assert len(capture_transport) == 1
    req = capture_transport[0]
    assert "access_token" not in str(req.url)
    assert req.headers.get("authorization") == f"Bearer {TOKEN}"


def test_resolve_waba_for_phone_bearer_only(capture_transport):
    waba, err = resolve_waba_for_phone(PHONE, TOKEN, TENANT)
    assert waba == WABA
    assert err is None
    req = capture_transport[0]
    assert "access_token" not in str(req.url)
    assert req.headers.get("authorization") == f"Bearer {TOKEN}"


def test_commit_connection_integrity_error_maps_to_asset_race(caplog):
    caplog.set_level(logging.ERROR, logger="nahla.wa_conn_svc")
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "sqlite"

    with patch("core.tenant_integrity.assert_phone_id_not_claimed"), \
         patch("core.tenant_integrity.assert_waba_id_not_claimed"), \
         patch("core.tenant_integrity.evict_phone_id_from_other_tenants"), \
         patch("core.tenant_integrity.evict_waba_id_from_other_tenants"), \
         patch("services.whatsapp_connection_service.validate_phone_waba_match", return_value=(True, WABA, None)), \
         patch("services.whatsapp_connection_service.fetch_phone_metadata", return_value={}), \
         patch("services.whatsapp_connection_service.register_phone_number", return_value=(True, None)), \
         patch("services.whatsapp_connection_service.subscribe_phone_webhook", return_value=(True, None)), \
         patch("services.whatsapp_platform.wa_connection_secrets.store_access_token"), \
         patch("services.whatsapp_asset_lock.whatsapp_asset_advisory_lock_hold"), \
         patch("services.whatsapp_platform.wa_token_validation.validate_meta_access_token_sync", return_value=SimpleNamespace(is_valid=True, token_status="valid", token_source_label="system_user", warnings=[], expires_at=None, error_message=None)), \
         patch("services.whatsapp_platform.wa_token_validation.apply_validation_to_connection"), \
         patch("services.whatsapp_platform.wa_token_validation.production_sending_allowed", return_value=True), \
         patch("database.models.WhatsAppConnection") as conn_cls:
        conn = MagicMock()
        conn.id = 1
        conn.status = "pending"
        conn.sending_enabled = True
        conn_cls.return_value = conn
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.commit.side_effect = IntegrityError("insert", {}, Exception("dup"))

        with pytest.raises(WhatsAppConnectionConflict) as excinfo:
            commit_connection(
                db,
                tenant_id=TENANT,
                phone_number_id=PHONE,
                waba_id=WABA,
                access_token=TOKEN,
                connection_type="embedded",
                skip_phone_register=True,
            )
        assert excinfo.value.code == "CONFLICT_ASSET_RACE"
        assert PHONE not in caplog.text
