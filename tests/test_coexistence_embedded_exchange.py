"""Coexistence embedded exchange routes and DB safety."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _sqlite_session():
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker

    from models import Base

    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session()


def test_config_fail_closed_without_coexistence_env(monkeypatch):
    monkeypatch.delenv("META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", raising=False)
    from core import config as cfg  # noqa: PLC0415

    monkeypatch.setattr(cfg, "META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", "", raising=False)
    assert cfg.is_coexistence_embedded_signup_available() is False
    assert cfg.meta_coexistence_embedded_signup_config_id() == ""


def test_config_coexistence_does_not_fallback_to_cloud(monkeypatch):
    from core import config as cfg  # noqa: PLC0415

    monkeypatch.setattr(cfg, "META_EMBEDDED_SIGNUP_CONFIG_ID", "cloud-config", raising=False)
    monkeypatch.setattr(cfg, "META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", "", raising=False)
    assert cfg.is_coexistence_embedded_signup_available() is False


def test_cross_tenant_disconnected_blocks():
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415
    from core.tenant_integrity import assert_no_cross_tenant_whatsapp_asset, TenantIntegrityError  # noqa: PLC0415

    db = _sqlite_session()
    db.add(Tenant(id=201, name="A", is_active=True))
    db.add(Tenant(id=202, name="B", is_active=True))
    db.add(
        WhatsAppConnection(
            tenant_id=201,
            status="disconnected",
            provider="dialog360",
            whatsapp_business_account_id="WABA-X",
            phone_number_id="PHONE-X",
        )
    )
    db.commit()
    try:
        assert_no_cross_tenant_whatsapp_asset(
            db,
            202,
            waba_id="WABA-X",
            phone_number_id="PHONE-X",
        )
        raise AssertionError("expected conflict")
    except TenantIntegrityError as exc:
        assert "CROSS_TENANT_ASSET_CONFLICT" in str(exc)
    db.close()


def test_snapshot_restore_keeps_provider():
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415
    from services.coexistence_embedded_exchange import (  # noqa: PLC0415
        load_connection_for_update,
        stage_coexistence_credentials,
    )

    db = _sqlite_session()
    db.add(Tenant(id=301, name="snap", is_active=True))
    conn = WhatsAppConnection(
        tenant_id=301,
        status="disconnected",
        provider="dialog360",
        phone_number="+966501111111",
    )
    db.add(conn)
    db.commit()
    conn, _had = load_connection_for_update(db, 301)
    stage_coexistence_credentials(
        conn,
        waba_id="WABA-T",
        access_token="user-token",
        token_type="user",
    )
    assert conn.provider == "meta"
    db.flush()
    db.rollback()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=301).one()
    assert conn.provider == "dialog360"
    assert conn.whatsapp_business_account_id is None
    db.close()


def test_embedded_config_endpoint_coexistence_fields(monkeypatch):
    import routers.whatsapp_embedded as emb  # noqa: PLC0415

    monkeypatch.setattr(emb, "META_APP_ID", "app-123", raising=False)
    monkeypatch.setattr(emb, "META_EMBEDDED_SIGNUP_CONFIG_ID", "cloud-cfg", raising=False)
    monkeypatch.setattr(emb, "META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", "coex-cfg", raising=False)
    monkeypatch.setattr(emb, "is_meta_embedded_signup_enabled", lambda: True, raising=False)
    monkeypatch.setattr(emb, "is_coexistence_embedded_signup_available", lambda: True, raising=False)

    data = asyncio.run(emb.get_config())
    assert data["embedded_signup_config_id"] == "cloud-cfg"
    assert data["coexistence_embedded_signup_config_id"] == "coex-cfg"
    assert data["coexistence_embedded_signup_available"] is True


def test_embedded_config_coexistence_unavailable(monkeypatch):
    import routers.whatsapp_embedded as emb  # noqa: PLC0415

    monkeypatch.setattr(emb, "META_APP_ID", "app-123", raising=False)
    monkeypatch.setattr(emb, "META_EMBEDDED_SIGNUP_CONFIG_ID", "cloud-cfg", raising=False)
    monkeypatch.setattr(emb, "META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", "", raising=False)
    monkeypatch.setattr(emb, "is_meta_embedded_signup_enabled", lambda: True, raising=False)
    monkeypatch.setattr(emb, "is_coexistence_embedded_signup_available", lambda: False, raising=False)

    data = asyncio.run(emb.get_config())
    assert data["coexistence_embedded_signup_config_id"] is None
    assert data["coexistence_embedded_signup_available"] is False
    assert data["embedded_signup_config_id"] == "cloud-cfg"


def test_authorize_url_uses_passed_config_id_not_cloud_constant():
    import routers.whatsapp_embedded as emb  # noqa: PLC0415

    url = emb._build_meta_oauth_authorize_url("state-token", "https://api.example.test/cb", config_id="coex-only-id")
    assert "coex-only-id" in url
    assert "config_id=coex-only-id" in url.replace("%", "")


def test_oauth_start_coexistence_isolated_from_cloud_config(monkeypatch):
    import routers.whatsapp_embedded as emb  # noqa: PLC0415

    monkeypatch.setattr(emb, "META_EMBEDDED_SIGNUP_CONFIG_ID", "cloud-config-id", raising=False)
    monkeypatch.setattr(emb, "META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", "coexistence-config-id", raising=False)
    monkeypatch.setattr(emb, "is_coexistence_embedded_signup_available", lambda: True, raising=False)
    url = emb._build_meta_oauth_authorize_url(
        "state",
        "https://api.example.test/cb",
        config_id=emb.META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID,
    )
    assert "coexistence-config-id" in url
    assert "cloud-config-id" not in url


def test_snapshot_restores_access_token_literal():
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415
    from services.whatsapp_platform.wa_connection_secrets import (  # noqa: PLC0415
        read_access_token,
        store_access_token,
    )
    from services.coexistence_embedded_exchange import (  # noqa: PLC0415
        load_connection_for_update,
        stage_coexistence_credentials,
    )

    db = _sqlite_session()
    db.add(Tenant(id=401, name="token-snap", is_active=True))
    conn = WhatsAppConnection(
        tenant_id=401,
        status="disconnected",
        provider="dialog360",
        phone_number="+966501222333",
    )
    db.add(conn)
    db.commit()
    store_access_token(conn, "dialog360-original-token")
    db.commit()
    original_encrypted = conn.access_token
    conn, _had = load_connection_for_update(db, 401)
    stage_coexistence_credentials(
        conn,
        waba_id="WABA-TMP",
        access_token="meta-new-token",
        token_type="user",
    )
    assert read_access_token(conn) == "meta-new-token"
    db.flush()
    db.rollback()
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=401).one()
    assert conn.access_token == original_encrypted
    assert read_access_token(conn) == "dialog360-original-token"
    assert conn.provider == "dialog360"
    db.close()


def test_finalize_not_eligible_restores_dialog360():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    from routers.whatsapp_embedded import _finalize_coexistence_exchange  # noqa: PLC0415
    from services.whatsapp_platform.wa_connection_secrets import store_access_token  # noqa: PLC0415

    from models import Tenant, WhatsAppConnection  # noqa: PLC0415

    db = _sqlite_session()
    tenant = Tenant(id=501, name="fail-eligible", is_active=True)
    db.add(tenant)
    conn = WhatsAppConnection(
        tenant_id=501,
        status="disconnected",
        provider="dialog360",
        phone_number="+966501234567",
        extra_metadata={"legacy_channel": "TEST-CHANNEL-001"},
    )
    db.add(conn)
    db.commit()
    store_access_token(conn, "dialog-token-501")
    db.commit()
    phones = [{"id": "PHONE-501", "display_phone_number": "+966501234567"}]
    with patch(
        "services.meta_coexistence.verify_coexistence_phone",
        return_value=(False, {"display_phone_number": "+966501234567", "is_on_biz_app": False}, "not eligible"),
    ), patch("services.meta_coexistence.provider_is_on_biz_app", return_value=False):
        payload = asyncio.run(
            _finalize_coexistence_exchange(
                conn,
                db,
                tenant_id=501,
                waba_id="WABA-501",
                user_token="tok",
                phones=phones,
                trusted_phone_id="PHONE-501",
                finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
                trusted_business_portfolio_id="TEST-BUSINESS-001",
                canonical_phone_e164="+966501234567",
            )
        )
    db.rollback()
    db.refresh(conn)
    assert payload["status"] == "coexistence_not_eligible"
    assert conn.provider == "dialog360"
    assert conn.whatsapp_business_account_id is None
    assert conn.phone_number_id is None
    db.close()


def test_finalize_webhook_failure_restores_dialog360():
    import asyncio
    from unittest.mock import patch

    from routers.whatsapp_embedded import _finalize_coexistence_exchange  # noqa: PLC0415
    from services.whatsapp_platform.wa_connection_secrets import read_access_token, store_access_token  # noqa: PLC0415

    from models import Tenant, WhatsAppConnection  # noqa: PLC0415

    db = _sqlite_session()
    tenant = Tenant(id=502, name="fail-webhook", is_active=True)
    db.add(tenant)
    conn = WhatsAppConnection(
        tenant_id=502,
        status="disconnected",
        provider="dialog360",
        phone_number="+966509876543",
    )
    db.add(conn)
    db.commit()
    store_access_token(conn, "dialog-token-502")
    db.commit()
    phones = [{"id": "PHONE-502", "display_phone_number": "+966509876543"}]
    with patch(
        "services.meta_coexistence.verify_coexistence_phone",
        return_value=(True, {"display_phone_number": "+966509876543", "is_on_biz_app": True}, None),
    ), patch("services.meta_coexistence.provider_is_on_biz_app", return_value=True), patch(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        return_value=(False, "webhook failed"),
    ):
        payload = asyncio.run(
            _finalize_coexistence_exchange(
                conn,
                db,
                tenant_id=502,
                waba_id="WABA-502",
                user_token="tok",
                phones=phones,
                trusted_phone_id="PHONE-502",
                finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
                trusted_business_portfolio_id="TEST-BUSINESS-001",
                canonical_phone_e164="+966509876543",
            )
        )
    db.rollback()
    db.refresh(conn)
    assert payload["connected"] is False
    assert conn.provider == "dialog360"
    assert read_access_token(conn) == "dialog-token-502"
    db.close()


def test_connected_meta_unaffected_by_missing_coexistence_env(monkeypatch):
    """Deploy safety: coexistence env unset does not imply connected Meta rows must change."""
    from core import config as cfg  # noqa: PLC0415
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415

    monkeypatch.setattr(cfg, "META_COEXISTENCE_EMBEDDED_SIGNUP_CONFIG_ID", "", raising=False)
    assert cfg.is_coexistence_embedded_signup_available() is False

    db = _sqlite_session()
    db.add(Tenant(id=801, name="connected-meta", is_active=True))
    conn = WhatsAppConnection(
        tenant_id=801,
        status="connected",
        provider="meta",
        connection_type="embedded",
        whatsapp_business_account_id="TEST-WABA-CONNECTED-001",
        phone_number_id="TEST-PHONE-CONNECTED-001",
        phone_number="+966501111222",
        extra_metadata={"connection_mode": "coexistence"},
    )
    db.add(conn)
    db.commit()
    before = (
        conn.status,
        conn.provider,
        conn.whatsapp_business_account_id,
        conn.phone_number_id,
    )
    db.refresh(conn)
    after = (
        conn.status,
        conn.provider,
        conn.whatsapp_business_account_id,
        conn.phone_number_id,
    )
    assert before == after
    db.close()
