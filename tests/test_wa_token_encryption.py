"""
tests/test_wa_token_encryption.py
──────────────────────────────────
WhatsApp access_token encryption, validation, and admin write paths.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from core.wa_token_crypto import decrypt_access_token, encrypt_access_token, is_encrypted_at_rest  # noqa: E402
from models import Base, Tenant, WhatsAppConnection  # noqa: E402
from services.whatsapp_platform.wa_connection_secrets import (  # noqa: E402
    read_access_token,
    store_access_token,
)
from services.whatsapp_platform.wa_token_validation import (  # noqa: E402
    classify_debug_info,
    production_sending_allowed,
)


def _make_db():
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
    db = Session()
    tenant = Tenant(name="Enc Store", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return db, tenant


def test_encrypt_roundtrip_and_legacy_plaintext():
    plain = "EAABtestPlainMetaTokenValue1234567890"
    enc = encrypt_access_token(plain)
    assert is_encrypted_at_rest(enc)
    assert enc != plain
    assert decrypt_access_token(enc) == plain
    assert decrypt_access_token(plain) == plain  # backward compat


def test_store_access_token_never_persists_plaintext():
    db, tenant = _make_db()
    conn = WhatsAppConnection(tenant_id=tenant.id, status="connected", provider="meta")
    db.add(conn)
    db.commit()

    plain = "EAABstoreTestToken9876543210ABCDEF"
    store_access_token(conn, plain)
    db.commit()
    db.refresh(conn)

    assert conn.access_token != plain
    assert is_encrypted_at_rest(conn.access_token)
    assert read_access_token(conn) == plain


def test_temporary_token_not_production_ready():
    debug = {
        "is_valid": True,
        "type": "USER",
        "expires_at": 9999999999,
        "scopes": ["whatsapp_business_messaging"],
        "app_id": "123",
    }
    result = classify_debug_info(debug)
    assert result.is_valid is True
    assert production_sending_allowed(result) is False
    assert any("System User" in w or "User access" in w for w in result.warnings)


def test_permanent_system_user_token_production_ready():
    debug = {
        "is_valid": True,
        "type": "SYSTEM_USER",
        "expires_at": 0,
        "scopes": ["whatsapp_business_messaging"],
        "app_id": "123",
    }
    result = classify_debug_info(debug)
    assert result.production_ready is True
    assert production_sending_allowed(result) is True


def test_expiring_token_marked_expiring():
    import time

    soon = int(time.time()) + 5 * 86400
    debug = {
        "is_valid": True,
        "type": "SYSTEM_USER",
        "expires_at": soon,
        "scopes": ["whatsapp_business_messaging"],
        "app_id": "123",
    }
    result = classify_debug_info(debug)
    assert result.token_status == "expiring"
    assert result.health_status == "token_expiring_soon"


def test_commit_connection_encrypts_token(monkeypatch):
    db, tenant = _make_db()
    from services import whatsapp_connection_service as wa_svc  # noqa: E402

    monkeypatch.setattr(
        wa_svc,
        "validate_phone_waba_match",
        lambda *_a, **_k: (True, "111", None),
    )
    monkeypatch.setattr(wa_svc, "register_phone_number", lambda *_a, **_k: (True, None))
    monkeypatch.setattr(wa_svc, "subscribe_phone_webhook", lambda *_a, **_k: (True, None))
    monkeypatch.setattr(
        "services.whatsapp_platform.wa_token_validation.validate_meta_access_token_sync",
        lambda _t: classify_debug_info({
            "is_valid": True,
            "type": "SYSTEM_USER",
            "expires_at": 0,
            "scopes": ["whatsapp_business_messaging"],
            "app_id": "123",
        }),
    )

    plain = "EAABcommitFlowTokenABCDEF1234567890"
    wa_svc.commit_connection(
        db,
        tenant_id=tenant.id,
        phone_number_id="999888777",
        waba_id="111222333",
        access_token=plain,
        connection_type="cloud_api",
        actor="test",
    )
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant.id).one()
    assert conn.access_token != plain
    assert is_encrypted_at_rest(conn.access_token)
    assert read_access_token(conn) == plain


def test_admin_set_token_requires_admin_and_encrypts():
    db, tenant = _make_db()
    conn = WhatsAppConnection(
        tenant_id=tenant.id,
        status="connected",
        provider="meta",
        phone_number_id="123",
        whatsapp_business_account_id="456",
    )
    db.add(conn)
    db.commit()

    from routers import admin as admin_router  # noqa: E402

    body = admin_router._SetWaTokenBody(
        access_token="EAABadminSetTokenValue123456789012345",
        token_type="permanent_system_user",
    )
    with patch(
        "services.whatsapp_platform.wa_token_validation.validate_meta_access_token_sync",
        return_value=classify_debug_info({
            "is_valid": True,
            "type": "SYSTEM_USER",
            "expires_at": 0,
            "scopes": ["whatsapp_business_messaging"],
            "app_id": "123",
        }),
    ):
        result = asyncio.run(
            admin_router.admin_whatsapp_set_token(
                tenant.id,
                body,
                db,
                _admin={"role": "admin", "sub": "admin@test"},
            )
        )
    db.refresh(conn)
    assert "access_token" not in result
    assert is_encrypted_at_rest(conn.access_token)
    assert read_access_token(conn) == body.access_token.strip()
    assert result["production_ready"] is True


def test_expired_token_health_check_disables_sending_not_status():
    db, tenant = _make_db()
    conn = WhatsAppConnection(
        tenant_id=tenant.id,
        status="connected",
        provider="meta",
        sending_enabled=True,
        phone_number_id="123",
        whatsapp_business_account_id="456",
    )
    store_access_token(conn, "EAABexpiredTokenValue123456789012345")
    db.add(conn)
    db.commit()

    from services.whatsapp_platform.wa_token_validation import (  # noqa: E402
        TokenValidationResult,
        apply_validation_to_connection,
    )

    bad = TokenValidationResult(
        is_valid=False,
        production_ready=False,
        token_status="expired",
        token_type="USER",
        token_source_label="user",
        expires_at=None,
        data_access_expires_at=None,
        scopes=[],
        app_id="123",
        warnings=["expired"],
        error_code="190",
        error_message="Token expired",
        debug_info={},
    )
    apply_validation_to_connection(conn, bad)
    conn.sending_enabled = False
    db.commit()
    db.refresh(conn)

    assert conn.status == "connected"
    assert conn.sending_enabled is False
    meta = dict(conn.extra_metadata or {})
    assert meta.get("health_status") == "token_expired"
