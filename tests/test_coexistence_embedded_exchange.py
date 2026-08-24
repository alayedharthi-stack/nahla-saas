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
        _conn_field_snapshot,
        restore_connection_snapshot,
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
    snap = _conn_field_snapshot(conn)
    stage_coexistence_credentials(
        conn,
        waba_id="WABA-T",
        access_token="user-token",
        token_type="user",
    )
    assert conn.provider == "meta"
    restore_connection_snapshot(conn, snap)
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
